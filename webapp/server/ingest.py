"""Snapshot ingestion for one polled source (classify-phase storage).

One poll = one snapshot: decode, parse by kind, gate novelty, persist, then
mark the snapshot boundary (a2: absent for 2 consecutive snapshots deactivates).
"""

import json

from . import envelopes, novelty


class IngestError(ValueError):
    """Feed body could not be ingested: invalid JSON or wrong envelope shape."""


_PARSERS = {
    "ashby": envelopes.parse_ashby,
    "greenhouse": envelopes.parse_greenhouse,
    "lever": envelopes.parse_lever,
}


def ingest_snapshot(store, source_row, body, now):
    """Ingest one fetched body for one source and return the classify summary.

    new_records passes only the alert gate's first condition (first-seen AND
    within the bootstrap window); is_new never claims original-publication
    proof — liveness-verified is a separate later gate (a2).
    """
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise IngestError("invalid JSON") from exc
    kind = source_row["kind"]
    try:
        records = _PARSERS[kind](
            payload, source_row["id"], source_row["name"], now)
    except envelopes.EnvelopeError as exc:
        raise IngestError(f"{kind} envelope: {exc}") from exc
    seen_jobs = store.list_jobs(source_id=source_row["id"], active_only=False)
    seen_raw_ids = {job["raw_id"] for job in seen_jobs}
    seen_titles = {
        f"{novelty.normalize_title(job['title'])}|{novelty.normalize_title(job['employer'])}": job["last_seen_at"]
        for job in seen_jobs
    }
    new_records = [
        record for record in records
        if novelty.is_new(record, seen_raw_ids, now)
    ]
    _, updated_count = store.upsert_jobs(source_row["id"], records, now)
    store.mark_snapshot(
        source_row["id"], [record["raw_id"] for record in records], now)
    return {
        "new_records": new_records,
        "total_parsed": len(records),
        "updated_count": updated_count,
        "possible_stale": [
            record for record in records
            if novelty.possible_stale(record, now)
        ],
        "republish_candidates": [
            record for record in new_records
            if novelty.republish_candidate(record, seen_titles, now)
        ],
        "employer": source_row["name"],
    }
