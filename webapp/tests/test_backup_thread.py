"""RED tests — nightly backup thread wiring (a6: 02:00 UTC, 7-day retention).

The thread owns scheduling only: it decides WHEN a backup is due and calls an
injected runner; backup_database/prune_backups are already covered by
test_backup.py. A fake clock and a spy runner keep these tests deterministic.

Semantics pin: boot captures the last-backup instant, so the first backup runs
at the FIRST 02:00 UTC boundary strictly after boot — never at boot itself.
"""

import unittest

from server import backup


class SpyRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, db_path, backup_dir, now):
        self.calls.append((db_path, backup_dir, now))


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


# 2023-11-15T01:59:00Z = 1700013540; the next 02:00 UTC = 1700013600.
BEFORE_0200 = 1_700_013_540.0
AT_0200 = 1_700_013_600.0


class BackupThreadTests(unittest.TestCase):
    def test_first_backup_runs_at_first_0200_after_boot(self):
        clock = FakeClock(BEFORE_0200)
        runner = SpyRunner()
        thread = backup.BackupThread("/data/app.sqlite", "/data/backups",
                                     clock=clock, runner=runner)
        # Booted before 02:00: not due yet — no boot-time backup.
        self.assertFalse(thread.run_once())
        self.assertEqual(runner.calls, [])
        clock.now = AT_0200  # the first boundary after boot
        self.assertTrue(thread.run_once())
        self.assertEqual(
            runner.calls, [("/data/app.sqlite", "/data/backups", AT_0200)])
        # A second call in the same window (before the next 02:00) is not due.
        self.assertFalse(thread.run_once())
        self.assertEqual(len(runner.calls), 1)

    def test_not_due_before_any_boundary(self):
        clock = FakeClock(BEFORE_0200)
        runner = SpyRunner()
        thread = backup.BackupThread("/d/app.sqlite", "/d/backups",
                                     clock=clock, runner=runner)
        self.assertFalse(thread.run_once())
        self.assertEqual(runner.calls, [])

    def test_due_again_after_next_0200(self):
        clock = FakeClock(BEFORE_0200)
        runner = SpyRunner()
        thread = backup.BackupThread("/d/app.sqlite", "/d/backups",
                                     clock=clock, runner=runner)
        clock.now = AT_0200
        self.assertTrue(thread.run_once())
        clock.now = AT_0200 + 86400  # next day's 02:00:00 exactly
        self.assertTrue(thread.run_once())
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
