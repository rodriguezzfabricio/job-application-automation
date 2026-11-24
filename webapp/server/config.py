"""Pinned configuration constants (knowledge/README.md — do not re-derive)."""

import os

__version__ = "0.1.0"

# Polling (a1): 900 s cadence, jitter +0..180 s, serialized.
POLL_INTERVAL = 900
JITTER_MAX = 180

# Health thresholds (a8 F5): /healthz 503 at heartbeat > 2x interval (alerting only);
# watchdog force-exit at > 3x interval (separate layer, not in the skeleton yet).
HEARTBEAT_ALERT_AGE = 2 * POLL_INTERVAL
WATCHDOG_KILL_AGE = 3 * POLL_INTERVAL

# Per-board discovery health uses the same 2x-interval window as the heartbeat.
BOARD_STALE_AGE = 2 * POLL_INTERVAL

# a2: one shared staleness constant for Sources-row chips: interval + 900
# (= 1800 s at the pinned cadence) — replaces the as-built hardcoded 2700 s.
BOARD_STALE_AFTER = POLL_INTERVAL + 900

DB_PATH = os.environ.get("DB_PATH", "/data/app.sqlite")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")
BACKUP_ENABLED = os.environ.get("BACKUP_ENABLED", "1") == "1"

# Alert tier (P3/S1): per-cycle liveness cap keeps the Q39 tick budget safe;
# the alert text renders the seeded system claim (P6).
ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "1") == "1"
MAX_LIVENESS_CHECKS_PER_CYCLE = 5
LIVENESS_TIMEOUT_S = 10
ALERT_CLAIM_ID = "first-hour"
PORT = int(os.environ.get("PORT", "8000"))
POLLER_ENABLED = os.environ.get("POLLER_ENABLED", "1") == "1"

FETCH_TIMEOUT_S = 20  # a1: 20 s GET deadline (E9 P2 measured 0.64 s typical).

# Politeness (a1, pinned): pause after 5 consecutive 429/403; one auto-probe
# after max(6 h, server Retry-After); backoff = min(86400, interval ×
# 2**min(N−1,5)). The 86400 cap binds ONLY the self-computed term — a server
# Retry-After is honored in full, unclamped.
PAUSE_AFTER_429 = 5
PROBE_AFTER_S = 6 * 3600
BACKOFF_CAP_S = 86400
BACKOFF_EXPONENT_CAP = 5

# Interim write-auth until a11 sessions land (L-0010): every write endpoint
# requires `Authorization: Bearer <SETUP_TOKEN>`.
SETUP_TOKEN = os.environ.get("SETUP_TOKEN", "")
