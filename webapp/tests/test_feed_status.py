"""Feed-health state-machine RED tests (a1 "Feed-death classification", a9 F3 chips).

Pins the contract, one behavior per test:
- schema-fingerprint drift is a warning only: chip stays healthy, no operator
  message, never dead
- 2 consecutive invalid envelopes -> chip "failed"
- 3x404/410 -> chip "failed", message "failed — suspected posting-API outage",
  dead stays False
- 3x empty -> warning flag (counter) only: chip healthy, never dead
- `dead` is reachable ONLY through operator_dead; operator_revive is the only
  un-dead; failures/empties/invalids/throttles never set it
- success resets the invalid/404/empty counters
- 5 consecutive throttled events pause the board (config.PAUSE_AFTER_429)
- stale boundary: strictly older than board_stale_after_s is stale, exactly at
  the boundary is not
- never-polled renders healthy until the first failure attempt (L-0015)
"""

import unittest

from server import config, feed_status

NOW = 1_000_000_000.0


def _feed(state, kind, **extra):
    event = {"kind": kind}
    event.update(extra)
    return feed_status.classify_feed_event(state, event, NOW)


class FeedStatusStateMachine(unittest.TestCase):
    def test_new_state_defaults_all_fields(self):
        self.assertEqual(
            feed_status.new_state(),
            {
                "schema_fingerprint": None,
                "consecutive_invalid": 0,
                "consecutive_http_404": 0,
                "consecutive_empty": 0,
                "dead": False,
                "paused": False,
                "consecutive_429": 0,
                "last_status": None,
                "last_success_at": None,
                "last_failure_at": None,
            },
        )

    def test_fingerprint_drift_is_warning_not_failure(self):
        s = _feed(feed_status.new_state(), "success")
        s = _feed(s, "fingerprint_drift", schema_fingerprint="fp2")
        self.assertEqual(s["schema_fingerprint"], "fp2")
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "healthy")
        self.assertIsNone(feed_status.feed_failure_message(s))
        self.assertFalse(s["dead"])

    def test_two_invalid_envelopes_failed(self):
        s = _feed(feed_status.new_state(), "success")
        s = _feed(s, "invalid_envelope")
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "healthy")
        s = _feed(s, "invalid_envelope")
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "failed")
        self.assertFalse(s["dead"])

    def test_three_404_failed_suspected_outage_never_dead(self):
        s = _feed(feed_status.new_state(), "success")
        for _ in range(2):
            s = _feed(s, "http_404_410", status=404)
        self.assertIsNone(feed_status.feed_failure_message(s))
        s = _feed(s, "http_404_410", status=404)
        self.assertEqual(s["consecutive_http_404"], 3)
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "failed")
        self.assertEqual(
            feed_status.feed_failure_message(s),
            "failed — suspected posting-API outage",
        )
        self.assertFalse(s["dead"])

    def test_three_empty_warning_only_never_dead(self):
        s = _feed(feed_status.new_state(), "success")
        for _ in range(3):
            s = _feed(s, "empty")
        self.assertEqual(s["consecutive_empty"], 3)
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "healthy")
        self.assertIsNone(feed_status.feed_failure_message(s))
        self.assertFalse(s["dead"])

    def test_dead_requires_operator_event(self):
        s = feed_status.new_state()
        for kind in ("http_404_410", "empty", "invalid_envelope", "failure", "throttled"):
            for _ in range(5):
                s = _feed(s, kind, status=404)
        self.assertFalse(s["dead"])
        s = _feed(s, "operator_dead")
        self.assertTrue(s["dead"])
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "dead")

    def test_operator_revive_undeads(self):
        s = _feed(feed_status.new_state(), "operator_dead")
        s = _feed(s, "operator_revive")
        self.assertFalse(s["dead"])

    def test_success_resets_counters(self):
        s = feed_status.new_state()
        for _ in range(3):
            s = _feed(s, "invalid_envelope")
        for _ in range(4):
            s = _feed(s, "http_404_410")
        for _ in range(3):
            s = _feed(s, "empty")
        s = _feed(s, "success", status=200)
        self.assertEqual(s["consecutive_invalid"], 0)
        self.assertEqual(s["consecutive_http_404"], 0)
        self.assertEqual(s["consecutive_empty"], 0)
        self.assertEqual(s["last_success_at"], NOW)
        self.assertEqual(s["last_status"], 200)

    def test_pause_after_five_throttled(self):
        s = _feed(feed_status.new_state(), "success")
        for _ in range(config.PAUSE_AFTER_429 - 1):
            s = _feed(s, "throttled", status=429)
        self.assertFalse(s["paused"])
        s = _feed(s, "throttled", status=429)
        self.assertEqual(s["consecutive_429"], config.PAUSE_AFTER_429)
        self.assertTrue(s["paused"])
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "paused")

    def test_stale_boundary_exact(self):
        s = _feed(feed_status.new_state(), "success")
        self.assertEqual(
            feed_status.status_chip(s, NOW + 1800, 900, 1800), "healthy"
        )
        self.assertEqual(
            feed_status.status_chip(s, NOW + 1800 + 0.001, 900, 1800), "stale"
        )

    def test_never_polled_healthy_until_first_failure(self):
        s = feed_status.new_state()
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "healthy")
        s = _feed(s, "failure", status=500)
        self.assertEqual(feed_status.status_chip(s, NOW, 900, 1800), "failed")

    def test_classify_returns_copy_not_mutation(self):
        s = feed_status.new_state()
        out = _feed(s, "success")
        self.assertIsNone(s["last_success_at"])
        self.assertIsNot(out, s)
