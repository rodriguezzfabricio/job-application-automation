"""Scheduled-backup + retention tests (a12/a6 P9 contract, stdlib-only).

Contracts pinned here:
- next_backup_at: the NEXT 02:00 UTC strictly after now (02:00:00.0 exact → +24 h).
- backup_database: WAL-safe copy via sqlite3.Connection.backup(); file name
  app-<YYYYMMDDTHHMMSS>Z.sqlite; dest dir created when missing; absolute path.
- prune_backups: deletes app-*.sqlite strictly older than retention_days by
  MTIME (the one pinned rule — name-embedded age alone never deletes); never
  touches non-matching files; absent dir is a no-op.
- backup_due: True iff the next 02:00 UTC after last_backup_epoch_s is <= now.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from server import backup


def utc(y, mo, d, h, mi, s):
    """Known UTC instant as a tz-aware datetime (tests inject the clock)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def epoch_s(dt):
    return int(dt.timestamp())


class NextBackupAt(unittest.TestCase):
    def test_0159_returns_0200_same_day(self):
        now = epoch_s(utc(2026, 10, 20, 1, 59, 0))
        self.assertEqual(
            backup.next_backup_at(now),
            epoch_s(utc(2026, 10, 20, 2, 0, 0)),
        )

    def test_exact_0200_returns_24h_later(self):
        # now is exactly 02:00:00.0 UTC → the next one is the following day.
        now = epoch_s(utc(2026, 10, 20, 2, 0, 0))
        self.assertEqual(
            backup.next_backup_at(now),
            epoch_s(utc(2026, 10, 21, 2, 0, 0)),
        )

    def test_2300_returns_0200_next_day(self):
        now = epoch_s(utc(2026, 10, 20, 23, 0, 0))
        self.assertEqual(
            backup.next_backup_at(now),
            epoch_s(utc(2026, 10, 21, 2, 0, 0)),
        )

    def test_020001_returns_0200_next_day(self):
        # Strictly after now: one second past the boundary already misses it.
        now = epoch_s(utc(2026, 10, 20, 2, 0, 1))
        self.assertEqual(
            backup.next_backup_at(now),
            epoch_s(utc(2026, 10, 21, 2, 0, 0)),
        )

    def test_deterministic_and_strictly_future(self):
        instants = [
            utc(2026, 10, 20, 0, 0, 0),
            utc(2026, 10, 20, 1, 59, 59),
            utc(2026, 10, 20, 2, 0, 0),
            utc(2026, 10, 20, 2, 0, 1),
            utc(2026, 10, 20, 23, 59, 59),
        ]
        for dt in instants:
            now = epoch_s(dt)
            first = backup.next_backup_at(now)
            self.assertEqual(first, backup.next_backup_at(now))
            self.assertGreater(first, now)


class BackupDue(unittest.TestCase):
    def test_due_when_0200_boundary_passed_since_last_backup(self):
        last = epoch_s(utc(2026, 10, 20, 1, 59, 0))
        now = epoch_s(utc(2026, 10, 20, 2, 0, 0))
        self.assertTrue(backup.backup_due(now, last))

    def test_not_due_at_same_instant_as_last_0200_backup(self):
        # Last run was exactly at 02:00; the next boundary is 24 h later.
        t = epoch_s(utc(2026, 10, 20, 2, 0, 0))
        self.assertFalse(backup.backup_due(t, t))

    def test_due_exactly_24h_after_last_0200(self):
        last = epoch_s(utc(2026, 10, 20, 2, 0, 0))
        now = epoch_s(utc(2026, 10, 21, 2, 0, 0))
        self.assertTrue(backup.backup_due(now, last))

    def test_not_due_one_second_before_next_boundary(self):
        last = epoch_s(utc(2026, 10, 20, 2, 0, 0))
        now = epoch_s(utc(2026, 10, 21, 1, 59, 59))
        self.assertFalse(backup.backup_due(now, last))

    def test_due_when_never_backed_up(self):
        # last_backup=0 (epoch): the 1970-01-02 02:00 boundary long passed.
        self.assertTrue(backup.backup_due(epoch_s(utc(2026, 10, 20, 2, 0, 0)), 0))


class BackupDatabase(unittest.TestCase):
    def test_backup_creates_valid_sqlite_with_identical_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "data", "app.sqlite")
            os.makedirs(os.path.dirname(src))
            conn = sqlite3.connect(src)
            # WAL-active source; the connection stays open during the backup,
            # so a raw file copy would be inconsistent — backup() must not be.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO jobs (title) VALUES (?)", [("alpha",), ("beta",)]
            )
            conn.commit()

            dest_dir = os.path.join(tmp, "backups")  # does not exist yet
            dest = backup.backup_database(
                src, dest_dir, epoch_s(utc(2026, 10, 20, 2, 0, 0))
            )
            conn.close()

            self.assertTrue(os.path.isabs(dest))
            self.assertEqual(
                os.path.basename(dest), "app-20261020T020000Z.sqlite"
            )
            check = sqlite3.connect(dest)
            rows = check.execute("SELECT title FROM jobs ORDER BY id").fetchall()
            check.close()
            self.assertEqual([r[0] for r in rows], ["alpha", "beta"])

    def test_backup_database_missing_source_raises(self):
        # A typo'd source path must not silently produce an empty backup (P9).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backup.backup_database(
                    os.path.join(tmp, "nope.sqlite"),
                    os.path.join(tmp, "backups"),
                    epoch_s(utc(2026, 10, 20, 2, 0, 0)),
                )


class PruneBackups(unittest.TestCase):
    def setUp(self):
        self.now = epoch_s(utc(2026, 10, 20, 2, 0, 0))

    def _touch(self, path, mtime_s):
        with open(path, "w") as f:
            f.write("x")
        os.utime(path, (mtime_s, mtime_s))

    def test_removes_10_day_old_keeps_3_day_old_and_non_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "app-20261010T020000Z.sqlite")
            recent = os.path.join(tmp, "app-20261017T020000Z.sqlite")
            foreign = os.path.join(tmp, "tracking-notes.txt")
            self._touch(old, self.now - 10 * 86400)
            self._touch(recent, self.now - 3 * 86400)
            self._touch(foreign, self.now - 10 * 86400)  # old mtime, wrong name
            backup.prune_backups(tmp, self.now)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(recent))
            self.assertTrue(os.path.exists(foreign))

    def test_strictly_older_boundary_keeps_exactly_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = os.path.join(tmp, "app-20261013T020000Z.sqlite")
            self._touch(boundary, self.now - 7 * 86400)  # == cutoff → kept
            backup.prune_backups(tmp, self.now)
            self.assertTrue(os.path.exists(boundary))
            # One second past the cutoff → deleted.
            older = os.path.join(tmp, "app-20261012T235959Z.sqlite")
            self._touch(older, self.now - 7 * 86400 - 1)
            backup.prune_backups(tmp, self.now)
            self.assertFalse(os.path.exists(older))

    def test_name_embedded_age_alone_does_not_delete(self):
        # Pinned rule: retention is mtime-based; an old-looking name with a
        # fresh mtime survives.
        with tempfile.TemporaryDirectory() as tmp:
            named_old = os.path.join(tmp, "app-20261001T020000Z.sqlite")
            self._touch(named_old, self.now - 3 * 86400)
            backup.prune_backups(tmp, self.now)
            self.assertTrue(os.path.exists(named_old))

    def test_absent_dir_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "does-not-exist")
            backup.prune_backups(missing, self.now)  # must not raise


if __name__ == "__main__":
    unittest.main()
