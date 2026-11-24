"""Usage pace gate, lean runs, context wrap-up and memory trim.

Evidence and numbers: codex/eval/USAGE_OPTIMIZATION_2026-11-13.md. Nothing here reads this Mac's real Codex logs.
"""
import datetime as dt
import io
import re
import json
import tempfile
import os
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from test_runtime import RuntimeTests
from job_runtime import cards, jr, pace
from job_runtime.coordinator import remaining_work
from job_runtime.stop_hook import decide, RUN_CAP_MINUTES

NOW = 1794640000.0          # a fixed epoch for the pure pace tests


def meters(used5, left5_min, used_w=5.0, left_w_days=6.5):
    return {'5h': {'used': used5, 'resets_at': NOW + left5_min * 60, 'window_min': 300},
            'week': {'used': used_w, 'resets_at': NOW + left_w_days * 86400, 'window_min': 10080}}


class PaceRules(unittest.TestCase):
    def test_no_reading_never_blocks(self):
        self.assertEqual(pace.decide({}, NOW)['mode'], 'run')

    def test_near_the_limit_skips(self):
        self.assertEqual(pace.decide(meters(92, 200), NOW)['mode'], 'skip')
        self.assertEqual(pace.decide(meters(97, 20), NOW)['mode'], 'skip')       # 11-13 16:18: started at 97%, died

    def test_eighty_percent_is_lean(self):
        self.assertEqual(pace.decide(meters(83, 20), NOW)['mode'], 'lean')       # 11-13 18:18 began at 83%

    def test_ahead_of_an_even_spend_is_lean(self):
        # 2 h into the window (elapsed 120 + headroom 75 = 65% budget): 70% used is ahead, 50% is fine
        self.assertEqual(pace.decide(meters(70, 180), NOW)['mode'], 'lean')
        self.assertEqual(pace.decide(meters(50, 180), NOW)['mode'], 'run')

    def test_a_window_that_has_reset_counts_as_unused(self):
        self.assertEqual(pace.decide(meters(99, -5), NOW)['mode'], 'run')

    def week_at_noon(self, used_now, used_at_six, ready=None):
        noon = dt.datetime(2026, 11, 14, 12, 0, tzinfo=pace.ET).timestamp()
        six = dt.datetime(2026, 11, 14, 6, 0, tzinfo=pace.ET)
        m = {'5h': {'used': 10.0, 'resets_at': noon + 250 * 60, 'window_min': 300},
             'week': {'used': used_now, 'resets_at': noon + 6.5 * 86400, 'window_min': 10080},
             'day_start': {'used': used_at_six, 'at': six.isoformat()}}
        return pace.decide(m, noon, ready)

    def test_weekly_share_drives_the_gate(self):
        # 06:00 reading 20%, 6.75 days left: today's share (100-20)/6.75 = 11.9 points; by noon (6 of 18 operating
        # hours + 2 h headroom) 5.3 points may be spent
        self.assertEqual(self.week_at_noon(23, 20)['mode'], 'run')
        d = self.week_at_noon(30, 20)
        self.assertEqual(d['mode'], 'lean')
        self.assertIn('weekly', d['why'])
        self.assertAlmostEqual(d['week']['per_day'], 11.9, places=1)
        self.assertEqual(self.week_at_noon(30, 20, ready=0)['mode'], 'skip')          # lean with nothing ready
        self.assertEqual(self.week_at_noon(30, 20, ready=3)['mode'], 'lean')
        self.assertEqual(self.week_at_noon(33, 20)['mode'], 'skip')                    # today's whole share is used
        self.assertEqual(pace.decide(meters(10, 250, used_w=97, left_w_days=3), NOW)['mode'], 'skip')

    def test_server_reset_drops_the_stored_mode(self):
        old = meters(85, 30, used_w=16, left_w_days=6.9)
        stored = {'mode': 'lean', 'meters': old}
        same = meters(40, 200, used_w=17, left_w_days=6.9)
        self.assertEqual(pace.live_mode(stored, same, NOW), 'lean')              # same weekly window: never looser
        reset = meters(3, 290, used_w=0, left_w_days=7.0)                         # 11-13 17:18: 20% -> 0%, new reset time
        self.assertTrue(pace.server_reset(old, reset))
        self.assertFalse(pace.server_reset(old, same))
        self.assertEqual(pace.live_mode(stored, reset, NOW), 'run')
        self.assertEqual(pace.live_mode(stored, None, NOW), 'lean')               # no reading: keep what it began with

    def test_reads_meters_and_context_from_a_session_log(self):
        with tempfile.TemporaryDirectory() as d:
            day = Path(d) / '2026' / '11' / '13'
            day.mkdir(parents=True)
            sid = '01a1d7c1-f310-7112-b1ea-16b340540c8b'
            rec = {'timestamp': '2026-11-14T01:00:00Z', 'type': 'event_msg', 'payload': {
                'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 151234}},
                'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': 64.0, 'window_minutes': 300, 'resets_at': NOW},
                                'secondary': {'used_percent': 11.0, 'window_minutes': 10080, 'resets_at': NOW + 86400}}}}
            premium = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None,
                                                         'rate_limits': {'limit_id': 'premium', 'primary': None}}}
            (day / ('rollout-2026-11-13T20-52-54-%s.jsonl' % sid)).write_text(json.dumps(rec) + '\n' + json.dumps(premium) + '\n')
            m = pace.read_meters(sessions=d)
            self.assertEqual((m['5h']['used'], m['week']['used']), (64.0, 11.0))
            self.assertEqual(pace.session_context(sid, sessions=d), 151234)
            self.assertIsNone(pace.session_context('no-such-session', sessions=d))
            self.assertIsNone(pace.session_context('../../etc', sessions=d))


class LeanRuns(RuntimeTests):
    def setUp(self):
        super().setUp()
        self.meters = {}
        self.mem = Path(self.tmp.name) / 'automation' / 'memory.md'
        self.mem.parent.mkdir()
        self._saved = (jr.MEMORY, jr.read_meters, jr.PRIVATE)
        jr.MEMORY = self.mem

    def tearDown(self):
        jr.MEMORY, jr.read_meters, jr.PRIVATE = self._saved
        super().tearDown()

    def call(self, *argv):
        jr.PRIVATE = self.path.parent
        state = dict(self.run, owner=self.owner, token=self.token, policy=self.policy)
        d = self.path.parent / 'codex-runtime' / 'runs' / self.run['run_id']
        d.mkdir(parents=True, exist_ok=True)
        (d / 'run.json').write_text(json.dumps(state))
        jr.store = lambda: self.s
        jr._close = lambda s: None
        jr.read_meters = lambda: self.meters
        buf = io.StringIO()
        env = {'CODEX_THREAD_ID': '' if argv[0] == 'begin' else self.owner}
        with patch.dict(os.environ, env), redirect_stdout(buf):
            code = jr.main(list(argv))
        return code, buf.getvalue()

    def queue_cards(self, n=3, source='linkedin'):
        cs = [{'id': str(11960000 + i), 'title': 'Software Engineer, New Grad', 'company': 'Acme%d' % i,
               'location': 'Remote, US', 'age': '2 hours ago'} for i in range(1, n + 1)]
        return cards.ingest(self.s, source, cs, self.owner, self.token)

    def live(self, used5, left5_min, used_w=5.0):
        t = self.at.timestamp()
        return {'account': 'fixture-account', 'sampled_at': t, '5h': {'used': used5, 'resets_at': t + left5_min * 60, 'window_min': 300},
                'week': {'used': used_w, 'resets_at': t + 6.5 * 86400, 'window_min': 10080}}

    def test_gate_skip_writes_a_skip_record_the_watchdog_can_name(self):
        self.meters = self.live(95, 30)
        code, out = self.call('gate')
        self.assertEqual(code, 0)
        self.assertIn('SKIP', out)
        self.assertEqual(self.mem.exists(), False)                           # no memory file: trim is a no-op
        recs = list((self.path.parent / 'codex-runtime' / 'scheduled-skips').glob('*-usage-pace.json'))
        self.assertEqual(len(recs), 1)
        text = recs[0].read_text().lower()
        self.assertTrue('usage' in text and 'limit' in text)                  # watchdog stopped_cause wording

    def test_gate_run_and_lean_say_what_to_do(self):
        self.meters = self.live(10, 290)
        self.assertIn('RUN', self.call('gate')[1])
        self.meters = self.live(85, 60)
        self.assertIn('LEAN', self.call('gate')[1])

    def test_gate_allows_no_model_work_without_a_verified_meter(self):
        code, out = self.call('gate')
        self.assertEqual(code, 0)
        self.assertIn('verified current weekly Codex meter unavailable', out)
        self.assertIn('SKIP', out)

    def test_begin_refuses_to_start_a_run_that_would_die_at_the_limit(self):
        self.meters = self.live(96, 20)
        snap = Path(self.tmp.name) / 'snap.json'
        snap.write_text(json.dumps({'read_at': self.at.isoformat(), 'snapshots': {
            'Settings': [['k', 'v']], 'Today': self.packet['commands']['Today'], 'Jobs': self.packet['commands']['Jobs'],
            'Recruiters': self.packet['commands']['Recruiters'], 'Companies': self.packet['commands']['Companies']}}))
        before = self.s.con.execute('SELECT count(*) FROM jr_runs').fetchone()[0]
        code, out = self.call('begin', str(snap), 'owner-2')
        self.assertEqual(code, 2)
        self.assertIn('usage pace SKIP', out)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM jr_runs').fetchone()[0], before)

    def test_stale_snapshot_keeps_same_funded_reservation(self):
        import sqlite3
        self.s.con.execute("UPDATE jr_runs SET status='partial', ended=?", (self.at.isoformat(),))
        self.s.release(self.owner,self.token)
        self.at+=dt.timedelta(minutes=20)
        (self.path.parent/'rubric-overrides.md').write_text('fixture overrides')
        self.meters=self.live(25,270)
        self.run={'run_id':'placeholder','expected':[]}
        snap=Path(self.tmp.name)/'snap.json'
        packet={'read_at':(self.at-dt.timedelta(minutes=10)).isoformat(),
                'snapshots':{'Settings':[['k','v']],**self.packet['commands']}}
        snap.write_text(json.dumps(packet))
        code,out=self.call('begin',str(snap),'owner-2')
        self.assertEqual(code,2,out)
        self.assertIn('fresh command packet required',out)
        with sqlite3.connect(self.path.parent/'codex-runtime'/'quota.sqlite') as ledger:
            old,=ledger.execute("SELECT id,status,charged FROM reservations WHERE owner='owner-2'").fetchall()
            self.assertEqual(old[1:],('active',2))
            packet['read_at']=self.at.isoformat()
            snap.write_text(json.dumps(packet))
            code,out=self.call('begin',str(snap),'owner-2')
            self.assertEqual(code,0,out)
            rid=json.loads(out)['run_id']
            self.assertEqual(ledger.execute("SELECT id,status,charged FROM reservations WHERE owner='owner-2'").fetchall(),[old])
            self.assertEqual(self.s.con.execute('SELECT value FROM jr_meta WHERE key=?',('quota:'+rid,)).fetchone()[0],old[0])

    def test_lean_begin_defers_discovery_and_trims_memory(self):
        self.s.con.execute("UPDATE jr_runs SET status='partial', ended=?", (self.at.isoformat(),))
        self.s.release(self.owner, self.token)
        self.at += dt.timedelta(minutes=20)                                   # 12:20 ET: a FAST slot
        (self.path.parent / 'rubric-overrides.md').write_text('fixture overrides')
        self.mem.write_text('\n\n'.join('2026-11-13T%02d:00:00+00:00: run note %d %s' % (h, h, 'x' * 900) for h in range(10)))
        self.meters = self.live(85, 60)
        snap = Path(self.tmp.name) / 'snap.json'
        snap.write_text(json.dumps({'read_at': self.at.isoformat(), 'snapshots': {
            'Settings': [['k', 'v']], 'Today': self.packet['commands']['Today'], 'Jobs': self.packet['commands']['Jobs'],
            'Recruiters': self.packet['commands']['Recruiters'], 'Companies': self.packet['commands']['Companies']}}))
        self.run = {'run_id': 'placeholder', 'expected': []}                   # call() needs a run.json; begin makes its own
        code, out = self.call('begin', str(snap), 'owner-2')
        self.assertEqual(code, 0, out)
        self.assertIn('deferred_by_pace', out)
        rid = re.search(r'"run_id": "([0-9a-f-]{36})"', out).group(1)
        got = {r['stage']: r['status'] for r in self.s.con.execute('SELECT stage,status FROM jr_actions WHERE run_id=?', (rid,))}
        self.assertEqual((got.get('handshake'), got.get('linkedin')), ('deferred', 'deferred'))
        text = self.mem.read_text()
        self.assertTrue(text.startswith(jr.MEMORY_HEAD))
        self.assertLessEqual(len(text), jr.MEMORY_KEEP_CHARS + len(jr.MEMORY_HEAD) + 10)
        self.assertIn('run note 9', text)
        archive = (self.mem.parent / 'memory-archive.md').read_text()
        self.assertIn('run note 0', archive)
        self.assertNotIn('run note 9', archive)

    def test_application_quota_blocks_new_work_and_allows_deterministic_finish(self):
        import sqlite3

        self.meters = self.live(20, 290)
        self.assertIn('RUN', self.call('gate')[1])
        ledger = self.path.parent / 'codex-runtime' / 'quota.sqlite'
        with sqlite3.connect(ledger) as con:
            rid, = con.execute('SELECT id FROM reservations WHERE owner=?', (self.owner,)).fetchone()
        self.s.con.execute('INSERT INTO jr_meta VALUES (?,?)', ('quota:' + self.run['run_id'], rid))
        self.assertIn('remaining', self.call('quota')[1])
        self.meters['week']['used'] += 2
        code, out = self.call('cards_next', '{"source":"linkedin"}')
        self.assertEqual(code, 2)
        self.assertIn('usage limit', out)
        self.assertIn('"wrap_up_now": true', self.call('brief')[1])
        with patch('job_runtime.alerts.send'):
            self.assertEqual(self.call('finish', '{}')[0], 0)
        with sqlite3.connect(ledger) as con:
            self.assertEqual(con.execute('SELECT status, charged FROM reservations WHERE id=?', (rid,)).fetchone(),
                             ('settled', 3))

    def test_lean_run_counts_no_card_reading_as_work_left(self):
        self.queue_cards(3)
        self.assertIn('linkedin cards to read', remaining_work(self.s, self.run['run_id']))
        self.s.con.execute('INSERT INTO jr_meta VALUES(?,?)', ('pace:' + self.run['run_id'], json.dumps({'mode': 'lean'})))
        self.assertNotIn('linkedin cards to read', remaining_work(self.s, self.run['run_id']))

    def test_lean_run_may_defer_discovery_with_cards_queued(self):
        self.s.con.execute('UPDATE jr_runs SET expected=?', (json.dumps(['commands', 'resume_sync', 'handshake', 'linkedin', 'plan',
                                                                        'match', 'one_click', 'fill', 'audit', 'mirror']),))
        self.queue_cards(1)
        code, out = self.call('stage', 'linkedin', 'deferred', '{"refs":["x"],"reason":"Discovery time budget reached"}')
        self.assertIn('still queued', out)                                    # a normal run is still refused
        self.s.con.execute('INSERT INTO jr_meta VALUES(?,?)', ('pace:' + self.run['run_id'], json.dumps({'mode': 'lean'})))
        code, out = self.call('stage', 'linkedin', 'deferred', '{"refs":["usage pace"],"reason":"lean run"}')
        self.assertEqual(code, 0, out)

    @unittest.mock.patch('job_runtime.stop_hook.continuation_funded', return_value=True)
    def test_stop_hook_does_not_push_cards_in_a_lean_run(self, _funded):
        self.queue_cards(3)
        self.s.con.execute('INSERT INTO jr_meta VALUES(?,?)', ('pace:' + self.run['run_id'], json.dumps({'mode': 'lean'})))
        self.at = dt.datetime.fromisoformat(self.s.con.execute('SELECT started FROM jr_runs').fetchone()[0]) + dt.timedelta(minutes=5)
        out = decide(self.s, self.owner, '')
        self.assertNotIn('cards to read', out['reason'])
        self.assertIn('LEAN', out['reason'])

    @unittest.mock.patch('job_runtime.stop_hook.continuation_funded', return_value=True)
    def test_big_context_gets_one_wrap_up_then_stops(self, _funded):
        self.queue_cards(3)
        self.at = dt.datetime.fromisoformat(self.s.con.execute('SELECT started FROM jr_runs').fetchone()[0]) + dt.timedelta(minutes=5)
        out = decide(self.s, self.owner, '', context_tokens=pace.CONTEXT_WRAP_TOKENS + 1000)
        self.assertIn('context', out['reason'])
        self.assertIn('finish', out['reason'])
        self.assertIsNone(decide(self.s, self.owner, '', context_tokens=pace.CONTEXT_WRAP_TOKENS + 2000))

    @unittest.mock.patch('job_runtime.stop_hook.continuation_funded', return_value=True)
    def test_small_context_keeps_pushing_work(self, _funded):
        self.queue_cards(3)
        self.at = dt.datetime.fromisoformat(self.s.con.execute('SELECT started FROM jr_runs').fetchone()[0]) + dt.timedelta(minutes=5)
        out = decide(self.s, self.owner, '', context_tokens=90000)
        self.assertIn('3 linkedin cards to read', out['reason'])

    def test_run_cap_fits_the_two_hour_schedule(self):
        self.assertTrue(all(v <= 60 for v in RUN_CAP_MINUTES.values()))

    def test_full_runs_include_discovery(self):
        expected = json.loads(self.s.con.execute('SELECT expected FROM jr_runs').fetchone()[0])       # fixture slot 12:00 ET
        self.assertEqual(self.run['mode'], 'FULL')
        for stage in ('intake', 'feeds', 'handshake', 'linkedin'):
            self.assertIn(stage, expected)
        self.assertLess(expected.index('linkedin'), expected.index('plan'))

    def test_memory_trim_is_idempotent_and_leaves_small_files(self):
        self.mem.write_text('2026-11-13T01:00:00+00:00: short note\n')
        self.assertEqual(jr.trim_memory(self.mem)['trimmed'], 0)
        self.mem.write_text('\n\n'.join('2026-11-13T%02d:00:00+00:00: note %d %s' % (h, h, 'y' * 1500) for h in range(6)))
        first = jr.trim_memory(self.mem, keep=4000)
        self.assertGreater(first['trimmed'], 0)
        self.assertEqual(jr.trim_memory(self.mem, keep=4000)['trimmed'], 0)
        self.assertIn('note 5', self.mem.read_text())


def load_tests(loader, tests, pattern):
    # LeanRuns inherits the RuntimeTests fixture; run only the tests defined here, not the inherited ones again
    names = [n for n in PaceRules.__dict__ if n.startswith('test_')]
    lean = [n for n in LeanRuns.__dict__ if n.startswith('test_')]
    return unittest.TestSuite([PaceRules(n) for n in names] + [LeanRuns(n) for n in lean])


if __name__ == '__main__':
    unittest.main()
