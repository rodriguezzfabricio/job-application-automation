"""Read structured job postings from public Wellfound pages."""
from __future__ import annotations

import json
import re
from typing import Any

from . import transport
from .job import Job

HOST = "wellfound.com"
JOBS = "https://wellfound.com/jobs"

_LINK = re.compile(r'href="(/jobs/\d+-[a-z0-9\-]+)"')
_LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


def listing_ids(limit: int | None = None) -> list[str]:
    """Job page paths from the server-rendered listing."""
    resp = transport.http_get(JOBS, host=HOST)
    paths = list(dict.fromkeys(_LINK.findall(resp.text)))
    return paths[:limit] if limit else paths


def _location(ld: dict[str, Any]) -> str:
    if ld.get("jobLocationType") == "TELECOMMUTE":
        base = "Remote"
    else:
        base = ""
    places = ld.get("jobLocation") or []
    parts: list[str] = []
    for place in places if isinstance(places, list) else [places]:
        addr = (place or {}).get("address") or {}
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            val = addr.get(key)
            if isinstance(val, dict):
                val = val.get("name")
            if val and val not in parts:
                parts.append(str(val))
    where = ", ".join(parts)
    if base and where:
        return f"{base} ({where})"
    return base or where


def _salary(ld: dict[str, Any]) -> str:
    base = ld.get("baseSalary") or {}
    value = base.get("value") or {}
    lo, hi = value.get("minValue"), value.get("maxValue")
    unit = (value.get("unitText") or "").lower()
    if not lo and not hi:
        return ""
    suffix = {"year": "/yr", "hour": "/hr", "month": "/mo"}.get(unit, "")
    if hi and lo:
        return f"${lo:,.0f} - ${hi:,.0f}{suffix}"
    return f"${(hi or lo):,.0f}{suffix}"


def _to_job(path: str, html: str) -> Job | None:
    m = _LD.search(html)
    if not m:
        return None
    try:
        ld = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if ld.get("@type") != "JobPosting":
        return None

    org = ld.get("hiringOrganization") or {}
    ident = ld.get("identifier") or {}
    months = ((ld.get("experienceRequirements") or {}).get("monthsOfExperience"))
    seniority = ""
    if isinstance(months, (int, float)):
        seniority = f"{months / 12:.0f} yr exp required" if months else "no prior experience required"

    return Job(
        source="wellfound",
        title=ld.get("title") or "",
        company=org.get("name") or "",
        location=_location(ld),
        url=f"https://{HOST}{path}",
        # `directApply` means the form is on Wellfound itself, not an ATS page.
        apply_url="" if ld.get("directApply") else (org.get("sameAs") or ""),
        posted_at=ld.get("datePosted"),
        salary=_salary(ld),
        seniority=seniority,
        description=ld.get("description") or "",
        external_id=str(ident.get("value") or path.split("/")[2].split("-")[0]),
        raw={"directApply": ld.get("directApply"),
             "employmentType": ld.get("employmentType"),
             "monthsOfExperience": months},
    )


def latest(limit: int = 25) -> list[Job]:
    """Newest postings from the allowed listing surface."""
    jobs: list[Job] = []
    for path in listing_ids(limit=limit):
        try:
            html = transport.http_get(f"https://{HOST}{path}", host=HOST,
                                      referer=JOBS).text
        except transport.Blocked:
            continue
        job = _to_job(path, html)
        if job:
            jobs.append(job)
    jobs.sort(key=lambda j: j.posted_at or "", reverse=True)
    return jobs


def job(path: str) -> Job | None:
    """One posting by its `/jobs/<id>-<slug>` path."""
    html = transport.http_get(f"https://{HOST}{path}", host=HOST, referer=JOBS).text
    return _to_job(path, html)
