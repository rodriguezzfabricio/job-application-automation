"""RED tests — outbox persistence (delivery phase, S1/P10).

Admitted job alerts land in the outbox; the Phase-5 Gmail sender drains
unsent rows. The kind vocabulary is closed at the schema level.
"""

import os
import sqlite3
import tempfile
import unittest

from server import db


ALERT = {
    "kind": "job_alert",
    "job_id": "uuid-1",
    "title": "Backend Engineer",
    "employer": "Acme",
    "url": "https://jobs.acme.example/uuid-1",
    "alert_text": "first-hour job discovery [heuristic — a1, 2026-10-21]",
    "reservation": {"kind": "alert", "day_key": "2026-10-21", "at": "t1"},
}


class OutboxTable(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)

    def test_insert_and_list_unsent(self):
        outbox_id = self.store.insert_outbox_alert(ALERT, "ashby:acme", now=1_700_000_000.0)
        rows = self.store.list_unsent_alerts()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], outbox_id)
        self.assertEqual(row["kind"], "job_alert")
        self.assertEqual(row["job_id"], "uuid-1")
        self.assertEqual(row["source_id"], "ashby:acme")
        self.assertEqual(row["alert_text"], ALERT["alert_text"])
        self.assertEqual(row["created_at"], "2023-11-14T22:13:20Z")
        self.assertIsNone(row["sent_at"])

    def test_mark_sent_removes_from_unsent(self):
        outbox_id = self.store.insert_outbox_alert(ALERT, "ashby:acme", now=1_700_000_000.0)
        self.store.mark_outbox_sent(outbox_id, now=1_700_000_100.0)
        self.assertEqual(self.store.list_unsent_alerts(), [])
        with self.store.conn() as c:
            row = c.execute("SELECT sent_at FROM outbox WHERE id=?",
                            (outbox_id,)).fetchone()
        self.assertEqual(row["sent_at"], "2023-11-14T22:15:00Z")

    def test_kind_vocabulary_closed(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert_outbox_alert(
                dict(ALERT, kind="daily_digest"), "ashby:acme", now=1_700_000_000.0)


if __name__ == "__main__":
    unittest.main()
