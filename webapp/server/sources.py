"""Sanctioned feed transport (scar P1, IRON_LAW §13) + board registration shapes.

Only the frozen ATS feed hosts are representable. Anything else — LinkedIn,
Indeed, any other host, any non-http(s) scheme — is refused at transport
construction with AllowlistViolation. Extending the allowlist is a code change
plus review, never a runtime registration. Registration builds its URL from a
frozen kind -> template map over the same sanctioned hosts, so registration
cannot widen the transport either.
"""

import contextlib
import email.utils
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SANCTIONED_FEED_HOSTS = frozenset(
    {"api.ashbyhq.com", "boards-api.greenhouse.io", "api.lever.co"}
)

USER_AGENT = "first-hour-job-discovery/0.1 (personal job-board watcher)"

# Frozen kind -> feed-URL templates (a1). Hosts are derived from the templates,
# so a template can never leave the sanctioned set without a code change.
_BOARD_URL_TEMPLATES = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
    "lever": "https://api.lever.co/v0/postings/{board}",
}

BOARD_KINDS = tuple(_BOARD_URL_TEMPLATES)


class AllowlistViolation(ValueError):
    """The requested URL is not representable in this transport (P1)."""


class InvalidBoardToken(ValueError):
    """The board token cannot be embedded in a feed URL safely."""


def parse_retry_after(headers):
    """Seconds from a Retry-After header (delta-seconds or HTTP-date); None if
    absent/garbage. Server values are returned unclamped — the caller decides
    how to honor them (a1: honored in full)."""
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    return parsed.timestamp() - time.time()


class FeedTransport:
    """Thin urllib wrapper whose construction is the allowlist gate."""

    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise AllowlistViolation(f"non-http scheme: {parsed.scheme!r}")
        if parsed.hostname not in SANCTIONED_FEED_HOSTS:
            raise AllowlistViolation(f"host not sanctioned: {parsed.hostname!r}")
        self.url = url

    def fetch(self, timeout_s: int):
        """Return the envelope (status, body, retry_after). HTTP 4xx/5xx are
        returned as statuses, never raised — the politeness machine must see
        them (the as-built EnvelopeError dropped status/headers, making the
        Retry-After branch unreachable; a1)."""
        req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                return exc.code, exc.read(), parse_retry_after(exc.headers)
        with contextlib.closing(resp):
            return resp.status, resp.read(), parse_retry_after(resp.headers)


def build_transport(url: str) -> FeedTransport:
    """Construction-time allowlist gate; raises AllowlistViolation on refusal."""
    return FeedTransport(url)


def build_feed_url(kind: str, board_id: str):
    """Build a board feed URL from kind + token (a1 registration contract).

    The template host is frozen in _BOARD_URL_TEMPLATES — an unknown kind is
    non-representable, and the token cannot smuggle a different host/scheme."""
    if kind not in _BOARD_URL_TEMPLATES:
        raise InvalidBoardToken(f"unknown board kind: {kind!r}")
    token = str(board_id).strip()
    if (
        not token
        or len(token) > 100
        or ".." in token
        or any(ch in token for ch in "/?#% ")
    ):
        raise InvalidBoardToken("malformed board token")
    # Measured against the live APIs (2026-10-21): Greenhouse serves name
    # slugs (/boards/airbnb/jobs -> 200) and a 32-hex token 404s — so no
    # per-vendor charset rule beyond the shared injection guards above.
    return _BOARD_URL_TEMPLATES[kind].format(board=token)


def validate_envelope(kind: str, body: bytes) -> bool:
    """Typed envelope check on a live 200 body (a1): ashby/greenhouse = JSON
    object with a "jobs" list; lever = JSON array of postings."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if kind == "lever":
        return isinstance(data, list)
    if kind in ("ashby", "greenhouse"):
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    return False
