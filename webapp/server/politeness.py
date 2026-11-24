"""Politeness state machine (a1, L-0010).

- backoff = min(86400, interval × 2**min(N−1,5)) for the self-computed term;
  a server Retry-After is honored IN FULL, unclamped (a1).
- Pause after 5 consecutive 429/403; one auto-probe after max(6 h, the last
  server Retry-After).
- Per-host cooldown scope: a Retry-After seen on one board throttles the whole
  host; an early request against an outstanding server deadline is
  non-representable (HostCoolingDown raised before any socket opens).
- Serialization: at most one in-flight GET per host.
"""

import threading
import time

from . import config, sources


class HostCoolingDown(Exception):
    """The host has an outstanding server Retry-After deadline (a1)."""

    def __init__(self, host, until):
        super().__init__(f"host {host} cooling down until {until}")
        self.host = host
        self.until = until


def backoff_seconds(n, interval=config.POLL_INTERVAL):
    """Self-computed backoff for the n-th consecutive 429/403 (a1, golden)."""
    n = max(1, int(n))
    return min(config.BACKOFF_CAP_S, interval * 2 ** min(n - 1, config.BACKOFF_EXPONENT_CAP))


class PoliteFetcher:
    """Gated, serialized fetch + store classification.

    `transport_factory` defaults to the allowlisted sources.build_transport;
    tests inject a fixture factory. The store (db.Store) is the only writer.
    """

    def __init__(self, store, transport_factory=sources.build_transport, clock=time.time):
        self._store = store
        self._transport_factory = transport_factory
        self._clock = clock
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _host_lock(self, host):
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def fetch(self, url, host, timeout_s):
        """One gated fetch. Refused (before any socket) while the host has an
        outstanding server deadline. Double-checked: the deadline is re-read
        inside the per-host lock, because a cooldown recorded while we waited
        for the lock must still stop the socket (a1: early request
        non-representable)."""
        now = self._clock()
        until = self._store.host_cooldown_until(host)
        if until and until > now:
            raise HostCoolingDown(host, until)
        transport = self._transport_factory(url)
        with self._host_lock(host):
            until = self._store.host_cooldown_until(host)
            if until and until > now:
                raise HostCoolingDown(host, until)
            return transport.fetch(timeout_s)

    def poll(self, row, timeout_s):
        """One polite poll of a due board: fetch + classify + store writes.

        Callers decide whether the board is due (poller skips deferred_until,
        check-now refuses) — poll() itself is the raw primitive."""
        host = row["host"]
        status, body, retry_after = self.fetch(row["feed_url"], host, timeout_s)
        if 200 <= status < 300:
            self._store.record_success(row["id"], status=status)
            return status, body
        if status in (429, 403):
            self._store.record_throttled(
                row["id"], host, status, retry_after, self._clock()
            )
        else:
            self._store.record_fetch(row["id"], status=status)
        return status, body
