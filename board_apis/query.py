"""Filter and deduplicate job-board results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from . import handshake, hiringcafe, transport, wellfound, ziprecruiter
from .job import Job


@dataclass
class Query:
    keyword: str = ""
    location: str = ""
    max_age_hours: float | None = None
    days: int | None = None            # hiring.cafe's native look-back
    limit: int = 40
    sources: Sequence[str] = ("hiringcafe", "ziprecruiter", "wellfound")


def _matches(job: Job, q: Query, *, check_age: bool = True) -> bool:
    if q.location:
        needle = q.location.lower().strip()
        hay = f"{job.location} {job.title}".lower()
        # "Oregon" or "OR" should both hit "Portland, Oregon, United States"
        if needle not in hay:
            return False
    if check_age and q.max_age_hours is not None:
        age = job.age_hours
        if age is None or age > q.max_age_hours:
            return False
    return True


def _dedup(jobs: Iterable[Job]) -> list[Job]:
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        key = job.dedup_key
        if key and key not in seen:
            seen.add(key)
            out.append(job)
    return out


def search(q: Query, *, session=None) -> tuple[list[Job], dict[str, str]]:
    """Fan out across the requested boards, then filter and de-duplicate.

    Returns `(jobs, errors)` so a blocked board degrades to a note instead of
    losing the boards that did answer.
    """
    collected: list[Job] = []
    errors: dict[str, str] = {}

    def _run(sess) -> None:
        if "hiringcafe" in q.sources:
            try:
                if q.keyword:
                    collected.extend(hiringcafe.search(
                        q.keyword, days=q.days, limit=q.limit))
                else:
                    collected.extend(hiringcafe.recent(limit=q.limit))
            except transport.Blocked as exc:
                errors["hiringcafe"] = str(exc)

        if "ziprecruiter" in q.sources and q.keyword:
            try:
                collected.extend(ziprecruiter.search(
                    q.keyword, location=q.location or None, session=sess))
            except transport.Blocked as exc:
                errors["ziprecruiter"] = str(exc)

        if "wellfound" in q.sources:
            try:
                collected.extend(wellfound.latest(limit=max(q.limit, 50)))
            except transport.Blocked as exc:
                errors["wellfound"] = str(exc)

        if "handshake" in q.sources and q.keyword:
            try:
                collected.extend(handshake.search(q.keyword, limit=q.limit,
                                                  session=sess))
            except (handshake.NeedsLogin, transport.Blocked) as exc:
                errors["handshake"] = str(exc)

    needs_browser = bool({"ziprecruiter", "handshake"} & set(q.sources))
    if session is not None:
        _run(session)
    elif needs_browser:
        with transport.browser() as sess:
            _run(sess)
    else:
        _run(None)

    jobs = [j for j in collected if _matches(j, q)]
    jobs = _dedup(jobs)
    jobs.sort(key=lambda j: (j.age_hours if j.age_hours is not None else 1e9))
    return jobs[:q.limit], errors


def search_all(q: Query) -> tuple[list[Job], dict[str, str]]:
    """Alias kept for readability at call sites."""
    return search(q)
