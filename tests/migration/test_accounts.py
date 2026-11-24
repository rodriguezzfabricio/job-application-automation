"""Signing in / creating the account on account sites (Workday, iCIMS, amazon.jobs...).

Until now the owner did every sign-in himself and the agent only noticed the wall.
He supplied one email and one password for all portals, so the agent can do it:
the browser fills the private secret without exposing it in the plan.

Nothing here is allowed to change behaviour when the credentials file is absent.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'stopgap-tools'))
from job_runtime import accounts  # noqa: E402
from test_runtime import RuntimeTests  # noqa: E402  (puts stopgap-tools on sys.path first)

WD = 'https://examplea.wd5.myworkdayjobs.com/en-US/Careers/job/US---Example-City/Associate-Data-Engineer_REF598269W'
EMAIL, PASSWORD = 'applicant@example.invalid', 'test-only-credential'


class AccountsStoreTests(unittest.TestCase):
    """Pure functions over the private root: no database, no browser."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def creds(self, payload):
        Path(self.private, accounts.FILE).write_text(json.dumps(payload))

    def test_no_file_means_no_credentials_and_the_human_keeps_the_job(self):
        self.assertEqual(accounts.load(self.private), {})
        self.assertIsNone(accounts.credentials(self.private, 'amazon.jobs'))
        self.assertFalse(accounts.has_credentials(self.private, 'amazon.jobs'))
        self.assertEqual(accounts.assignment(self.private, 'amazon.jobs'), 'human')

    def test_one_email_and_password_serve_every_portal(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        for portal in ('amazon.jobs', 'careers.microsoft.com', 'examplea.wd5.myworkdayjobs.com'):
            c = accounts.credentials(self.private, portal)
            self.assertEqual((c['email'], c['password']), (EMAIL, PASSWORD))
            self.assertEqual(c['source'], 'default')
            self.assertEqual(accounts.assignment(self.private, portal), 'agent')

    def test_a_portal_entry_overrides_the_default(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD},
                    'portals': {'amazon.jobs': {'email': 'other@example.com', 'password': 'x'}}})
        c = accounts.credentials(self.private, 'amazon.jobs')
        self.assertEqual((c['email'], c['password'], c['source']), ('other@example.com', 'x', 'portal'))
        self.assertEqual(accounts.credentials(self.private, 'careers.microsoft.com')['source'], 'default')

    def test_a_half_filled_credential_falls_back_to_the_default(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD},
                    'portals': {'amazon.jobs': {'email': 'other@example.com'}}})
        self.assertEqual(accounts.credentials(self.private, 'amazon.jobs')['password'], PASSWORD)

    def test_recipe_for_a_new_site_says_create_and_verify(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        r = accounts.recipe(self.private, 'amazon.jobs', 'new')
        text = ' | '.join(r['steps'])
        self.assertIn('Create account', text)
        self.assertIn('verification mail', text)
        self.assertNotIn(EMAIL, json.dumps(r))
        self.assertNotIn(PASSWORD, json.dumps(r))

    def test_recipe_for_an_existing_account_signs_in_without_creating(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        text = ' | '.join(accounts.recipe(self.private, 'amazon.jobs', 'again')['steps'])
        self.assertIn('Sign in', text)
        self.assertNotIn('Create account', text)
        self.assertNotIn('verification mail', text)

    def test_recipe_of_a_ready_site_says_nothing_is_needed(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        self.assertEqual(accounts.recipe(self.private, 'amazon.jobs', 'ready')['steps'], [])

    def test_recipe_says_the_agent_must_not_invent_a_password(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        text = ' '.join(accounts.recipe(self.private, 'amazon.jobs', 'new')['steps'])
        self.assertIn('private autofill', text)

    def test_workday_is_a_guest_form_site(self):
        self.assertTrue(accounts.guest_form(WD))
        self.assertTrue(accounts.guest_form('https://exampleb.wd5.myworkdayjobs.com/acjobsite/job/x'))
        self.assertFalse(accounts.guest_form('https://www.amazon.jobs/en/jobs/90070919/x'))
        self.assertFalse(accounts.guest_form('https://careers.microsoft.com/us/en/job/1'))

    def test_recipe_for_workday_fills_as_a_guest_before_any_sign_in(self):
        # Live-verified 2026-11-13: Workday's Apply Manually drops into a 5-step guest
        # form; the account is only asked for at the final Review/Submit step.
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})
        steps = accounts.recipe(self.private, 'examplea.wd5.myworkdayjobs.com', 'new')['steps']
        self.assertTrue(steps[0].startswith('Fill the application as a guest'))
        self.assertIn('Only at the final Review/Submit step', steps[1])
        self.assertIn('Create account / Register', ' | '.join(steps))
        self.assertIn('verification mail', ' | '.join(steps))

    def test_an_empty_credential_file_is_not_a_credential(self):
        self.creds({'default': {'email': '', 'password': ''}})
        self.assertFalse(accounts.has_credentials(self.private, 'amazon.jobs'))
        self.creds({'default': {'email': EMAIL}})  # no password
        self.assertFalse(accounts.has_credentials(self.private, 'amazon.jobs'))


class AccountPlannerTests(RuntimeTests):
    """The plan decides who signs in; with no credentials it must behave exactly as before."""

    def creds(self, payload):
        Path(self.s.path.parent, accounts.FILE).write_text(json.dumps(payload))

    def only_default(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})

    def wd_job(self, status='Matched', suffix='wd'):
        return self.job('Sample Network', suffix, link=WD, status=status)

    def test_without_credentials_an_account_site_still_goes_to_the_human(self):
        k = self.wd_job()
        q = self.plan_now()
        self.assertEqual(q['to_wall'], [k])
        self.assertEqual(q['to_signin'], [])
        self.assertEqual(q['signin'], {})

    def test_with_credentials_the_site_becomes_the_agents_own_queue(self):
        k = self.wd_job()
        self.only_default()
        q = self.plan_now()
        self.assertEqual(q['to_signin'], [k])
        self.assertEqual(q['to_wall'], [])
        self.assertNotIn(k, q['to_fill'])
        r = q['signin']['examplea.wd5.myworkdayjobs.com']
        self.assertNotIn(EMAIL, json.dumps(r))
        self.assertNotIn(PASSWORD, json.dumps(r))
        self.assertTrue(r['steps'])

    def test_a_walled_row_on_a_credentialed_site_is_the_agents_job_not_a_handoff(self):
        k = self.wd_job(status='Walled')
        self.assertEqual([h['job_key'] for h in self.plan_now()['handoffs']], [k])
        self.only_default()
        q = self.plan_now()
        self.assertEqual(q['to_signin'], [k])
        self.assertNotIn(k, [h['job_key'] for h in q['handoffs']])

    def test_marking_the_site_returns_its_jobs_to_the_fill_queue(self):
        k = self.wd_job()
        self.only_default()
        accounts.mark(self.s, 'examplea.wd5.myworkdayjobs.com', 'signed-up', 'agent created the account')
        q = self.plan_now()
        self.assertEqual(q['to_signin'], [])
        self.assertIn(k, q['to_fill'])

    def test_marking_a_link_not_a_portal_name_works_too(self):
        self.only_default()
        accounts.mark(self.s, WD, 'ok', 'agent signed in')
        self.assertEqual(accounts.credentials(self.s.path.parent, 'examplea.wd5.myworkdayjobs.com')['email'], EMAIL)
        self.assertIn('examplea.wd5.myworkdayjobs.com', accounts.ready_portals(self.s))

    def test_signing_in_is_capped_per_run_because_it_is_slow(self):
        keys = [self.wd_job(suffix='wd%d' % i) for i in range(3)]
        for i, k in enumerate(keys):
            self.s.con.execute('UPDATE jobs SET link=? WHERE job_key=?',
                               ('https://t%d.wd5.myworkdayjobs.com/en-US/x/job/a/b_%d' % (i, i), k))
        self.only_default()
        self.assertEqual(len(self.plan_now(dict(self.policy, signins_per_run=1))['to_signin']), 1)

    def test_a_greenhouse_site_never_asks_for_a_sign_in(self):
        k = self.job('Example Lending', 'aff', link='https://job-boards.greenhouse.io/examplecompany/jobs/9004579879')
        self.only_default()
        q = self.plan_now()
        self.assertIn(k, q['to_fill'])
        self.assertEqual(q['to_signin'], [])

    def plan_now(self, policy=None):
        from job_runtime.planner import plan
        return plan(self.s, policy or self.s.latest_policy(), 'FULL')


class AccountOpTests(RuntimeTests):
    """The two ops the recipe and the dashboard need."""

    def creds(self, payload):
        Path(self.s.path.parent, accounts.FILE).write_text(json.dumps(payload))

    def only_default(self):
        self.creds({'default': {'email': EMAIL, 'password': PASSWORD}})

    def call(self, **q):
        from job_runtime.__main__ import dispatch
        return dispatch(self.s, dict({'owner': self.owner, 'token': self.token}, **q))

    def test_open_portals_lists_only_sites_with_work_waiting(self):
        self.job('Sample Network', 'wd', link=WD)
        self.job('Example Lending', 'aff', link='https://job-boards.greenhouse.io/examplecompany/jobs/1')
        self.assertEqual(accounts.open_portals(self.s), {'examplea.wd5.myworkdayjobs.com'})

    def test_account_mark_op_records_the_sign_in_and_frees_the_jobs(self):
        k = self.job('Sample Network', 'wd', link=WD)
        self.only_default()
        out = self.call(op='account_mark', portal=WD, what='signed-up', job_key=k,
                        auth={'signed_in':True, 'origin':'https://examplea.wd5.myworkdayjobs.com', 'control':'sign_out','tab_id':'T',
                              'form':{'url':WD+'/apply','tab_id':'T','account':False,'requisition_id':'REF598269W',
                                      'fields':[{'label':'Resume','type':'file'}]}})
        self.assertEqual(out['portal'], 'examplea.wd5.myworkdayjobs.com')
        from job_runtime.planner import plan
        self.assertIn(k, plan(self.s, self.s.latest_policy(), 'FULL')['to_fill'])

    def test_accounts_op_splits_agent_sites_from_the_owners(self):
        self.job('Sample Network', 'wd', link=WD)
        self.only_default()
        out = self.call(op='accounts')
        self.assertTrue(out['has_default'])
        self.assertIn('examplea.wd5.myworkdayjobs.com', out['agent'])
        self.assertEqual(out['human'], [])

    def test_accounts_op_without_a_file_reports_everything_as_the_owners(self):
        self.job('Sample Network', 'wd', link=WD)
        out = self.call(op='accounts')
        self.assertFalse(out['has_default'])
        self.assertEqual(out['agent'], [])
        self.assertIn('examplea.wd5.myworkdayjobs.com', out['human'])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (AccountsStoreTests, AccountPlannerTests, AccountOpTests):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith('test_'))
    return suite
