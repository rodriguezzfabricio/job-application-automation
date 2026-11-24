"""Snapshot-ingestion tests — one poll → parse, novelty gate, persist, snapshot
boundary (a1 raw-id unseen; a2 freshness: window, watermark, republish flag).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from server import db, ingest


NOW = 1_700_000_000.0


def _iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _ashby_body(raw_id="uuid-1", title="Backend Engineer",
                published_epoch_s=NOW - 100):
    return json.dumps({
        "jobs": [{
            "id": raw_id,
            "title": title,
            "location": "Remote",
            "jobUrl": f"https://jobs.acme.example/{raw_id}",
            "publishedAt": _iso(published_epoch_s),
            "isListed": True,
        }],
    })


def _lever_body(raw_id="posting-1", title="Frontend Engineer",
                created_epoch_s=NOW - 100):
    # a2: Lever createdAt is epoch MILLISECONDS, not seconds
    return json.dumps([{
        "id": raw_id,
        "text": title,
        "hostedUrl": f"https://jobs.acme.example/{raw_id}",
        "categories": {"location": "NYC"},
        "createdAt": int(created_epoch_s * 1000),
    }])


class IngestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)

    def _source(self, kind="ashby", source_id="ashby:acme", name="Acme"):
        self.store.add_source({
            "id": source_id, "name": name, "kind": kind,
            "host": "board.example", "feed_url": "https://board.example/feed",
        })
        return next(r for r in self.store.list_sources()
                    if r["id"] == source_id)

    def test_happy_ashby_snapshot_counts_new(self):
        result = ingest.ingest_snapshot(
            self.store, self._source(), _ashby_body(), NOW)
        self.assertEqual(result["total_parsed"], 1)
        self.assertEqual(len(result["new_records"]), 1)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["employer"], "Acme")

    def test_job_absent_from_two_snapshots_deactivates(self):
        source = self._source()
        both = json.dumps({"jobs": [
            {"id": "job-1", "title": "Kept", "publishedAt": _iso(NOW - 100)},
            {"id": "job-2", "title": "Dropped",
             "publishedAt": _iso(NOW - 100)},
        ]})
        only_one = json.dumps({"jobs": [
            {"id": "job-1", "title": "Kept", "publishedAt": _iso(NOW - 100)},
        ]})
        ingest.ingest_snapshot(self.store, source, both, NOW)
        ingest.ingest_snapshot(self.store, source, only_one, NOW)
        self.assertEqual(self.store.get_job("ashby:acme:job-2")["active"], 1)
        ingest.ingest_snapshot(self.store, source, only_one, NOW)
        self.assertEqual(self.store.get_job("ashby:acme:job-2")["active"], 0)

    def test_republish_candidate_same_title_employer_seen_earlier(self):
        source = self._source()
        ingest.ingest_snapshot(self.store, source, _ashby_body(), NOW)
        again = ingest.ingest_snapshot(
            self.store, source, _ashby_body(raw_id="uuid-2"), NOW)
        self.assertEqual(len(again["new_records"]), 1)
        self.assertEqual(len(again["republish_candidates"]), 1)
        self.assertEqual(
            again["republish_candidates"][0]["raw_id"], "uuid-2")

    def test_possible_stale_flags_31_day_old_record(self):
        body = _ashby_body(raw_id="old-31d", title="Ancient Role",
                           published_epoch_s=NOW - 31 * 86400)
        result = ingest.ingest_snapshot(self.store, self._source(), body, NOW)
        self.assertEqual(len(result["possible_stale"]), 1)
        self.assertEqual(result["possible_stale"][0]["raw_id"], "old-31d")

    def test_wrong_envelope_shape_raises_ingest_error_with_kind(self):
        with self.assertRaises(ingest.IngestError) as cm:
            ingest.ingest_snapshot(
                self.store, self._source(),
                json.dumps({"results": []}), NOW)
        self.assertIn("ashby", str(cm.exception))

    def test_invalid_json_raises_ingest_error(self):
        with self.assertRaises(ingest.IngestError) as cm:
            ingest.ingest_snapshot(
                self.store, self._source(), b"{not json", NOW)
        self.assertEqual(str(cm.exception), "invalid JSON")

    def test_lever_epoch_ms_fixture_is_new(self):
        result = ingest.ingest_snapshot(
            self.store, self._source(kind="lever", source_id="lever:acme"),
            _lever_body(), NOW)
        self.assertEqual(result["total_parsed"], 1)
        self.assertEqual(len(result["new_records"]), 1)
        # epoch-ms divided to seconds (a2 trap)
        self.assertAlmostEqual(
            result["new_records"][0]["first_published_epoch_s"], NOW - 100)

    def test_changed_content_is_updated_not_new(self):
        source = self._source()
        ingest.ingest_snapshot(self.store, source, _ashby_body(), NOW)
        changed = ingest.ingest_snapshot(
            self.store, source, _ashby_body(title="Staff Backend Engineer"),
            NOW)
        self.assertEqual(changed["updated_count"], 1)
        self.assertEqual(len(changed["new_records"]), 0)

    def test_second_identical_snapshot_is_zero_new_zero_updated(self):
        source = self._source()
        body = _ashby_body()
        first = ingest.ingest_snapshot(self.store, source, body, NOW)
        self.assertEqual(len(first["new_records"]), 1)
        second = ingest.ingest_snapshot(self.store, source, body, NOW)
        self.assertEqual(len(second["new_records"]), 0)
        self.assertEqual(second["updated_count"], 0)

    def test_future_or_outside_window_records_are_not_new(self):
        body = json.dumps({"jobs": [
            {"id": "future-1", "title": "Future Role",
             "publishedAt": _iso(NOW + 1000)},
            {"id": "old-1", "title": "Old Role",
             "publishedAt": _iso(NOW - 7200)},
        ]})
        result = ingest.ingest_snapshot(self.store, self._source(), body, NOW)
        self.assertEqual(result["total_parsed"], 2)
        self.assertEqual(result["new_records"], [])


if __name__ == "__main__":
    unittest.main()
