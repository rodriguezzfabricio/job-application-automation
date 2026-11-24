"""Budget guardrails — a8 pins (knowledge/a8.md, IRON_LAW §14.6, scar P2/S1).

Enforced here:
- ≤50 job alerts + 1 digest per DAY as ATOMIC in-transport reservations
  (scar S1 alert-fatigue prevention, P2 budget): two threads racing the last
  slot → exactly one succeeds.
- reserve-before-spend ledger: reservation is atomic, balance never negative.

Not enforced here (by design):
- Hosting keeps billing while the kill switch is armed — nothing in this
  module changes host billing; the kill banner says so.
- Persistence: this module is pure + thread-safe for a single process; the
  caller persists day counters and the ledger. A new day MUST use a new
  day_key (caller keys by date string) — `reset_day` exists for explicit
  clears, never for reusing a key.
"""

import threading


class BudgetError(Exception):
    """Raised when a reservation would exceed a day cap or the spend balance."""


class Budget:
    """Atomic per-day caps for job alerts and digests.

    day_key is caller-chosen (e.g. a date string). A new day = a new key;
    counters under one key never expire on their own.
    """

    def __init__(self, max_alerts_per_day=50, max_digests_per_day=1):
        self._max_alerts = max_alerts_per_day
        self._max_digests = max_digests_per_day
        self._lock = threading.Lock()
        # day_key -> [alerts_used, digests_used]
        self._days = {}

    def reserve_alert(self, day_key, now):
        """Atomically reserve one alert slot for day_key; BudgetError at the cap."""
        with self._lock:
            used = self._days.setdefault(day_key, [0, 0])
            if used[0] >= self._max_alerts:
                raise BudgetError(
                    f"alert cap reached for {day_key}: {used[0]}/{self._max_alerts}"
                )
            used[0] += 1
            return {"kind": "alert", "day_key": day_key, "at": now}

    def reserve_digest(self, day_key, now):
        """Atomically reserve the one daily digest slot; BudgetError at the cap."""
        with self._lock:
            used = self._days.setdefault(day_key, [0, 0])
            if used[1] >= self._max_digests:
                raise BudgetError(
                    f"digest cap reached for {day_key}: {used[1]}/{self._max_digests}"
                )
            used[1] += 1
            return {"kind": "digest", "day_key": day_key, "at": now}

    def remaining_alerts(self, day_key):
        with self._lock:
            return self._max_alerts - self._days.get(day_key, [0, 0])[0]

    def reset_day(self, day_key):
        """Clear day_key's counters. Prefer a fresh key for a fresh day."""
        with self._lock:
            self._days.pop(day_key, None)


class SpendLedger:
    """Reserve-before-spend ledger for the DeepSeek path (a8 R6/R7/R8).

    v1 pin: 20,000 µ$ per-call reservation ceiling — the request builder
    proves each call bills ≤15,000 µ$ at peak prices, so the 20,000 µ$
    reservation carries margin; the monthly settled-spend cap is
    1,500,000 µ$ and is enforced by the caller's persistence layer, not
    here. Reservation is atomic under the lock: balance never goes
    negative.
    """

    def __init__(self, balance_usd_micros: int):
        self._balance = balance_usd_micros
        self._lock = threading.Lock()
        self._next_id = 0
        # reservation_id -> amount_usd_micros still reserved
        self._reservations = {}

    def reserve_spend(self, amount_usd_micros):
        """Atomically set aside amount; BudgetError if it exceeds the balance."""
        with self._lock:
            if amount_usd_micros > self._balance:
                raise BudgetError(
                    f"reservation of {amount_usd_micros} exceeds balance {self._balance}"
                )
            rid = self._next_id
            self._next_id += 1
            self._reservations[rid] = amount_usd_micros
            self._balance -= amount_usd_micros
            return rid

    def settle(self, reservation_id, actual_usd_micros):
        """Release the unused portion back, atomically. Unknown or over-actual
        settles are refused (BudgetError) so accounting can never be double
        released or silently absorbed."""
        with self._lock:
            if reservation_id not in self._reservations:
                raise BudgetError(f"unknown reservation {reservation_id}")
            reserved = self._reservations[reservation_id]
            if actual_usd_micros > reserved:
                raise BudgetError(
                    f"settle {actual_usd_micros} exceeds reservation {reserved}"
                )
            del self._reservations[reservation_id]
            self._balance += reserved - actual_usd_micros

    def balance_usd_micros(self):
        with self._lock:
            return self._balance
