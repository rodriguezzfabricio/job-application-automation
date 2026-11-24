"""Parse public ZipRecruiter listing pages."""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from . import transport
from .job import Job

HOST = "www.ziprecruiter.com"
WARM = "https://www.ziprecruiter.com/"

_ENTRY = re.compile(
    r"^###\s+\[(?P<title>[^\]]+)\]\((?P<url>https?://[^\)]+)\)\s*$",
    re.MULTILINE,
)
_FIELD = re.compile(r"^\*\*(?P<key>[A-Za-z ]+):\*\*\s*(?P<value>.*)$", re.MULTILINE)
_JID = re.compile(r"[?&]jid=([0-9a-f]+)")


def _title_slug(query: str) -> str:
    """`software engineer` -> `Software-Engineer`."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", query).strip("-")
    return "-".join(part.capitalize() for part in cleaned.split("-") if part)


def _location_path(location: str) -> str:
    """`Austin, TX` -> `/-in-Austin,TX`;  `Texas` -> `/--in-Texas`."""
    location = location.strip()
    if "," in location:
        city, _, state = location.partition(",")
        return f"/-in-{city.strip().replace(' ', '-')},{state.strip().upper()}"
    return f"/--in-{location.replace(' ', '-')}"


def path_for(query: str, location: str | None = None) -> str:
    path = f"/Jobs/{_title_slug(query)}"
    return path + _location_path(location) if location else path


def _to_jobs(markdown: str) -> list[Job]:
    matches = list(_ENTRY.finditer(markdown))
    jobs: list[Job] = []
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        block = markdown[m.end():end]
        fields = {fm.group("key").strip().lower(): fm.group("value").strip()
                  for fm in _FIELD.finditer(block)}
        url = m.group("url")
        jid = _JID.search(url)
        jobs.append(Job(
            source="ziprecruiter",
            title=m.group("title").strip(),
            company=fields.get("company", ""),
            location=fields.get("location", ""),
            url=url,
            # A ZipRecruiter job card is an aggregator page, not the employer's
            # ATS, so this deliberately leaves apply_url empty (rubric G3).
            apply_url="",
            posted_at=None,
            salary=fields.get("salary", ""),
            external_id=jid.group(1) if jid else "",
        ))
    return jobs


def search(query: str, *, location: str | None = None, session=None) -> list[Job]:
    """Listings for one query (optionally one location)."""
    path = path_for(query, location)

    def _collect(sess: transport.BrowserSession) -> list[Job]:
        markdown = sess.fetch(HOST, path, accept="text/markdown")
        return _to_jobs(markdown)

    if session is not None:
        return _collect(session)
    with transport.browser() as sess:
        return _collect(sess)


def search_many(queries: Iterable[str], locations: Iterable[str | None] = (None,),
                *, session=None) -> list[Job]:
    """Sweep queries x locations, de-duplicated by `jid`.

    This is how you get breadth out of a surface that has no pagination.
    """
    jobs: list[Job] = []
    seen: set[str] = set()

    def _sweep(sess: transport.BrowserSession) -> None:
        for query in queries:
            for location in locations:
                try:
                    found = search(query, location=location, session=sess)
                except transport.Blocked:
                    # One blocked shape should not kill the sweep.
                    continue
                for job in found:
                    if job.dedup_key not in seen:
                        seen.add(job.dedup_key)
                        jobs.append(job)

    if session is not None:
        _sweep(session)
    else:
        with transport.browser() as sess:
            _sweep(sess)
    return jobs
