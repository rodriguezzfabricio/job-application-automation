"""Read a locally exported Handshake result set."""
import json
import os
from pathlib import Path
from typing import Any
from .job import Job

HOST = 'app.joinhandshake.com'


class NeedsLogin(RuntimeError):
    """A local result file is required."""


def _export():
    value = os.environ.get('JOB_HANDSHAKE_EXPORT')
    return Path(value) if value else None


def status():
    path = _export()
    return {'export_available': bool(path and path.is_file())}


def _to_jobs(payload: dict[str, Any], keyword: str) -> list[Job]:
    data = (payload.get("data") or {}).get("jobSearch") or {}
    edges = data.get("edges") or []
    jobs: list[Job] = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        job = node.get("job") or {}
        if not job:
            continue
        employer = job.get("employer") or {}
        locations = [loc.get("displayName") for loc in (job.get("locations") or [])
                     if loc.get("displayName")]
        salary = job.get("salaryRange") or {}
        pay = ""
        if salary.get("min") or salary.get("max"):
            unit = ((salary.get("paySchedule") or {}).get("friendlyName") or "").lower()
            suffix = {"yearly": "/yr", "hourly": "/hr", "monthly": "/mo"}.get(unit, "")
            pay = (f"${salary.get('min', 0):,.0f} - ${salary.get('max', 0):,.0f}{suffix}"
                   if salary.get("max") else f"${salary.get('min', 0):,.0f}{suffix}")
        jobs.append(Job(
            source="handshake",
            title=job.get("title") or "",
            company=employer.get("name") or "",
            location=", ".join(locations) or ("Remote" if job.get("remote") else ""),
            url=f"https://{HOST}/jobs/{job.get('id')}",
            apply_url="",          # apply happens inside Handshake
            posted_at=job.get("applyStart"),
            salary=pay,
            external_id=str(job.get("id") or ""),
            raw={"match": keyword, "search_id": data.get("searchId")},
        ))
    return jobs


def search(keyword, limit=20, session=None):
    path = _export()
    if not path or not path.is_file():
        raise NeedsLogin('Set JOB_HANDSHAKE_EXPORT to a local result file')
    records = _to_jobs(json.loads(path.read_text(encoding='utf-8')), keyword)
    words = keyword.casefold().split()
    return [job for job in records if all(word in (job.title+' '+job.company).casefold() for word in words)][:limit]
