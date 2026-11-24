"""RED tests — applications read API (a7 endpoint allowlist).

GET /api/applications lists every tracked application; GET /api/applications/<id>
returns one; unknown ids 404. Read-only: no POST/DELETE routes exist for
applications in this slice (writes stay on the jobs/status/mark-applied
surface, which lands with the tracking endpoints).
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from server import config, db, health, httpd


class ApplicationsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_token = config.SETUP_TOKEN
        config.SETUP_TOKEN = "test-setup-token"
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
        config.SETUP_TOKEN = cls._saved_token

    def _seed(self):
        self.store.mark_applied("ashby:acme:job-1", now=1_700_000_000.0)
        with self.store.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO applications (canonical_job_id)"
                " VALUES ('greenhouse:acme:job-2')"
            )

    def test_list_applications(self):
        self._seed()
        with urllib.request.urlopen(f"{self.base}/api/applications") as resp:
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read())
        ids = {a["canonical_job_id"] for a in payload["applications"]}
        self.assertEqual(ids, {"ashby:acme:job-1", "greenhouse:acme:job-2"})

    def test_get_one_application(self):
        self._seed()
        with urllib.request.urlopen(
            f"{self.base}/api/applications/ashby:acme:job-1"
        ) as resp:
            payload = json.loads(resp.read())
        self.assertEqual(
            payload["application"]["canonical_job_id"], "ashby:acme:job-1"
        )
        self.assertEqual(payload["application"]["status"], "Applied")

    def test_unknown_application_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.base}/api/applications/ghost:job")
        self.assertEqual(cm.exception.code, 404)

    def test_export_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.base}/api/export")
        self.assertEqual(cm.exception.code, 401)

    def test_export_returns_both_csv_artifacts(self):
        self._seed()
        req = urllib.request.Request(f"{self.base}/api/export", headers={
            "Authorization": f"Bearer {config.SETUP_TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
        self.assertEqual(
            set(payload), {"applications.csv", "status_history.csv"})
        self.assertTrue(payload["applications.csv"].startswith(
            "canonical_job_id,employer,title"))
        self.assertTrue(payload["status_history.csv"].startswith(
            "id,application_id,status,changed_at"))


if __name__ == "__main__":
    unittest.main()
