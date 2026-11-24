"""Scheduled backup + retention logic (a12/a6 P9, stdlib-only).

`next_backup_at` / `backup_due` are pure time functions; `backup_database`
copies a WAL-active SQLite store with sqlite3.Connection.backup() (checkpoint-
safe — never shutil.copy); `prune_backups` enforces the 7-day retention by
mtime. The caller injects every clock value and owns the scheduler thread.
"""

import datetime
import glob
import os
import sqlite3
import threading
import time

_BACKUP_HOUR_UTC = 2  # a6: nightly 02:00 UTC


def next_backup_at(now_epoch_s):
    """Epoch seconds of the next 02:00 UTC strictly after now_epoch_s.

    Exactly 02:00:00.0 UTC maps to the following day (24 h later).
    """
    now = datetime.datetime.fromtimestamp(now_epoch_s, tz=datetime.timezone.utc)
    target = now.replace(hour=_BACKUP_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return int(target.timestamp())


def backup_due(now_epoch_s, last_backup_epoch_s):
    """True iff a 02:00 UTC boundary has passed since the last backup, i.e.
    next_backup_at(last_backup_epoch_s) <= now_epoch_s."""
    return next_backup_at(last_backup_epoch_s) <= now_epoch_s


def backup_database(source_path, dest_dir, now_epoch_s):
    """WAL-safe copy of source_path to dest_dir/app-<YYYYMMDDTHHMMSS>Z.sqlite.

    Uses sqlite3.Connection.backup() — checkpoint-safe under WAL, unlike a
    raw file copy. Creates dest_dir when missing; returns the absolute path.
    """
    if not os.path.isfile(source_path):
        # A typo'd source must never yield a silently empty backup (P9).
        raise FileNotFoundError(source_path)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(
        now_epoch_s, tz=datetime.timezone.utc
    ).strftime("%Y%m%dT%H%M%S")
    dest_path = os.path.join(dest_dir, "app-" + stamp + "Z.sqlite")
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return os.path.abspath(dest_path)


def prune_backups(backup_dir, now_epoch_s, retention_days=7):
    """Delete app-*.sqlite files whose mtime is strictly older than
    retention_days. Non-matching files are never touched; an absent directory
    is a no-op."""
    if not os.path.isdir(backup_dir):
        return
    cutoff = now_epoch_s - retention_days * 86400
    for path in glob.glob(os.path.join(backup_dir, "app-*.sqlite")):
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)


class BackupThread(threading.Thread):
    """Nightly 02:00 UTC backup + 7-day retention inside the one process.

    Boot captures the last-backup instant, so the first backup runs at the
    FIRST 02:00 UTC boundary strictly after boot — never at boot itself.
    Scheduling only: the injected runner does the actual backup (production
    runner = backup_database); a failed run must not kill the thread.
    """

    def __init__(self, db_path, backup_dir, clock=time.time, runner=None):
        super().__init__(name="backup", daemon=True)
        self.db_path = db_path
        self.backup_dir = backup_dir
        self._clock = clock
        self._runner = runner or (
            lambda db_path, backup_dir, now: backup_database(
                db_path, backup_dir, now)
        )
        self._last_backup_at = clock()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_once(self):
        """Back up iff a 02:00 UTC boundary passed since the last run."""
        now = self._clock()
        if not backup_due(now, self._last_backup_at):
            return False
        self._runner(self.db_path, self.backup_dir, now)
        self._last_backup_at = now
        prune_backups(self.backup_dir, now)
        return True

    def run(self):
        while not self._stop.is_set():
            next_at = next_backup_at(self._clock())
            while not self._stop.is_set():
                now = self._clock()
                if now >= next_at:
                    break
                self._stop.wait(min(3600.0, max(1.0, next_at - now)))
            if self._stop.is_set():
                break
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — a missed backup must not kill the thread
                pass
