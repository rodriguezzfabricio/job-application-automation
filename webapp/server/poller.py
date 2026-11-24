"""In-process poller thread — Phase 1 politeness wiring (a1, L-0010).

Single OS thread, one process (a12 Q37). Each tick: heartbeat, then one
serialized pass over registered boards through the sanctioned transport via
PoliteFetcher (per-host locks, host-cooldown gate, 5x429/403 pause, backoff).
Boards with a future deferred_until are skipped — the skip stays visible on the
row (health chips), and a host-cooldown hit is recorded back onto the board
(a1: deferrals recorded, never silently dropped). 900 s cadence + 0..180 s
jitter. Each due board runs poll_board: polite fetch, feed-status
classification (drift/404/empty/dead counters via MIGRATION-002), snapshot
ingestion of successful bodies (classify-phase storage), then the alert pass
(liveness-gated, budgeted, outbox-persisted — P3/S1) when enabled.
"""

import random
import threading
import time
from datetime import datetime, timezone

from . import config, feed_status, ingest, politeness


def _state_from_row(row):
    """Seed the feed_status state from a sources row's persisted columns."""
    return {
        "schema_fingerprint": row["schema_fingerprint"],
        "consecutive_invalid": row["consecutive_invalid"],
        "consecutive_http_404": row["consecutive_http_404"],
        "consecutive_empty": row["consecutive_empty"],
        "dead": bool(row["dead"]),
        "paused": bool(row["paused"]),
        "consecutive_429": row["consecutive_429"],
        "last_status": row["last_status"],
        "last_success_at": row["last_success_at"],
        "last_failure_at": row["last_failure_at"],
    }


def poll_board(store, fetcher, row, now, timeout_s=None):
    """One polite poll of a due board, then classify + ingest (a2/L-0018).

    The fetcher owns politeness writes (record_success/record_throttled/
    record_fetch); this function owns the feed-state reducer and the
    snapshot ingestion. Returns (http_status, feed_state, summary) where
    summary is the ingest summary on a successful 2xx parse, else None.
    """
    timeout_s = timeout_s if timeout_s is not None else config.FETCH_TIMEOUT_S
    status, body = fetcher.poll(row, timeout_s)
    # Re-read: the fetcher has written politeness facts (429 counter, pause,
    # timestamps) and the caller's row may be stale — counters must accumulate.
    row = store.get_source(row["id"])
    state = _state_from_row(row)
    summary = None
    if 200 <= status < 300:
        state = feed_status.classify_feed_event(
            state, {"kind": "success", "status": status}, now)
        try:
            summary = ingest.ingest_snapshot(store, row, body, now)
        except ingest.IngestError:
            state = feed_status.classify_feed_event(
                state, {"kind": "invalid_envelope", "status": status}, now)
        else:
            # Empty-but-valid is still a successful poll (a2): the empty
            # counter warns, it never fails the board.
            if summary["total_parsed"] == 0:
                state = feed_status.classify_feed_event(
                    state, {"kind": "empty"}, now)
    elif status in (429, 403):
        state = feed_status.classify_feed_event(
            state, {"kind": "throttled", "status": status}, now)
    elif status in (404, 410):
        state = feed_status.classify_feed_event(
            state, {"kind": "http_404_410", "status": status}, now)
    else:
        state = feed_status.classify_feed_event(
            state, {"kind": "failure", "status": status}, now)
    store.save_feed_state(row["id"], state)
    return status, state, summary


def run_alert_pass(store, alert_builder, registry, claim_ids,
                   employer_transport_factory, liveness_tier,
                   row, summary, now):
    """The delivery tier for one successful snapshot (P3/S1).

    Per new record (capped): liveness check stamps record["liveness_ok"],
    then the AlertBuilder gates (first-seen ∧ within-window ∧ liveness),
    reserves a budget slot, and admitted alerts persist to the outbox.
    Returns the number of alerts created. A BudgetError stops the pass —
    never crashes the poller.
    """
    seen = {
        job["raw_id"]
        for job in store.list_jobs(source_id=row["id"], active_only=False)
    }
    day_key = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    transport = None
    careers_url = row.get("careers_url")
    if careers_url:
        transport = employer_transport_factory(careers_url)
    created = 0
    for record in summary["new_records"][:config.MAX_LIVENESS_CHECKS_PER_CYCLE]:
        tier = liveness_tier(record, row, transport, now)
        record["liveness_ok"] = tier["liveness_ok"]
    try:
        results = alert_builder.build_summary_alerts(
            summary, registry, claim_ids, day_key, now, seen)
    except Exception:  # noqa: BLE001 — BudgetError/ClaimError must not kill the tick
        return created
    for result in results:
        alert = result.get("alert")
        if alert is not None:
            store.insert_outbox_alert(alert, row["id"], now)
            created += 1
    return created


class PollerThread(threading.Thread):
    def __init__(self, store, heartbeat, fetcher=None, alert_builder=None,
                 registry=None, claim_ids=None, employer_transport_factory=None,
                 liveness_tier=None):
        super().__init__(name="poller", daemon=True)
        self.store = store
        self.heartbeat = heartbeat
        self.fetcher = fetcher or politeness.PoliteFetcher(store)
        self.alert_builder = alert_builder
        self.registry = registry
        self.claim_ids = claim_ids
        self.employer_transport_factory = employer_transport_factory
        self.liveness_tier = liveness_tier
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _tick(self):
        self.heartbeat.touch()
        now = time.time()
        for row in self.store.list_sources():
            # kill_gate() lands here when the kill-switch phase is built (a8).
            if row["deferred_until"] and row["deferred_until"] > now:
                continue
            try:
                status, _state, summary = poll_board(
                    self.store, self.fetcher, row, now)
            except politeness.HostCoolingDown as exc:
                # Another board's Retry-After throttles this host too (a1);
                # record the deferral on this board so it is operator-visible.
                self.store.defer_board(row["id"], exc.until)
            except Exception:  # noqa: BLE001 — record, never crash the loop
                self.store.record_fetch(row["id"], status=0)
                continue
            if (self.alert_builder and self.liveness_tier
                    and self.employer_transport_factory
                    and summary and 200 <= status < 300):
                try:
                    run_alert_pass(
                        self.store, self.alert_builder, self.registry,
                        self.claim_ids, self.employer_transport_factory,
                        self.liveness_tier, row, summary, now)
                except Exception:  # noqa: BLE001 — alert tier must not kill the tick
                    pass

    def run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — a tick crash must not kill the poller
                pass
            jitter = random.uniform(0, config.JITTER_MAX)
            self._stop.wait(config.POLL_INTERVAL + jitter)
