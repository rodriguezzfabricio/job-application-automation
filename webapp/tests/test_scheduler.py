"""Scheduler planning-logic tests (a1 Q39 contract, pure — no threads, no db).

Every function in server.scheduler maps to the Q39 workload contract; the
caller injects due/host data and owns threads, clock, and store writes.
"""

import unittest

from server import scheduler


def make_board(board_id, host, deferred_until=0.0):
    """Canonical board dict; 0.0 means due now."""
    return {"id": board_id, "host": host, "deferred_until": deferred_until}


class AdvisoryCapacity(unittest.TestCase):
    def test_capacity_formula_golden_24_true_25_false(self):
        # Pinned arithmetic: N×(30+2)+120 ≤ 900.
        # 24×32+120 = 768+120 = 888 ≤ 900 → True.
        # 25×32+120 = 800+120 = 920 > 900 → False.
        self.assertTrue(scheduler.advisory_capacity_ok(24))
        self.assertFalse(scheduler.advisory_capacity_ok(25))


class DueOrdering(unittest.TestCase):
    def test_due_boards_sorted_by_deferred_until_then_id(self):
        boards = [
            make_board("b", "h1", 0.0),
            make_board("a", "h1", 0.0),
            make_board("d", "h1", 100.0),  # not due at now=75
            make_board("c", "h1", 50.0),
            make_board("e", "h1", 75.0),  # boundary: == now is due
        ]
        due = scheduler.due_boards(boards, now=75.0)
        self.assertEqual([b["id"] for b in due], ["a", "b", "c", "e"])


class PlanCycle(unittest.TestCase):
    def test_plan_cycle_host_straggler_skip_recorded_not_dropped(self):
        boards = [
            make_board("s1", "stuck-host"),
            make_board("s2", "stuck-host", 999999.0),  # straggler precedence over deferral
            make_board("ok1", "fine-host"),
            make_board("wait", "fine-host", 100.0),
        ]
        plan = scheduler.plan_cycle(boards, now=10.0, straggler_hosts={"stuck-host"})
        self.assertEqual([b["id"] for b in plan["due"]], ["ok1"])
        self.assertEqual([b["id"] for b in plan["deferred_host"]], ["s1", "s2"])
        self.assertEqual([b["id"] for b in plan["deferred_until"]], ["wait"])
        # Never dropped silently: every input board lands in exactly one bucket.
        buckets = plan["due"] + plan["deferred_host"] + plan["deferred_until"]
        self.assertEqual(sorted(b["id"] for b in buckets), sorted(b["id"] for b in boards))


class FairOrder(unittest.TestCase):
    def test_fair_order_puts_last_cycle_skipped_boards_first(self):
        boards = [
            make_board("polled-b", "h1"),
            make_board("skipped-a", "h2"),
            make_board("polled-a", "h1"),
            make_board("skipped-b", "h2"),
        ]
        order = scheduler.fair_order(
            previous_cycle_polled=["polled-a", "polled-b"],
            boards=boards,
            now=0.0,
            straggler_hosts=set(),
        )
        self.assertEqual(
            [b["id"] for b in order],
            ["skipped-a", "skipped-b", "polled-a", "polled-b"],
        )

    def test_fair_order_deterministic_tie_break(self):
        # One group, identical deferred_until → id breaks the tie, always.
        boards = [
            make_board("z", "h1", 5.0),
            make_board("a", "h1", 5.0),
            make_board("m", "h1", 5.0),
        ]
        order = scheduler.fair_order([], boards, now=0.0, straggler_hosts=set())
        self.assertEqual([b["id"] for b in order], ["a", "m", "z"])


class WorkerWait(unittest.TestCase):
    def test_wait_for_workers_split_done_vs_stragglers(self):
        workers = [
            {"name": "ashby-board", "host": "api.ashbyhq.com", "deadline_epoch_s": 100.0},
            {"name": "gh-board", "host": "boards-api.greenhouse.io", "deadline_epoch_s": 300.0},
            {"name": "lever-board", "host": "api.lever.co", "deadline_epoch_s": 100.0},  # == now → done
        ]
        done, stragglers = scheduler.wait_for_workers(workers, now=100.0)
        self.assertEqual([w["name"] for w in done], ["ashby-board", "lever-board"])
        self.assertEqual([w["name"] for w in stragglers], ["gh-board"])


class Watchdog(unittest.TestCase):
    def test_watchdog_force_exit_boundary_exactly_3x_interval(self):
        # Pinned: force-exit only when surviving > 3×interval (3×900 = 2700 s).
        straggler = {"name": "stuck", "host": "h1", "deadline_epoch_s": 0.0}
        self.assertFalse(
            scheduler.watchdog_force_exit(
                straggler, started_at_epoch_s=1000.0, now=1000.0 + 3 * 900
            )
        )
        self.assertTrue(
            scheduler.watchdog_force_exit(
                straggler, started_at_epoch_s=1000.0, now=1000.0 + 3 * 900 + 1
            )
        )


if __name__ == "__main__":
    unittest.main()
