"""Pure liveness-check predicates for the a2 Tier-1 liveness contract.

Contract — knowledge/a2.md ("Liveness check", Tier 1): feed still lists the job
active AND careers page readable on official_host AND an ownership link to the
registered board host with matching board AND application page readable with no
closed markers AND the rendered text contains the job-title tokens (a generic
200 shell fails that positive check).

Only the text/HTML predicate checks live here; HTTP fetching and tier
composition belong to other modules.
"""

import re
from urllib.parse import urlparse

CLOSED_MARKERS = (
    "position filled",
    "no longer accepting",
    "this posting has closed",
    "job is closed",
    "we are no longer accepting applications",
    "posting closed",
)


def scan_closed_markers(text, markers=CLOSED_MARKERS):
    """Markers from `markers` whose casefolded form appears in the casefolded
    rendered text, returned as the tuple's original-case strings (a2 Tier 1)."""
    folded = text.casefold()
    return [marker for marker in markers if marker.casefold() in folded]


def title_tokens_present(text, title):
    """True iff every whitespace-separated token of `title` appears in the
    rendered text — the a2 Tier-1 positive check that a generic 200 shell fails.

    Tokens are casefolded, stripped, then stripped of leading/trailing
    non-alphanumerics; empty tokens are dropped; a tokenless title is False.
    """
    folded = text.casefold()
    tokens = []
    for raw in title.split():
        token = raw.casefold().strip()
        while token and not token[0].isalnum():
            token = token[1:]
        while token and not token[-1].isalnum():
            token = token[:-1]
        if token:
            tokens.append(token)
    if not tokens:
        return False
    # Substring (not whole-word) matching on purpose: a2 says the rendered text
    # must "contain the job-title tokens"; keep that conservative reading.
    return all(token in folded for token in tokens)


_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def href_links(text):
    """Every href target in `text`, in document order (a2 ownership-link scan)."""
    return _HREF_RE.findall(text)


def ownership_link_present(links, host, board_token):
    """True iff some link parses to a URL whose hostname equals the registered
    board `host` AND whose path contains `board_token` (a2 Tier-1 ownership).
    Unparseable links are skipped."""
    for link in links:
        try:
            parsed = urlparse(link)
            hostname = parsed.hostname
        except ValueError:
            continue
        if hostname == host and board_token in parsed.path:
            return True
    return False


def application_page_readable(text, markers=CLOSED_MARKERS):
    """True iff the rendered text is non-empty and contains no closed markers
    (a2 Tier-1 application-page check). This alone does NOT prove the right
    page: a generic shell with no job content still passes here and must fail
    title_tokens_present instead."""
    return bool(text) and not scan_closed_markers(text, markers)


def liveness_checks_pass(text, links, host, board_token, title):
    """True iff the three a2 Tier-1 checks all hold: the rendered text contains
    the job-title tokens, an ownership link targets the registered board host
    with matching board, and the application page is readable and marker-free.
    Convenience only — the caller composes the rest of the Tier-1 evidence."""
    return (
        title_tokens_present(text, title)
        and ownership_link_present(links, host, board_token)
        and application_page_readable(text)
    )
