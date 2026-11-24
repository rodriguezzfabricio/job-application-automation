"""Politeness state-machine RED tests (a1 politeness contract, L-0010).

Pins the rebuild design:
- backoff = min(86400, 900 × 2**min(N−1,5))  (golden, a1)
- envelope preserves status + Retry-After (unclamped server value)
- pause after 5 consecutive 429/403; one auto-probe after max(6 h, Retry-After)
- per-host cooldown scope: one board's Retry-After throttles the whole host
- an early request against an outstanding server deadline is non-representable
- success resets the machine
- the poller honors deferred_until and records host-cooldown deferrals

A GREEN on any of these before the machine existed is itself a failure.
"""

import os
import tempfile
import time
import unittest
from email.message import Message
from email.utils import formatdate

from server import config, db, health, politeness, poller, sources
from tests.fixtures import FixtureServer, fixture_transport_factory

ASHBY_OK = b'{"jobs": [{"id": "j1"}]}'


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SpyFetcher:
    """Records which boards the poller tried; raises canned outcomes per id."""

    def __init__(self, outcomes=None):
        self.polled = []
        self.outcomes = outcomes or {}

    def poll(self, row, timeout_s):
        self.polled.append(row["id"])
        outcome = self.outcomes.get(row["id"])
        if isinstance(outcome, Exception):
            raise outcome
        return 200, ASHBY_OK


class PolitenessStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.clock = FakeClock(1_000_000_000.0)
        self.server = FixtureServer([(200, {}, ASHBY_OK)])
        self.server.start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self.tmpdir.cleanup)
        self.fetcher = politeness.PoliteFetcher(
            self.store,
            transport_factory=fixture_transport_factory(self.server.url()),
            clock=self.clock,
        )

    def _add_board(self, board_id, host="api.ashbyhq.com"):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO sources (id, name, kind, host, feed_url)"
                " VALUES (?,?,?,?,?)",
                (
                    f"ashby:{board_id}",
                    board_id,
                    "ashby",
                    host,
                    f"https://{host}/posting-api/job-board/{board_id}",
                ),
            )
        return self.store.get_source(f"ashby:{board_id}")

    def _poll_n(self, row, n):
        for _ in range(n):
            self.fetcher.poll(row, timeout_s=config.FETCH_TIMEOUT_S)

    # --- golden backoff sequence (a1, cited) ---

    def test_backoff_golden_sequence(self):
        # 900s × 2**min(N−1,5): tops out at 900×32=28800; the 86400 ceiling is
        # a safety cap that never binds at the pinned cadence (a1).
        expected = [900, 1800, 3600, 7200, 14400, 28800, 28800]
        for n, want in zip(range(1, 8), expected):
            self.assertEqual(politeness.backoff_seconds(n), want, f"n={n}")
        self.assertEqual(politeness.backoff_seconds(100), 28800)
        self.assertLessEqual(politeness.backoff_seconds(100), config.BACKOFF_CAP_S)

    def test_backoff_cap_golden_binds(self):
        # The 86400 ceiling must bind for larger intervals (mutation check:
        # removing min() fails this line).
        self.assertEqual(
            politeness.backoff_seconds(1, interval=999999), config.BACKOFF_CAP_S
        )

    # --- Retry-After parsing (the production helper) ---

    def test_parse_retry_after_seconds(self):
        headers = Message()
        headers["Retry-After"] = "3600"
        self.assertEqual(sources.parse_retry_after(headers), 3600.0)

    def test_parse_retry_after_unclamped(self):
        headers = Message()
        headers["Retry-After"] = "9999999"  # beyond the 86400 self-computed cap
        self.assertEqual(sources.parse_retry_after(headers), 9999999.0)

    def test_parse_retry_after_http_date(self):
        target = time.time() + 1800
        headers = Message()
        headers["Retry-After"] = formatdate(target, usegmt=True)
        parsed = sources.parse_retry_after(headers)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, 1800.0, delta=5.0)

    def test_parse_retry_after_absent_or_garbage(self):
        self.assertIsNone(sources.parse_retry_after(Message()))
        headers = Message()
        headers["Retry-After"] = "not-a-date"
        self.assertIsNone(sources.parse_retry_after(headers))

    # --- unclamped server Retry-After through the full loop ---

    def test_server_retry_after_unclamped_host_cooldown(self):
        self.server.responses[:] = [(429, {"Retry-After": "9999999"}, b"")]
        row = self._add_board("acme")
        self._poll_n(row, 1)
        self.assertAlmostEqual(
            self.store.host_cooldown_until("api.ashbyhq.com"),
            self.clock.now + 9999999,
            delta=1.0,
        )
        row = self.store.get_source("ashby:acme")
        self.assertAlmostEqual(row["deferred_until"], self.clock.now + 9999999, delta=1.0)

    # --- backoff without Retry-After (self-computed term) ---

    def test_backoff_deferral_without_retry_after(self):
        self.server.responses[:] = [(429, {}, b"")]
        row = self._add_board("acme")
        self._poll_n(row, 1)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["consecutive_429"], 1)
        self.assertEqual(row["paused"], 0)
        self.assertAlmostEqual(row["deferred_until"], self.clock.now + 900, delta=1.0)
        self._poll_n(row, 1)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["consecutive_429"], 2)
        self.assertAlmostEqual(row["deferred_until"], self.clock.now + 1800, delta=1.0)

    # --- pause after 5 consecutive 429/403 + one probe after 6 h ---

    def test_five_throttles_pause_with_six_hour_probe(self):
        # No Retry-After here: consecutive polls at the same instant would be
        # correctly blocked by a server cooldown; the unclamped-server-term
        # path is pinned by test_server_retry_after_unclamped_host_cooldown.
        self.server.responses[:] = 5 * [(429, {}, b"")] + [(200, {}, ASHBY_OK)]
        row = self._add_board("acme")
        self._poll_n(row, 5)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["consecutive_429"], 5)
        self.assertEqual(row["paused"], 1)
        # probe deadline = max(backoff(5)=14400, 6h=21600)
        self.assertAlmostEqual(
            row["deferred_until"], self.clock.now + 6 * 3600, delta=1.0
        )
        # one auto-probe after the window; success resets the machine
        self.clock.advance(6 * 3600 + 1)
        self._poll_n(row, 1)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["paused"], 0)
        self.assertEqual(row["consecutive_429"], 0)
        self.assertEqual(row["deferred_until"], 0)
        self.assertIsNotNone(row["last_success_at"])

    # --- per-host cooldown scope ---

    def test_host_cooldown_scopes_to_whole_host(self):
        self.server.responses[:] = [(429, {"Retry-After": "7200"}, b"")]
        row_a = self._add_board("acme")
        row_b = self._add_board("beta")
        self._poll_n(row_a, 1)
        self.assertAlmostEqual(
            self.store.host_cooldown_until("api.ashbyhq.com"),
            self.clock.now + 7200,
            delta=1.0,
        )
        with self.assertRaises(politeness.HostCoolingDown):
            self.fetcher.poll(row_b, timeout_s=config.FETCH_TIMEOUT_S)
        self.assertEqual(len(self.server.hits), 1)  # board B opened no socket

    # --- early request against an outstanding server deadline ---

    def test_early_request_non_representable(self):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO host_cooldowns (host, until) VALUES (?, ?)",
                ("api.ashbyhq.com", self.clock.now + 5000),
            )
        with self.assertRaises(politeness.HostCoolingDown) as ctx:
            self.fetcher.fetch(
                "https://api.ashbyhq.com/posting-api/job-board/acme",
                "api.ashbyhq.com",
                timeout_s=config.FETCH_TIMEOUT_S,
            )
        self.assertEqual(ctx.exception.host, "api.ashbyhq.com")
        self.assertEqual(len(self.server.hits), 0)

    def test_host_cooldown_never_shortened(self):
        # Regression (reviewer finding): a smaller Retry-After recorded later
        # must not erase a longer outstanding deadline on the same host.
        # Store-level: with the fetch gate active, two boards can only observe
        # different Retry-After values at different instants, so the invariant
        # is exercised directly against record_throttled (deterministic).
        self._add_board("acme")
        self._add_board("beta")
        now = self.clock.now
        self.store.record_throttled("ashby:acme", "api.ashbyhq.com", 429, 3600, now)
        self.store.record_throttled(
            "ashby:beta", "api.ashbyhq.com", 429, 120, now + 130
        )
        self.assertAlmostEqual(
            self.store.host_cooldown_until("api.ashbyhq.com"), now + 3600, delta=1.0
        )

    # --- success resets the machine ---

    def test_success_resets_counters(self):
        self.server.responses[:] = [(429, {}, b""), (200, {}, ASHBY_OK)]
        row = self._add_board("acme")
        self._poll_n(row, 2)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["consecutive_429"], 0)
        self.assertEqual(row["paused"], 0)
        self.assertEqual(row["deferred_until"], 0)
        self.assertIsNotNone(row["last_success_at"])

    # --- poller honors deferred_until + records host-cooldown deferrals ---

    def test_poller_skips_deferred_board(self):
        self._add_board("acme")
        self._add_board("beta")
        self.store.defer_board("ashby:acme", time.time() + 10_000)
        spy = SpyFetcher()
        t = poller.PollerThread(self.store, health.Heartbeat(), fetcher=spy)
        t._tick()
        self.assertEqual(spy.polled, ["ashby:beta"])

    def test_poller_records_host_deferral_on_cooldown(self):
        self._add_board("acme")
        self._add_board("beta")
        until = time.time() + 300
        spy = SpyFetcher(
            {"ashby:beta": politeness.HostCoolingDown("api.ashbyhq.com", until)}
        )
        t = poller.PollerThread(self.store, health.Heartbeat(), fetcher=spy)
        t._tick()
        row = self.store.get_source("ashby:beta")
        self.assertGreaterEqual(row["deferred_until"], until)
        row = self.store.get_source("ashby:acme")
        self.assertEqual(row["deferred_until"], 0)  # untouched board unaffected


if __name__ == "__main__":
    unittest.main()
