"""Classify-phase envelope parsers for the three sanctioned job-board feeds.

Produces canonical job records per knowledge/a1.md (source coverage: Ashby,
Greenhouse and Lever board connectors) and knowledge/a2.md (freshness:
per-source first-published signals).
"""

from datetime import datetime, timezone


class EnvelopeError(ValueError):
    """Raised when a feed payload does not have the expected envelope shape."""


def _iso_to_epoch_s(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # date-only/naive values are midnight-UTC per a2 (machine TZ must not leak in)
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_ashby(payload, source_id, employer, fetched_at_epoch_s):
    if not isinstance(payload, dict):
        raise EnvelopeError("ashby payload must be a JSON object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise EnvelopeError("ashby payload is missing its jobs list")
    records = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        raw_id = job.get("id")
        title = job.get("title")
        if raw_id is None or raw_id == "" or title is None or title == "":
            continue
        record = {
            "source_id": source_id,
            "raw_id": str(raw_id),
            "title": title,
            "employer": employer,
            "location": job.get("location"),
            "url": job.get("jobUrl") or job.get("applyUrl"),
            "first_published_field": "publishedAt",
            "first_published_epoch_s": _iso_to_epoch_s(
                job.get("publishedAt")),
            "fetched_at_epoch_s": fetched_at_epoch_s,
            "raw": job,
        }
        if "isListed" in job:
            record["is_listed"] = job["isListed"]
        records.append(record)
    return records


def _greenhouse_first_published_epoch_s(job):
    metadata = job.get("metadata")
    if not isinstance(metadata, list):
        return None
    for item in metadata:
        if isinstance(item, dict) and item.get("name") == "first_published":
            return _iso_to_epoch_s(item.get("value"))
    return None


def parse_greenhouse(payload, source_id, employer, fetched_at_epoch_s):
    if not isinstance(payload, dict):
        raise EnvelopeError("greenhouse payload must be a JSON object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise EnvelopeError("greenhouse payload is missing its jobs list")
    records = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        raw_id = job.get("id")
        title = job.get("title")
        if raw_id is None or raw_id == "" or title is None or title == "":
            continue
        record = {
            "source_id": source_id,
            "raw_id": str(raw_id),
            "title": title,
            "employer": employer,
            "location": (job.get("location") or {}).get("name"),
            "url": job.get("absolute_url"),
            "first_published_field": "first_published",
            "first_published_epoch_s": _greenhouse_first_published_epoch_s(job),
            "fetched_at_epoch_s": fetched_at_epoch_s,
            "raw": job,
        }
        if "internal_job_id" in job:
            record["internal_job_id"] = (
                str(job["internal_job_id"])
                if job["internal_job_id"] is not None else None)
        records.append(record)
    return records


def _lever_created_epoch_s(value):
    if value is None:
        return None
    try:
        # a2: Lever createdAt is epoch MILLISECONDS, not seconds
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def parse_lever(payload, source_id, employer, fetched_at_epoch_s):
    if not isinstance(payload, list):
        raise EnvelopeError("lever payload must be a JSON array")
    records = []
    for posting in payload:
        if not isinstance(posting, dict):
            continue
        raw_id = posting.get("id")
        title = posting.get("text")
        if raw_id is None or raw_id == "" or title is None or title == "":
            continue
        record = {
            "source_id": source_id,
            "raw_id": str(raw_id),
            "title": title,
            "employer": employer,
            "location": (posting.get("categories") or {}).get("location"),
            "url": posting.get("hostedUrl") or posting.get("applyUrl"),
            "first_published_field": "createdAt",
            "first_published_epoch_s": _lever_created_epoch_s(
                posting.get("createdAt")),
            "fetched_at_epoch_s": fetched_at_epoch_s,
            "raw": posting,
        }
        records.append(record)
    return records
