"""Account sites (Workday etc.): the agent never opens a sign-in page; they go to 'Apply yourself'
until he marks the site 'signed up', which is remembered (portals table), not a 5-minute observation."""
import unittest
from test_runtime import RuntimeTests  # puts stopgap-tools on sys.path first
import tracker
from job_runtime import adapters
from job_runtime.planner import plan, apply_walls

WD = 'https://examplea.wd5.myworkdayjobs.com/en-US/Careers/job/US---Example-City/Associate-Data-Engineer_REF598269W'


class AccountSiteTests(RuntimeTests):
    def wd_job(self, status='Matched', link=WD, company='Sample Network'):
        return self.job(company, 'wd', link=link, status=status)

    def test_unsigned_workday_job_goes_to_apply_yourself_not_fill(self):
        k = self.wd_job()
        q = plan(self.s, self.policy, 'FULL')
        self.assertEqual(q['to_wall'], [k])
        self.assertNotIn(k, q['to_fill'])
        self.assertEqual(q['to_fill'], [self.key])
        self.assertEqual(apply_walls(self.s, q['to_wall'], self.owner, self.token), [k])
        j = self.s.job(k)
        self.assertEqual(j['status'], 'Walled')
        self.assertEqual(tracker.today_section(j), 'Sign up')

    def test_signed_up_site_is_remembered_and_filled(self):
        k = self.wd_job()
        tracker.set_portal(self.s.con, WD, 'signed-up', 'fixture')
        q = plan(self.s, self.policy, 'FULL')
        self.assertIn(k, q['to_fill'])
        self.assertEqual(q['to_wall'], [])

    def test_walled_job_returns_when_site_is_ready(self):
        k = self.wd_job(status='Walled')
        self.assertIn(k, [h['job_key'] for h in plan(self.s, self.policy, 'FULL')['handoffs']])
        tracker.set_portal(self.s.con, WD, 'signed-up', 'fixture')
        self.assertIn(k, plan(self.s, self.policy, 'FULL')['to_fill'])

    def test_signing_up_overrides_an_old_sign_in_wall_blocker(self):
        k = self.wd_job()
        adapters.defer(self.s, 'fill', 'Sample Network', {'reason': 'Create Account/Sign In step1of8'}, self.owner, self.token)
        self.assertEqual(plan(self.s, self.policy, 'FULL')['to_wall'], [k])  # his list, not a silent skip
        tracker.set_portal(self.s.con, WD, 'signed-up', 'fixture')
        self.assertIn(k, plan(self.s, self.policy, 'FULL')['to_fill'])

    def test_today_signed_up_mark_makes_site_ready(self):
        k = self.wd_job(status='Walled')
        self.packet['commands']['Today'].append(['Apply yourself', 'signed up', k])
        adapters.import_commands(self.s, self.packet, self.owner, self.token)
        self.assertIn(k, plan(self.s, self.policy, 'FULL')['to_fill'])

    def test_greenhouse_is_not_an_account_site(self):
        k = self.job('Example Lending', 'aff', link='https://job-boards.greenhouse.io/examplecompany/jobs/9004579879')
        q = plan(self.s, self.policy, 'FULL')
        self.assertIn(k, q['to_fill'])
        self.assertEqual(q['to_wall'], [])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(AccountSiteTests(n) for n in AccountSiteTests.__dict__ if n.startswith('test_'))
