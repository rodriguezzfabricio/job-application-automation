"""Heartbeat + health summary (R3 / a8 F5).

Two distinct signals, deliberately separate (this was the E9 defect):
- heartbeat  = poller-loop liveness (process signal; fresh even when feeds fail)
- per-board last_success_at = DISCOVERY success (R3: feeds actually succeeded)

/healthz is 503 when the heartbeat is stale OR a registered board is a real
discovery failure (never succeeded but HAS failed, or last success older than
2x interval). A board that has never been attempted yet (no success AND no
failure) is configuration-in-progress, not failure — live lesson L-0015: the
old never-polled→503 rule made Fly's check fail and the proxy stopped routing
the whole site. Zero boards likewise stays 200 with an explicit note.
"""

import time


class Heartbeat:
    """Thread-safe monotonic heartbeat timestamp (wall clock, seconds)."""

    def __init__(self):
        self._lock = __import__("threading").Lock()
        self._at = time.time()

    def touch(self, age_s: float = 0.0):
        """Record a beat now; age_s backdates it (test seam only)."""
        with self._lock:
            self._at = time.time() - age_s

    def age(self) -> float:
        with self._lock:
            return time.time() - self._at


def health_summary(store, heartbeat, interval: int, now=None):
    """Return (http_status, payload) for /healthz."""
    now = time.time() if now is None else now
    payload = {
        "status": "ok",
        "heartbeat_age_s": round(heartbeat.age(), 1),
        "boards_registered": 0,
        "stale_boards": [],
        "pending_first_poll": [],
    }
    status = 200

    if heartbeat.age() > 2 * interval:
        status = 503
        payload["status"] = "degraded"
        payload["heartbeat"] = "stale"
        return status, payload

    for row in store.list_sources():
        payload["boards_registered"] += 1
        if row["last_success_at"] is None:
            if row["last_failure_at"] is None:
                # Never attempted yet: pending, not failing (L-0015).
                payload["pending_first_poll"].append(row["id"])
                continue
            payload["stale_boards"].append(
                {"id": row["id"], "last_success_at": None, "failed": True}
            )
            continue
        if now - row["last_success_at"] > 2 * interval:
            payload["stale_boards"].append(
                {
                    "id": row["id"],
                    "last_success_at": row["last_success_at"],
                    "age_s": round(now - row["last_success_at"], 1),
                }
            )

    if payload["stale_boards"]:
        status = 503
        payload["status"] = "degraded"

    if payload["boards_registered"] == 0:
        # [inference — operator sign-off queued, L-0006] zero boards = unconfigured,
        # not failed: 200 with an explicit note keeps the first deploy healthy.
        payload["note"] = (
            "no sources registered — discovery has not been configured yet;"
            " register boards in Settings"
        )
    return status, payload
