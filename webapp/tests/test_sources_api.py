"""Sources API RED tests — board registration, check-now, resume (a1/a9, L-0010).

Server-enforced contract:
- Registration: kind -> frozen URL template over sanctioned hosts only; token
  shape validation; live 200 with typed envelope validation. An unknown kind is
  non-representable, so registration can never widen the feed transport (P1).
- check-now is an authenticated POST; while the board is backing off / paused /
  under a host Retry-After deadline the server refuses (429 + Retry-After).
- Manual resume never overrides an outstanding host server deadline (409 with
  the a1 deferral message).
- Interim auth: Bearer SETUP_TOKEN on every write endpoint until a11 sessions.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from server import config, db, health, httpd, politeness, sources
from tests.fixtures import FixtureServer, fixture_transport_factory

ASHBY_OK = b'{"jobs": [{"id": "j1"}]}'
NOT_AN_ENVELOPE = b'{"results": []}'

TOKEN = "test-setup-token"


class SourcesApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_token = config.SETUP_TOKEN
        config.SETUP_TOKEN = TOKEN
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.store = db.Store(os.path.join(cls.tmpdir.name, "t.sqlite"))
        cls.heartbeat = health.Heartbeat()
        cls.heartbeat.touch()
        cls.fixture = FixtureServer([(200, {}, ASHBY_OK)])
        cls.fixture.start()
        cls.fetcher = politeness.PoliteFetcher(
            cls.store, transport_factory=fixture_transport_factory(cls.fixture.url())
        )
        cls.httpd = httpd.make_server(
            ("127.0.0.1", 0),
            store=cls.store,
            heartbeat=cls.heartbeat,
            interval=config.POLL_INTERVAL,
            fetcher=cls.fetcher,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.fixture.stop()
        cls.tmpdir.cleanup()
        config.SETUP_TOKEN = cls._saved_token

    def setUp(self):
        with self.store.conn() as c:
            c.execute("DELETE FROM sources")
            c.execute("DELETE FROM host_cooldowns")
        self.fixture.hits.clear()
        self.fixture.responses[:] = [(200, {}, ASHBY_OK)]

    def _post(self, path, payload=None, token=TOKEN):
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), json.loads(err.read().decode("utf-8"))
        with resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))

    def _insert_board(self, board_id="acme", **overrides):
        row = {
            "id": f"ashby:{board_id}",
            "name": board_id,
            "kind": "ashby",
            "host": "api.ashbyhq.com",
            "feed_url": f"https://api.ashbyhq.com/posting-api/job-board/{board_id}",
        }
        row.update(overrides)
        cols = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        with self.store.conn() as c:
            c.execute(
                f"INSERT INTO sources ({cols}) VALUES ({placeholders})",
                row,
            )
        return self.store.get_source(row["id"])

    # --- interim write auth ---

    def test_write_endpoints_require_setup_token(self):
        status, _, body = self._post("/api/sources", token=None)
        self.assertEqual(status, 401, body)
        status, _, _ = self._post("/api/sources", token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(len(self.fixture.hits), 0)

    def test_write_endpoints_refuse_when_token_unconfigured(self):
        saved = config.SETUP_TOKEN
        config.SETUP_TOKEN = ""
        try:
            status, _, body = self._post("/api/sources", token=None)
            self.assertEqual(status, 503, body)
        finally:
            config.SETUP_TOKEN = saved
        self.assertEqual(len(self.fixture.hits), 0)

    # --- registration cannot widen the transport ---

    def test_register_unknown_kind_refused(self):
        status, _, body = self._post(
            "/api/sources",
            {"name": "LinkedIn", "kind": "linkedin", "board_id": "jobs"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(len(self.fixture.hits), 0)  # no socket ever opened
        self.assertEqual(self.store.list_sources(), [])

    def test_register_with_careers_url_persists(self):
        status, _, body = self._post(
            "/api/sources",
            {"name": "Acme2", "kind": "ashby", "board_id": "acme2",
             "careers_url": "https://jobs.ashbyhq.com/acme2"},
        )
        self.assertEqual(status, 201, body)
        row = self.store.get_source("ashby:acme2")
        self.assertEqual(row["careers_url"], "https://jobs.ashbyhq.com/acme2")

    def test_register_http_careers_url_refused_before_any_socket(self):
        hits_before = len(self.fixture.hits)
        status, _, body = self._post(
            "/api/sources",
            {"name": "Acme3", "kind": "ashby", "board_id": "acme3",
             "careers_url": "http://jobs.ashbyhq.com/acme3"},
        )
        self.assertEqual(status, 400, body)
        self.assertIsNone(self.store.get_source("ashby:acme3"))
        self.assertEqual(len(self.fixture.hits), hits_before)  # P1: no socket

    def test_feed_url_templates_stay_sanctioned(self):
        samples = {"ashby": "acme", "greenhouse": "a" * 32, "lever": "acme"}
        for kind, board_id in samples.items():
            url = sources.build_feed_url(kind, board_id)
            host = urllib.parse.urlparse(url).hostname
            self.assertIn(host, sources.SANCTIONED_FEED_HOSTS, kind)

    def test_register_bad_token_shape_refused(self):
        for kind, board_id in (
            ("greenhouse", "BAD TOKEN"),  # spaces/injection chars
            ("greenhouse", "../evil"),
            ("ashby", "a/b"),
            ("ashby", "../evil"),
            ("ashby", ""),
        ):
            status, _, body = self._post(
                "/api/sources", {"name": "x", "kind": kind, "board_id": board_id}
            )
            self.assertEqual(status, 400, (kind, board_id, body))
        self.assertEqual(len(self.fixture.hits), 0)

    def test_greenhouse_name_slug_token_accepted(self):
        # Measured against the live API (2026-10-21): boards-api.greenhouse.io
        # serves name slugs (e.g. /boards/airbnb/jobs -> 200) while a 32-hex
        # token 404s — the shape rule must accept slugs, not hex-only.
        url = sources.build_feed_url("greenhouse", "airbnb")
        self.assertEqual(
            url, "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs"
        )

    # --- live 200 + typed envelope validation ---

    def test_register_valid_ashby_envelope_201(self):
        status, _, body = self._post(
            "/api/sources", {"name": "Acme", "kind": "ashby", "board_id": "acme"}
        )
        self.assertEqual(status, 201, body)
        row = self.store.get_source("ashby:acme")
        self.assertIsNotNone(row)
        self.assertEqual(
            row["feed_url"], "https://api.ashbyhq.com/posting-api/job-board/acme"
        )
        self.assertEqual(len(self.fixture.hits), 1)

    def test_register_invalid_envelope_refused(self):
        self.fixture.responses[:] = [(200, {}, NOT_AN_ENVELOPE)]
        status, _, body = self._post(
            "/api/sources", {"name": "Acme", "kind": "ashby", "board_id": "acme"}
        )
        self.assertEqual(status, 400, body)
        self.assertIsNone(self.store.get_source("ashby:acme"))

    def test_register_upstream_error_reported(self):
        self.fixture.responses[:] = [(500, {}, b"boom")]
        status, _, body = self._post(
            "/api/sources", {"name": "Acme", "kind": "ashby", "board_id": "acme"}
        )
        self.assertEqual(status, 502, body)
        self.assertIsNone(self.store.get_source("ashby:acme"))

    def test_register_duplicate_conflicts(self):
        self._post("/api/sources", {"name": "Acme", "kind": "ashby", "board_id": "acme"})
        status, _, body = self._post(
            "/api/sources", {"name": "Acme again", "kind": "ashby", "board_id": "acme"}
        )
        self.assertEqual(status, 409, body)

    # --- check-now server-side refusal (a9) ---

    def test_check_now_refused_while_deferred(self):
        self._insert_board("acme", deferred_until=time.time() + 7200)
        status, headers, body = self._post("/api/sources/ashby:acme/check-now")
        self.assertEqual(status, 429, body)
        self.assertIn("Retry-After", headers)
        self.assertGreaterEqual(body["retry_after_s"], 3600)
        self.assertIn("backing off", body["error"])
        self.assertEqual(len(self.fixture.hits), 0)

    def test_check_now_refused_under_host_deadline(self):
        self._insert_board("acme")
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO host_cooldowns (host, until) VALUES (?, ?)",
                ("api.ashbyhq.com", time.time() + 3000),
            )
        status, headers, body = self._post("/api/sources/ashby:acme/check-now")
        self.assertEqual(status, 429, body)
        self.assertIn("Retry-After", headers)
        self.assertEqual(len(self.fixture.hits), 0)

    def test_check_now_throttle_replies_retry_after(self):
        # The check itself hitting 429/403 must surface the fresh cooldown as
        # 429 + Retry-After (a9 countdown), not as a 200 {"status": 429}.
        self.fixture.responses[:] = [(429, {}, b"")]
        self._insert_board("acme")
        status, headers, body = self._post("/api/sources/ashby:acme/check-now")
        self.assertEqual(status, 429, body)
        self.assertIn("Retry-After", headers)
        self.assertIn("backing off", body["error"])

    def test_malformed_content_length_is_400(self):
        req = urllib.request.Request(
            self.base + "/api/sources", data=b"{}", method="POST"
        )
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", "abc")
        code = 0
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            code = resp.status
            resp.close()
        except urllib.error.HTTPError as err:
            code = err.code
        except Exception:  # noqa: BLE001 — RED state: handler crash = no response
            code = 0
        self.assertEqual(code, 400)

    def test_check_now_fetches_when_due(self):
        self._insert_board("acme")
        status, _, body = self._post("/api/sources/ashby:acme/check-now")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.fixture.hits), 1)
        row = self.store.get_source("ashby:acme")
        self.assertIsNotNone(row["last_success_at"])

    def test_check_now_unknown_board_404(self):
        status, _, _ = self._post("/api/sources/ashby:ghost/check-now")
        self.assertEqual(status, 404)

    # --- resume never overrides an outstanding server deadline (a1) ---

    def test_resume_clears_pause_when_no_host_deadline(self):
        self._insert_board(
            "acme",
            paused=1,
            consecutive_429=5,
            deferred_until=time.time() + 3600,
        )
        status, _, body = self._post("/api/sources/ashby:acme/resume")
        self.assertEqual(status, 200, body)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["paused"], 0)
        self.assertEqual(row["consecutive_429"], 0)
        self.assertEqual(row["deferred_until"], 0)

    def test_resume_refused_while_host_deadline_outstanding(self):
        self._insert_board("acme", paused=1, consecutive_429=5)
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO host_cooldowns (host, until) VALUES (?, ?)",
                ("api.ashbyhq.com", time.time() + 3000),
            )
        status, _, body = self._post("/api/sources/ashby:acme/resume")
        self.assertEqual(status, 409, body)
        self.assertTrue(
            body["error"].startswith("resume deferred — host Retry-After active until"),
            body,
        )
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["paused"], 1)  # pause state untouched


if __name__ == "__main__":
    unittest.main()
