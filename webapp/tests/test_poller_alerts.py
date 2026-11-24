"""RED tests — poller alert pass (P3 gate + S1 budget + outbox persistence).

After a successful poll, the alert tier runs: per new record (capped), a
liveness check stamps record["liveness_ok"], the AlertBuilder gates and
reserves, and admitted alerts persist to the outbox. BudgetError from the
reservation stops the pass without crashing the poller.
"""

import os
import tempfile
import unittest

from server import alerts, budget, claims, db, poller


NOW = 1_700_000_000.0


def make_record(raw_id="uuid-1", liveness=True):
    return {
        "raw_id": raw_id,
        "title": "Backend Engineer",
        "employer": "Acme",
        "url": f"https://jobs.acme.example/{raw_id}",
        "first_published_epoch_s": NOW - 100,
        "liveness_ok": liveness,
    }


def fake_liveness(record, source_row, transport, now):
    """Test double: liveness is whatever the record was seeded with."""
    return {
        "liveness_ok": record["liveness_ok"],
        "evidence_level": "reduced",
        "checks": {},
        "observed_at": now,
    }


def fake_transport_factory(careers_url):
    return object()  # fake_liveness ignores the transport


class PollerAlertPass(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)
        self.store.add_source({
            "id": "ashby:acme", "name": "Acme", "kind": "ashby",
            "host": "api.ashbyhq.com",
            "feed_url": "https://api.ashbyhq.com/posting-api/job-board/acme",
            "careers_url": "https://jobs.ashbyhq.com/acme",
        })
        self.row = self.store.get_source("ashby:acme")
        self.registry = claims.Registry()
        alerts.seed_system_claims(self.registry)
        self.builder = alerts.AlertBuilder(budget.Budget())

    def _run(self, records):
        summary = {"new_records": records, "total_parsed": len(records),
                   "updated_count": 0, "possible_stale": [],
                   "republish_candidates": [], "employer": "Acme"}
        return poller.run_alert_pass(
            self.store, self.builder, self.registry, ["first-hour"],
            fake_transport_factory, fake_liveness,
            self.row, summary, NOW)

    def test_admitted_alert_persists_to_outbox(self):
        created = self._run([make_record()])
        self.assertEqual(created, 1)
        rows = self.store.list_unsent_alerts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "uuid-1")
        self.assertEqual(rows[0]["source_id"], "ashby:acme")
        self.assertIn("[operator-stated — ", rows[0]["alert_text"])

    def test_ghost_record_never_reaches_outbox(self):
        created = self._run([make_record(liveness=False)])
        self.assertEqual(created, 0)
        self.assertEqual(self.store.list_unsent_alerts(), [])

    def test_liveness_checks_capped_per_cycle(self):
        calls = []

        def counting_liveness(record, source_row, transport, now):
            calls.append(record["raw_id"])
            return fake_liveness(record, source_row, transport, now)

        summary = {"new_records": [make_record(f"uuid-{i}") for i in range(6)],
                   "total_parsed": 6, "updated_count": 0, "possible_stale": [],
                   "republish_candidates": [], "employer": "Acme"}
        poller.run_alert_pass(
            self.store, self.builder, self.registry, ["first-hour"],
            fake_transport_factory, counting_liveness,
            self.row, summary, NOW)
        self.assertEqual(len(calls), 5)  # MAX_LIVENESS_CHECKS_PER_CYCLE

    def test_budget_exhaustion_does_not_crash(self):
        builder = alerts.AlertBuilder(budget.Budget(max_alerts_per_day=0))
        summary = {"new_records": [make_record()], "total_parsed": 1,
                   "updated_count": 0, "possible_stale": [],
                   "republish_candidates": [], "employer": "Acme"}
        created = poller.run_alert_pass(
            self.store, builder, self.registry, ["first-hour"],
            fake_transport_factory, fake_liveness,
            self.row, summary, NOW)
        self.assertEqual(created, 0)
        self.assertEqual(self.store.list_unsent_alerts(), [])


if __name__ == "__main__":
    unittest.main()
