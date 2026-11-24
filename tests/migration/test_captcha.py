"""The CAPTCHA protocol helper that codex/stages/CAPTCHA.md tells the worker to call.

The doc ships commands and receipt fields (captchas_seen/resolved/handed_off) but the
tool itself was never written, so this file pins its contract.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'stopgap-tools'))
import captcha  # noqa: E402
from job_runtime.policy import effective, digest  # noqa: E402

FIELDS = dict(run_id='r1', stage='fill', host='careers.example.com', provider='turnstile',
              action='clicked', outcome='passed', attempts=1, elapsed_seconds=12)


class CaptchaRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = str(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, **kw):
        return captcha.record(self.private, **dict(FIELDS, **kw))

    def lines(self):
        p = Path(self.private) / 'codex-runtime' / 'captcha' / 'events.jsonl'
        if not p.is_file():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def test_record_appends_and_returns_citable_refs(self):
        out = self.rec(note='checkbox passed')
        second = self.rec(run_id='r2', outcome='handoff', action='handed_off')
        self.assertEqual(len(self.lines()), 2)
        self.assertTrue(out['refs'])
        self.assertIn('turnstile', ' '.join(out['refs']))
        self.assertEqual(self.lines()[0]['note'], 'checkbox passed')
        self.assertNotEqual(out['refs'], second['refs'])

    def test_unknown_action_or_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            self.rec(action='solved_captcha')
        with self.assertRaises(ValueError):
            self.rec(outcome='ok')
        self.assertEqual(self.lines(), [])

    def test_token_observed_is_recorded_only_when_true(self):
        self.rec(token_observed=True)
        self.assertFalse(self.rec()['token_observed'])
        self.assertTrue(self.lines()[0]['token_observed'])


class CaptchaSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = str(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, **kw):
        captcha.record(self.private, **dict(FIELDS, **kw))

    def test_summary_counts_one_record_per_challenge_and_ignores_other_runs(self):
        self.rec(outcome='passed')                            # r1 resolved
        self.rec(action='detected', outcome='detected_only')  # r1 detected, never clicked
        self.rec(action='handed_off', outcome='handoff')      # r1 left for him
        self.rec(action='blocked', outcome='blocked')         # r1 host blocked
        self.rec(run_id='other', outcome='passed')            # a different run
        s = captcha.summary(self.private, 'r1')
        self.assertEqual((s['captchas_seen'], s['captchas_resolved'], s['captchas_handed_off']),
                         (4, 1, 1))

    def test_summary_of_an_untouched_run_is_all_zeros(self):
        s = captcha.summary(self.private, 'never')
        self.assertEqual(s, {'run_id': 'never', 'captchas_seen': 0, 'captchas_resolved': 0,
                             'captchas_handed_off': 0})


class CaptchaPolicyTests(unittest.TestCase):
    """The stage reads the effective bounds once, from the run's stored policy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'tracker.sqlite'
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE jr_runs(id TEXT PRIMARY KEY,policy TEXT)")
        self.policy = effective({'captcha_policy': 'checkbox', 'captcha_max_attempts': 3,
                                 'captcha_budget_seconds': 30, 'captcha_max_per_run': 2})
        con.execute("INSERT INTO jr_runs VALUES('r1',?)", (json.dumps(self.policy),))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_policy_comes_from_the_run_not_from_defaults(self):
        p = captcha.policy(str(self.db), 'r1')
        self.assertEqual(p['captcha_policy'], 'checkbox')
        self.assertEqual(p['captcha_max_attempts'], 3)
        self.assertEqual(p['captcha_budget_seconds'], 30)
        self.assertEqual(p['captcha_max_per_run'], 2)
        self.assertEqual(p['policy_hash'], self.policy['hash'])

    def test_unknown_run_is_an_error_not_a_silent_default(self):
        with self.assertRaises(ValueError):
            captcha.policy(str(self.db), 'missing')


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (CaptchaRecordTests, CaptchaSummaryTests, CaptchaPolicyTests):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith('test_'))
    return suite
