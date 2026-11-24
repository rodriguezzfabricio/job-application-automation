"""RED tests — jobs table (classify-phase storage, a1 raw-id novelty + a2).

Canonical identity is source-scoped raw-id (a1: within-vendor novelty = raw-id
unseen; cross-board merge is REBUILD design, never here). The table carries
first_seen_at (this system's first sight — never original-publication proof,
a2) and an absence streak: a job missing from 2 consecutive snapshots
deactivates (a2), and content changes are edits — never re-alerts.
"""

import os
import tempfile
import time
import unittest

from server import db


def _record(**overrides):
    record = {
        "source_id": "ashby:acme",
        "raw_id": "job-1",
        "title": "Backend Engineer",
        "employer": "Acme",
        "location": None,
        "url": None,
        "first_published_field": "publishedAt",
        "first_published_epoch_s": 1_700_000_000.0,
        "fetched_at_epoch_s": 1_700_000_100.0,
    }
    record.update(overrides)
    return record


class JobsTable(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)

    def test_upsert_inserts_new_jobs_and_counts_them(self):
        new, updated = self.store.upsert_jobs(
            "ashby:acme", [_record()], now=1_700_000_100.0
        )
        self.assertEqual((new, updated), (1, 0))
        job = self.store.get_job("ashby:acme:job-1")
        self.assertIsNotNone(job)
        self.assertEqual(job["first_seen_at"], 1_700_000_100.0)

    def test_same_raw_id_same_content_is_not_new(self):
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_000_100.0)
        new, updated = self.store.upsert_jobs(
            "ashby:acme", [_record(fetched_at_epoch_s=1_700_001_000.0)],
            now=1_700_001_000.0,
        )
        self.assertEqual((new, updated), (0, 0))
        job = self.store.get_job("ashby:acme:job-1")
        self.assertEqual(job["first_seen_at"], 1_700_000_100.0)  # unchanged
        self.assertEqual(job["last_seen_at"], 1_700_001_000.0)

    def test_content_change_is_an_edit_not_new(self):
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_000_100.0)
        new, updated = self.store.upsert_jobs(
            "ashby:acme", [_record(title="Staff Backend Engineer")],
            now=1_700_001_000.0,
        )
        self.assertEqual((new, updated), (0, 1))
        job = self.store.get_job("ashby:acme:job-1")
        self.assertEqual(job["title"], "Staff Backend Engineer")
        self.assertEqual(job["first_seen_at"], 1_700_000_100.0)

    def test_raw_id_uniqueness_is_scoped_to_source(self):
        # Same raw id on a DIFFERENT board is a different row (a1: no
        # cross-board merge in v1).
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_000_100.0)
        other = _record(
            source_id="greenhouse:beta", raw_id="job-1",
            first_published_field="first_published",
        )
        new, _ = self.store.upsert_jobs("greenhouse:beta", [other], now=1_700_000_100.0)
        self.assertEqual(new, 1)
        self.assertIsNotNone(self.store.get_job("greenhouse:beta:job-1"))

    def test_absence_for_two_snapshots_deactivates(self):
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_000_100.0)
        # snapshot 1 without the job
        self.store.mark_snapshot("ashby:acme", [], now=1_700_001_000.0)
        job = self.store.get_job("ashby:acme:job-1")
        self.assertEqual(job["active"], 1)
        # snapshot 2 without the job -> deactivated
        self.store.mark_snapshot("ashby:acme", [], now=1_700_001_900.0)
        job = self.store.get_job("ashby:acme:job-1")
        self.assertEqual(job["active"], 0)

    def test_returning_job_resets_absence_streak(self):
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_000_100.0)
        self.store.mark_snapshot("ashby:acme", [], now=1_700_001_000.0)
        self.store.upsert_jobs("ashby:acme", [_record()], now=1_700_001_900.0)
        job = self.store.get_job("ashby:acme:job-1")
        self.assertEqual(job["active"], 1)


if __name__ == "__main__":
    unittest.main()
