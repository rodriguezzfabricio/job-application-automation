import datetime as dt
import json
import unittest

from job_runtime import account_flow as flow, accounts
from job_runtime.store import Denied
from test_runtime import RuntimeTests


class Registration(RuntimeTests):
    def setUp(self):
        super().setUp()
        self.site='price.wd5.myworkdayjobs.com'
        self.origin='https://'+self.site
        self.url=self.origin+'/en-US/Careers/job/Portland/Software-Engineer_91001'
        self.wall=self.origin+'/en-US/Careers/register'
        self.job_id=self.job(link=self.url)
        (self.path.parent/accounts.FILE).write_text(json.dumps({'default':{'email':'fake@example.invalid','password':'fixture'}}))
        self.q=dict(owner=self.owner,token=self.token,job_key=self.job_id)
        self.hash=flow.digest(self.wall)

    def register(self):
        flow.begin(self.s,dict(self.q,mode='register',page_origin=self.origin,page_sha256=self.hash))
        flow.consume(self.s,self.wall)
        return flow.register_started(self.s,dict(self.q,page_sha256=self.hash))

    def observe(self, url=None, confirmation=True, **extra):
        auth={'signed_in':True,'origin':self.origin,'control':'sign_out','tab_id':'t','creation_confirmed':confirmation,
              'creation_notice':'Your account has been created.' if confirmation else '',
              'form':{'tab_id':'t','account':False,'url':url or self.url+'/apply','fields':[{'type':'file','label':'CV'}]}}
        auth.update(extra)
        return flow.mark_observed(self.s,dict(self.q,portal=self.site,auth=auth))

    def test_direct_registration_transition_is_bound_to_button_page_tab_time_and_return(self):
        self.register()
        transition={'before_page_sha256':self.hash,'before_tab':'t','after_tab':'t','before_signed_in':False,
                    'intervening_auth':False,'button':'Create Account','clicked_at':self.s.now(),'returned_at':self.s.now()}
        for wrong in ({'before_tab':'other'},{'intervening_auth':True},{'before_page_sha256':'a'*64},
                      {'button':'Sign In'},{'clicked_at':(self.at-dt.timedelta(seconds=1)).isoformat()}):
            with self.assertRaises(Denied): self.observe(confirmation=False,registration_transition={**transition,**wrong})
        with self.assertRaises(Denied): self.observe(creation_notice='Once your account has been created you can apply.')
        self.assertEqual(self.observe(confirmation=False,registration_transition=transition)['outcome'],'created')
        basis=self.s.con.execute("SELECT json_extract(detail,'$.basis') FROM events WHERE kind='portal-account-created'").fetchone()[0]
        self.assertEqual(basis,'direct_registration_to_original_form')

    def test_an_existing_session_cannot_close_a_native_autofill_failure(self):
        from job_runtime import learning
        learning.observe(self.s,self.run['run_id'],self.job_id,'question','Private autofill left the fields empty.')
        self.observe()
        self.assertEqual(learning.review(self.s)[0]['state'],'Open')
        login=self.origin+'/en-US/Careers/login'
        flow.begin(self.s,dict(self.q,mode='sign_in',page_origin=self.origin,page_sha256=flow.digest(login)))
        flow.consume(self.s,login)
        self.observe()
        self.assertEqual(learning.review(self.s)[0]['state'],'Verified')

    def test_combined_registration_needs_independent_creation_and_original_form(self):
        self.site='hire.example.invalid'
        self.origin='https://'+self.site
        self.url=self.origin+'/jobs/22002'
        self.job_id=self.job(link=self.url)
        self.q['job_key']=self.job_id
        job=self.s.job(self.job_id)
        attempt={'id':'application-1','run_id':self.run['run_id']}
        generic=[{'rule':'recruiting_consent','field':'Terms of service and privacy policy'}]
        approved=[{'rule':'recruiting_consent','field':'Applying to this job you are creating an account'}]
        with self.s.tx():
            flow.combined_registration_started(self.s,attempt,job,generic)
        self.assertFalse(flow.value(self.s,flow.combined_key(job)))
        with self.s.tx():
            flow.combined_registration_started(self.s,attempt,job,approved)
        with self.assertRaises(Denied),self.s.tx():
            flow.combined_registration_started(self.s,attempt,job,approved)
        with self.assertRaises(Denied): self.observe(confirmation=False)
        with self.assertRaises(Denied): self.observe(self.origin+'/jobs/22003/apply')
        with self.assertRaises(Denied): flow.decision(self.s,self.q)
        self.assertEqual(self.observe()['outcome'],'created')
        self.assertEqual(self.observe()['outcome'],'created')
        self.assertEqual(self.s.con.execute("SELECT count(*) FROM events WHERE kind='portal-account-created'").fetchone()[0],1)

    def test_interrupted_registration_then_return_to_wrong_job(self):
        self.assertTrue(self.register()['click_once'])
        self.s.release(self.owner,self.token)
        self.q.update(owner='later',token=self.s.acquire('later'))
        self.assertEqual(flow.decision(self.s,self.q)['mode'],'reconcile_registration')
        with self.assertRaises(Denied):flow.begin(self.s,dict(self.q,mode='register',page_origin=self.origin,page_sha256=self.hash))
        for url in (self.origin+'/en-US/Careers/candidateHome',self.url.replace('91001','91002'),self.url.replace('/Careers/','/Other/')):
            with self.assertRaises(Denied):self.observe(url)
        with self.assertRaises(Denied):self.observe(confirmation=False)
        self.assertEqual(self.observe()['outcome'],'created')
        self.assertEqual(self.observe()['outcome'],'created')
        self.assertEqual(self.s.con.execute("SELECT count(*) FROM events WHERE kind='portal-account-created'").fetchone()[0],1)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],0)

    def test_existing_account_response_and_expired_request(self):
        self.register()
        before=self.s.clock
        self.s.clock=lambda:before()+dt.timedelta(seconds=flow.TTL+1)
        with self.assertRaises(Denied):flow.verification_filter(self.s,self.q)
        self.s.clock=before
        self.assertEqual(flow.account_exists(self.s,dict(self.q,origin=self.origin,page_sha256=self.hash,notice='Account already exists'))['mode'],'sign_in')
        self.assertEqual(flow.decision(self.s,self.q)['mode'],'sign_in')
        self.assertEqual(self.observe(confirmation=False)['outcome'],'signed_in')

    def test_activation_is_distinct_from_matching_and_must_restore_original_form(self):
        self.register()
        scope = flow.tenant(self.url)
        challenge = flow.digest(self.wall+'/code')
        flow.code_challenge(self.s,dict(self.q,origin=self.origin,tenant=scope,page_sha256=challenge,input_count=1,label='Verification code'))
        filter = flow.verification_filter(self.s,self.q)
        message = dict(authenticated=True,authenticated_sender_domain='workday.com',recipient_sha256=filter['recipient_sha256'],
                       purpose='account_verification',received_at=self.s.now(),message_id='fixture-message',
                       destination_origin=self.origin,destination_tenant=scope,method='code',
                       action_sha256=flow.digest('code|fixture-message|'+challenge))
        for changes in ({'action_sha256':'f'*64},{'authenticated_sender_domain':'unrelated.invalid'},{'destination_tenant':scope+'2'}):
            self.assertFalse(flow.verification_match(self.s,dict(self.q,mail={**message,**changes}))['matched'])
        self.assertTrue(flow.verification_match(self.s,dict(self.q,mail=message))['matched'])
        with self.assertRaises(Denied): self.observe(confirmation=False)
        success=dict(self.q,message_id=message['message_id'],action_sha256=message['action_sha256'],origin=self.origin,tenant=scope,notice='Email address has been verified')
        with self.assertRaises(Denied): flow.verification_used(self.s,dict(success,notice='Please verify your email'))
        self.assertTrue(flow.verification_used(self.s,success)['activation_observed'])
        with self.assertRaises(Denied): self.observe(self.origin+'/en-US/Careers/candidateHome',confirmation=False)
        self.assertEqual(self.observe(confirmation=False)['outcome'],'created')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(Registration(k) for k in Registration.__dict__ if k.startswith('test_'))
