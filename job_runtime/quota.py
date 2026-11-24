"""Durable quota reservations shared by the scheduled job worker and model replays.

Codex reports account-wide weekly percentage points, currently at integer precision.
A reservation is charged at least its full size even on an unchanged, missing or failed
measurement. Visible increases during an open batch may include unrelated activity or
overlapping batches; conservative attribution can overcharge. One worker reservation
covers its nested work. Admission and checkpoints cannot bound a call already in flight.
"""
import datetime as dt
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .pace import ET, SKIP_WEEK_PCT, WINDOW_WEEK_MIN
from .usage import account_fingerprint, read_live_meters

COMBINED_CAP = 25
BENCHMARK_CAP = 2
APPLICATION_BUDGET = 2
APPLICATION_STARTUP = 1
PRIVATE_ROOT = str(Path(__file__).resolve().parents[1] / 'stopgap-private')


class QuotaUnavailable(RuntimeError):
    """No verified allowance is available for additional model work."""


class Quota:
    def __init__(self, root=None, meter=None, clock=None):
        self.path = Path(root or os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or PRIVATE_ROOT) / 'codex-runtime' / 'quota.sqlite'
        self.meter = meter or read_live_meters
        self.clock = clock or time.time

    @contextmanager
    def _transaction(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            con = sqlite3.connect(self.path, timeout=20)
            con.row_factory = sqlite3.Row
            try:
                con.execute('BEGIN IMMEDIATE')
                con.execute('''CREATE TABLE IF NOT EXISTS meter (
                    id INTEGER PRIMARY KEY CHECK (id = 1), account TEXT NOT NULL,
                    reset_at INTEGER NOT NULL, used REAL NOT NULL, sampled_at REAL NOT NULL)''')
                con.execute('''CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, consumer TEXT NOT NULL, owner TEXT, reset_at INTEGER NOT NULL,
                    budget INTEGER NOT NULL, startup INTEGER NOT NULL, baseline REAL NOT NULL,
                    status TEXT NOT NULL, charged INTEGER NOT NULL, observed REAL)''')
                con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS quota_owner
                    ON reservations(reset_at, consumer, owner) WHERE owner IS NOT NULL''')
                con.execute('CREATE TABLE IF NOT EXISTS meter_day (reset_at INTEGER, day TEXT, used REAL, '
                            'sampled_at REAL, PRIMARY KEY (reset_at, day))')
                if not con.execute("SELECT 1 FROM sqlite_master WHERE name='account_meter_day'").fetchone():
                    con.execute('CREATE TABLE account_meter_day (account TEXT NOT NULL, reset_at INTEGER NOT NULL, '
                                'day TEXT NOT NULL, used REAL NOT NULL, sampled_at REAL NOT NULL, PRIMARY KEY(account,reset_at,day))')
                    # Only carry a legacy baseline forward if it belongs to the current binding.
                    bound = con.execute('SELECT * FROM meter WHERE id=1').fetchone()
                    if bound:
                        transferred = 0
                        if con.execute("SELECT 1 FROM sqlite_master WHERE name='account_transfers'").fetchone():
                            transferred = con.execute('SELECT COALESCE(MAX(sampled_at),0) FROM account_transfers WHERE reset_at=?',
                                                      (bound['reset_at'],)).fetchone()[0]
                        con.execute('INSERT INTO account_meter_day SELECT ?,reset_at,day,used,sampled_at FROM meter_day '
                                    'WHERE reset_at=? AND sampled_at>=?', (bound['account'],bound['reset_at'],transferred))
                        self._day_sample(con, bound['account'], bound['reset_at'], bound['used'], bound['sampled_at'])
                con.execute('CREATE TABLE IF NOT EXISTS reservation_times (id TEXT PRIMARY KEY, opened_at REAL NOT NULL)')
                try:
                    yield con
                except QuotaUnavailable:
                    # A sample that denied admission may still reveal already-spent points.
                    con.commit()
                    raise
                con.commit()
            finally:
                con.close()
        except (OSError, sqlite3.Error) as exc:
            raise QuotaUnavailable('quota ledger unavailable') from exc

    @staticmethod
    def _day_sample(con, account, reset_at, used, sampled_at):
        day = dt.datetime.fromtimestamp(sampled_at, ET).date().isoformat()
        con.execute('INSERT OR IGNORE INTO account_meter_day VALUES (?,?,?,?,?)', (account,reset_at,day,used,sampled_at))

    def _sample(self, con, reading=None):
        raw = self.meter() if reading is None else reading
        now = self.clock()
        try:
            week = raw['week']
            account = raw['account']
            sampled_at, used = float(raw['sampled_at']), float(week['used'])
            reset_at = int(week['resets_at'])
            if not isinstance(account, str) or not account or int(week['window_min']) != WINDOW_WEEK_MIN:
                raise ValueError
            if not now - 30 <= sampled_at <= now + 2 or not 0 <= used <= 100:
                raise ValueError
            if not now < reset_at <= now + WINDOW_WEEK_MIN * 60 + 120:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise QuotaUnavailable('verified current weekly Codex meter unavailable') from exc

        prev = con.execute('SELECT * FROM meter WHERE id=1').fetchone()
        if prev:
            if account != prev['account']:
                raise QuotaUnavailable('Codex account changed')
            if sampled_at < prev['sampled_at']:
                raise QuotaUnavailable('weekly Codex meter is stale')
            if now < prev['reset_at'] and abs(reset_at - prev['reset_at']) <= 1:
                reset_at = prev['reset_at']  # Server rounding of the same advertised reset second.
            if reset_at != prev['reset_at']:
                if now < prev['reset_at'] or reset_at <= prev['reset_at']:
                    raise QuotaUnavailable('weekly Codex reset changed before the verified window ended')
            elif used < prev['used']:
                raise QuotaUnavailable('weekly Codex meter decreased within its reset')
        con.execute('INSERT OR REPLACE INTO meter VALUES (1,?,?,?,?)', (account, reset_at, used, sampled_at))
        self._day_sample(con,account,reset_at,used,sampled_at)
        return reset_at, used

    def rebind(self, expected_old, expected_new):
        """Operator-authorized transfer in one reset window. Keep every charged reservation."""
        with self._transaction() as con:
            reading = self.meter()
            previous = con.execute('SELECT * FROM meter WHERE id=1').fetchone()
            if (not previous or previous['account'] != expected_old or not reading.get('account_id_verified')
                    or reading.get('account') != expected_new or not expected_new or expected_new == expected_old
                    or reading['week']['resets_at'] != previous['reset_at']
                    or con.execute("SELECT 1 FROM reservations WHERE status IN ('active','suspended')").fetchone()):
                raise QuotaUnavailable('account transfer requires the confirmed identities, same reset, and no open holds')
            # Validate the new sample before changing the binding. A rejected sample must roll back.
            con.execute('SAVEPOINT transfer_binding')
            try:
                con.execute('DELETE FROM meter WHERE id=1')
                reset, used = self._sample(con, reading)
                con.execute('CREATE TABLE IF NOT EXISTS account_transfers '
                            '(sampled_at REAL PRIMARY KEY, old_account TEXT, new_account TEXT, reset_at INTEGER)')
                con.execute('INSERT INTO account_transfers VALUES (?,?,?,?)',
                            (reading['sampled_at'], expected_old, expected_new, reset))
            except Exception:
                con.execute('ROLLBACK TO transfer_binding')
                raise
            finally:
                con.execute('RELEASE transfer_binding')
            return {'already_committed': self._totals(con, reset, used)[0], 'cap': COMBINED_CAP}

    def pace_reading(self, reading):
        """Attach the first verified observation of this ET day."""
        with self._transaction() as con:
            reset_at, _ = self._sample(con, reading)
            day = dt.datetime.fromtimestamp(reading['sampled_at'], ET).date().isoformat()
            row = con.execute('SELECT * FROM account_meter_day WHERE account=? AND reset_at=? AND day=?',
                              (reading['account'],reset_at,day)).fetchone()
            first_at = dt.datetime.fromtimestamp(row['sampled_at'], dt.timezone.utc).isoformat()
            return {**reading, 'day_start': {'used': row['used'], 'at': first_at}}

    @staticmethod
    def _observed(row, used):
        return max(0.0, used - row['baseline'])

    def _totals(self, con, reset_at, used):
        combined = benchmark = 0
        for row in con.execute('SELECT * FROM reservations WHERE reset_at=?', (reset_at,)):
            hold = row['charged']
            if row['status'] in ('active', 'suspended'):
                hold = max(hold, math.ceil(self._observed(row, used)) + row['startup'])
            combined += hold
            if row['consumer'] == 'benchmark':
                benchmark += hold
        return combined, benchmark

    def begin(self, consumer, budget=1, *, owner=None, startup=0, reading=None, reserve=True):
        """Atomically reserve points for a batch. An owner key makes worker gate retries idempotent."""
        if consumer not in ('application', 'benchmark'):
            raise ValueError('unknown quota consumer')
        if type(budget) is not int or type(startup) is not int or not 0 <= startup < budget:
            raise ValueError('quota budget must specify whole points and leave room after startup')
        with self._transaction() as con:
            reset_at, used = self._sample(con, reading)
            if owner is not None:
                row = con.execute('SELECT * FROM reservations WHERE reset_at=? AND consumer=? AND owner=?',
                                  (reset_at, consumer, owner)).fetchone()
                if row is not None:
                    self._check(con, row, reset_at, used)
                    return row['id'] if reserve else None
            combined, benchmark = self._totals(con, reset_at, used)
            if combined + budget > COMBINED_CAP:
                raise QuotaUnavailable('combined 25-point weekly quota is committed')
            if consumer == 'benchmark' and benchmark + budget > BENCHMARK_CAP:
                raise QuotaUnavailable('benchmark 2-point weekly quota is committed')
            if used + budget > SKIP_WEEK_PCT:
                raise QuotaUnavailable('weekly Codex meter lacks headroom')
            if not reserve:
                return None
            rid = uuid.uuid4().hex
            con.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (rid, consumer, owner, reset_at, budget, startup, used, 'active', budget, None))
            con.execute('INSERT INTO reservation_times VALUES(?,?)', (rid,self.clock()))
            return rid

    @staticmethod
    def _row(con, rid):
        row = con.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise QuotaUnavailable('unknown quota reservation')
        return row

    def _check(self, con, row, reset_at, used, status='active'):
        if row['status'] != status or row['reset_at'] != reset_at:
            raise QuotaUnavailable('quota reservation is unavailable in the verified weekly window')
        observed = self._observed(row, used)
        remaining = row['budget'] - row['startup'] - observed
        combined, benchmark = self._totals(con, reset_at, used)
        if (remaining <= 0 or used >= SKIP_WEEK_PCT or combined > COMBINED_CAP
                or (row['consumer'] == 'benchmark' and benchmark > BENCHMARK_CAP)):
            raise QuotaUnavailable('quota reservation allowance is used')
        return {'remaining': remaining, 'observed_delta': observed, 'reset_at': reset_at}

    def check_ready(self,rid,*,reading=None):
        """Continue an already funded application within remaining shared weekly headroom."""
        from .pace import decide
        reading=self.meter() if reading is None else reading
        with self._transaction() as con:
            reset,used=self._sample(con,reading)
            row=self._row(con,rid)
            if row['consumer']!='application' or row['status']!='active' or row['reset_at']!=reset:
                raise QuotaUnavailable('active application batch in this weekly window required')
            pace=decide(reading,dt.datetime.fromtimestamp(self.clock(),ET),ready=1)
            if pace['mode']=='skip':
                raise QuotaUnavailable(pace['why'])
            observed=self._observed(row,used)
            remaining=row['budget']-row['startup']-observed
            combined,_=self._totals(con,reset,used)
            if combined>COMBINED_CAP or (remaining<=0 and combined>=COMBINED_CAP):
                raise QuotaUnavailable('combined 25-point weekly quota is committed')
            return {'remaining':max(0,remaining),'weekly_uncommitted':COMBINED_CAP-combined,
                    'observed_delta':observed,'reset_at':reset,'ready_only':remaining<=0 or pace['mode']=='lean'}

    def check(self, rid, *, reading=None, account_id=None, authorize_next=True):
        """Recheck allowance, or validate a completed receipt against the active account."""
        with self._transaction() as con:
            row = self._row(con, rid)
            if reading is None:
                reading = self.meter()
            reset_at, used = self._sample(con, reading)
            if account_id and reading.get('account_id_verified') and account_fingerprint(account_id) != reading['account']:
                raise QuotaUnavailable('model call used a different Codex account')
            if authorize_next:
                return self._check(con, row, reset_at, used)
            if row['status'] != 'active' or row['reset_at'] != reset_at:
                raise QuotaUnavailable('quota reservation is unavailable in the verified weekly window')
            observed = self._observed(row, used)
            return {'remaining': max(0, row['budget'] - row['startup'] - observed),
                    'observed_delta': observed, 'reset_at': reset_at}

    def suspend(self, rid):
        """Pause a batch without releasing its hold."""
        with self._transaction() as con:
            row = self._row(con, rid)
            if row['status'] not in ('active', 'suspended'):
                raise QuotaUnavailable('quota reservation is already settled')
            con.execute("UPDATE reservations SET status='suspended' WHERE id=?", (rid,))

    def resume(self, rid):
        """Validate a paused reservation before reactivating its existing hold."""
        with self._transaction() as con:
            row = self._row(con, rid)
            reset_at, used = self._sample(con)
            result = self._check(con, row, reset_at, used, status='suspended')
            con.execute("UPDATE reservations SET status='active' WHERE id=?", (rid,))
            return result

    def finish(self, rid):
        """Settle an attempted batch, including failed or interrupted model calls."""
        with self._transaction() as con:
            row = self._row(con, rid)
            status, charged, observed = row['status'], row['charged'], row['observed']
            if status in ('active', 'suspended'):
                status = 'uncertain'
                prev = con.execute('SELECT reset_at, used FROM meter WHERE id=1').fetchone()
                if prev and prev['reset_at'] == row['reset_at']:
                    charged = max(charged, math.ceil(self._observed(row, prev['used'])) + row['startup'])
                try:
                    reset_at, used = self._sample(con)
                    if row['reset_at'] == reset_at:
                        observed = self._observed(row, used)
                        charged = max(charged, math.ceil(observed) + row['startup'])
                        status = 'settled'
                except QuotaUnavailable:
                    pass  # the reservation remains charged through the verified reset
                con.execute('UPDATE reservations SET status=?,charged=?,observed=? WHERE id=?',
                            (status, charged, observed, rid))
            return {'status': status, 'charged': charged, 'observed_delta': observed, 'reset_at': row['reset_at']}
