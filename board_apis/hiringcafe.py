"""Parse public HiringCafe search results."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Iterable

from . import transport
from .job import Job

HOST = "hiringcafe.com"
HOME = "https://hiringcafe.com/"


def _extract_array(html: str, key: str = '"ssrHits":[') -> list[dict[str, Any]]:
    """Pull a balanced JSON array out of the page without a full RSC parser."""
    for needle in (key, key.replace('"', '\\"')):
        start = html.find(needle)
        if start == -1:
            continue
        i = html.index("[", start)
        depth, esc, instr = 0, False, False
        for j in range(i, len(html)):
            ch = html[j]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                instr = not instr
                continue
            if instr:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[i:j + 1])
    return []


def _to_job(hit: dict[str, Any]) -> Job:
    info = hit.get("job_information") or {}
    v5 = hit.get("v5_processed_job_data") or {}
    org = hit.get("enriched_company_data") or {}
    apply_url = hit.get("apply_url") or ""

    salary = ""
    lo, hi = v5.get("yearly_min_compensation"), v5.get("yearly_max_compensation")
    if lo or hi:
        salary = f"${lo:,.0f} - ${hi:,.0f}/yr" if hi else f"${lo:,.0f}/yr"
    elif v5.get("hourly_min_compensation"):
        salary = f"${v5.get('hourly_min_compensation')}/hr"

    return Job(
        source="hiringcafe",
        title=info.get("title") or v5.get("job_title_raw") or "",
        company=org.get("name") or v5.get("company_name") or "",
        location=v5.get("formatted_workplace_location") or "",
        url=apply_url,
        apply_url=apply_url,
        posted_at=v5.get("estimated_publish_date"),
        salary=salary,
        seniority=v5.get("seniority_level") or "",
        external_id=hit.get("id") or "",
        raw={k: hit.get(k) for k in ("source", "board_token", "is_expired",
                                     "strict_dedup_cluster_id")},
    )


def _fetch(query: str | None, user_state: dict[str, Any] | None,
           timeout: int = 40) -> list[Job]:
    url = HOME
    params: dict[str, Any] = {}
    if query or user_state:
        state = dict(user_state or {})
        if query:
            state["searchQuery"] = query
        params["searchState"] = json.dumps(state, separators=(",", ":"))
    if params:
        url = HOME + "?" + urllib.parse.urlencode(params)

    resp = transport.http_get(url, host=HOST, timeout=timeout)

    hits = _extract_array(resp.text)
    return [_to_job(h) for h in hits if isinstance(h, dict)]


def recent(limit: int | None = None) -> list[Job]:
    """The default recent-jobs feed. This is the robots.txt-allowed surface."""
    jobs = _fetch(None, None)
    return jobs[:limit] if limit else jobs


def search_native(query: str, *, days: int | None = None,
                  limit: int | None = None) -> list[Job]:
    """The site's own keyword query. Role-relevant, but not fresh.

    hiring.cafe expects a rich `searchState` object (Google-Places location
    records plus dozens of filter fields). A partial object is accepted and
    then largely ignored: `dateFetchedPastNDays` has no effect, results come
    back relevance-ordered, and the median posting is months old (measured:
    min 79 h, median ~3000 h). Use this to answer "what software engineering
    roles exist", never "what posted today".
    """
    state: dict[str, Any] = {}
    if days:
        state["dateFetchedPastNDays"] = days
    jobs = _fetch(query, state)
    return jobs[:limit] if limit else jobs


def search(query: str, *, days: int | None = None,
           limit: int | None = None) -> list[Job]:
    """Role-relevant search (see `search_native`)."""
    return search_native(query, days=days, limit=limit)
