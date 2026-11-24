"""Alert outbox-builder RED tests — delivery phase (P3 gate + P10 kinds + S1 budget + P6 claims).

Pins under test — knowledge/a2.md (alert gate), knowledge/a5.md (closed message
kinds, send budget), knowledge/a8.md + server/budget.py (S1 reservations),
knowledge/a10.md + server/claims.py (P6 registry-only rendering):

- P3: the alert gate is first-seen AND within-window AND liveness-verified;
  a record failing any condition never produces an alert and the gate decision
  is returned for observability.
- P10: the only representable message kind is "job_alert"; any other kind —
  including "daily_digest" — raises AlertError, and no digest builder exists.
- S1: every emission passes through budget.reserve_alert(day_key, now) BEFORE
  the payload is constructed; at cap, BudgetError propagates and no payload is
  returned; blocked records never reserve.
- P6: alert_text is composed exclusively from claims.render output; unknown
  claim ids raise ClaimError; no bare-string path exists in the module.
- Payload shape pinned exactly:
  {"kind", "job_id", "title", "employer", "url", "alert_text", "reservation"}.

A GREEN on any of these before the module existed is itself a failure.
"""

import inspect
import unittest

from server import alerts, claims
from server.budget import Budget, BudgetError

# A fixed epoch-seconds instant; the module has no clock of its own and every
# call site injects `now` (contract: caller injects now).
NOW = 1_700_000_000.0
DAY_KEY = "2026-10-20"


def make_record(**overrides):
    """A canonical envelope-style job record inside the bootstrap window."""
    record = {
        "source_id": 1,
        "raw_id": "ashby-abc",
        "title": "Software Engineer I",
        "employer": "Acme Robotics",
        "location": "Remote (US)",
        "url": "https://jobs.acme.example/ashby-abc",
        "first_published_field": "publishedAt",
        "first_published_epoch_s": NOW - 100,
        "fetched_at_epoch_s": NOW,
        "raw": {},
        # Pinned liveness shape: the caller's liveness tier sets this boolean
        # before the builder runs; missing or non-True means "not verified".
        "liveness_ok": True,
    }
    record.update(overrides)
    return record


def make_registry():
    registry = claims.Registry()
    registry.add(
        "latency",
        "<=25 min detection to send-accepted",
        "IRON_LAW §12",
        "design-target",
        "2026-10-20",
    )
    return registry


class AlertGateTests(unittest.TestCase):
    """P3: first-seen AND within-window AND liveness-verified."""

    def test_gate_blocks_not_first_seen(self):
        record = make_record()
        decision = alerts.gate_record(record, seen_raw_ids={"ashby-abc"}, now=NOW)
        self.assertEqual(
            decision,
            {
                "first_seen": False,
                "within_window": True,
                "liveness_ok": True,
                "admitted": False,
            },
        )

    def test_gate_blocks_outside_bootstrap_window(self):
        # Age 3601 s exceeds the 3600 s bootstrap window (a2/novelty pin).
        record = make_record(first_published_epoch_s=NOW - 3601)
        decision = alerts.gate_record(record, seen_raw_ids=set(), now=NOW)
        self.assertFalse(decision["within_window"])
        self.assertTrue(decision["first_seen"])
        self.assertTrue(decision["liveness_ok"])
        self.assertFalse(decision["admitted"])

    def test_gate_blocks_future_first_published_signal(self):
        record = make_record(first_published_epoch_s=NOW + 5)
        decision = alerts.gate_record(record, seen_raw_ids=set(), now=NOW)
        self.assertFalse(decision["within_window"])
        self.assertFalse(decision["admitted"])

    def test_gate_blocks_missing_first_published_signal(self):
        record = make_record(first_published_epoch_s=None)
        decision = alerts.gate_record(record, seen_raw_ids=set(), now=NOW)
        self.assertFalse(decision["within_window"])
        self.assertFalse(decision["admitted"])

    def test_gate_blocks_liveness_false(self):
        record = make_record(liveness_ok=False)
        decision = alerts.gate_record(record, seen_raw_ids=set(), now=NOW)
        self.assertFalse(decision["liveness_ok"])
        self.assertTrue(decision["first_seen"])
        self.assertTrue(decision["within_window"])
        self.assertFalse(decision["admitted"])

    def test_gate_blocks_missing_liveness_key(self):
        # The pinned liveness shape is the boolean record["liveness_ok"]; a
        # missing key is "not verified", never silently verified.
        record = make_record()
        del record["liveness_ok"]
        decision = alerts.gate_record(record, seen_raw_ids=set(), now=NOW)
        self.assertFalse(decision["liveness_ok"])
        self.assertFalse(decision["admitted"])

    def test_gate_admits_when_all_three_hold(self):
        decision = alerts.gate_record(make_record(), seen_raw_ids=set(), now=NOW)
        self.assertEqual(
            decision,
            {
                "first_seen": True,
                "within_window": True,
                "liveness_ok": True,
                "admitted": True,
            },
        )


class MessageKindClosureTests(unittest.TestCase):
    """P10: the vocabulary is exactly ("job_alert",)."""

    def setUp(self):
        self.builder = alerts.AlertBuilder(Budget())

    def test_message_kind_vocabulary_is_closed(self):
        self.assertEqual(alerts.MESSAGE_KINDS, ("job_alert",))

    def test_non_alert_kind_raises_alert_error(self):
        with self.assertRaises(alerts.AlertError):
            self.builder.build_message(
                "application",
                make_record(),
                make_registry(),
                ["latency"],
                DAY_KEY,
                NOW,
                set(),
            )

    def test_daily_digest_kind_raises_alert_error(self):
        # A digest kind is refused the same way: it does not exist here.
        with self.assertRaises(alerts.AlertError):
            self.builder.build_message(
                "daily_digest",
                make_record(),
                make_registry(),
                ["latency"],
                DAY_KEY,
                NOW,
                set(),
            )

    def test_no_digest_builder_exists_in_module(self):
        # The kind must NOT exist: no digest constructor path, only refusal.
        self.assertFalse(hasattr(alerts.AlertBuilder, "build_digest"))
        self.assertNotIn("build_digest", inspect.getsource(alerts))

    def test_refused_kind_never_reserves(self):
        budget = Budget()
        builder = alerts.AlertBuilder(budget)
        with self.assertRaises(alerts.AlertError):
            builder.build_message(
                "daily_digest",
                make_record(),
                make_registry(),
                ["latency"],
                DAY_KEY,
                NOW,
                set(),
            )
        self.assertEqual(budget.remaining_alerts(DAY_KEY), 50)

    def test_build_message_job_alert_delegates_to_build_alert(self):
        outcome = self.builder.build_message(
            "job_alert",
            make_record(),
            make_registry(),
            ["latency"],
            DAY_KEY,
            NOW,
            set(),
        )
        self.assertEqual(outcome["alert"]["kind"], "job_alert")


class AlertEmissionTests(unittest.TestCase):
    """Gate -> reserve -> render -> emit, with the S1 budget as the only path."""

    def setUp(self):
        self.budget = Budget()
        self.builder = alerts.AlertBuilder(self.budget)

    def test_gate_pass_emits_exactly_one_alert(self):
        outcome = self.builder.build_alert(
            make_record(),
            make_registry(),
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        self.assertTrue(outcome["gate"]["admitted"])
        self.assertIsNotNone(outcome["alert"])
        self.assertEqual(outcome["alert"]["kind"], "job_alert")
        # Exactly one alert = exactly one reservation for one admitted record.
        self.assertEqual(self.budget.remaining_alerts(DAY_KEY), 49)

    def test_blocked_record_never_emits_nor_reserves(self):
        record = make_record(liveness_ok=False)
        outcome = self.builder.build_alert(
            record,
            make_registry(),
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        self.assertFalse(outcome["gate"]["admitted"])
        self.assertEqual(outcome["gate"]["liveness_ok"], False)
        self.assertIsNone(outcome["alert"])
        self.assertEqual(self.budget.remaining_alerts(DAY_KEY), 50)

    def test_emission_without_reservation_impossible(self):
        # A budget already at cap: reserve raises, BudgetError propagates, and
        # no payload is returned (the only payload site is after the reserve).
        capped = Budget(max_alerts_per_day=0)
        builder = alerts.AlertBuilder(capped)
        with self.assertRaises(BudgetError):
            builder.build_alert(
                make_record(),
                make_registry(),
                ["latency"],
                DAY_KEY,
                NOW,
                seen_raw_ids=set(),
            )
        self.assertEqual(capped.remaining_alerts(DAY_KEY), 0)

    def test_reservation_precedes_payload_construction_structurally(self):
        # S1 pin, greppable: exactly one reserve site in the module, and it
        # sits before the only alert-payload literal, so no code path can
        # construct a payload without reserving first. The payload-shape
        # docstring mentions the literal by design, so probe the build_alert
        # body only.
        source = inspect.getsource(alerts)
        self.assertEqual(source.count("reserve_alert("), 1)
        body = source.split("def build_alert")[1]
        self.assertLess(
            body.index("reserve_alert("),
            body.index('"kind": "job_alert"'),
        )

    def test_alert_text_renders_only_via_claims_render(self):
        registry = make_registry()
        outcome = self.builder.build_alert(
            make_record(),
            registry,
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        self.assertEqual(
            outcome["alert"]["alert_text"],
            claims.render(registry, "latency"),
        )
        # P6 pin, greppable: one render site, and no bare-string path exists.
        source = inspect.getsource(alerts)
        self.assertEqual(source.count("claims.render("), 1)
        self.assertNotIn("render_bare", source)

    def test_unknown_claim_id_raises(self):
        registry = make_registry()
        with self.assertRaises(claims.ClaimError):
            self.builder.build_alert(
                make_record(),
                registry,
                ["missing_claim"],
                DAY_KEY,
                NOW,
                seen_raw_ids=set(),
            )

    def test_empty_claim_ids_refused(self):
        # An alert's quantitative text must come from the registry; zero
        # claims would render an empty alert_text, so it is refused up front.
        with self.assertRaises(alerts.AlertError):
            self.builder.build_alert(
                make_record(),
                make_registry(),
                [],
                DAY_KEY,
                NOW,
                seen_raw_ids=set(),
            )

    def test_payload_shape_exact_match(self):
        registry = make_registry()
        outcome = self.builder.build_alert(
            make_record(),
            registry,
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        expected = {
            "kind": "job_alert",
            "job_id": "ashby-abc",
            "title": "Software Engineer I",
            "employer": "Acme Robotics",
            "url": "https://jobs.acme.example/ashby-abc",
            "alert_text": claims.render(registry, "latency"),
            "reservation": {"kind": "alert", "day_key": DAY_KEY, "at": NOW},
        }
        self.assertEqual(outcome["alert"], expected)
        self.assertEqual(
            sorted(outcome["alert"].keys()),
            sorted(
                [
                    "kind",
                    "job_id",
                    "title",
                    "employer",
                    "url",
                    "alert_text",
                    "reservation",
                ]
            ),
        )


class SummaryEmissionTests(unittest.TestCase):
    """The builder consumes ingestion summaries' new_records (a2/ingest shape)."""

    def setUp(self):
        self.budget = Budget()
        self.builder = alerts.AlertBuilder(self.budget)

    def test_summary_emits_only_admitted_records(self):
        summary = {
            "new_records": [
                make_record(),
                make_record(raw_id="ashby-ghost", liveness_ok=False),
            ]
        }
        outcomes = self.builder.build_summary_alerts(
            summary,
            make_registry(),
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        self.assertEqual(len(outcomes), 2)
        self.assertIsNotNone(outcomes[0]["alert"])
        self.assertIsNone(outcomes[1]["alert"])
        self.assertFalse(outcomes[1]["gate"]["admitted"])
        # Exactly one alert for two records: one reservation consumed.
        self.assertEqual(self.budget.remaining_alerts(DAY_KEY), 49)

    def test_summary_all_blocked_emits_nothing_and_never_reserves(self):
        summary = {
            "new_records": [
                make_record(raw_id="a", liveness_ok=False),
                make_record(raw_id="b", first_published_epoch_s=NOW - 9999),
            ]
        }
        outcomes = self.builder.build_summary_alerts(
            summary,
            make_registry(),
            ["latency"],
            DAY_KEY,
            NOW,
            seen_raw_ids=set(),
        )
        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.assertIsNone(outcome["alert"])
            self.assertFalse(outcome["gate"]["admitted"])
        self.assertEqual(self.budget.remaining_alerts(DAY_KEY), 50)


if __name__ == "__main__":
    unittest.main()
