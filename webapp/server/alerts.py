"""Delivery-phase outbox builder: P3 alert gate, P10 kind closure, S1 budget.

Pinned shapes (caller contract):
- Liveness arrives per record as the boolean record["liveness_ok"], set by the
  caller's liveness tier before this module runs. A missing key or any value
  other than the boolean True is "not verified" and blocks the alert.
- Gate decision, returned for observability (never raised):
      {"first_seen", "within_window", "liveness_ok", "admitted"}
  admitted = first-seen AND within-window AND liveness-verified; first-seen
  means raw_id not in the caller-supplied seen_raw_ids, the window is the
  3600 s bootstrap window (novelty), and "new" never claims
  original-publication proof (a2).
- Payload (the only representable message):
      {"kind": "job_alert", "job_id", "title", "employer", "url",
       "alert_text", "reservation"}
  job_id is the record's raw_id (the within-vendor unseen identity); the
  caller keeps (source_id, job_id) uniqueness. reservation is the verbatim
  return of the budget's alert reservation.
- Budget (S1): every emission reserves one alert slot through the injected
  Budget before the payload is constructed, so emission without a reservation
  is impossible; BudgetError propagates to the caller. day_key is
  caller-chosen (a fresh UTC date string per day, budget.py contract) and now
  is the injected epoch-seconds instant — this module keeps no clock, db, or
  network of its own.
- Message kinds (P10): the closed vocabulary is ("job_alert",). Any other
  kind — including any digest kind — is unrepresentable and raises AlertError;
  no digest builder exists in this module.
- Claims (P6): alert_text is composed exclusively from claims.render outputs;
  unknown claim ids raise claims.ClaimError; no bare string can reach a
  payload.
"""

from server import claims, novelty


MESSAGE_KINDS = ("job_alert",)


class AlertError(ValueError):
    """A message that cannot be represented in this module (P10)."""


def seed_system_claims(registry):
    """Register the pinned system claims at boot (P6: every rendered
    quantitative string must come from the registry, annotated)."""
    registry.add(
        "first-hour",
        "first-hour job discovery",
        "operator intent (2026-10-16 request)",
        "operator-stated",
        "2026-10-16",
    )


def gate_record(record, seen_raw_ids, now):
    """The P3 alert gate: first-seen AND within-window AND liveness-verified."""
    first_seen = record["raw_id"] not in seen_raw_ids
    within_window = novelty.within_bootstrap_window(record, now)
    liveness_ok = record.get("liveness_ok") is True
    return {
        "first_seen": first_seen,
        "within_window": within_window,
        "liveness_ok": liveness_ok,
        "admitted": first_seen and within_window and liveness_ok,
    }


class AlertBuilder:
    """Builds alert payloads for ingestion summaries' new_records."""

    def __init__(self, budget):
        self._budget = budget

    def build_alert(self, record, registry, claim_ids, day_key, now, seen_raw_ids):
        """Gate, render, reserve, then emit one job_alert payload (or None).

        Returns {"gate": decision, "alert": payload-or-None}; a blocked record
        emits nothing and consumes no budget slot. BudgetError from the
        reservation propagates so the caller catches it.
        """
        decision = gate_record(record, seen_raw_ids, now)
        if not decision["admitted"]:
            return {"gate": decision, "alert": None}
        if not claim_ids:
            raise AlertError("a job alert requires at least one claim id")
        alert_text = "\n".join(
            claims.render(registry, claim_id) for claim_id in claim_ids
        )
        reservation = self._budget.reserve_alert(day_key, now)
        alert = {
            "kind": "job_alert",
            "job_id": record["raw_id"],
            "title": record["title"],
            "employer": record["employer"],
            "url": record["url"],
            "alert_text": alert_text,
            "reservation": reservation,
        }
        return {"gate": decision, "alert": alert}

    def build_message(
        self, kind, record, registry, claim_ids, day_key, now, seen_raw_ids
    ):
        """The closed-kind entry: only "job_alert" is representable (P10)."""
        if kind not in MESSAGE_KINDS:
            raise AlertError(f"unrepresentable message kind: {kind!r}")
        return self.build_alert(
            record, registry, claim_ids, day_key, now, seen_raw_ids
        )

    def build_summary_alerts(
        self, summary, registry, claim_ids, day_key, now, seen_raw_ids
    ):
        """One build_alert per record in summary["new_records"]; decisions are
        observable per record, order preserved."""
        return [
            self.build_alert(record, registry, claim_ids, day_key, now, seen_raw_ids)
            for record in summary["new_records"]
        ]
