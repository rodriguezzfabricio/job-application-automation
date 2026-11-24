"""Corrected delivery engine — implements Q038/Q141/Q155/Q162 and E9-S-001..009.

One engine.sqlite holds committed decision/packet/outbox/reservation state. Rows
are immutable (no UPDATE on decisions/packets, no REPLACE, no DELETE). Admission is
a single atomic transaction: a failed decision insert can never leave qualified
outbox state. The original 600-second qualification deadline is never extended and
never rolls over through caps or retries. Caps use aware-UTC day/month buckets
reserved at claim and re-checked at handoff. Known-retryable outcomes (401/429/
not_attempted) may retry a fresh full guard under the SAME original deadline, at
most 4 attempts; REJECTED is terminal; timeout/reset/malformed/crash outcomes are
DELIVERY_UNKNOWN and terminal for auto-retry. A committed revocation blocks both
admission and handoff. The default sink is a local filesystem capture; any real
transport is injected and remains operator-gated (sending OFF).

This module never decides eligibility itself: the caller injects `eligibility`
(source-bound selection over approved facts) and `compose` (deterministic
PDF/MIME generation), so untrusted job data cannot author its own packet.
Profile/ref bytes are never stored here — only their digests; the caller injects
fresh loaders at every guard so late revocation or file change is observed.
"""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

READY = 'READY'
ATTEMPTING = 'ATTEMPTING'
CAPTURED = 'CAPTURED'
API_ACCEPTED = 'API_ACCEPTED'
RETRY_WAIT = 'RETRY_WAIT'
REJECTED = 'REJECTED'
UNKNOWN = 'UNKNOWN'
HOLD = 'HOLD'
STATES = (READY, ATTEMPTING, CAPTURED, API_ACCEPTED, RETRY_WAIT, REJECTED, UNKNOWN, HOLD)
TERMINAL_FOR_AUTORETRY = (CAPTURED, API_ACCEPTED, REJECTED, UNKNOWN, HOLD)

SHA = re.compile(r'[0-9a-f]{64}')


class Hold(Exception):
    """Typed review/hold reason; admission and handoff surface it as a status."""


class EngineFault(Exception):
    """Corruption or invariant violation; never silently converted to success."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


# Volatile per-fetch metadata excluded from source content identity: observation
# timestamps legitimately change across snapshots without changing job content.
VOLATILE_SOURCE_KEYS = ('source_observed_at',)


def content_canonical(raw):
    """Canonical bytes of source CONTENT (volatile observation metadata removed)."""
    return canonical({k: v for k, v in raw.items() if k not in VOLATILE_SOURCE_KEYS})


def utc_stamp(now):
    """Return an aware UTC datetime for int epoch, ISO string or datetime input."""
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    if isinstance(now, (int, float)):
        if not isinstance(now, int) or not 0 <= now <= 4_102_444_800:
            raise ValueError('epoch seconds integer required')
        return datetime.fromtimestamp(now, tz=timezone.utc)
    if isinstance(now, str):
        parsed = datetime.fromisoformat(now.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ValueError('unsupported clock value')


class DeliveryEngine:
    """Corrected single-file delivery core. See module docstring for contracts."""

    def __init__(self, root, *, version, recipient, daily_cap=3, monthly_cap=60,
                 max_attempts=4, expiry_seconds=600, queue_ttl_hours=24,
                 retry_window_days=7, payload_cap=3 * 1024 * 1024):
        if not isinstance(version, str) or not version or len(version) > 64:
            raise ValueError('policy version required')
        if not isinstance(recipient, str) or not recipient or len(recipient) > 320:
            raise ValueError('pinned recipient required')
        for name, value in (('daily_cap', daily_cap), ('monthly_cap', monthly_cap),
                            ('max_attempts', max_attempts), ('expiry_seconds', expiry_seconds),
                            ('queue_ttl_hours', queue_ttl_hours), ('retry_window_days', retry_window_days),
                            ('payload_cap', payload_cap)):
            if not isinstance(value, int) or value < 1:
                raise ValueError('invalid ' + name)
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for sub in ('objects', 'captures'):
            path = self.root / sub
            path.mkdir(mode=0o700, exist_ok=True)
        self.version = version
        self.recipient = recipient
        self.daily_cap = daily_cap
        self.monthly_cap = monthly_cap
        self.max_attempts = max_attempts
        self.expiry_seconds = expiry_seconds
        self.queue_ttl = timedelta(hours=queue_ttl_hours)
        self.retry_window = timedelta(days=retry_window_days)
        self.payload_cap = payload_cap
        self.db = self.root / 'engine.sqlite'
        with self.worker_lock(), self.conn() as c:
            self._schema(c)
            # Crash recovery: an ATTEMPTING claim whose owner died is UNKNOWN and
            # terminal for auto-retry. Runs under the same exclusive lock on open.
            c.execute("UPDATE outbox SET state=? WHERE state=?", (UNKNOWN, ATTEMPTING))

    # --- database ---------------------------------------------------------
    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA synchronous=FULL')
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def worker_lock(self):
        fd = os.open(self.root / 'worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _schema(self, c):
        c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(
                job TEXT PRIMARY KEY,
                raw TEXT NOT NULL,
                source_sha TEXT NOT NULL,
                observed INTEGER NOT NULL,
                eligible INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions(
                id TEXT PRIMARY KEY,
                job TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,
                prior_id TEXT,
                source_sha TEXT NOT NULL,
                profile_sha TEXT NOT NULL,
                ref_sha TEXT NOT NULL,
                policy TEXT NOT NULL,
                created INTEGER NOT NULL,
                expires INTEGER NOT NULL,
                selected TEXT NOT NULL,
                UNIQUE(job, generation)
            );
            CREATE TABLE IF NOT EXISTS packets(
                id TEXT PRIMARY KEY REFERENCES decisions(id),
                pdf_sha TEXT NOT NULL,
                mime_sha TEXT NOT NULL,
                recipient TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY REFERENCES packets(id),
                state TEXT NOT NULL CHECK(state IN ('READY','ATTEMPTING','CAPTURED','API_ACCEPTED','RETRY_WAIT','REJECTED','UNKNOWN','HOLD')),
                attempts INTEGER NOT NULL DEFAULT 0,
                first_attempt INTEGER,
                next_attempt_at TEXT,
                provider_id TEXT,
                last_event TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reservations(
                id TEXT REFERENCES outbox(id),
                attempt_no INTEGER NOT NULL,
                day TEXT NOT NULL,
                month TEXT NOT NULL,
                PRIMARY KEY(id, attempt_no)
            );
            CREATE TRIGGER IF NOT EXISTS decisions_immutable BEFORE UPDATE ON decisions
                BEGIN SELECT RAISE(ABORT,'immutable decision'); END;
            CREATE TRIGGER IF NOT EXISTS packets_immutable BEFORE UPDATE ON packets
                BEGIN SELECT RAISE(ABORT,'immutable packet'); END;
        ''')
        for table in ('packets', 'outbox'):
            c.execute("CREATE TRIGGER IF NOT EXISTS {t}_no_replace BEFORE INSERT ON {t} "
                      "WHEN EXISTS(SELECT 1 FROM {t} WHERE id=NEW.id) "
                      "BEGIN SELECT RAISE(ABORT,'no replacement'); END".format(t=table))
            c.execute("CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                      "BEGIN SELECT RAISE(ABORT,'authoritative state retained'); END".format(t=table))
        # Reservations are additive per attempt (PK id+attempt_no); no replacement
        # trigger, but rows are authoritative once recorded.
        c.execute("CREATE TRIGGER IF NOT EXISTS reservations_no_delete BEFORE DELETE ON reservations "
                  "BEGIN SELECT RAISE(ABORT,'authoritative state retained'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS sources_no_replace BEFORE INSERT ON sources "
                  "WHEN EXISTS(SELECT 1 FROM sources WHERE job=NEW.job) "
                  "BEGIN SELECT RAISE(ABORT,'no replacement'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS sources_no_delete BEFORE DELETE ON sources "
                  "BEGIN SELECT RAISE(ABORT,'authoritative state retained'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS decisions_no_replace BEFORE INSERT ON decisions "
                  "WHEN EXISTS(SELECT 1 FROM decisions WHERE id=NEW.id OR (job=NEW.job AND generation=NEW.generation)) "
                  "BEGIN SELECT RAISE(ABORT,'no replacement'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions "
                  "BEGIN SELECT RAISE(ABORT,'authoritative state retained'); END")

    # --- object store -----------------------------------------------------
    def inert_file(self, data):
        """Content-addressed private store; files carry no authority by themselves."""
        if not isinstance(data, bytes) or not 0 < len(data) <= self.payload_cap:
            raise Hold('payload size')
        key = digest(data)
        path = self.root / 'objects' / key
        if path.exists():
            if path.read_bytes() != data:
                raise EngineFault('object corruption')
            return key
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return key

    def fetch_object(self, key):
        if not SHA.fullmatch(key):
            raise EngineFault('invalid object key')
        path = self.root / 'objects' / key
        if not path.exists():
            raise EngineFault('missing packet object')
        data = path.read_bytes()
        if digest(data) != key:
            raise EngineFault('packet object corrupt')
        return data

    # --- authority --------------------------------------------------------
    def revoke_approval(self):
        """Sole owned approval writer. Serializes on the same worker lock and DB."""
        with self.worker_lock(), self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute("INSERT INTO meta(k,v) VALUES('approval_revoked','true') "
                          "ON CONFLICT(k) DO UPDATE SET v='true'")
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise

    @staticmethod
    def _revoked(c):
        return c.execute("SELECT 1 FROM meta WHERE k='approval_revoked' AND v='true'").fetchone() is not None

    # --- admission --------------------------------------------------------
    def admit(self, *, job_id, raw, profile, ref, eligibility, compose, created,
              fail_point=None):
        """Admit one qualified job as an immutable decision/packet/outbox row.

        job_id: canonical stable job identity (unique per job, never per profile).
        raw: current normalized source row (dict). profile: approved profile dict.
        ref: source reference document (dict) pinned by ref_sha.
        eligibility(raw, profile) -> [fact ids]: source-bound selection; must raise
            Hold on any unsupported/unknown mandatory requirement.
        compose(raw, profile, chosen, created) -> (pdf bytes, mime bytes):
            deterministic for a fixed `created`.
        created: epoch seconds of qualification. Returns QUALIFIED/REVIEW/HOLD.
        """
        if not isinstance(job_id, str) or not job_id or len(job_id) > 1000:
            raise ValueError('canonical job identity required')
        if not isinstance(raw, dict) or not isinstance(profile, dict) or not isinstance(ref, dict):
            raise ValueError('raw/profile/ref dictionaries required')
        if not isinstance(created, int) or created <= 0:
            raise ValueError('qualification epoch required')
        try:
            chosen = eligibility(raw, profile)
            if not isinstance(chosen, list) or not chosen or any(not isinstance(x, str) or not x for x in chosen) or len(set(chosen)) != len(chosen):
                raise Hold('invalid selection')
            pdf, mime = compose(raw, profile, chosen, created)
            if not isinstance(pdf, bytes) or not isinstance(mime, bytes) or not pdf or not mime:
                raise Hold('empty artifact')
            pdf_sha = self.inert_file(pdf)
            mime_sha = self.inert_file(mime)
            source_sha = digest(content_canonical(raw))
            profile_sha = digest(canonical(profile))
            ref_sha = digest(canonical(ref))
            with self.worker_lock(), self.conn() as c:
                c.execute('BEGIN IMMEDIATE')
                try:
                    if self._revoked(c):
                        raise sqlite3.IntegrityError('approval revoked before admission')
                    if fail_point == 'decision':
                        raise sqlite3.IntegrityError('injected at decision insertion')
                    row = c.execute('SELECT * FROM sources WHERE job=?', (job_id,)).fetchone()
                    if row is None or row['source_sha'] != source_sha or row['raw'] != content_canonical(raw).decode():
                        raise Hold('source changed or unobserved before admission')
                    if not row['eligible']:
                        raise Hold('baseline/not eligible for detection')
                    chosen_now = eligibility(raw, profile)
                    if chosen_now != chosen:
                        raise Hold('selection changed during admission')
                    history = c.execute('SELECT d.id, d.generation, o.state, d.expires '
                                        'FROM decisions d LEFT JOIN outbox o ON o.id=d.id '
                                        'WHERE d.job=? ORDER BY d.generation', (job_id,)).fetchall()
                    generation = 1
                    prior_id = None
                    if history:
                        # Q141: only never-attempted history may regenerate, and only
                        # after the prior never-attempted deadline actually expired.
                        if any(h['state'] in (ATTEMPTING, RETRY_WAIT, CAPTURED, API_ACCEPTED, REJECTED, UNKNOWN) for h in history):
                            raise Hold('attempted or uncertain history; no regeneration')
                        if any(h['state'] == READY and h['expires'] > created for h in history):
                            raise Hold('prior generation still within its deadline')
                        generation = max(h['generation'] for h in history) + 1
                        prior_id = max(history, key=lambda h: h['generation'])['id']
                    id_value = digest(canonical([job_id, generation, source_sha, profile_sha, ref_sha, self.version, created]))
                    if fail_point == 'packet':
                        raise sqlite3.IntegrityError('injected at packet insertion')
                    c.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (id_value, job_id, generation, prior_id, source_sha, profile_sha, ref_sha,
                               self.version, created, created + self.expiry_seconds,
                               json.dumps(chosen)))
                    c.execute('INSERT INTO packets VALUES(?,?,?,?)',
                              (id_value, pdf_sha, mime_sha, self.recipient))
                    if fail_point == 'outbox':
                        raise sqlite3.IntegrityError('injected at outbox insertion')
                    c.execute("INSERT INTO outbox(id,state,last_event) VALUES(?,?,?)",
                              (id_value, READY, 'admitted'))
                    c.execute('COMMIT')
                except BaseException:
                    c.execute('ROLLBACK')
                    raise
            return {'status': 'QUALIFIED', 'id': id_value, 'selected': chosen, 'generation': generation}
        except Hold as e:
            return {'status': 'REVIEW', 'reason': str(e)}
        except (sqlite3.Error, OSError, ValueError, EngineFault) as e:
            return {'status': 'HOLD', 'reason': type(e).__name__}

    def observe(self, job_id, raw, observed, eligible):
        """Record a normalized source row (caller-owned discovery state mirror).

        Only complete valid source envelopes reach this method; invalid responses
        must be rejected by the discovery layer before observation. A changed raw
        payload for an existing identity is refused (no replacement).
        """
        if not isinstance(job_id, str) or not job_id or not isinstance(raw, dict) or not isinstance(observed, int):
            raise ValueError('invalid observation')
        text = content_canonical(raw).decode()
        with self.worker_lock(), self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old = c.execute('SELECT * FROM sources WHERE job=?', (job_id,)).fetchone()
                if old is not None and old['source_sha'] == digest(content_canonical(raw)) and old['raw'] == text:
                    c.execute('UPDATE sources SET observed=?, eligible=? WHERE job=?',
                              (observed, int(bool(eligible)), job_id))
                elif old is not None:
                    raise sqlite3.IntegrityError('no replacement of authoritative source row')
                else:
                    c.execute('INSERT INTO sources VALUES(?,?,?,?,?)',
                              (job_id, text, digest(content_canonical(raw)), observed, int(bool(eligible))))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise

    # --- handoff ----------------------------------------------------------
    def validated(self, c, id_value, expected_state, raw, profile, ref, eligibility, compose):
        """Every authority/artifact guard, re-run at claim and at final capture."""
        if self._revoked(c):
            raise Hold('approval revoked')
        d = c.execute('SELECT * FROM decisions JOIN packets USING(id) JOIN outbox USING(id) '
                      'WHERE id=?', (id_value,)).fetchone()
        if not d or d['state'] != expected_state or d['attempts'] > self.max_attempts:
            raise Hold('not ready/uncertain/exhausted')
        if d['policy'] != self.version or d['recipient'] != self.recipient or d['expires'] != d['created'] + self.expiry_seconds:
            raise Hold('policy/recipient')
        if d['source_sha'] != digest(content_canonical(raw)) or d['profile_sha'] != digest(canonical(profile)) or d['ref_sha'] != digest(canonical(ref)):
            raise Hold('final roots')
        chosen = eligibility(raw, profile)
        if chosen != json.loads(d['selected']):
            raise Hold('final roots')
        pdf, mime = compose(raw, profile, chosen, d['created'])
        if digest(pdf) != d['pdf_sha'] or digest(mime) != d['mime_sha']:
            raise Hold('final artifact binding')
        if self.fetch_object(d['pdf_sha']) != pdf or self.fetch_object(d['mime_sha']) != mime:
            raise Hold('final artifact binding')
        return d, mime

    def handoff(self, id_value, *, now, clock=None, transport=None, setup=None,
                before_send=None, after_send=None, raw_loader=None,
                profile_loader=None, ref_loader=None, eligibility=None, compose=None):
        """Claim, reserve, re-validate after any blocking work, then capture or send.

        setup() runs OUTSIDE the lock (models delayed preparation). The decisive
        clock is read AFTER the lock is acquired and again at the final boundary.
        raw_loader(job_id)/profile_loader()/ref_loader() must re-read the current
        protected bytes so late revocation or file change is observed.
        transport.send(mime_bytes) -> result dict; see _classify. None = local capture.
        Returns a state string or HOLD.
        """
        if setup is not None:
            now = setup()
        if clock is None:
            clock = lambda: now
        for required in (raw_loader, profile_loader, ref_loader, eligibility, compose):
            if not callable(required):
                raise ValueError('all loaders/guards required at handoff')
        try:
            with self.worker_lock():
                return self._handoff(id_value, clock, transport, before_send, after_send,
                                     raw_loader, profile_loader, ref_loader, eligibility, compose)
        except (Hold, sqlite3.Error, OSError, ValueError, KeyError, EngineFault):
            return HOLD

    def _handoff(self, id_value, clock, transport, before_send, after_send,
                 raw_loader, profile_loader, ref_loader, eligibility, compose):
        with self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                d = c.execute('SELECT * FROM decisions JOIN packets USING(id) JOIN outbox USING(id) '
                              'WHERE id=?', (id_value,)).fetchone()
                if d is None:
                    raise Hold('unknown packet')
                if d['state'] not in (READY, RETRY_WAIT):
                    raise Hold('not ready/uncertain/exhausted')
                if d['state'] == RETRY_WAIT and d['next_attempt_at'] is not None:
                    next_at = datetime.fromisoformat(d['next_attempt_at'])
                    if utc_stamp(clock()) < next_at:
                        raise Hold('retry backoff not elapsed')
                raw = raw_loader(d['job'])
                if not isinstance(raw, dict):
                    raise Hold('source row unavailable')
                row = c.execute('SELECT * FROM sources WHERE job=?', (d['job'],)).fetchone()
                if row is None or row['source_sha'] != digest(content_canonical(raw)):
                    raise Hold('source changed')
                profile = profile_loader()
                ref = ref_loader()
                self.validated(c, id_value, d['state'], raw, profile, ref, eligibility, compose)
                final_now = clock()
                if not isinstance(final_now, int) or not d['created'] <= final_now < d['expires'] or d['attempts'] >= self.max_attempts:
                    raise Hold('deadline/attempts')
                stamp = utc_stamp(final_now)
                day, month = stamp.strftime('%Y-%m-%d'), stamp.strftime('%Y-%m')
                if c.execute('SELECT COUNT(*) FROM reservations WHERE day=?', (day,)).fetchone()[0] >= self.daily_cap \
                        or c.execute('SELECT COUNT(*) FROM reservations WHERE month=?', (month,)).fetchone()[0] >= self.monthly_cap:
                    raise Hold('cap')
                attempt_no = d['attempts'] + 1
                c.execute('INSERT INTO reservations VALUES(?,?,?,?)', (id_value, attempt_no, day, month))
                c.execute("UPDATE outbox SET state=?, attempts=attempts+1, first_attempt=COALESCE(first_attempt,?), "
                          "next_attempt_at=NULL, last_event='claimed' WHERE id=?",
                          (ATTEMPTING, final_now, id_value))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
        if before_send is not None:
            before_send()
        with self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                d = c.execute('SELECT * FROM decisions JOIN packets USING(id) JOIN outbox USING(id) '
                              'WHERE id=?', (id_value,)).fetchone()
                if d is None:
                    raise Hold('unknown packet')
                raw = raw_loader(d['job'])
                profile = profile_loader()
                ref = ref_loader()
                d, mime = self.validated(c, id_value, ATTEMPTING, raw, profile, ref, eligibility, compose)
                last_now = clock()
                if not isinstance(last_now, int) or not d['created'] <= last_now < d['expires']:
                    raise Hold('final deadline')
                last = utc_stamp(last_now)
                res = c.execute('SELECT day, month FROM reservations WHERE id=? AND attempt_no='
                                '(SELECT attempts FROM outbox WHERE id=?)', (id_value, id_value)).fetchone()
                if res is None or last.strftime('%Y-%m-%d') != res['day'] or last.strftime('%Y-%m') != res['month']:
                    raise Hold('handoff bucket changed; retain prior reservation')
                if transport is None:
                    path = self.root / 'captures' / (id_value + '.eml')
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as f:
                        f.write(mime)
                        f.flush()
                        os.fsync(f.fileno())
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    state, provider, retry_after = CAPTURED, None, None
                else:
                    result = transport.send(mime)
                    state, provider = self._classify(result, d['attempts'])
                    retry_after = result.get('retry_after') if isinstance(result, dict) else None
                if after_send is not None:
                    after_send()
                next_at = self._retry_at(d, last_now, retry_after) if state == RETRY_WAIT else None
                c.execute("UPDATE outbox SET state=?, provider_id=COALESCE(?, provider_id), "
                          "next_attempt_at=?, last_event=? WHERE id=?",
                          (state, provider, next_at,
                           'captured' if state == CAPTURED else 'sent:' + state, id_value))
                c.execute('COMMIT')
                return state
            except BaseException:
                c.execute('ROLLBACK')
                c.execute("UPDATE outbox SET state=?, last_event='handoff_fault' WHERE id=?", (UNKNOWN, id_value))
                raise

    def _retry_at(self, d, now, retry_after=None):
        if d['attempts'] >= self.max_attempts:
            return None
        seconds = 60 * (2 ** (d['attempts'] - 1))
        if retry_after is not None:
            if isinstance(retry_after, str) and retry_after.isascii() and retry_after.isdigit():
                seconds = max(seconds, int(retry_after))
            elif isinstance(retry_after, str):
                parsed = datetime.fromisoformat(retry_after.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = max(seconds, int((parsed.astimezone(timezone.utc) - utc_stamp(now)).total_seconds()))
            elif isinstance(retry_after, int):
                seconds = max(seconds, retry_after)
        if not 0 <= seconds <= 7 * 86400:
            return None
        return (utc_stamp(now) + timedelta(seconds=seconds)).isoformat()

    def _classify(self, result, attempts):
        """Map a transport outcome to a bounded state. Provider acceptance is not inbox receipt."""
        if not isinstance(result, dict):
            return UNKNOWN, None
        state = result.get('state')
        if state == 'api_accepted' and isinstance(result.get('provider_message_id'), str) \
                and re.fullmatch(r'[A-Za-z0-9_./+=:-]{1,256}', result['provider_message_id']):
            return API_ACCEPTED, result['provider_message_id']
        if state == 'not_attempted':
            return (RETRY_WAIT if attempts < self.max_attempts else HOLD), None
        if state == 'rejected' and isinstance(result.get('http_status'), int) and result['http_status'] in (401, 429):
            return (RETRY_WAIT if attempts < self.max_attempts else HOLD), None
        if state == 'rejected' and isinstance(result.get('http_status'), int) and 400 <= result['http_status'] < 500:
            return REJECTED, None
        return UNKNOWN, None

    # --- queue management -------------------------------------------------
    def candidates(self, now, limit=20):
        """READY/RETRY_WAIT rows still within the original deadline, attempts,
        retry backoff and the 24h queue TTL. The 600s packet deadline is enforced
        at handoff, never renewed here."""
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('bounded candidate page required')
        at = utc_stamp(now)
        with self.conn() as c:
            rows = c.execute("SELECT o.id FROM outbox o JOIN decisions d ON d.id=o.id "
                             "WHERE o.state IN (?,?) AND o.attempts<? AND d.expires>? "
                             "AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?) "
                             "AND (o.first_attempt IS NULL OR o.first_attempt>=?) "
                             "AND d.created>=? ORDER BY d.created, o.rowid LIMIT ?",
                             (READY, RETRY_WAIT, self.max_attempts, int(at.timestamp()),
                              at.isoformat(), int((at - self.retry_window).timestamp()),
                              int((at - self.queue_ttl).timestamp()), limit)).fetchall()
            return [r['id'] for r in rows]

    def expire_stale(self, now):
        """Terminal-hold rows whose deadline/retry window/queue TTL elapsed.

        Never silently drops: state becomes HOLD with a typed event, preserving
        the immutable decision/packet history."""
        at = utc_stamp(now)
        with self.worker_lock(), self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute("UPDATE outbox SET state=?, last_event='expired:deadline' WHERE id IN ("
                          "SELECT o.id FROM outbox o JOIN decisions d ON d.id=o.id "
                          "WHERE o.state IN (?,?) AND d.expires<=?)",
                          (HOLD, READY, RETRY_WAIT, int(at.timestamp())))
                c.execute("UPDATE outbox SET state=?, last_event='expired:retry_window' WHERE id IN ("
                          "SELECT o.id FROM outbox o WHERE o.state=? AND o.first_attempt IS NOT NULL AND o.first_attempt<=?)",
                          (HOLD, RETRY_WAIT, int((at - self.retry_window).timestamp())))
                c.execute("UPDATE outbox SET state=?, last_event='expired:queue_ttl' WHERE id IN ("
                          "SELECT o.id FROM outbox o JOIN decisions d ON d.id=o.id "
                          "WHERE o.state IN (?,?) AND d.created<=?)",
                          (HOLD, READY, RETRY_WAIT, int((at - self.queue_ttl).timestamp())))
                changed = c.total_changes
                c.execute('COMMIT')
                return changed
            except BaseException:
                c.execute('ROLLBACK')
                raise

    def status(self):
        with self.conn() as c:
            return {r['state']: r['n'] for r in c.execute('SELECT state, COUNT(*) n FROM outbox GROUP BY state')}

    @classmethod
    def status_only(cls, root):
        """Read-only status inspection; opens no locks and mutates nothing."""
        db = Path(root) / 'engine.sqlite'
        if not db.exists():
            return {'states': {}, 'decisions': 0, 'captures': 0}
        c = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            states = {r['state']: r['n'] for r in c.execute('SELECT state, COUNT(*) n FROM outbox GROUP BY state')}
            decisions = c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
            return {'states': states, 'decisions': decisions}
        finally:
            c.close()
