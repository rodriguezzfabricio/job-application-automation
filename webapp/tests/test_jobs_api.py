"""RED tests — /api/jobs read surface + a2 pinned constants.

Jobs are served read-only (no job mutation exists — the system never submits,
IRON_LAW §7; applications are the only operator-writable job state and live
under the applications table). BOARD_STALE_AFTER pins the a2 chip staleness
constant (interval + 900 = 1800 s).
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request

from server import config, db, health, httpd


class JobsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.store = db.Store(os.path.join(cls.tmpdir.name, "t.sqlite"))
        cls.heartbeat = health.Heartbeat()
        cls.heartbeat.touch()
        cls.httpd = httpd.make_server(
            ("127.0.0.1", 0),
            store=cls.store,
            heartbeat=cls.heartbeat,
            interval=config.POLL_INTERVAL,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        with self.store.conn() as c:
            c.execute("DELETE FROM jobs")

    def _get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_board_stale_after_pin(self):
        self.assertEqual(config.BOARD_STALE_AFTER, config.POLL_INTERVAL + 900)

    def test_jobs_endpoint_lists_jobs_newest_first(self):
        self.store.upsert_jobs(
            "ashby:acme",
            [
                {
                    "source_id": "ashby:acme", "raw_id": "j1",
                    "title": "Backend Engineer", "employer": "Acme",
                    "location": None, "url": None,
                    "first_published_field": "publishedAt",
                    "first_published_epoch_s": 1_700_000_000.0,
                    "fetched_at_epoch_s": 1_700_000_100.0,
                }
            ],
            now=1_700_000_100.0,
        )
        self.store.upsert_jobs(
            "ashby:acme",
            [
                {
                    "source_id": "ashby:acme", "raw_id": "j2",
                    "title": "Frontend Engineer", "employer": "Acme",
                    "location": None, "url": None,
                    "first_published_field": "publishedAt",
                    "first_published_epoch_s": 1_700_000_200.0,
                    "fetched_at_epoch_s": 1_700_000_200.0,
                }
            ],
            now=1_700_000_200.0,
        )
        status, body = self._get_json("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([j["raw_id"] for j in body["jobs"]], ["j2", "j1"])

    def test_jobs_endpoint_empty(self):
        status, body = self._get_json("/api/jobs")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["jobs"], [])


if __name__ == "__main__":
    unittest.main()
