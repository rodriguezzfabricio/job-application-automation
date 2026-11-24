"""Browser discovery as a code-owned queue: cards in, batch out, each read page becomes a matchable row."""
import datetime as dt
import unittest
from test_runtime import RuntimeTests
from job_runtime import cards
from job_runtime.cards import age_hours, ingest, next_batch, commit, link_of
from job_runtime.planner import plan

POSTING = ('We are hiring a new grad software engineer to build services in Python and Go. '
           'Requirements: Python, SQL, REST APIs, Git. Nice to have: AWS. ') * 8


def card(i, title='Software Engineer, New Grad', company='Acme', age='2 hours ago', **extra):
    return dict({'id': str(11950000 + i), 'title': title, 'company': company, 'location': 'Remote, US', 'age': age}, **extra)


class AgeTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(age_hours('Just now'), 0.0)
        self.assertEqual(age_hours('45 minutes ago'), 0.75)
        self.assertEqual(age_hours('Posted 3 hours ago'), 3)
        self.assertEqual(age_hours('1 day ago'), 23)
        self.assertEqual(age_hours('3 days ago'), 71)      # owner: "3 days ago" still passes the 72h window
        self.assertEqual(age_hours('4 days ago'), 95)
        self.assertEqual(age_hours('yesterday'), 23)
        self.assertIsNone(age_hours(''))
        self.assertIsNone(age_hours('Featured'))


class CardTests(RuntimeTests):
    def ingest(self, cs, source='linkedin', limit=12):
        return ingest(self.s, source, cs, self.owner, self.token, limit)

    def read(self, **extra):
        return dict({'status': 'ok', 'apply': 'easy', 'age': '2 hours ago', 'description': POSTING}, **extra)

    def test_ingest_keeps_candidates_and_filters_the_rest(self):
        out = self.ingest([card(1), card(2, title='Senior Staff Engineer'), card(3, age='5 days ago'),
                           card(4, age='Reposted 2 days ago'), card(5, title='Barista'), {'id': 'x', 'title': 'a', 'company': 'b'},
                           card(1)])
        self.assertEqual(out['new'], 1)
        self.assertEqual(out['queue'], 1)
        self.assertEqual([c['id'] for c in out['batch']], ['11950001'])
        self.assertEqual(out['skipped']['reposted'], 1)
        self.assertEqual(out['skipped']['older than 72h'], 1)
        self.assertEqual(out['skipped']['title is not a target role'], 2)   # senior and barista
        self.assertEqual(out['skipped']['bad id'], 1)
        self.assertEqual(out['skipped']['already queued'], 1)

    def test_batch_is_freshest_first_and_limited(self):
        out = self.ingest([card(1, age='2 days ago'), card(2, age='10 minutes ago'), card(3, age='5 hours ago')], limit=2)
        self.assertEqual([c['id'] for c in out['batch']], ['11950002', '11950003'])
        self.assertEqual(out['queue'], 3)
        self.assertEqual(len(next_batch(self.s, 'linkedin', 12)), 3)

    def test_known_jobs_and_applications_are_not_read_again(self):
        self.job('Acme', 'dup', link=link_of('linkedin', '11950001'))
        out = self.ingest([card(1), card(2)])
        self.assertEqual(out['skipped']['already a job or application'], 1)
        self.assertEqual(out['new'], 1)

    def test_commit_makes_a_matchable_one_click_row(self):
        self.ingest([card(1)])
        r = commit(self.s, 'linkedin', '11950001', self.read(), self.owner, self.token)
        self.assertEqual(r['outcome'], 'row')
        j = self.s.job(r['row'])
        self.assertEqual((j['source'], j['status'], j['resume']), ('linkedin', 'New', ''))
        self.assertIn('easy apply only', j['notes'])
        self.assertEqual(j['link'], link_of('linkedin', '11950001'))
        q = plan(self.s, self.policy, 'FAST')
        self.assertIn(r['row'], q['to_match'])                      # next stage: batch match
        self.assertEqual(next_batch(self.s, 'linkedin', 12), [])     # no longer pending
        again = self.ingest([card(1)])
        self.assertEqual(again['new'], 0)

    def test_external_apply_uses_the_employer_page_and_the_normal_form_path(self):
        self.ingest([card(1)])
        r = commit(self.s, 'linkedin', '11950001', self.read(apply='external', employer_link='https://job-boards.greenhouse.io/acme/jobs/123'),
                   self.owner, self.token)
        j = self.s.job(r['row'])
        self.assertEqual(j['link'], 'https://job-boards.greenhouse.io/acme/jobs/123')
        self.assertNotIn('easy apply only', j['notes'])

    def test_handshake_marker_and_url(self):
        self.ingest([card(2)], source='handshake')
        r = commit(self.s, 'handshake', '11950002', self.read(), self.owner, self.token)
        self.assertIn('quick apply only', self.s.job(r['row'])['notes'])
        self.assertTrue(self.s.job(r['row'])['link'].startswith('https://app.joinhandshake.com/jobs/'))

    def test_closed_applied_reposted_and_old_pages_make_no_row(self):
        self.ingest([card(i) for i in range(1, 5)])
        for i, extra in ((1, {'status': 'closed'}), (2, {'status': 'applied'}), (3, {'status': 'reposted'}), (4, {'age': '6 days ago'})):
            r = commit(self.s, 'linkedin', str(11950000 + i), self.read(**extra), self.owner, self.token)
            self.assertIsNone(r['row'], extra)
        self.assertEqual(next_batch(self.s, 'linkedin', 12), [])

    def test_clearance_posting_is_screened_without_a_row(self):
        self.ingest([card(1)])
        r = commit(self.s, 'linkedin', '11950001', self.read(description=POSTING + ' An active Secret security clearance is required.'),
                   self.owner, self.token)
        self.assertEqual((r['outcome'], r['gate']), ('hard gate', 'clearance_required'))

    def test_readers_come_from_code(self):
        r = cards.readers('linkedin')
        self.assertEqual(r['list_selector'], '[componentkey^="job-card-component-ref-"]')
        self.assertIn("slice(23)", r['list_js'])                       # len('job-card-component-ref-') == 23
        self.assertEqual(len('job-card-component-ref-'), 23)
        self.assertIn('No longer accepting applications', r['page_js'])
        self.assertIn('{query}', r['search_url'])
        self.assertIn('data-hook', cards.readers('handshake')['list_selector'])

    def test_job_closed_ends_the_job_with_page_proof(self):
        from job_runtime.store import Denied
        k = self.job('Hossam', 'hm')
        with self.assertRaises(Denied):
            cards.job_closed(self.s, k, {'refs': ['tab'], 'quote': 'We couldn\u2019t submit your application'}, self.owner, self.token)
        out = cards.job_closed(self.s, k, {'refs': ['tab'], 'quote': 'No longer accepting applications'}, self.owner, self.token)
        self.assertTrue(out['changed'])
        self.assertEqual(self.s.job(k)['status'], 'Not eligible')
        self.assertIn('posting closed', self.s.job(k)['reason'])
        self.assertNotIn(k, plan(self.s, self.policy, 'FAST')['to_one_click'])

    def test_daily_cap_stops_the_batch(self):
        from job_runtime import adapters
        self.packet['settings']['cards_per_day'] = 2
        adapters.import_commands(self.s, self.packet, self.owner, self.token)
        out = self.ingest([card(i) for i in range(1, 6)])
        self.assertEqual(len(out['batch']), 2)
        for c in out['batch']:
            commit(self.s, 'linkedin', c['id'], self.read(), self.owner, self.token)
        self.assertEqual(next_batch(self.s, 'linkedin', 12), [])
        self.assertEqual(cards.budget_left(self.s, 'linkedin'), 0)
        self.assertEqual(cards.budget_left(self.s, 'handshake'), 2)   # each source has its own budget

    def test_batch_size_is_capped_by_policy(self):
        out = self.ingest([card(i) for i in range(1, 30)], limit=99)
        self.assertEqual(len(out['batch']), 12)

    def test_short_page_is_retried_once_then_dropped(self):
        self.ingest([card(1)])
        first = commit(self.s, 'linkedin', '11950001', self.read(description='Loading...'), self.owner, self.token)
        self.assertTrue(first['retry'])
        self.assertEqual(len(next_batch(self.s, 'linkedin', 12)), 1)
        second = commit(self.s, 'linkedin', '11950001', self.read(status='unreadable'), self.owner, self.token)
        self.assertFalse(second['retry'])
        self.assertEqual(next_batch(self.s, 'linkedin', 12), [])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(CardTests(n) for n in CardTests.__dict__ if n.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(AgeTests))
    return suite
