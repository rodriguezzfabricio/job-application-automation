"""The phone alert must name the cause, not just say "no run finished".

Owner 2026-11-13: this morning it said "no run finished" when Google Drive was
disconnected. The four messages in health/watchdog are liveness only; the run's own
receipts (failed stages, missing receipts, deferred backlog) hold the reason.
"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'stopgap-tools'))
sys.path.insert(0, str(ROOT / 'ops' / 'job-watchdog'))
import watchdog  # noqa: E402
from job_runtime import adapters  # noqa: E402
from job_runtime.health import cause, health_events  # noqa: E402
from job_runtime.operations import publish_health, record_stage  # noqa: E402
from test_runtime import RuntimeTests  # noqa: E402


class CauseTests(RuntimeTests):
    def partial(self):
        self.s.con.execute("UPDATE jr_runs SET status='partial',ended=? WHERE id=?", (self.s.now(), self.run['run_id']))

    def health(self):
        self.partial()
        return publish_health(self.s, self.run['run_id'], self.s.audit(self.run['run_id']))

    def test_the_cause_names_the_deferred_backlog_that_holds_work(self):
        adapters.defer(self.s, 'requirements', 'k1', {'reason': 'no complete description'}, self.owner, self.token)
        adapters.defer(self.s, 'requirements', 'k2', {'reason': 'no complete description'}, self.owner, self.token)
        adapters.defer(self.s, 'fill', 'k3', {'reason': 'sign-in wall'}, self.owner, self.token)
        c = self.health()['cause']
        self.assertIn('deferred: match 2', c)
        self.assertIn('fill 1', c)

    def test_the_cause_names_a_failed_stage(self):
        record_stage(self.s, self.run['run_id'], 'one_click', self.owner, self.token, 'failed', {'refs': ['x']})
        self.assertIn('one_click failed', self.health()['cause'])

    def test_the_cause_says_when_a_stage_left_no_receipt(self):
        record_stage(self.s, self.run['run_id'], 'one_click', self.owner, self.token, 'succeeded', {'refs': ['x']})
        self.assertIn('no receipt:', self.health()['cause'])

    def test_a_run_with_every_receipt_and_no_backlog_has_an_empty_cause(self):
        expected = json.loads(self.s.con.execute("SELECT expected FROM jr_runs WHERE id=?",
                                                 (self.run['run_id'],)).fetchone()[0])
        for stage in expected:
            record_stage(self.s, self.run['run_id'], stage, self.owner, self.token, 'succeeded', {'refs': ['x']})
        self.assertEqual(self.health()['cause'], '')

    def test_cause_is_short_enough_for_a_phone_line(self):
        for i in range(6):
            adapters.defer(self.s, 'provider%d' % i, 'k%d' % i, {'reason': 'x'}, self.owner, self.token)
        self.assertLessEqual(len(self.health()['cause']), 160)


class AlertMessageTests(RuntimeTests):
    def stale_state(self):
        old = (self.s.clock() - dt.timedelta(hours=2)).isoformat()
        return {'last_completed_at': old, 'last_success_at': old, 'browser_last_success_at': self.s.clock().isoformat()}

    def test_the_phone_message_carries_the_cause(self):
        state = dict(self.stale_state(), cause='deferred: match 121, oldest 9h')
        text = ' '.join(e['text'] for e in health_events(self.s.clock(), state, {}, {}))
        self.assertIn('No job run has finished in 60 minutes', text)
        self.assertIn('deferred: match 121', text)

    def test_the_message_is_unchanged_when_there_is_no_cause(self):
        events = health_events(self.s.clock(), self.stale_state(), {}, {})
        self.assertEqual([e['text'] for e in events],
                         ['No job run has finished in 60 minutes.'])


class WatchdogCauseTests(unittest.TestCase):
    """The installed watchdog only reads JSON, so the cause has to travel in the state file."""

    def setUp(self):
        self.now = dt.datetime(2026, 11, 13, 15, 0, tzinfo=dt.timezone.utc)

    def decisions(self, **state):
        base = {'last_completed_at': (self.now - dt.timedelta(hours=2)).isoformat(),
                'browser_last_success_at': self.now.isoformat(), 'cause': 'deferred: match 121, oldest 9h'}
        base.update(state)
        return watchdog.decisions(self.now, base, {})

    def test_the_cause_reaches_the_watchdog_message(self):
        out = self.decisions()
        self.assertEqual(out[0][0], 'stopped')
        self.assertIn('deferred: match 121', out[0][1])
        self.assertIn('No job run has finished in 60 minutes', out[0][1])

    def test_without_a_cause_the_message_is_unchanged(self):
        self.assertEqual(self.decisions(cause=''),
                         [('stopped', 'No job run has finished in 60 minutes. Check the Mac, Codex usage and Brave.')])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (CauseTests, AlertMessageTests, WatchdogCauseTests):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith('test_'))
    return suite
