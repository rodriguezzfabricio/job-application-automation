"""Employer-page transport (scar P1) + Tier-1 liveness tier (a1/a2).

The employer transport fetches the registered careers page for the liveness
check. It is the containment pin: refusal happens at construction (HTTPS-only,
usable host required — before any socket opens), fetches are GET-only and
body-bounded, and redirects can never widen the transport beyond the careers
endpoint. liveness_tier composes the a2 Tier-1 checks over the injected
transport (DI seam, so the tier is testable against local fixtures).
"""

import contextlib
import urllib.error
import urllib.parse
import urllib.request

from server.liveness import (
    application_page_readable,
    href_links,
    ownership_link_present,
    title_tokens_present,
)

# Body reads are bounded: a careers page may never grow the fetch buffer
# without limit, on success or on HTTP errors (resource pin).
RESPONSE_BODY_CAP = 256 * 1024

USER_AGENT = "first-hour-job-discovery/0.1 (personal job-board watcher)"

# a2 latency budget: the liveness GET (incl. body download) must fit 20 s.
EMPLOYER_FETCH_TIMEOUT_S = 20


class EmployerTransportError(ValueError):
    """The requested employer fetch is not representable (P1 containment)."""


class EmployerRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect containment (P1): follows only redirects that stay on the
    registered careers endpoint — same hostname and same effective port.
    Anything else raises EmployerTransportError before the redirected request
    opens, so a redirect can never widen the transport to a new host."""

    def __init__(self, endpoint):
        super().__init__()
        self._endpoint = endpoint

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        parsed = urllib.parse.urlparse(target)
        host, port = self._endpoint
        if parsed.hostname != host or _effective_port(parsed) != port:
            raise EmployerTransportError(
                f"redirect escapes the registered careers endpoint: {newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _effective_port(parsed):
    """Port with the scheme default filled in — the endpoint key's port half,
    so a same-host redirect cannot sneak in a different port/scheme either."""
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


class EmployerTransport:
    """Employer-page transport pinned to the careers endpoint derived from the
    registered careers_url. fetch() targets only that URL, so no other host is
    ever representable in the request itself."""

    def __init__(self, careers_url):
        # No scheme gate here on purpose: the class doubles as the fixture/DI
        # seam (liveness_tier tests inject it against http://127.0.0.1
        # servers). The HTTPS-only rule is enforced by build_employer_transport,
        # the only production entry point.
        try:
            parsed = urllib.parse.urlparse(careers_url)
            # .port is a property whose access raises for a non-numeric port,
            # so the endpoint key must be built inside the same guard.
            port = _effective_port(parsed)
        except ValueError as exc:
            raise EmployerTransportError(
                f"unparseable careers URL: {careers_url!r}"
            ) from exc
        host = parsed.hostname
        if not host or any(ch.isspace() for ch in host):
            raise EmployerTransportError(
                f"careers URL has no usable host: {careers_url!r}"
            )
        self._url = careers_url
        self._endpoint = (host, port)
        self._opener = urllib.request.build_opener(
            EmployerRedirectHandler(self._endpoint)
        )

    def fetch(self, timeout_s):
        """GET the pinned careers URL and return (status, body_text).

        GET-only by construction: urllib's Request defaults to GET when no
        data is supplied and no method parameter exists here. The body read is
        capped at RESPONSE_BODY_CAP bytes and decoded UTF-8 with
        errors="replace". HTTP errors return (code, bounded body) instead of
        raising — the liveness tier treats non-2xx as a failed check."""
        req = urllib.request.Request(self._url, headers={"User-Agent": USER_AGENT})
        try:
            resp = self._opener.open(req, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                body = exc.read(RESPONSE_BODY_CAP)
                return exc.code, body.decode("utf-8", errors="replace")
        with contextlib.closing(resp):
            body = resp.read(RESPONSE_BODY_CAP)
            return resp.status, body.decode("utf-8", errors="replace")


def build_employer_transport(careers_url):
    """Construction-time gate: HTTPS-only employer transport pinned to the
    careers host (a1). Refusal happens here, before any socket opens; no URL
    whose host is not derived from the careers_url itself is representable."""
    try:
        parsed = urllib.parse.urlparse(str(careers_url))
    except ValueError as exc:
        raise EmployerTransportError(
            f"unparseable careers URL: {careers_url!r}"
        ) from exc
    if parsed.scheme != "https":
        raise EmployerTransportError(
            f"employer transport is HTTPS-only: scheme {parsed.scheme!r}"
        )
    return EmployerTransport(careers_url)


def liveness_tier(record, source_row, employer_transport, now):
    """a2 Tier-1 liveness check over the injected employer transport.

    Evidence is always "reduced" in v1 (no template registered). Fails closed:
    a missing/empty/unparseable careers_url, any transport exception, or a
    non-2xx yields liveness_ok False with the check booleans set — never a
    crash and never a fabricated True. The ownership check pins to the
    careers URL's own hostname (official_host), never the feed API host, with
    the board token from the source id (a2: ownership link to the registered
    board host with matching board)."""
    checks = {
        "careers_page_readable": False,
        "ownership_link": False,
        "application_page_readable": False,
        "title_tokens": False,
    }
    careers_url = source_row.get("careers_url") or ""
    try:
        official_host = urllib.parse.urlparse(careers_url).hostname
    except ValueError:
        official_host = None
    if not official_host:
        return _tier_result(False, checks, now)
    board_token = source_row["id"].split(":", 1)[1]
    try:
        status, text = employer_transport.fetch(EMPLOYER_FETCH_TIMEOUT_S)
    except Exception:
        # Any transport failure falsifies the readable check and therefore the
        # whole tier; no exception may escape into the alert path.
        return _tier_result(False, checks, now)
    readable = status is not None and 200 <= status < 300 and bool(text)
    checks["careers_page_readable"] = readable
    if readable:
        links = href_links(text)
        checks["ownership_link"] = ownership_link_present(
            links, official_host, board_token
        )
        checks["application_page_readable"] = application_page_readable(text)
        checks["title_tokens"] = title_tokens_present(text, record["title"])
    return _tier_result(all(checks.values()), checks, now)


def _tier_result(liveness_ok, checks, now):
    """The exact a2 Tier-1 evidence shape; a single definition keeps the
    caller-visible shape from drifting between pass and fail paths."""
    return {
        "liveness_ok": liveness_ok,
        "evidence_level": "reduced",
        "checks": checks,
        "observed_at": now,
    }
