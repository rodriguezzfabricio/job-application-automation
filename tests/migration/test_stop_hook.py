"""Stop hook: a job run keeps going while work is queued, but never traps a session (owner 2026-11-13)."""
import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from test_runtime import RuntimeTests
from job_runtime import cards
from job_runtime.stop_hook import decide, MAX_BLOCKS, RUN_CAP_MINUTES

HOOK = Path(__file__).resolve().parents[2] / 'codex' / 'hooks' / 'keep_going.py'


class StopHookTests(RuntimeTests):
    def setUp(self):
        super().setUp()
        guard = patch('job_runtime.stop_hook.continuation_funded', return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    def queue_cards(self, n=3, source='linkedin'):
        cs = [{'id': str(11960000 + i), 'title': 'Software Engineer, New Grad', 'company': 'Acme%d' % i,
               'location': 'Remote, US', 'age': '2 hours ago'} for i in range(1, n + 1)]
        return cards.ingest(self.s, source, cs, self.owner, self.token)

    def test_other_sessions_stop_normally(self):
        self.queue_cards(3)
        self.assertIsNone(decide(self.s, 'some-other-codex-thread', 'done'))

    def test_queued_work_blocks_the_stop_with_the_list(self):
        self.queue_cards(3)
        out = decide(self.s, self.owner, 'The cycle is incomplete; resume the run next time.')
        self.assertEqual(out['decision'], 'block')
        self.assertIn('3 linkedin cards to read', out['reason'])
        self.assertIn('cards_next', out['reason'])
        self.assertIn('per-job handoff', out['reason'])

    def test_run_level_blocker_lets_it_stop(self):
        self.queue_cards(3)
        self.assertIsNone(decide(self.s, self.owner, 'Stopped: ChatGPT usage limit reached, try again at 22:04.'))
        self.assertIsNone(decide(self.s, self.owner, 'LinkedIn shows a CAPTCHA on every page.'))

    def test_high_usage_lets_it_stop(self):
        self.queue_cards(3)
        self.assertIsNone(decide(self.s, self.owner, '', usage_pct=92))

    def test_at_most_max_blocks_per_run(self):
        self.queue_cards(3)
        for _ in range(MAX_BLOCKS):
            self.assertIsNotNone(decide(self.s, self.owner, ''))
        self.assertIsNone(decide(self.s, self.owner, ''))

    def test_past_the_time_cap_one_wrap_up_then_stop(self):
        self.queue_cards(3)
        self.at += dt.timedelta(minutes=RUN_CAP_MINUTES['FULL'] + 1)
        out = decide(self.s, self.owner, '')
        self.assertIn('Time is up', out['reason'])
        self.assertIn('finish', out['reason'])
        self.assertIsNone(decide(self.s, self.owner, ''))

    def test_finished_run_stops_normally(self):
        self.s.con.execute("UPDATE jr_runs SET status='partial'")
        self.queue_cards(3)
        self.assertIsNone(decide(self.s, self.owner, ''))

    def test_no_jobs_but_open_stages_asks_for_the_stages(self):
        self.s.con.execute("UPDATE jobs SET status='Applied' WHERE job_key=?", (self.key,))
        out = decide(self.s, self.owner, '')
        self.assertEqual(out['decision'], 'block')
        self.assertIn('mirror', out['reason'])

    def test_state_file_records_each_push(self):
        self.queue_cards(3)
        decide(self.s, self.owner, '')
        state = json.loads((self.path.parent / 'codex-runtime' / 'runs' / self.run['run_id'] / 'stop-hook.json').read_text())
        self.assertEqual(state['blocks'], 1)
        self.assertEqual(state['log'][0]['kind'], 'work_left')

    def test_script_end_to_end(self):
        self.queue_cards(3)
        self.s.con.commit()
        # HOME points at the fixture folder so the hook cannot read this Mac's real Codex usage logs: with the live
        # 5-hour meter at 90% or more the hook (correctly) lets the turn end and this test failed (11-13 21:05).
        env = dict(os.environ, JOB_AUTOMATION_PRIVATE_ROOT=str(self.path.parent), HOME=str(self.path.parent))
        run = lambda sid: subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
            {'session_id': sid, 'hook_event_name': 'Stop', 'stop_hook_active': False, 'last_assistant_message': 'resume next time'}),
            capture_output=True, text=True, env=env, timeout=60)
        r = run(self.owner)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), '')  # legacy run without a funded continuation
        r = run('not-a-job-run')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ''))
        r = subprocess.run([sys.executable, str(HOOK)], input='not json', capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ''))
        self.assertIn('stop allowed', (self.path.parent / 'codex-runtime' / 'stop-hook.log').read_text())


if __name__ == '__main__':
    unittest.main()
