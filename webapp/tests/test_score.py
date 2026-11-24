"""Scoring & eligibility contract tests (IRON_LAW §9 / §1.1; coverage: eligibility, work-auth).

Pins under test: Layer-1 deal-breakers (citizenship, clearance) gate before scoring;
role buckets (new_grad_swe / entry_ai / excluded_ml); the P8 work-auth ternary never
guesses; 70/30 score bound to non-empty reasons with severity vocabulary.
"""

import unittest

from server import score


class BuildReasonTests(unittest.TestCase):
    def test_build_reason_rejects_unknown_severity(self):
        with self.assertRaises(score.SchemaError):
            score.build_reason("rule.citizenship", "bogus", ["ref"], "text")

    def test_build_reason_shape(self):
        reason = score.build_reason("rule.citizenship", "deal_breaker",
                                    ["marker:us citizenship"], "requires citizenship")
        self.assertEqual(reason["rule_id"], "rule.citizenship")
        self.assertEqual(reason["severity"], "deal_breaker")
        self.assertEqual(reason["evidence_refs"], ["marker:us citizenship"])
        self.assertEqual(reason["text"], "requires citizenship")


class DealbreakerTests(unittest.TestCase):
    def test_analyze_dealbreakers_flags_citizenship(self):
        reasons = score.analyze_dealbreakers("must be a US citizen to apply")
        self.assertTrue(any(r["rule_id"] == "rule.citizenship" for r in reasons))

    def test_citizenship_marker_zeroes_score_and_sets_deal_breaker_verdict(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        result = score.score_job(
            "Entry level software engineer. Requires US citizenship. Python.", profile)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["verdict"], "deal_breaker")
        self.assertTrue(any(
            r["severity"] == "deal_breaker" and r["rule_id"] == "rule.citizenship"
            for r in result["reasons"]))

    def test_clearance_marker_is_deal_breaker(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        result = score.score_job(
            "Entry level engineer, active security clearance required, python.", profile)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["verdict"], "deal_breaker")
        self.assertTrue(any(
            r["rule_id"] == "rule.clearance" for r in result["reasons"]))


class BucketTests(unittest.TestCase):
    def test_ml_engineer_role_is_reject_not_eligible_even_when_skills_match(self):
        profile = {"work_auth": "eligible", "role_bucket": "entry_ai",
                   "skills": ["python", "pytorch"]}
        result = score.score_job(
            "Machine Learning Engineer role: python and pytorch required.", profile)
        self.assertEqual(result["bucket"], "excluded_ml")
        self.assertNotEqual(result["verdict"], "eligible")
        self.assertEqual(result["verdict"], "reject")
        self.assertTrue(any(r["severity"] == "reject" for r in result["reasons"]))

    def test_unknown_bucket_is_review(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        result = score.score_job(
            "Senior staff software engineer, distributed systems.", profile)
        self.assertIsNone(result["bucket"])
        self.assertEqual(result["verdict"], "review")
        self.assertTrue(any(r["severity"] == "review" for r in result["reasons"]))

    def test_ai_markers_classify_entry_ai(self):
        self.assertEqual(score.classify_bucket(
            "AI Software Engineer building agents with tool calling and custom MCPs"),
            "entry_ai")

    def test_entry_level_markers_classify_new_grad_swe(self):
        self.assertEqual(score.classify_bucket("Entry level backend software engineer"),
                         "new_grad_swe")


class WorkAuthTests(unittest.TestCase):
    def test_work_auth_explicit_citizenship_rejects_even_if_profile_eligible(self):
        self.assertEqual(score.work_auth_verdict("eligible", "requires US citizenship"),
                         "reject-on-explicit-language")

    def test_work_auth_needs_review_without_markers_stays_review(self):
        self.assertEqual(score.work_auth_verdict("needs-review",
                                                 "entry level software engineer"),
                         "review")

    def test_work_auth_needs_review_with_sponsorship_ambiguity_stays_review(self):
        self.assertEqual(score.work_auth_verdict("needs-review",
                                                 "must be authorized to work in the US"),
                         "review")

    def test_work_auth_eligible_without_reject_language_is_eligible(self):
        self.assertEqual(score.work_auth_verdict("eligible",
                                                 "entry level software engineer"),
                         "eligible")

    def test_needs_review_profile_never_yields_eligible_verdict(self):
        profile = {"work_auth": "needs-review", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        result = score.score_job("entry level software engineer, python", profile)
        self.assertEqual(result["work_auth"], "review")
        self.assertEqual(result["verdict"], "review")


class ScoreTests(unittest.TestCase):
    def test_score_stays_within_0_100(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        for text in ("entry level software engineer, python",
                     "entry level software engineer, no overlap at all"):
            result = score.score_job(text, profile)
            self.assertGreaterEqual(result["score"], 0.0)
            self.assertLessEqual(result["score"], 100.0)

    def test_score_70_30_golden_math(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python", "sql", "flask", "docker"]}
        result = score.score_job(
            "Entry level software engineer. Strong python and sql required.", profile)
        self.assertEqual(result["bucket"], "new_grad_swe")
        self.assertEqual(result["score"], 65.0)  # 70 * 0.5 + 30 * 1.0
        self.assertEqual(result["verdict"], "eligible")

    def test_internal_builder_refuses_empty_reasons(self):
        with self.assertRaises(score.SchemaError):
            score._build_result(50.0, [], None, "eligible")

    def test_score_job_always_carries_non_empty_reasons(self):
        profile = {"work_auth": "eligible", "role_bucket": "new_grad_swe",
                   "skills": ["python"]}
        result = score.score_job("entry level software engineer, python", profile)
        self.assertEqual(result["verdict"], "eligible")
        self.assertTrue(result["reasons"])


if __name__ == "__main__":
    unittest.main()
