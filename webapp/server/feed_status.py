"""Feed-health state machine (a1 "Feed-death classification"; chips: a2/a9 F3).

Pure functions over a plain dict: no db, no network, no clock of its own.
The caller supplies `now` and passes copies around; inputs are never mutated.

`dead` is reachable ONLY through operator_dead, and operator_revive is the
only un-dead. Empty feeds and 404/410 runs never auto-dead a board; the
3x404 message ("failed — suspected posting-API outage") is a failure signal,
not a death sentence — polling continues at backoff (a1).
"""

from . import config


def new_state():
    """Fresh board-health state with every field defaulted."""
    return {
        "schema_fingerprint": None,
        "consecutive_invalid": 0,
        "consecutive_http_404": 0,
        "consecutive_empty": 0,
        "dead": False,
        "paused": False,
        "consecutive_429": 0,
        "last_status": None,
        "last_success_at": None,
        "last_failure_at": None,
    }


def classify_feed_event(state, event, now):
    """Apply one feed event to a copy of `state` and return that copy."""
    out = dict(state)
    kind = event.get("kind")
    status = event.get("status")
    if kind == "success":
        out["consecutive_invalid"] = 0
        out["consecutive_http_404"] = 0
        out["consecutive_empty"] = 0
        out["last_success_at"] = now
    elif kind == "empty":
        out["consecutive_empty"] += 1
    elif kind == "invalid_envelope":
        out["consecutive_invalid"] += 1
        out["last_failure_at"] = now
    elif kind == "http_404_410":
        out["consecutive_http_404"] += 1
        out["last_failure_at"] = now
    elif kind == "fingerprint_drift":
        if "schema_fingerprint" in event:
            out["schema_fingerprint"] = event["schema_fingerprint"]
    elif kind == "throttled":
        out["consecutive_429"] += 1
        out["last_failure_at"] = now
        if out["consecutive_429"] >= config.PAUSE_AFTER_429:
            out["paused"] = True
    elif kind == "failure":
        out["last_failure_at"] = now
    elif kind == "operator_dead":
        out["dead"] = True
    elif kind == "operator_revive":
        out["dead"] = False
    if status is not None and kind != "fingerprint_drift":
        out["last_status"] = status
    return out


def status_chip(state, now, interval_s, board_stale_after_s):
    """Health chip from the F3 canonical set {healthy, stale, failed, paused, dead}."""
    if state["dead"]:
        return "dead"
    if state["paused"]:
        return "paused"
    if state["last_success_at"] is None:
        # Never-polled renders healthy until the first failed attempt (L-0015).
        return "failed" if state["last_failure_at"] is not None else "healthy"
    if now - state["last_success_at"] > board_stale_after_s:
        return "stale"
    if state["consecutive_invalid"] >= 2 or state["consecutive_http_404"] >= 3:
        return "failed"
    return "healthy"


def feed_failure_message(state):
    """Operator-visible failure message (a1 wording), or None when healthy."""
    if state["consecutive_http_404"] >= 3:
        return "failed — suspected posting-API outage"
    return None
