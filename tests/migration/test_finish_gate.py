"""finish() refuses to end a run early while productive work remains, but never loops and never hides a blocker."""
import datetime as dt
import json
import unittest
from test_runtime import RuntimeTests
from job_runtime import adapters, cards
from job_runtime.coordinator import finish, remaining_work, MAX_PUSHES
from job_runtime.store import Denied

POSTING = ('We are hiring a new grad software engineer to build services in Python and Go. '
           'Requirements: Python, SQL, REST APIs, Git. Nice to have: AWS. ') * 8


class FinishGateTests(RuntimeTests):
    def no_other_work(self):
        self.s.con.execute("UPDATE jobs SET status='Applied' WHERE job_key=?", (self.key,))   # the fixture job would be fillable

    def queue_cards(self, n=3, source='linkedin'):
        cs = [{'id': str(11950000 + i), 'title': 'Software Engineer, New Grad', 'company': 'Acme%d' % i, 'location': 'Remote, US', 'age': '2 hours ago'}
              for i in range(1, n + 1)]
        return cards.ingest(self.s, source, cs, self.owner, self.token)

    def test_early_finish_with_queued_cards_is_refused_with_the_list(self):
        self.queue_cards(3)
        with self.assertRaisesRegex(Denied, 'WORK REMAINS.*3 linkedin cards to read'):
            finish(self.s, self.run['run_id'], self.owner, self.token)
        self.assertEqual(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], 'running')

    def test_refusal_is_limited_so_it_cannot_loop(self):
        self.queue_cards(3)
        for _ in range(MAX_PUSHES):
            with self.assertRaises(Denied):
                finish(self.s, self.run['run_id'], self.owner, self.token)
        self.assertIn(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], ('running',))
        finish(self.s, self.run['run_id'], self.owner, self.token)           # third call ends the run
        self.assertNotEqual(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], 'running')

    def test_per_job_problems_are_not_a_blocker(self):
        self.queue_cards(3)
        with self.assertRaises(Denied):
            finish(self.s, self.run['run_id'], self.owner, self.token,
                   blocker='LinkedIn explicit submission failure; OpenAI action-time arbitration approval pending')

    def test_a_declared_blocker_ends_the_run(self):
        self.queue_cards(3)
        finish(self.s, self.run['run_id'], self.owner, self.token, blocker='usage limit')
        self.assertNotEqual(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], 'running')

    def test_a_blocked_source_receipt_is_respected(self):
        self.no_other_work()
        self.queue_cards(3)
        a = self.s.action(self.run['run_id'], 'linkedin', 'browser', {}, 'k-linkedin')
        self.s.receipt(self.owner, self.token, a['id'], 'deferred', {'refs': ['fixture'], 'blocked': 'reCAPTCHA wall'})
        self.assertNotIn('linkedin cards to read', remaining_work(self.s, self.run['run_id']))
        finish(self.s, self.run['run_id'], self.owner, self.token)

    def test_no_work_finishes_normally(self):
        self.no_other_work()
        finish(self.s, self.run['run_id'], self.owner, self.token)           # nothing queued beyond the fixture job
        self.assertNotEqual(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], 'running')

    def test_long_run_finishes_even_with_work_left(self):
        self.queue_cards(3)
        self.at += dt.timedelta(minutes=20)
        self.s.con.execute("UPDATE jr_settings SET observed=?", (self.at.isoformat(),))
        finish(self.s, self.run['run_id'], self.owner, self.token)

    def test_postings_with_full_text_count_as_matchable_work(self):
        k = self.job('Beta', 'beta', resume='')
        f = self.path.parent / 'beta.txt'
        f.write_text(POSTING)
        adapters.defer(self.s, 'requirements', k, {'description_file': str(f), 'description_complete': True, 'source_ref': 'fixture'}, self.owner, self.token)
        self.assertEqual(remaining_work(self.s, self.run['run_id']).get('postings with full text to match'), 1)

    def test_ready_forms_tried_in_this_run_are_not_pushed_again(self):
        left = remaining_work(self.s, self.run['run_id'])
        self.assertEqual(left.get('forms ready to fill'), 1)                  # the fixture job is fillable
        self.prepare()                                                         # attempt reserved in this run
        self.assertNotIn('forms ready to fill', remaining_work(self.s, self.run['run_id']))


class DeadLeaseTests(FinishGateTests):
    def test_dead_lease_is_taken_over_but_a_live_one_is_not(self):
        from job_runtime.operations import take_over_dead_lease, DEAD_LEASE_MINUTES
        self.assertIsNone(take_over_dead_lease(self.s))                        # our own live lease
        self.s.con.execute("UPDATE jr_leases SET expires=? WHERE name='worker'", (self.at.timestamp() - 60,))
        self.assertIsNone(take_over_dead_lease(self.s))                        # just expired: maybe a slow heartbeat
        self.s.con.execute("UPDATE jr_leases SET expires=? WHERE name='worker'", (self.at.timestamp() - DEAD_LEASE_MINUTES * 60 - 1,))
        ev = take_over_dead_lease(self.s)
        self.assertIn('auto takeover', ev['reason'])
        self.assertEqual(self.s.con.execute("SELECT owner FROM jr_leases").fetchone()[0], '')
        self.assertEqual(self.s.con.execute('SELECT status FROM jr_runs').fetchone()[0], 'interrupted')
        self.assertTrue(self.s.acquire('next-run'))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(FinishGateTests(n) for n in FinishGateTests.__dict__ if n.startswith('test_'))
    suite.addTests(DeadLeaseTests(n) for n in DeadLeaseTests.__dict__ if n.startswith('test_'))
    return suite
