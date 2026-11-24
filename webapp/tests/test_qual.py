"""Qualification fairness RED tests — a2.md Q41 pins.

Pins under test:
- candidates processed in per-job next_due order (None first, ties by id).
- 2-strike deferral: failures==2 -> 2x backoff, ==3 -> 4x (capped 24 h),
  ==4 -> 7-day hold; deferred jobs consume no shortlist slots.
- success resets; operator retry-now clears next_due but keeps strikes and
  never lifts an active hold.
- acceptance: 3 failing + 1 eligible job reach qualification within
  2*ceil(failing/S)+1 cycles (S=3 -> cycle 3, S=1 -> cycle 7), derived,
  not hardcoded.
"""

import math
import unittest

from server.qual import QualQueue


def _fresh():
    return {"failures": 0, "qual_next_due": None, "on_hold_until": None}


class QualQueueTest(unittest.TestCase):

    def test_pick_orders_by_next_due(self):
        q = QualQueue(shortlist_limit=10)
        now = 1000.0
        states = {
            "past2": dict(_fresh(), qual_next_due=800.0),
            "none1": _fresh(),
            "past1": dict(_fresh(), qual_next_due=500.0),
            "none2": _fresh(),
            "past3": dict(_fresh(), qual_next_due=999.0),
        }
        picked = q.pick(["past3", "none1", "past1", "past2", "none2"], states, now)
        self.assertEqual(picked, ["none1", "none2", "past1", "past2", "past3"])

    def test_deferred_jobs_consume_no_slots(self):
        q = QualQueue(shortlist_limit=2)
        now = 1000.0
        states = {
            "a": {"failures": 2, "qual_next_due": now + 9999.0, "on_hold_until": None},
            "b": _fresh(),
            "c": _fresh(),
        }
        picked = q.pick(["a", "b", "c"], states, now)
        self.assertEqual(picked, ["b", "c"])

    def test_two_strike_deferral_sets_double_backoff(self):
        q = QualQueue(shortlist_limit=3)
        now = 1000.0
        state = _fresh()
        first = q.record_failure("j", state, now)
        self.assertEqual(first["failures"], 1)
        self.assertIsNone(first["qual_next_due"])  # first strike never defers
        second = q.record_failure("j", first, now)
        self.assertEqual(second["failures"], 2)
        self.assertEqual(second["qual_next_due"], now + 2 * q.backoff_base_s)
        self.assertEqual(state, _fresh())  # pure: input untouched

    def test_third_failure_quadruple_backoff_capped(self):
        q = QualQueue(shortlist_limit=3)
        now = 1000.0
        state = {"failures": 2, "qual_next_due": now + 1800.0, "on_hold_until": None}
        third = q.record_failure("j", state, now)
        self.assertEqual(third["qual_next_due"], now + 4 * 900.0)
        big = QualQueue(shortlist_limit=3, backoff_base_s=100000.0)
        capped = big.record_failure("j", state, now)
        self.assertEqual(big.backoff_cap_s, 86400.0)
        self.assertEqual(capped["qual_next_due"], now + big.backoff_cap_s)

    def test_fourth_failure_seven_day_hold(self):
        q = QualQueue(shortlist_limit=3)
        now = 1000.0
        state = {"failures": 3, "qual_next_due": now + 3600.0, "on_hold_until": None}
        fourth = q.record_failure("j", state, now)
        self.assertEqual(fourth["failures"], 4)
        self.assertEqual(fourth["on_hold_until"], now + 7 * 86400)
        self.assertEqual(fourth["qual_next_due"], now + 7 * 86400)

    def test_success_resets_state(self):
        q = QualQueue(shortlist_limit=3)
        state = {"failures": 4, "qual_next_due": 9999.0, "on_hold_until": 9999.0}
        self.assertEqual(q.record_success("j", state, 5000.0), _fresh())

    def test_retry_now_clears_due_keeps_failures(self):
        q = QualQueue(shortlist_limit=3)
        now = 1000.0
        deferred = {"failures": 2, "qual_next_due": now + 1800.0, "on_hold_until": None}
        retried = q.retry_now("j", deferred, now)
        self.assertEqual(retried["failures"], 2)  # strikes kept: the fairness mechanism
        self.assertIsNone(retried["qual_next_due"])
        held = {"failures": 4, "qual_next_due": 700000.0, "on_hold_until": 700000.0}
        still_held = q.retry_now("j", held, now)
        self.assertEqual(still_held, held)  # active hold is not lifted
        self.assertIsNot(still_held, held)  # pure: no in-place mutation

    def _run_acceptance(self, failing, eligible, limit):
        jobs = failing + [eligible]
        q = QualQueue(shortlist_limit=limit)
        states = {job: _fresh() for job in jobs}
        bound = 2 * math.ceil(len(failing) / limit) + 1
        now = 0.0
        for cycle in range(1, bound + 1):
            picked, states, now = q.step_cycle(jobs, states, failing, now, 900.0)
            if eligible in picked:
                return cycle, bound
        return None, bound

    def test_acceptance_limit_three(self):
        reached, bound = self._run_acceptance(["j1", "j2", "j3"], "j9", 3)
        self.assertIsNotNone(reached, "eligible job never reached qualification")
        self.assertLessEqual(reached, bound)

    def test_acceptance_limit_one(self):
        reached, bound = self._run_acceptance(["j1", "j2", "j3"], "j9", 1)
        self.assertIsNotNone(reached, "eligible job never reached qualification")
        self.assertLessEqual(reached, bound)

    def test_deterministic_tie_break(self):
        q = QualQueue(shortlist_limit=1)
        now = 1000.0
        states = {"b": _fresh(), "a": _fresh()}
        self.assertEqual(q.pick(["b", "a"], states, now), ["a"])
        self.assertEqual(q.pick(["b", "a"], states, now), ["a"])
        equal_dues = {
            "y": dict(_fresh(), qual_next_due=500.0),
            "x": dict(_fresh(), qual_next_due=500.0),
        }
        self.assertEqual(q.pick(["y", "x"], equal_dues, now), ["x"])
