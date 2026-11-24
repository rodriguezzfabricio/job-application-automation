"""Scar RED tests — the implementable subset of the 13 scar protocols.

Each test below pins the STRUCTURAL prevention named by its scar. A missing
prevention fails the test (RED); a present prevention passes. P2 (mail
transport refuses unauthenticated sender), P10 (mail capability limited to
alert kinds) and P11 (profile storage requires encryption key) are Phase 4/5
work and intentionally have no test here yet — recorded in the Ledger, not
silently skipped.
"""

import os
import re
import tempfile
import unittest

from server import alerts, budget, claims, db, exporter, factstore, score, sources


class P1Allowlist(unittest.TestCase):
    def test_platform_transport_allowlist_blocks_linkedin(self):
        with self.assertRaises(sources.AllowlistViolation):
            sources.build_transport("https://www.linkedin.com/jobs/view/1")
        with self.assertRaises(sources.AllowlistViolation):
            sources.build_transport("https://indeed.com/viewjob?jk=abc")


class P3AlertGate(unittest.TestCase):
    def test_alert_gate_blocks_bumped_or_ghost_posting(self):
        record = {
            "raw_id": "r1", "title": "Engineer", "employer": "Acme",
            "url": "https://jobs.acme.example/r1",
            "first_published_epoch_s": 1_700_000_000.0,
            "liveness_ok": True,
        }
        now = 1_700_000_000.0
        # Bumped (re-published same raw_id) -> first_seen False -> blocked.
        bumped = alerts.gate_record(record, {"r1"}, now)
        self.assertFalse(bumped["admitted"])
        # Ghost (liveness not verified) -> blocked even when unseen and fresh.
        ghost = dict(record, liveness_ok=False)
        self.assertFalse(alerts.gate_record(ghost, set(), now)["admitted"])


class P4CardinalSin(unittest.TestCase):
    def test_resume_claim_without_source_evidence_refused(self):
        store = factstore.FactStore()
        fact_id = store.add_fact(
            "Built a job-discovery service with Python.", "operator resume",
            "operator-stated",
        )
        # Verbatim reuse binds; free rewording does not (cardinal sin guard).
        ok = factstore.compose_claim(
            store, "Built a job-discovery service", [fact_id])
        self.assertEqual(ok["text"], "Built a job-discovery service")
        with self.assertRaises(factstore.FactError):
            factstore.compose_claim(
                store, "Built an automated job-search platform", [fact_id])


class P5Reasons(unittest.TestCase):
    def test_verdict_without_reasons_refused(self):
        with self.assertRaises(score.SchemaError):
            score._build_result(50.0, [], "entry_ai", "eligible")


class P6Renderer(unittest.TestCase):
    def test_product_claim_renderer_refuses_unsourced_stat(self):
        registry = claims.Registry()
        registry.add("latency", "<=25 min", "IRON_LAW", "design-target", "2026-10-20")
        with self.assertRaises(claims.ClaimError):
            claims.render_bare("99% uptime")
        # And the only emission path annotates provenance.
        self.assertIn("[design-target", claims.render(registry, "latency"))


class P7NoSubmission(unittest.TestCase):
    def test_no_submission_code_path_exists(self):
        # The egress table (a7): the ONLY permitted POST targets are DeepSeek
        # job-text, Google token, Google userinfo, Gmail send. Anything else
        # must be a GET — and no apply/submit route exists in the server.
        egress_hosts = {
            "api.deepseek.com", "oauth2.googleapis.com",
            "openidconnect.googleapis.com", "gmail.googleapis.com",
        }
        server_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
        violations = []
        for name in sorted(os.listdir(server_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(server_dir, name), encoding="utf-8") as fh:
                text = fh.read()
            if re.search(r"(?:submit|apply)[-_ ]application", text, re.I):
                violations.append(f"{name}: submission route text")
            for match in re.finditer(
                r"Request\(\s*([^,)]+)(?:,\s*data=([^)]*))?", text
            ):
                url_expr = match.group(1)
                has_data = match.group(2) is not None
                if has_data and not any(h in url_expr for h in egress_hosts):
                    violations.append(f"{name}: POST-shaped request {url_expr}")
        self.assertEqual(violations, [])


class P8WorkAuth(unittest.TestCase):
    def test_work_auth_classifier_never_guesses(self):
        # Unknown language -> review; only the pinned profile fact -> eligible.
        self.assertEqual(score.work_auth_verdict("needs-review", "no markers"), "review")
        self.assertEqual(score.work_auth_verdict("needs-review", "must be authorized"), "review")
        self.assertEqual(score.work_auth_verdict("eligible", "no markers"), "eligible")
        self.assertEqual(
            score.work_auth_verdict("eligible", "requires US citizenship"),
            "reject-on-explicit-language",
        )


class P9ExportCoverage(unittest.TestCase):
    def test_export_endpoint_covers_every_tracked_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = db.Store(os.path.join(tmp, "t.sqlite"))
            with store.conn() as c:
                app_cols = {r["name"] for r in c.execute("PRAGMA table_info(applications)")}
                hist_cols = {r["name"] for r in c.execute("PRAGMA table_info(status_history)")}
            self.assertEqual(app_cols, set(exporter.APPLICATIONS_COLUMNS))
            self.assertEqual(hist_cols, set(exporter.HISTORY_COLUMNS))


class P12IdempotentApplied(unittest.TestCase):
    def test_applied_action_idempotent_per_canonical_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = db.Store(os.path.join(tmp, "t.sqlite"))
            with store.conn() as c:
                c.execute("INSERT INTO applications (canonical_job_id) VALUES (?)",
                          ("ashby:acme:job-1",))
            self.assertTrue(store.mark_applied("ashby:acme:job-1", now=1_700_000_000.0))
            self.assertFalse(store.mark_applied("ashby:acme:job-1", now=1_700_000_100.0))
            app = store.get_application("ashby:acme:job-1")
            self.assertEqual(app["applied_at"], "2023-11-14T22:13:20Z")


class S1Budget(unittest.TestCase):
    def test_send_budget_enforced(self):
        b = budget.Budget(max_alerts_per_day=2)
        b.reserve_alert("2026-10-21", now="t1")
        b.reserve_alert("2026-10-21", now="t2")
        with self.assertRaises(budget.BudgetError):
            b.reserve_alert("2026-10-21", now="t3")


if __name__ == "__main__":
    unittest.main()
