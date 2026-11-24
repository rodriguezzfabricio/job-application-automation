"""`jr`, the compact run command line: finds the open run, fills owner/token/run_id, prints little."""
import datetime as dt
import io
from pathlib import Path
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from test_runtime import RuntimeTests
from job_runtime import jr


class JrTests(RuntimeTests):
    def call(self, *argv):
        jr.PRIVATE = self.path.parent
        (jr.PRIVATE / 'dashboard-sheet-ids.json').write_text(json.dumps({tab:i for i,tab in enumerate(jr.MIRROR_TABS)}))
        state = dict(self.run, owner=self.owner, token=self.token, policy=self.policy)
        d = self.path.parent / 'codex-runtime' / 'runs' / self.run['run_id']
        d.mkdir(parents=True, exist_ok=True)
        (d / 'run.json').write_text(json.dumps(state))
        jr.store = lambda: self.s
        jr._close = lambda s: None                               # the fixture keeps using its connection
        jr.read_meters = lambda: getattr(self, 'meters', {})      # never the live Codex logs (job_runtime/pace.py)
        buf = io.StringIO()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.owner}), redirect_stdout(buf):
            code = jr.main(list(argv))
        return code, buf.getvalue()

    def test_brief_and_ops_print_little_and_save_full_results(self):
        for i in range(12):
            self.job('Co%d' % i, 'c%d' % i)
        code, out = self.call('brief')
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), jr.LIMIT + 200)
        self.assertIn(self.run['run_id'], out)
        code, out = self.call('cards_js', '{"source":"linkedin"}')
        self.assertEqual(code, 0)
        self.assertIn('(full: ', out)
        full = out.split('(full: ')[1].strip().rstrip(')')
        self.assertIn('job-card-component-ref-', json.loads(open(full).read())['list_selector'])

    def test_errors_are_one_line(self):
        code, out = self.call('job_closed', '{"job_key":"nope","evidence":{"refs":["x"],"quote":"No longer accepting applications"}}')
        self.assertEqual(code, 2)
        self.assertIn('"ok": false', out)

    def test_tab_ownership_is_not_a_reason_to_skip_a_browser_stage(self):
        code, out = self.call('stage', 'linkedin', 'deferred', '{"refs":["x"],"reason":"Tab 1 is already part of browser session 9; untouched"}')
        self.assertEqual(code, 2)
        self.assertIn('createBrowserTab', out)
        code, out = self.call('stage', 'fill', 'deferred', '{"refs":["x"],"reason":"LinkedIn showed a login wall"}')
        self.assertEqual(code, 0)

    def test_discovery_is_not_deferred_while_cards_are_queued(self):
        from job_runtime import cards
        cards.ingest(self.s, 'linkedin', [{'id': '4490000001', 'title': 'Software Engineer, New Grad', 'company': 'Acme',
                                           'location': 'Remote, US', 'age': '2 hours ago'}], self.owner, self.token)
        code, out = self.call('stage', 'linkedin', 'deferred', '{"refs":["x"],"reason":"Discovery time budget reached"}')
        self.assertEqual(code, 2)
        self.assertIn('still queued', out)
        code, out = self.call('stage', 'linkedin', 'deferred', '{"refs":["x"],"reason":"LinkedIn showed a login wall"}')
        self.assertNotIn('still queued', out)                        # a real blocker is allowed through

    def test_mirror_stage_requires_fresh_readback_for_current_plan(self):
        from job_runtime.adapters import legacy
        header = legacy('tracker').HEADERS['Jobs']
        original = [header]
        desired = [header, ['', 'Acme', 'Engineer', '', '', 50, 'New', '', '', '', '', '', 'key-a']]
        with patch('job_runtime.reporting.exports', return_value={'Jobs': desired}):
            code, out = self.call('mirror_prepare', json.dumps({'snapshots': {'Jobs': original}}))
        self.assertEqual(code, 0, out)
        path = Path(out.split('(full: ')[1].strip().rstrip(')'))
        expected = json.loads(path.read_text())['expected']
        code, out = self.call('stage', 'mirror', 'succeeded', '{"refs":["fixture"]}')
        self.assertEqual(code, 2)
        self.assertIn('final mirror readback', out)
        def verify(rows, done):
            return self.call('mirror_verify', json.dumps({'snapshots': {'Jobs': rows},
                             'expected': expected, 'batches_applied': done, 'read_at': self.at.isoformat()}))
        self.assertEqual(verify(desired, 1)[0], 2)  # the initial preflight was not recorded
        self.assertIn('"ok": true', verify(original, 0)[1])
        self.assertIn('"ok": true', verify(desired, 1)[1])
        self.at += dt.timedelta(seconds=61)
        self.assertEqual(self.call('stage', 'mirror', 'succeeded', '{"refs":["fixture"]}')[0], 2)
        self.assertIn('"ok": true', verify(desired, 1)[1])
        changed = [header, ['', 'Other', 'Engineer', '', '', 50, 'New', '', '', '', '', '', 'key-b']]
        with patch('job_runtime.reporting.exports', return_value={'Jobs': changed}):
            code, out = self.call('mirror_prepare', json.dumps({'snapshots': {'Jobs': original}}))
            self.assertEqual(code, 0)
        self.assertEqual(verify(desired, 1)[0], 2)
        self.assertEqual(self.call('stage', 'mirror', 'succeeded', '{"refs":["fixture"]}')[0], 2)
        path = Path(out.split('(full: ')[1].strip().rstrip(')'))
        expected = json.loads(path.read_text())['expected']
        self.assertIn('"ok": true', verify(original, 0)[1])
        self.assertIn('"ok": true', verify(changed, 1)[1])
        self.assertEqual(self.call('stage', 'mirror', 'succeeded', '{"refs":["fixture"]}')[0], 0)

    def test_compact_cuts_lists_and_strings(self):
        c = jr.compact({'a': list(range(20)), 'b': 'x' * 500})
        self.assertEqual(len(c['a']), 6)
        self.assertLess(len(c['b']), 210)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(JrTests(n) for n in JrTests.__dict__ if n.startswith('test_'))
