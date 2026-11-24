"""Pure scheduler planning logic (a1 Q39 workload contract).

No threads, no db, no wall clock: the caller injects due/host data and owns
threads, clock, and store writes. Production wiring wraps real daemon-thread
`join(timeout=30 s)` waits and feeds the outcome into `wait_for_workers`.

Q39 pins: per-board slice = 20 s GET + 10 s parse in a daemon worker thread;
loop waits join(timeout=30 s) and continues regardless; per-board snapshot
write ≤ 2 s and per-cycle scheduling/health ≤ 10 s inside the 120 s stage
budget → N ≤ 24 boards advisory (N×(30+2)+120 ≤ 900; E9 measurement is
authoritative); stragglers never dropped; a straggler surviving > 3×interval
triggers the watchdog force-exit flag; next_due-ordered fair continuation;
15-min cadence best-effort with operator-visible degradation.
"""


def advisory_capacity_ok(board_count, slice_s=30.0, write_s=2.0,
                         stage_budget_s=120.0, interval_s=900.0):
    """Q39 N≤24 advisory: board_count×(slice_s+write_s)+stage_budget_s ≤ interval_s.

    24×(30+2)+120 = 888 ≤ 900 → True; 25×(30+2)+120 = 920 > 900 → False.
    """
    return board_count * (slice_s + write_s) + stage_budget_s <= interval_s


def due_boards(boards, now):
    """Q39 next_due order: boards with deferred_until ≤ now (0 = due now),
    sorted by (deferred_until, id) so the soonest-due board goes first."""
    return sorted(
        (b for b in boards if b["deferred_until"] <= now),
        key=lambda b: (b["deferred_until"], b["id"]),
    )


def plan_cycle(boards, now, straggler_hosts):
    """Q39 straggler skip: a board whose host is in straggler_hosts is skipped
    this cycle and recorded under deferred_host — never dropped silently.
    Remaining boards with deferred_until > now go to deferred_until; the rest
    are due. Each bucket is (deferred_until, id) ordered."""
    deferred_host, deferred_until, due = [], [], []
    for board in boards:
        if board["host"] in straggler_hosts:
            deferred_host.append(board)
        elif board["deferred_until"] > now:
            deferred_until.append(board)
        else:
            due.append(board)
    key = lambda b: (b["deferred_until"], b["id"])
    return {
        "due": sorted(due, key=key),
        "deferred_host": sorted(deferred_host, key=key),
        "deferred_until": sorted(deferred_until, key=key),
    }


def fair_order(previous_cycle_polled, boards, now, straggler_hosts):
    """Q39 fair continuation: boards not polled in the previous cycle come
    first, then the rest; each group is (deferred_until, id) ordered, so the
    result is deterministic. `now` and `straggler_hosts` are accepted for
    interface parity with plan_cycle; the caller folds that state into
    previous_cycle_polled."""
    polled = set(previous_cycle_polled)
    return sorted(
        boards,
        key=lambda b: (b["id"] in polled, b["deferred_until"], b["id"]),
    )


def wait_for_workers(workers, now, timeout_s=30.0):
    """Q39 worker-wait isolation, pure form: a worker whose deadline_epoch_s
    has passed (≤ now) is done; the rest are stragglers. In production the
    caller wraps real daemon-thread joins with timeout_s and feeds the
    outcome in here."""
    done = [w for w in workers if w["deadline_epoch_s"] <= now]
    stragglers = [w for w in workers if w["deadline_epoch_s"] > now]
    return done, stragglers


def watchdog_force_exit(straggler, started_at_epoch_s, now, interval_s=900.0):
    """Q39 watchdog: operator-alerted force-exit flag when a straggler has
    survived > 3×interval. `straggler` is the in-flight worker record the
    caller attaches to the alert; the decision is purely time-based."""
    return now - started_at_epoch_s > 3 * interval_s
