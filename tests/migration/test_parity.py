"""Claude-parity behaviours: instant phone alerts, opening walled jobs for him, the Today/tab sweep."""
import datetime as dt
import json
import os
import unittest
from pathlib import Path
from test_runtime import RuntimeTests
import tracker
from job_runtime import adapters, alerts
from job_runtime.planner import plan, apply_walls, mark_opened
from job_runtime.sweep import sweep

WD = 'https://examplea.wd5.myworkdayjobs.com/en-US/Careers/job/US---Example-City/Associate-Data-Engineer_REF598269W'


class ParityTests(RuntimeTests):
    def outbox(self):
        root = alerts.box(self.s)
        self.assertNotEqual(root, alerts.REAL_BOX)                       # tests never text his phone
        d = Path(root) / 'outbox'
        return sorted(p.read_text() for p in d.glob('*.txt')) if d.exists() else []

    def test_alerts_finish_this_signin_blocked_failed(self):
        k = self.job('Example Tools', 'co')
        adapters.defer(self.s, 'application_handoff', k, {'job_key': k, 'handoff': 'signature and screening questions left'}, self.owner, self.token)
        adapters.defer(self.s, 'fill', 'Example Lending', {'reason': 'CUA filechooser timed out'}, self.owner, self.token)        # retried, not his
        w = self.job('Sample Network', 'wd', link=WD)
        apply_walls(self.s, [w], self.owner, self.token)
        a = self.s.action(self.run['run_id'], 'linkedin', 'browser', {}, 'k1')
        self.s.receipt(self.owner, self.token, a['id'], 'deferred', {'refs': ['x'], 'blocked': 'reCAPTCHA wall'})
        b = self.s.action(self.run['run_id'], 'intake', 'gmail', {}, 'k2')
        self.s.receipt(self.owner, self.token, b['id'], 'failed', {'refs': ['x'], 'reason': 'Gmail tool missing'})
        out = alerts.send(self.s, self.run['run_id'])
        texts = ' | '.join(out['queued'])
        self.assertIn('Finish this: Example Tools - Software Engineer. signature and screening questions left.', texts)
        self.assertIn("Sign in needed: Sample Network - Software Engineer on Sample Network's Workday site", texts)
        self.assertIn('LinkedIn is blocked: reCAPTCHA wall', texts)
        self.assertIn('intake did not finish this run: Gmail tool missing', texts)
        self.assertNotIn('Example Lending', texts)
        c = self.s.action(self.run['run_id'], 'fill', 'browser', {}, 'k3')
        self.s.receipt(self.owner, self.token, c['id'], 'deferred', {'refs': ['x'], 'blocked': 'Example Works arbitration approval pending.'})
        self.assertFalse(any('fill is blocked' in t for t in alerts.send(self.s, self.run['run_id'])['queued']))
        self.assertEqual(len(self.outbox()), 4)
        self.assertEqual(alerts.send(self.s, self.run['run_id'])['queued'], [])   # 12 h de-dup: never twice

    def test_more_than_four_become_one_message(self):
        for i in range(6):
            k = self.job('Co%d' % i, 'c%d' % i)
            adapters.defer(self.s, 'application_handoff', k, {'job_key': k, 'handoff': 'needs you'}, self.owner, self.token)
        out = alerts.send(self.s, self.run['run_id'])
        self.assertEqual(len(out['queued']), 1)
        self.assertIn('+2 more in the Today tab', out['queued'][0])

    def test_walled_job_is_opened_once(self):
        w = self.job('Sample Network', 'wd', link=WD)
        apply_walls(self.s, [w], self.owner, self.token)
        q = plan(self.s, self.policy, 'FULL')
        self.assertEqual([x['job_key'] for x in q['walled_to_open']], [w])
        self.assertEqual(mark_opened(self.s, [w], self.owner, self.token), [w])
        self.assertEqual(plan(self.s, self.policy, 'FULL')['walled_to_open'], [])
        self.assertEqual(tracker.today_section(self.s.job(w)), 'Sign up')      # still on his list

    def test_sweep_expires_old_today_items(self):
        old = self.job('Old', 'old', status='Awaiting submit', posted_at=(self.at - dt.timedelta(hours=80)).isoformat())
        fresh = self.job('Fresh', 'fresh', status='Awaiting submit')
        out = sweep(self.s, self.owner, self.token, None)
        self.assertEqual(out['expired'], ['Old'])
        self.assertEqual(self.s.job(old)['status'], 'Stale')
        self.assertEqual(self.s.job(fresh)['status'], 'Awaiting submit')

    def test_sweep_with_tabs_closes_finished_requeues_lost_and_dedupes(self):
        done = self.job('Done', 'done', status='Applied')
        lost = self.job('Lost', 'lost', status='Awaiting submit')
        kept = self.job('Kept', 'kept', status='Awaiting submit')
        tabs = [{'id': 1, 'url': 'https://example.test/jobs/done'}, {'id': 2, 'url': 'https://example.test/jobs/kept'},
                {'id': 3, 'url': 'https://example.test/jobs/kept'}, {'id': 4, 'url': 'https://unrelated.test/page'}]
        out = sweep(self.s, self.owner, self.token, tabs, complete=True)
        self.assertEqual(out['close'], [1])
        self.assertEqual(out['duplicates'], [3])
        self.assertEqual(out['requeued'], ['Lost'])
        self.assertEqual(self.s.job(lost)['status'], 'Ready to fill')
        self.assertIn('refilled once', self.s.job(lost)['notes'])
        self.assertEqual(self.s.job(kept)['status'], 'Awaiting submit')
        snap = json.load(open(self.path.parent / 'scratch' / 'open-tabs.json'))
        self.assertIn(kept, snap['open_job_keys'])
        self.s.con.execute("UPDATE jobs SET status='Awaiting submit' WHERE job_key=?", (lost,))   # filled again, tab lost again
        self.assertEqual(sweep(self.s, self.owner, self.token, [])['requeued'], [])   # partial CUA list: no refill
        again = sweep(self.s, self.owner, self.token, [], complete=True)
        self.assertNotIn('Lost', again['requeued'])                            # refilled once only
        self.assertIn('Kept', again['requeued'])                               # its tab is gone now

    def test_open_application_page_makes_the_site_ready(self):
        w = self.job('Sample Network', 'wd', link=WD)
        apply_walls(self.s, [w], self.owner, self.token)
        tracker.set_portal(self.s.con, WD, 'wall', 'fixture')                   # site known, not signed in
        self.assertNotIn(w, plan(self.s, self.policy, 'FULL')['to_fill'])
        sweep(self.s, self.owner, self.token, [{'id': 9, 'url': 'https://examplea.wd5.myworkdayjobs.com/en-US/visa/userHome'}])
        snap = self.path.parent / 'scratch' / 'open-tabs.json'
        d = json.loads(snap.read_text())
        self.assertIn('examplea.wd5.myworkdayjobs.com', d['portal_tabs'])

    def test_tab_keys_match_apply_and_confirmation_pages(self):
        from job_runtime.sweep import tab_key
        TB = adapters.legacy('tabs')
        same = [('https://job-boards.greenhouse.io/samplevendor/jobs/91000101/confirmation', 'https://job-boards.greenhouse.io/samplevendor/jobs/91000101'),
                ('https://job-boards.greenhouse.io/sampleaero/jobs/9000990981?gh_jid=9000990981', 'https://boards.greenhouse.io/sampleaero/jobs/9000990981?gh_jid=9000990981'),
                ('https://recruiting.paylocity.com/Recruiting/jobs/Apply/9009741', 'https://recruiting.paylocity.com/recruiting/jobs/details/9009741'),
                ('https://examplec.wd3.myworkdayjobs.com/en-US/External/job/US---Example-City/Product-Software-Engineer_r8920074/apply/autofillWithResume',
                 'https://examplec.wd3.myworkdayjobs.com/external/job/product-software-engineer_r8920074'),
                ('https://app.joinhandshake.com/job-search/90017374?query=x', 'https://app.joinhandshake.com/jobs/90017374')]
        for a, b in same:
            self.assertEqual(tab_key(a, TB), tab_key(b, TB), a)
        self.assertNotEqual(tab_key('https://job-boards.greenhouse.io/samplevendor/jobs/1', TB), tab_key('https://job-boards.greenhouse.io/samplevendor/jobs/2', TB))

    def test_sweep_never_touches_non_job_tabs(self):
        sheet = 'https://docs.google.com/spreadsheets/d/abc/edit'
        out = sweep(self.s, self.owner, self.token, [{'id': 1, 'url': sheet}, {'id': 2, 'url': sheet}])
        self.assertEqual(out['duplicates'], [])                                 # his own duplicate tabs stay


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ParityTests(n) for n in ParityTests.__dict__ if n.startswith('test_'))
