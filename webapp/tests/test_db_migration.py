"""RED test — startup migration for the politeness schema (MIGRATION-001).

The live volume carries the skeleton `sources` table from the first deploy;
the Phase 1 code SELECTs the new politeness columns. CREATE TABLE IF NOT
EXISTS does not add columns to an existing table, so Store on an old-schema
file raised `no such column: consecutive_429` and took /healthz down (Fly
check critical, deploy v4). This test reproduces that exact condition: a DB
file written with the OLD schema must open cleanly with the new columns
present and defaulted, and the politeness writes must work on it.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from server import db

_OLD_SKELETON_SCHEMA = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('ashby','greenhouse','lever')),
    host TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    registered_at TEXT,
    last_success_at REAL,
    last_failure_at REAL,
    last_status INTEGER,
    jobs_seen INTEGER NOT NULL DEFAULT 0
);
"""

# The live volume condition MIGRATION-002 targets: a v1-deployed file that has
# the MIGRATION-001 politeness columns but none of the feed-state columns.
_OLD_MIGRATION_001_SCHEMA = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('ashby','greenhouse','lever')),
    host TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    registered_at TEXT,
    last_success_at REAL,
    last_failure_at REAL,
    last_status INTEGER,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    consecutive_429 INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    deferred_until REAL NOT NULL DEFAULT 0
);
"""


class StoreMigratesOldSchema(unittest.TestCase):
    def _old_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(_OLD_SKELETON_SCHEMA)
        conn.commit()
        conn.close()

    def test_opens_old_schema_and_exposes_politeness_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            self._old_db(path)
            store = db.Store(path)  # must migrate, not raise
            with store.conn() as c:
                c.execute(
                    "INSERT INTO sources (id, name, kind, host, feed_url)"
                    " VALUES ('ashby:acme','acme','ashby','api.ashbyhq.com',"
                    " 'https://api.ashbyhq.com/posting-api/job-board/acme')"
                )
            rows = store.list_sources()  # the operation that crashed live
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["consecutive_429"], 0)
            self.assertEqual(rows[0]["paused"], 0)
            self.assertEqual(rows[0]["deferred_until"], 0)

    def test_politeness_writes_work_on_migrated_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            self._old_db(path)
            store = db.Store(path)
            with store.conn() as c:
                c.execute(
                    "INSERT INTO sources (id, name, kind, host, feed_url)"
                    " VALUES ('ashby:acme','acme','ashby','api.ashbyhq.com',"
                    " 'https://api.ashbyhq.com/posting-api/job-board/acme')"
                )
            now = time.time()
            n = store.record_throttled(
                "ashby:acme", "api.ashbyhq.com", 429, 3600, now
            )
            self.assertEqual(n, 1)
            row = store.get_source("ashby:acme")
            self.assertEqual(row["consecutive_429"], 1)
            self.assertEqual(row["paused"], 0)
            self.assertAlmostEqual(
                store.host_cooldown_until("api.ashbyhq.com"), now + 3600, delta=1.0
            )


class StoreMigratesOldSchemaM002(unittest.TestCase):
    def _old_001_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(_OLD_MIGRATION_001_SCHEMA)
        conn.commit()
        conn.close()

    def test_opens_001_schema_and_exposes_feed_state_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            self._old_001_db(path)
            store = db.Store(path)  # must migrate, not raise
            with store.conn() as c:
                c.execute(
                    "INSERT INTO sources (id, name, kind, host, feed_url)"
                    " VALUES ('ashby:acme','acme','ashby','api.ashbyhq.com',"
                    " 'https://api.ashbyhq.com/posting-api/job-board/acme')"
                )
            row = store.get_source("ashby:acme")
            self.assertIsNone(row["schema_fingerprint"])
            self.assertEqual(row["consecutive_invalid"], 0)
            self.assertEqual(row["consecutive_http_404"], 0)
            self.assertEqual(row["consecutive_empty"], 0)
            self.assertEqual(row["dead"], 0)

    def test_save_feed_state_works_on_migrated_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            self._old_001_db(path)
            store = db.Store(path)
            with store.conn() as c:
                c.execute(
                    "INSERT INTO sources (id, name, kind, host, feed_url)"
                    " VALUES ('ashby:acme','acme','ashby','api.ashbyhq.com',"
                    " 'https://api.ashbyhq.com/posting-api/job-board/acme')"
                )
            store.save_feed_state("ashby:acme", {
                "schema_fingerprint": "fp-1",
                "consecutive_invalid": 2,
                "consecutive_http_404": 1,
                "consecutive_empty": 0,
                "dead": 0,
            })
            row = store.get_source("ashby:acme")
            self.assertEqual(row["schema_fingerprint"], "fp-1")
            self.assertEqual(row["consecutive_invalid"], 2)
            self.assertEqual(row["consecutive_http_404"], 1)
            self.assertEqual(row["consecutive_empty"], 0)
            self.assertEqual(row["dead"], 0)


# The pre-a6 applications/status_history signature: canonical id column named
# `id`, REAL epoch timestamps, no employer/title/apply-url fields, history
# keyed by job_id with an `at` column. MIGRATION-003 rebuilds both tables.
_OLD_APPLICATIONS_SCHEMA = """
CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'Saved'
        CHECK(status IN ('Saved','Applied','Interviewing','Offer','Rejected','Archived')),
    applied_at REAL,
    notes TEXT,
    cover_letter TEXT,
    resume_ref TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    at REAL NOT NULL
);
"""


class StoreMigratesOldApplications(unittest.TestCase):
    def _old_apps_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(_OLD_APPLICATIONS_SCHEMA)
        conn.execute(
            "INSERT INTO applications (id, status, applied_at, notes,"
            " cover_letter, resume_ref, updated_at)"
            " VALUES ('ashby:acme:job-1','Applied',1700000000.0,'note',"
            " 'cl','rr',1700000000.0)"
        )
        conn.execute(
            "INSERT INTO status_history (job_id, status, at)"
            " VALUES ('ashby:acme:job-1','Applied',1700000000.0)"
        )
        conn.commit()
        conn.close()

    def test_migration_003_rebuilds_to_a6_columns_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            self._old_apps_db(path)
            store = db.Store(path)  # must rebuild, not raise
            app = store.get_application("ashby:acme:job-1")
            self.assertEqual(app["canonical_job_id"], "ashby:acme:job-1")
            self.assertEqual(app["status"], "Applied")
            self.assertEqual(app["notes"], "note")
            self.assertEqual(app["cover_letter_ref"], "cl")
            self.assertEqual(app["tailored_resume_ref"], "rr")
            # REAL epoch timestamps convert to ISO-8601 TEXT (a6 pins TEXT).
            self.assertEqual(app["applied_at"], "2023-11-14T22:13:20Z")
            for column in ("employer", "title", "canonical_apply_url",
                           "saved_at", "status_changed_at", "fact_store_version"):
                self.assertIn(column, app)
            history = store.status_history("ashby:acme:job-1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["application_id"], "ashby:acme:job-1")
            self.assertEqual(history[0]["changed_at"], "2023-11-14T22:13:20Z")


if __name__ == "__main__":
    unittest.main()
