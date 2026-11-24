"""R3 RED test — /healthz reflects DISCOVERY success, not process liveness alone.

Negative control: the E9 probe refreshed its heartbeat in `finally`, so /healthz
reported ok:true even when every feed failed (probe_p3_slice.py:58). This test pins
the corrected contract (knowledge a8 F5 + R3):

- heartbeat fresh + all boards fresh            -> 200
- heartbeat fresh + a board with stale success  -> 503  (discovery failure)
- zero boards + fresh heartbeat                 -> 200 with boards_registered=0
- heartbeat stale                               -> 503  (process liveness)

A GREEN on this test before the fix existed is itself a failure (anti-cheat).
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request

from server import config, db, health, httpd


class HealthzReflectsDiscoveryFailure(unittest.TestCase):
    """Serves /healthz over a real socket against a temp DB (no network fetches)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "test.sqlite")
        cls.store = db.Store(cls.db_path)
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

    def _get_healthz(self):
        try:
            resp = urllib.request.urlopen(self.base + "/healthz", timeout=5)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))
        with resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _clear_boards(self):
        with self.store.conn() as c:
            c.execute("DELETE FROM sources")

    def _register_board(self, board_id, last_success_age_s=None):
        now = time.time()
        row = {
            "id": board_id,
            "name": board_id,
            "kind": "ashby",
            "host": "api.ashbyhq.com",
            "feed_url": f"https://api.ashbyhq.com/posting-api/job-board/{board_id}",
        }
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO sources (id, name, kind, host, feed_url) VALUES (:id,:name,:kind,:host,:feed_url)",
                row,
            )
            if last_success_age_s is not None:
                c.execute(
                    "UPDATE sources SET last_success_at=? WHERE id=?",
                    (now - last_success_age_s, board_id),
                )

    def test_fresh_heartbeat_and_fresh_board_is_200(self):
        self._clear_boards()
        self.heartbeat.touch()
        self._register_board("fresh-board", last_success_age_s=60)
        status, body = self._get_healthz()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "ok")

    def test_stale_board_returns_503_even_with_fresh_heartbeat(self):
        # The E9 defect: heartbeat fresh, every feed failed -> must NOT be ok.
        self._clear_boards()
        self.heartbeat.touch()
        self._register_board(
            "dead-board", last_success_age_s=2 * config.POLL_INTERVAL + 1
        )
        status, body = self._get_healthz()
        self.assertEqual(status, 503, body)
        self.assertIn("stale_boards", body)

    def test_zero_boards_fresh_heartbeat_is_200_with_note(self):
        self._clear_boards()
        self.heartbeat.touch()  # reset the shared heartbeat after other tests backdated it
        status, body = self._get_healthz()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["boards_registered"], 0)
        self.assertIn("note", body)

    def test_stale_heartbeat_returns_503(self):
        self._clear_boards()
        self.heartbeat.touch(age_s=2 * config.POLL_INTERVAL + 1)
        status, body = self._get_healthz()
        self.assertEqual(status, 503, body)
        self.assertIn("heartbeat", body)

    def test_never_polled_board_stays_200_with_pending_list(self):
        # Live defect (L-0015): a just-registered board with no poll yet made
        # /healthz 503, Fly's check failed, and the proxy stopped routing the
        # whole site. Never-attempted is not a discovery failure.
        self._clear_boards()
        self.heartbeat.touch()
        self._register_board("fresh-board")  # no success, no failure
        status, body = self._get_healthz()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["boards_registered"], 1)
        self.assertEqual(body["stale_boards"], [])
        self.assertIn("fresh-board", body["pending_first_poll"])

    def test_failing_board_without_success_is_503(self):
        # The E9 negative control must survive: feeds actively failing while
        # the heartbeat is fresh is the discovery-failure signature.
        self._clear_boards()
        self.heartbeat.touch()
        self._register_board("dead-board")
        with self.store.conn() as c:
            c.execute(
                "UPDATE sources SET last_failure_at=? WHERE id=?",
                (time.time() - 60, "dead-board"),
            )
        status, body = self._get_healthz()
        self.assertEqual(status, 503, body)
        self.assertIn("dead-board", [b["id"] for b in body["stale_boards"]])


if __name__ == "__main__":
    unittest.main()
