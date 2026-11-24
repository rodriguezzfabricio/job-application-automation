"""Exercise production packets with mock subscription and quota, no model traffic."""
import datetime as dt
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from job_runtime import judgment, model_routes
from job_runtime.policy import effective
from job_runtime.quota import QuotaUnavailable
from job_runtime.store import Denied, Store


NOW = dt.datetime(2026, 11, 24, 17, 0, tzinfo=dt.timezone.utc)
POSTING = 'https://jobs.example.test/jobs/1234/software-engineer'


def png():
    def chunk(kind, content):
        return struct.pack('!I', len(content)) + kind + content + struct.pack('!I', zlib.crc32(kind + content))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'tEXt', b'comment\0internal metadata') + chunk(b'IDAT', zlib.compress(b'\0\xff\xff\xff'))
            + chunk(b'IEND', b''))


class JudgmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        db = self.root / 'tracker.sqlite'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('CREATE TABLE jobs(job_key,link,company,title,location,status,score,your_call,posted_at)')
            con.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                        ('job', POSTING, 'Fixture', 'Software Engineer', 'United States',
                         'Ready to fill', 88, 'fill now', NOW.isoformat()))
        self.store = Store(str(db), clock=lambda: NOW)
        self.addCleanup(self.store.con.close)
        token = self.store.acquire('worker')
        self.store.con.execute('INSERT INTO jr_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                               ('run', NOW.isoformat(), 'FAST', 'worker', token, '{}', NOW.isoformat(),
                                None, 'running', '[]'))
        self.store.con.execute("INSERT INTO jr_meta VALUES('quota:run','quota-fixture')")
        self.base = {'job_key': 'job', 'owner': 'worker', 'token': token, 'run_id': 'run'}
        self.visible = {'job_url': POSTING, 'observed_at': NOW.isoformat(), 'ref': 'browser-ref'}
        (self.root / 'facts.md').write_text('- A1 (all): Built a Python dashboard using verified sales data.\n'
                                           '- D1 (all): Designed a PostgreSQL reporting schema.\n')
        policy = patch.object(self.store, 'policy_snapshot', return_value=effective({}))
        policy.start()
        self.addCleanup(policy.stop)
        quota = patch.object(judgment, 'Quota')
        self.quota = quota.start().return_value
        self.addCleanup(quota.stop)
        login = patch.object(model_routes, 'require_subscription', return_value='fixture-codex')
        self.login = login.start()
        self.addCleanup(login.stop)
        request = patch.object(model_routes, 'request_subscription')
        self.request = request.start()
        self.addCleanup(request.stop)

    def response(self, returned):
        self.request.side_effect = lambda codex, route, query, attachments: {
            'response': returned, 'returncode': 0, 'tools_used': False, 'served_model': route.model,
            'served_effort': route.effort, 'session_id': 'fixture-session',
            'usage': {'input_tokens': 20, 'output_tokens': 16}, 'account_id': 'fixture-account',
        }

    def decide(self, stage, **values):
        return judgment.decide(self.store, dict(self.base, stage=stage, **values))

    def test_classification_uses_exact_quote_then_actual_gate_and_deduplicates(self):
        excerpt = 'Candidates must have 4 years of professional Python experience.'
        self.response({'findings': [{'flag': 'professional_years_required', 'quote': excerpt}]})
        result = self.decide('classification', posting_url=POSTING, excerpt=excerpt)
        self.assertEqual(result['gate'], 'more than 3 required years')
        self.assertEqual(result['calls'], 1)
        self.assertTrue(self.decide('classification', posting_url=POSTING, excerpt=excerpt)['cached'])
        self.request.assert_called_once()
        usage = self.store.con.execute('SELECT input_tokens,output_tokens FROM jr_usage').fetchone()
        self.assertEqual(tuple(usage), (20, 16))
        self.assertGreaterEqual(self.quota.check.call_count, 2)

    def test_classification_does_not_convert_preferred_clearance_into_gate(self):
        quote = 'Security clearance is preferred, and local travel is required.'
        self.response({'findings': [{'flag': 'clearance_required', 'quote': quote}]})
        result = self.decide('classification', posting_url=POSTING, excerpt=quote)
        self.assertEqual(result['status'], 'pending')
        self.assertNotIn('gate', result)

    def test_writing_selects_only_a_relevant_saved_fact_for_a_visible_field(self):
        visible = dict(self.visible, key='field', label='Describe your Python experience', type='textarea', visible=True)
        self.response({'fact_id': 'A1'})
        result = self.decide('writing', purpose='field', fact_ids=['A1', 'D1'], observation=visible)
        self.assertEqual(result['answer']['value'], 'Built a Python dashboard using verified sales data.')
        self.assertEqual(result['answer']['claim_quotes'], ['- A1 (all): Built a Python dashboard using verified sales data.'])
        self.assertNotIn('PostgreSQL', self.request.call_args.args[2])
        other = dict(visible, label='Describe your COBOL experience')
        self.assertEqual(self.decide('writing', purpose='field', fact_ids=['A1'], observation=other)['calls'], 0)
        self.request.assert_called_once()

    def test_visual_sends_cropped_png_but_never_authorizes_an_action(self):
        folder = self.root / 'codex-runtime' / 'runs' / 'run'
        folder.mkdir(parents=True)
        (folder / 'form.png').write_bytes(png())
        seen = dict(self.visible, controls=[{'key': 'continue', 'label': 'Continue', 'visible': True}])
        crop = {'file': 'form.png', 'observation_ref': self.visible['ref'], 'no_values': True, 'noncredential_region': True}

        def read(codex, route, query, attachments):
            self.assertEqual(len(attachments), 1)
            self.assertNotIn(b'internal metadata', attachments[0].read_bytes())
            return {'response': {'target_ref': 'continue', 'seen_label': 'Continue'}, 'returncode': 0, 'tools_used': False,
                    'served_model': route.model, 'served_effort': route.effort, 'session_id': 'fixture'}
        self.request.side_effect = read
        result = self.decide('visual', observation=seen, target='advance_control', crop=crop)
        self.assertEqual(result['target_ref'], 'continue')
        self.assertTrue(result['readback_required'])
        with self.assertRaises(Denied):
            self.decide('visual', observation=seen, target='advance_control', crop=dict(crop, file='../form.png'))

    def test_missing_quota_and_secret_observation_do_not_call_model(self):
        self.quota.check.side_effect = QuotaUnavailable('fixture cap')
        self.response({'fact_ids': ['A1']})
        self.assertEqual(self.decide('writing', purpose='application_note', fact_ids=['A1'])['status'], 'pending')
        with self.assertRaises(Denied):
            self.decide('classification', posting_url=POSTING, excerpt='password: fixture-secret')
        self.login.assert_not_called()
        self.request.assert_not_called()
