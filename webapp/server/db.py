"""Store module — the only code allowed to touch the SQLite file (a12).

Tables: `sources` (board registry + per-board health + politeness + feed
state), `host_cooldowns` (per-host server Retry-After deadlines, a1), `jobs`
(classify-phase storage), `applications` (a6 13-field tracking schema),
`status_history` (every transition). WAL on, busy timeout. Outbox/facts tables
arrive with their own phases.
"""

import contextlib
import datetime
import hashlib
import json
import sqlite3
import time

from . import config
from .politeness import backoff_seconds  # pure policy, no cycle (politeness never imports db)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('ashby','greenhouse','lever')),
    host TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    registered_at TEXT,
    last_success_at REAL,
    last_failure_at REAL,
    last_status INTEGER,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    consecutive_429 INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    deferred_until REAL NOT NULL DEFAULT 0,
    schema_fingerprint TEXT,
    consecutive_invalid INTEGER NOT NULL DEFAULT 0,
    consecutive_http_404 INTEGER NOT NULL DEFAULT 0,
    consecutive_empty INTEGER NOT NULL DEFAULT 0,
    dead INTEGER NOT NULL DEFAULT 0,
    careers_url TEXT
);

CREATE TABLE IF NOT EXISTS host_cooldowns (
    host TEXT PRIMARY KEY,
    until REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    raw_id TEXT NOT NULL,
    title TEXT NOT NULL,
    employer TEXT NOT NULL,
    location TEXT,
    url TEXT,
    first_published_field TEXT,
    first_published_epoch_s REAL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    content_hash TEXT NOT NULL,
    absent_streak INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, raw_id)
);

CREATE TABLE IF NOT EXISTS applications (
    canonical_job_id TEXT PRIMARY KEY,
    employer TEXT,
    title TEXT,
    canonical_apply_url TEXT,
    status TEXT NOT NULL DEFAULT 'Saved'
        CHECK(status IN ('Saved','Applied','Interviewing','Offer','Rejected','Archived')),
    saved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    applied_at TEXT,
    status_changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notes TEXT,
    cover_letter_ref TEXT,
    tailored_resume_ref TEXT,
    fact_store_version TEXT
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,
    status TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind = 'job_alert'),
    job_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    employer TEXT,
    url TEXT,
    alert_text TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    sent_at TEXT
);
"""

_SOURCE_COLUMNS = (
    "id, name, kind, host, feed_url, registered_at, last_success_at,"
    " last_failure_at, last_status, jobs_seen, consecutive_429, paused,"
    " deferred_until, schema_fingerprint, consecutive_invalid,"
    " consecutive_http_404, consecutive_empty, dead, careers_url"
)

_JOB_COLUMNS = (
    "id, source_id, raw_id, title, employer, location, url,"
    " first_published_field, first_published_epoch_s, first_seen_at,"
    " last_seen_at, content_hash, absent_streak, active"
)

_CONTENT_FIELDS = (
    "title", "employer", "location", "url", "first_published_epoch_s",
)


def _content_hash(record):
    """Stable hash of the job's content fields — edits change it, re-polls
    don't (a2: same merge key + changed content = edit, never re-alert)."""
    return hashlib.sha256(
        json.dumps(
            {k: record.get(k) for k in _CONTENT_FIELDS}, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


# MIGRATION-001 (applied idempotently at every open): the Phase 1 politeness
# columns. MIGRATION-002: the classify-phase feed-state columns (the
# feed_status.py reducer persists exactly these). Additive ALTERs only —
# never drop, never rewrite. New installs get them from _SCHEMA; older volume
# files get them here (each shipped schema predates the later columns).
_MIGRATIONS = (
    ("sources", "consecutive_429", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "deferred_until", "REAL NOT NULL DEFAULT 0"),
    ("sources", "schema_fingerprint", "TEXT"),
    ("sources", "consecutive_invalid", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "consecutive_http_404", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "consecutive_empty", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "dead", "INTEGER NOT NULL DEFAULT 0"),
    # MIGRATION-004: the registered careers page for the liveness tier (a2).
    ("sources", "careers_url", "TEXT"),
)


def _migrate(c):
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    _migrate_003(c)


def _iso(epoch_s):
    """Epoch seconds -> ISO-8601 UTC TEXT, second precision (a6 timestamps)."""
    return datetime.datetime.fromtimestamp(
        epoch_s, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_iso(value):
    """Normalize one stored timestamp to ISO TEXT: numeric (or numeric TEXT,
    which SQLite's TEXT affinity produces when copying REALs) -> ISO-8601;
    already-textual values pass through."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    return _iso(value)


def _migrate_003(c):
    """Rebuild applications + status_history to the a6 13-field schema.

    The pre-a6 tables used `id`/`job_id` keys, REAL epoch timestamps, and
    lacked employer/title/apply-url fields. Column renames and type changes
    cannot be expressed as additive ALTERs, so both tables are rebuilt once
    (detected by the old signature); numeric timestamps convert to ISO TEXT.
    """
    apps_cols = {row["name"] for row in c.execute("PRAGMA table_info(applications)")}
    if "id" in apps_cols and "canonical_job_id" not in apps_cols:
        c.executescript(
            "CREATE TABLE applications_new ("
            " canonical_job_id TEXT PRIMARY KEY,"
            " employer TEXT, title TEXT, canonical_apply_url TEXT,"
            " status TEXT NOT NULL DEFAULT 'Saved' CHECK(status IN"
            " ('Saved','Applied','Interviewing','Offer','Rejected','Archived')),"
            " saved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
            " applied_at TEXT,"
            " status_changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
            " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
            " notes TEXT, cover_letter_ref TEXT,"
            " tailored_resume_ref TEXT, fact_store_version TEXT);"
            " INSERT INTO applications_new (canonical_job_id, status, applied_at,"
            " updated_at, notes, cover_letter_ref, tailored_resume_ref)"
            " SELECT id, status, applied_at, updated_at, notes,"
            " cover_letter, resume_ref FROM applications;"
            " DROP TABLE applications;"
            " ALTER TABLE applications_new RENAME TO applications;"
        )
        for row in c.execute("SELECT canonical_job_id, applied_at, updated_at"
                             " FROM applications").fetchall():
            c.execute(
                "UPDATE applications SET applied_at=?, updated_at=?"
                " WHERE canonical_job_id=?",
                (_as_iso(row["applied_at"]), _as_iso(row["updated_at"]),
                 row["canonical_job_id"]),
            )
    hist_cols = {row["name"] for row in c.execute("PRAGMA table_info(status_history)")}
    if "job_id" in hist_cols and "application_id" not in hist_cols:
        c.executescript(
            "CREATE TABLE status_history_new ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " application_id TEXT NOT NULL,"
            " status TEXT NOT NULL, changed_at TEXT NOT NULL);"
            " INSERT INTO status_history_new (id, application_id, status,"
            " changed_at) SELECT id, job_id, status, at FROM status_history;"
            " DROP TABLE status_history;"
            " ALTER TABLE status_history_new RENAME TO status_history;"
        )
        for row in c.execute("SELECT id, changed_at FROM status_history").fetchall():
            c.execute(
                "UPDATE status_history SET changed_at=? WHERE id=?",
                (_as_iso(row["changed_at"]), row["id"]),
            )


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.executescript(_SCHEMA)
            _migrate(c)

    @contextlib.contextmanager
    def conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_feed_state(self, source_id: str, state: dict):
        """Persist the feed_status reducer's five MIGRATION-002 counters.

        The fixed column list means callers can never inject SQL; missing keys
        fall back to the current row so partial states are safe.
        """
        with self.conn() as c:
            row = c.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None:
                return
            c.execute(
                "UPDATE sources SET schema_fingerprint=?, consecutive_invalid=?,"
                " consecutive_http_404=?, consecutive_empty=?, dead=?"
                " WHERE id=?",
                (
                    state.get("schema_fingerprint", row["schema_fingerprint"]),
                    state.get("consecutive_invalid", row["consecutive_invalid"]),
                    state.get("consecutive_http_404", row["consecutive_http_404"]),
                    state.get("consecutive_empty", row["consecutive_empty"]),
                    int(state.get("dead", row["dead"])),
                    source_id,
                ),
            )

    def list_sources(self):
        with self.conn() as c:
            rows = c.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_source(self, source_id: str):
        with self.conn() as c:
            row = c.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_source(self, source: dict):
        with self.conn() as c:
            c.execute(
                "INSERT INTO sources (id, name, kind, host, feed_url,"
                " registered_at, careers_url)"
                " VALUES (:id,:name,:kind,:host,:feed_url,:registered_at,"
                " :careers_url)",
                {
                    **source,
                    "registered_at": source.get("registered_at")
                    or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "careers_url": source.get("careers_url"),
                },
            )

    def record_fetch(self, source_id: str, status: int, jobs_seen: int = 0):
        """Update per-board health after one fetch (atomic with its own write)."""
        now = time.time()
        with self.conn() as c:
            if 200 <= status < 300:
                c.execute(
                    "UPDATE sources SET last_success_at=?, last_status=?, jobs_seen=jobs_seen+? WHERE id=?",
                    (now, status, jobs_seen, source_id),
                )
            else:
                c.execute(
                    "UPDATE sources SET last_failure_at=?, last_status=? WHERE id=?",
                    (now, status, source_id),
                )

    def record_success(self, source_id: str, status: int = 200, jobs_seen: int = 0):
        """2xx fetch: last_success + politeness machine fully reset."""
        now = time.time()
        with self.conn() as c:
            c.execute(
                "UPDATE sources SET last_success_at=?, last_status=?,"
                " jobs_seen=jobs_seen+?, consecutive_429=0, paused=0,"
                " deferred_until=0 WHERE id=?",
                (now, status, jobs_seen, source_id),
            )

    def record_throttled(self, source_id: str, host: str, status: int, retry_after, now: float) -> int:
        """One 429/403, atomic in one transaction: increment the counter,
        compute pause/deferral from the authoritative new count, and — when the
        server sent a Retry-After — extend the HOST-WIDE cooldown, never
        shortening it (a1: server deadline honored in full, unclamped).

        `paused = max(paused, ?)` so a concurrent success that already reset the
        machine cannot be overwritten back into pause."""
        with self.conn() as c:
            c.execute(
                "UPDATE sources SET consecutive_429 = consecutive_429 + 1,"
                " last_failure_at=?, last_status=? WHERE id=?",
                (now, status, source_id),
            )
            row = c.execute(
                "SELECT consecutive_429 FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            n = row["consecutive_429"] if row else 0
            paused = 1 if n >= config.PAUSE_AFTER_429 else 0
            deferred_s = max(
                backoff_seconds(n),
                config.PROBE_AFTER_S if paused else 0,
                retry_after or 0,
            )
            c.execute(
                "UPDATE sources SET deferred_until = max(deferred_until, ?),"
                " paused = max(paused, ?) WHERE id=?",
                (now + deferred_s, paused, source_id),
            )
            if retry_after:
                c.execute(
                    "INSERT INTO host_cooldowns (host, until) VALUES (?, ?)"
                    " ON CONFLICT(host) DO UPDATE SET"
                    " until = max(host_cooldowns.until, excluded.until)",
                    (host, now + retry_after),
                )
        return n

    def host_cooldown_until(self, host: str) -> float:
        with self.conn() as c:
            row = c.execute(
                "SELECT until FROM host_cooldowns WHERE host=?", (host,)
            ).fetchone()
        return row["until"] if row else 0.0

    def defer_board(self, source_id: str, until: float):
        """Record a deferral (host-cooldown skip etc.) without losing an
        already-later deadline."""
        with self.conn() as c:
            c.execute(
                "UPDATE sources SET deferred_until = max(deferred_until, ?)"
                " WHERE id=?",
                (until, source_id),
            )

    def resume_board(self, source_id: str):
        """Manual resume: clears pause/counter/self-deferral. Never touches
        host_cooldowns — the caller checks the outstanding server deadline
        first (a1: manual Resume never overrides it)."""
        with self.conn() as c:
            c.execute(
                "UPDATE sources SET paused=0, consecutive_429=0, deferred_until=0"
                " WHERE id=?",
                (source_id,),
            )

    # --- jobs (classify; a1 raw-id novelty, a2 freshness) ---

    def upsert_jobs(self, source_id: str, records, now: float):
        """Insert new raw ids, update changed content (edit, never new), touch
        last_seen. Returns (new_count, updated_count) atomically."""
        new_count = updated_count = 0
        with self.conn() as c:
            for record in records:
                raw_id = record["raw_id"]
                job_id = f"{source_id}:{raw_id}"
                content_hash = _content_hash(record)
                row = c.execute(
                    "SELECT content_hash FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO jobs (id, source_id, raw_id, title, employer,"
                        " location, url, first_published_field,"
                        " first_published_epoch_s, first_seen_at, last_seen_at,"
                        " content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id, source_id, raw_id, record["title"],
                            record["employer"], record.get("location"),
                            record.get("url"),
                            record.get("first_published_field"),
                            record.get("first_published_epoch_s"),
                            now, now, content_hash,
                        ),
                    )
                    new_count += 1
                    continue
                if row["content_hash"] != content_hash:
                    c.execute(
                        "UPDATE jobs SET title=?, employer=?, location=?, url=?,"
                        " first_published_epoch_s=?, content_hash=?,"
                        " last_seen_at=? WHERE id=?",
                        (
                            record["title"], record["employer"],
                            record.get("location"), record.get("url"),
                            record.get("first_published_epoch_s"),
                            content_hash, now, job_id,
                        ),
                    )
                    updated_count += 1
                else:
                    c.execute(
                        "UPDATE jobs SET last_seen_at=? WHERE id=?", (now, job_id)
                    )
                c.execute(
                    "UPDATE jobs SET absent_streak=0, active=1 WHERE id=?",
                    (job_id,),
                )
        return new_count, updated_count

    def mark_snapshot(self, source_id: str, raw_ids, now: float):
        """One snapshot boundary for a source: jobs absent from it grow their
        absence streak; 2 consecutive absences deactivate (a2)."""
        seen = set(raw_ids)
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, raw_id FROM jobs WHERE source_id=?", (source_id,)
            ).fetchall()
            for row in rows:
                # source ids are colon-shaped ("ashby:acme"), so only the
                # stored raw_id column is comparable to snapshot raw ids.
                if row["raw_id"] in seen:
                    continue
                c.execute(
                    "UPDATE jobs SET absent_streak = absent_streak + 1,"
                    " active = CASE WHEN absent_streak + 1 >= 2 THEN 0 ELSE active END"
                    " WHERE id=?",
                    (row["id"],),
                )

    def get_job(self, job_id: str):
        with self.conn() as c:
            row = c.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, source_id: str = None, active_only: bool = True):
        query = f"SELECT {_JOB_COLUMNS} FROM jobs"
        params = []
        if active_only:
            query += " WHERE active=1"
        if source_id is not None:
            query += (" AND" if " WHERE" in query else " WHERE") + " source_id=?"
            params.append(source_id)
        query += " ORDER BY first_seen_at DESC"
        with self.conn() as c:
            rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # --- applications (a6 13-field tracking schema; scar P12) ---

    def get_application(self, job_id: str):
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM applications WHERE canonical_job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_applications(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM applications ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, job_id: str, status: str, now: float):
        """Transition + append a history row (P12: canonical id is the only
        key; the six-status vocabulary is enforced by the schema CHECK).
        Timestamps are ISO-8601 TEXT per the a6 schema."""
        stamp = _iso(now)
        with self.conn() as c:
            c.execute(
                "UPDATE applications SET status=?, status_changed_at=?,"
                " updated_at=? WHERE canonical_job_id=?",
                (status, stamp, stamp, job_id),
            )
            c.execute(
                "INSERT INTO status_history (application_id, status, changed_at)"
                " VALUES (?,?,?)",
                (job_id, status, stamp),
            )

    def status_history(self, job_id: str):
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, application_id, status, changed_at"
                " FROM status_history WHERE application_id=?"
                " ORDER BY changed_at, id",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_status_history(self):
        """Every history row across all applications, changed_at ordered (P9)."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, application_id, status, changed_at"
                " FROM status_history ORDER BY changed_at, id"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- outbox (delivery phase; drained by the Phase-5 Gmail sender) ---

    def insert_outbox_alert(self, alert: dict, source_id: str, now: float):
        """Persist one admitted job_alert; returns the new row id."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO outbox (kind, job_id, source_id, title, employer,"
                " url, alert_text, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    alert["kind"], alert["job_id"], source_id,
                    alert.get("title"), alert.get("employer"), alert.get("url"),
                    alert.get("alert_text"), _iso(now),
                ),
            )
            return cur.lastrowid

    def list_unsent_alerts(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL"
                " ORDER BY created_at, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_outbox_sent(self, outbox_id: int, now: float):
        with self.conn() as c:
            c.execute(
                "UPDATE outbox SET sent_at=? WHERE id=?", (_iso(now), outbox_id)
            )

    def mark_applied(self, job_id: str, now: float) -> bool:
        """Idempotent (P12, a6): (a) absent row -> INSERT Applied with
        first-write-wins stamps; (b) Saved with applied_at NULL -> UPDATE once;
        (c) NO-OP whenever applied_at is set or the status would demote."""
        stamp = _iso(now)
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO applications (canonical_job_id, status,"
                " saved_at, applied_at, status_changed_at, updated_at)"
                " VALUES (?, 'Applied', ?, ?, ?, ?)",
                (job_id, stamp, stamp, stamp, stamp),
            )
            if cur.rowcount == 1:
                c.execute(
                    "INSERT INTO status_history (application_id, status,"
                    " changed_at) VALUES (?, 'Applied', ?)",
                    (job_id, stamp),
                )
                return True
            cur = c.execute(
                "UPDATE applications SET status='Applied', applied_at=?,"
                " status_changed_at=?, updated_at=?"
                " WHERE canonical_job_id=? AND status='Saved'"
                " AND applied_at IS NULL",
                (stamp, stamp, stamp, job_id),
            )
            if cur.rowcount == 1:
                c.execute(
                    "INSERT INTO status_history (application_id, status,"
                    " changed_at) VALUES (?, 'Applied', ?)",
                    (job_id, stamp),
                )
                return True
            return False

    def update_application(self, job_id: str, **fields):
        """Notes / cover letter / resume_ref (operator text, never rendered
        as claims — P4 binds claims to the fact store, not this text)."""
        allowed = ("notes", "cover_letter", "resume_ref")
        updates = {k: fields[k] for k in allowed if k in fields}
        if not updates:
            return
        sets = ", ".join(f"{k}=?" for k in updates)
        with self.conn() as c:
            c.execute(
                f"UPDATE applications SET {sets}, updated_at=? WHERE id=?",
                (*updates.values(), time.time(), job_id),
            )
