"""RED tests — applications table, a6-conformed (Phase 7 tracking, scar P12).

The a6 contract pins the 13-field applications schema (canonical_job_id is the
ONLY key; TEXT ISO-8601 timestamps; six statuses) and the three mark-applied
branches: (a) absent row -> INSERT Applied with first-write-wins stamps; (b)
Saved with applied_at NULL -> UPDATE to Applied once; (c) NO-OP whenever
applied_at is set, and never a demotion from Interviewing/Offer/etc.
"""

import os
import sqlite3
import tempfile
import unittest

from server import db

STATUSES = ("Saved", "Applied", "Interviewing", "Offer", "Rejected", "Archived")

NOW = 1_700_000_000.0


class ApplicationsTable(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)

    def _add(self, job_id="ashby:acme:job-1", **overrides):
        row = {"canonical_job_id": job_id}
        row.update(overrides)
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO applications (canonical_job_id) VALUES (?)",
                (row["canonical_job_id"],),
            )
        return self.store.get_application(job_id)

    def test_new_application_defaults_to_saved(self):
        app = self._add()
        self.assertEqual(app["status"], "Saved")
        self.assertIsNone(app["applied_at"])
        self.assertIsNotNone(app["updated_at"])  # ISO-8601 TEXT stamp

    def test_a6_columns_present(self):
        app = self._add()
        for column in (
            "canonical_job_id", "employer", "title", "canonical_apply_url",
            "status", "saved_at", "applied_at", "status_changed_at",
            "updated_at", "notes", "cover_letter_ref", "tailored_resume_ref",
            "fact_store_version",
        ):
            self.assertIn(column, app)

    def test_set_status_transitions_and_records_history(self):
        self._add()
        self.store.set_status("ashby:acme:job-1", "Interviewing", now=NOW)
        self.store.set_status("ashby:acme:job-1", "Offer", now=NOW + 100)
        app = self.store.get_application("ashby:acme:job-1")
        self.assertEqual(app["status"], "Offer")
        # status_changed_at = the LAST transition's stamp.
        self.assertEqual(app["status_changed_at"], "2023-11-14T22:15:00Z")
        history = self.store.status_history("ashby:acme:job-1")
        self.assertEqual(
            [(h["status"], h["changed_at"]) for h in history],
            [("Interviewing", "2023-11-14T22:13:20Z"),
             ("Offer", "2023-11-14T22:15:00Z")],
        )

    def test_invalid_status_refused(self):
        self._add()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.set_status("ashby:acme:job-1", "Nonsense", now=NOW)

    def test_mark_applied_absent_row_inserts_applied(self):
        # a6 branch (a): row absent -> INSERT Applied, stamps first-write-wins.
        self.assertTrue(self.store.mark_applied("ashby:acme:job-9", now=NOW))
        app = self.store.get_application("ashby:acme:job-9")
        self.assertEqual(app["status"], "Applied")
        self.assertEqual(app["applied_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(app["saved_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(len(self.store.status_history("ashby:acme:job-9")), 1)

    def test_mark_applied_is_idempotent(self):
        # a6 branch (b) then (c): the applied_at IS NULL guard never rewrites.
        self._add()
        first = self.store.mark_applied("ashby:acme:job-1", now=NOW)
        self.assertTrue(first)
        second = self.store.mark_applied("ashby:acme:job-1", now=NOW + 100)
        self.assertFalse(second)
        app = self.store.get_application("ashby:acme:job-1")
        self.assertEqual(app["applied_at"], "2023-11-14T22:13:20Z")  # first wins
        self.assertEqual(app["status"], "Applied")

    def test_mark_applied_never_demotes_higher_status(self):
        # a6: mark-applied only upgrades a Saved row; Interviewing stays.
        self._add()
        self.store.set_status("ashby:acme:job-1", "Interviewing", now=NOW)
        self.assertFalse(self.store.mark_applied("ashby:acme:job-1", now=NOW + 1))
        app = self.store.get_application("ashby:acme:job-1")
        self.assertEqual(app["status"], "Interviewing")
        self.assertIsNone(app["applied_at"])


if __name__ == "__main__":
    unittest.main()
