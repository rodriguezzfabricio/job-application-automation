"""Budget RED tests — a8 budget/kill-switch pins (knowledge/a8.md, IRON_LAW §14.6).

Pins under test:
- ≤50 job alerts + 1 digest per DAY, enforced as ATOMIC reservations
  in-transport (scar S1 alert-fatigue prevention, P2 budget).
- reserve-before-spend for the DeepSeek spend ledger: reservation is atomic,
  balance can never go negative.
- new day = new day_key (caller keys by date string).

A GREEN on any of these before the module existed is itself a failure.
"""

import threading
import unittest

from server.budget import Budget, BudgetError, SpendLedger


class BudgetAlertCapTests(unittest.TestCase):
    """Alert + digest day-caps, atomic under threads."""

    def test_51st_alert_refused(self):
        budget = Budget()
        for i in range(50):
            res = budget.reserve_alert("2026-10-20", now=f"t{i}")
            self.assertEqual(res, {"kind": "alert", "day_key": "2026-10-20", "at": f"t{i}"})
        with self.assertRaises(BudgetError):
            budget.reserve_alert("2026-10-20", now="t50")

    def test_exactly_50_succeed_under_60_concurrent_threads(self):
        budget = Budget()
        outcomes = []
        outcomes_lock = threading.Lock()

        def attempt(i):
            try:
                budget.reserve_alert("2026-10-20", now=f"t{i}")
                outcome = "ok"
            except BudgetError:
                outcome = "refused"
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 50)
        self.assertEqual(outcomes.count("refused"), 10)

    def test_second_digest_same_day_refused(self):
        budget = Budget()
        res = budget.reserve_digest("2026-10-20", now="morning")
        self.assertEqual(res, {"kind": "digest", "day_key": "2026-10-20", "at": "morning"})
        with self.assertRaises(BudgetError):
            budget.reserve_digest("2026-10-20", now="evening")

    def test_next_day_key_allows_again(self):
        budget = Budget()
        for _ in range(50):
            budget.reserve_alert("2026-10-20", now="t")
        budget.reserve_digest("2026-10-20", now="t")
        # A new day key starts fresh for both caps.
        self.assertEqual(
            budget.reserve_alert("2026-10-21", now="t"),
            {"kind": "alert", "day_key": "2026-10-21", "at": "t"},
        )
        self.assertEqual(
            budget.reserve_digest("2026-10-21", now="t"),
            {"kind": "digest", "day_key": "2026-10-21", "at": "t"},
        )

    def test_remaining_alerts_reports_available(self):
        budget = Budget()
        self.assertEqual(budget.remaining_alerts("2026-10-20"), 50)
        for _ in range(3):
            budget.reserve_alert("2026-10-20", now="t")
        self.assertEqual(budget.remaining_alerts("2026-10-20"), 47)
        self.assertEqual(budget.remaining_alerts("2026-10-21"), 50)

    def test_reset_day_clears_counters(self):
        budget = Budget()
        for _ in range(50):
            budget.reserve_alert("2026-10-20", now="t")
        budget.reset_day("2026-10-20")
        self.assertEqual(budget.remaining_alerts("2026-10-20"), 50)
        budget.reserve_alert("2026-10-20", now="t")


class SpendLedgerTests(unittest.TestCase):
    """Reserve-before-spend ledger: atomic reserve/settle, balance ≥ 0."""

    def test_spend_reservation_refuses_over_balance(self):
        ledger = SpendLedger(100)
        ledger.reserve_spend(100)
        self.assertEqual(ledger.balance_usd_micros(), 0)
        with self.assertRaises(BudgetError):
            ledger.reserve_spend(1)

    def test_settle_releases_the_delta(self):
        ledger = SpendLedger(1000)
        rid = ledger.reserve_spend(100)
        self.assertEqual(ledger.balance_usd_micros(), 900)
        ledger.settle(rid, 40)
        # Unused portion (100 - 40) released back: 900 + 60.
        self.assertEqual(ledger.balance_usd_micros(), 960)
        # Settling more than was reserved is refused, never silently absorbed.
        with self.assertRaises(BudgetError):
            ledger.settle(rid, 200)

    def test_balance_never_negative_under_concurrent_reserve_settle(self):
        ledger = SpendLedger(10)
        errors = []

        def worker():
            for _ in range(100):
                while True:
                    try:
                        rid = ledger.reserve_spend(1)
                        break
                    except BudgetError:
                        continue
                ledger.settle(rid, 0)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(ledger.balance_usd_micros(), 10)

    def test_reservation_ids_unique(self):
        ledger = SpendLedger(1000000)
        ids = [ledger.reserve_spend(1) for _ in range(500)]
        self.assertEqual(len(set(ids)), 500)


if __name__ == "__main__":
    unittest.main()
