import datetime as dt
import json
import unittest
from unittest.mock import patch

from job_runtime import account_flow, accounts, canary
from job_runtime.__main__ import dispatch
from job_runtime.form_step import plan
from job_runtime.store import Denied
from job_runtime.quota import Quota
from test_runtime import RuntimeTests


class PortalAccessAndRolloutTests(RuntimeTests):
    def test_private_grant_bound_to_one_page_lease_and_registration_email(self):
        portal = 'acme.wd5.myworkdayjobs.com'
        url = 'https://' + portal + '/en-US/one/register'
        job = self.job(link='https://' + portal + '/en-US/one/job/10000')
        (self.path.parent / accounts.FILE).write_text(json.dumps({'default': {
            'email': 'fixture@example.invalid', 'password': 'synthetic-only'}}))
        base = {'owner':self.owner, 'token':self.token, 'job_key':job}
        ready = dispatch(self.s, dict(base, op='account_begin', mode='register', page_origin='https://'+portal,
                                      page_sha256=account_flow.digest(url)))
        self.assertTrue(ready['ready'])
        self.assertNotIn('synthetic-only', json.dumps(ready))
        self.assertNotIn('fixture@example.invalid', json.dumps(ready))
        with self.assertRaises(Denied):
            account_flow.consume(self.s, url.replace('acme.', 'other.'))
        with self.assertRaises(Denied):
            account_flow.consume(self.s, url+'?new=1')
        self.assertEqual(account_flow.consume(self.s,url)['password'], 'synthetic-only')
        with self.assertRaises(Denied):
            account_flow.consume(self.s,url)
        observation=dict(base,op='account_fill_observed',page_sha256=account_flow.digest(url),
            autofill=dict(origin='https://'+portal,tenant=account_flow.tenant(url),email_populated=True,
                          all_password_fields_populated=False))
        with self.assertRaises(Denied):
            dispatch(self.s,observation)
        observation['autofill']['all_password_fields_populated']=True
        self.assertTrue(dispatch(self.s,observation)['private_fill_observed'])
        with self.assertRaises(Denied):
            dispatch(self.s,dict(base,op='account_mail_filter'))
        dispatch(self.s,dict(base,op='account_register_started',page_sha256=account_flow.digest(url)))
        self.assertEqual(dispatch(self.s,dict(base,op='account_decide'))['mode'],'reconcile_registration')
        with self.assertRaises(Denied):
            dispatch(self.s,dict(base,op='account_register_started',page_sha256=account_flow.digest(url)))
        filt=dispatch(self.s,dict(base,op='account_mail_filter'))
        mail=dict(authenticated=True,authenticated_sender_domain='myworkdayjobs.com',recipient_sha256=filt['recipient_sha256'],
                  destination_origin='https://'+portal, destination_tenant=portal+'/one', action_sha256='a'*64, purpose='account_verification',
                  message_id='fixture:mail-1',received_at=self.at.isoformat())
        for change in ({'authenticated_sender_domain':'example.invalid'}, {'destination_origin':'https://other.myworkdayjobs.com'},
                       {'recipient_sha256':'wrong'}, {'received_at':(self.at-dt.timedelta(seconds=1)).isoformat()}):
            self.assertFalse(dispatch(self.s,dict(base,op='account_match_mail',mail=dict(mail,**change)))['matched'])
        self.assertTrue(dispatch(self.s,dict(base,op='account_match_mail',mail=mail))['matched'])
        with self.assertRaises(Denied):
            dispatch(self.s,dict(base,op='account_mark',portal=url,what='verified'))
        self.assertFalse(accounts.account_site('https://example.invalid/myworkdayjobs.com'))
        self.assertFalse(accounts.account_site('https://myworkdayjobs.com.example.invalid/register'))
        self.assertFalse(accounts.account_site('http://'+portal))

    def test_unknown_cannot_create_a_submit_marker(self):
        aid=self.prepare()
        with self.assertRaisesRegex(Denied,'marker'):
            self.s.reconcile(self.owner,self.token,aid,'unknown',{'refs':['fixture:page'],'observed_at':self.at.isoformat()})
        state=self.s.con.execute('SELECT state FROM jr_attempts WHERE id=?',(aid,)).fetchone()[0]
        self.assertEqual(state,'prepared')

    def test_named_terms_and_saved_employer_dates(self):
        root=self.path.parent
        (root/'form-answers.md').write_text(
            '- Current company / current or most recent employer: Test Employer. Current title: Prior intern.\n'
            '- Earliest exact work start date: 2027-01-04 (January 4, 2027).\n'
            '- Exact expected graduation date: 2026-12-18 (December 18, 2026).\n'
            '- Example Engineer 12345 Terms of Use and Privacy Policy, to continue this application: Yes. Owner approved.\n'
            '- [allow] standard legal acknowledgement.\n')
        fields=[dict(key=k,label=l,type=t,required=True) for k,l,t in [
            ('org','Most recent employer','text'),('start','Earliest start date','date'),
            ('grad','Expected graduation date','date'),('agree','I accept the Terms of Use and Privacy Policy','checkbox'),
            ('unknown','I certify that I did not use AI','checkbox')]]
        for suffix, allowed in [('12345',True),('23456',False)]:
            key=self.job(suffix=suffix)
            result=dispatch(self.s,dict(op='form_step',owner=self.owner,token=self.token,job_key=key,fields=fields,controls=[]))
            actual={x['key']:x['value'] for x in result['ops']}
            self.assertEqual([actual[x] for x in ('org','start','grad')],['Test Employer','2027-01-04','2026-12-18'])
            self.assertEqual('agree' in actual,allowed)
            self.assertNotIn('unknown',actual)

    def test_standing_recruiting_consent_covers_the_entire_curately_clause(self):
        from job_runtime.terms import RECRUITING_CONSENT, saved_term
        from job_runtime.answers import validate_answers
        root=self.path.parent
        wording='Applying to this job you are creating an account, agreeing to Terms & Conditions, Privacy Policyand permitting text/calls about your application (Message/data rate may apply)'
        (root/'form-answers.md').write_text(RECRUITING_CONSENT)
        for field in (wording, 'I accept the terms of service and privacy policy', 'I consent to receive texts or phone calls regarding my application'):
            proof=saved_term(root,field,self.s.job(self.key))
            self.assertEqual(proof['rule'],'recruiting_consent')
            self.assertTrue(validate_answers(root,[dict(proof,field=field,refs=['fixture:employer'])],self.s.job(self.key)))
        for field in (wording+' and authorize a credit check', 'I consent to marketing calls', 'Agree to pay a $30 application fee'):
            self.assertIsNone(saved_term(root,field,self.s.job(self.key)))

    def test_recovery_verification_blocks_unresolved_eligibility_and_still_requires_history(self):
        from job_runtime import opening_history as history
        from job_runtime.adapters import defer
        job=self.s.job(self.key)
        identity=job['link']
        defer(self.s,'recovery_verification',identity,{'opening_id':'1','reason':'Original employer date unknown'},self.owner,self.token)
        with self.assertRaises(Denied):history.guard(self.s,job,self.run['run_id'])
        self.s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',('history-required:'+self.key,'true'))
        q=dict(job_key=self.key,owner=self.owner,token=self.token,evidence={'employer_url':identity,'source_posting':identity,
            'employer':'Example','employer_requisition':'1','observed_at':self.s.now(),'employer_posted_at':self.s.now(),
            'condition_resolved':'Original employer date unknown','date_quote':'Published today','condition_quote':'Published today',
            'job_identity_quote':'Engineer 1','refs':[identity],'opening_id':'1'})
        self.assertTrue(history.verify_recovery(self.s,q)['resolved'])
        with self.assertRaisesRegex(Denied,'history'):history.guard(self.s,job,self.run['run_id'])

    def test_question_parks_once_until_the_saved_answers_change(self):
        from job_runtime.planner import plan as queue
        for question in ('Which campus?', 'Repeated different question'):
            dispatch(self.s,dict(op='form_question',owner=self.owner,token=self.token,job_key=self.key,
                                 question=question,refs=['fixture:page']))
        result=queue(self.s,self.policy,'FULL')
        self.assertNotIn(self.key,result['to_fill'])
        self.assertIn({'job_key':self.key,'reason':'Which campus?','questions':['Which campus?','Repeated different question']},result['handoffs'])
        (self.path.parent/'form-answers.md').write_text('School campus: fixture')
        self.assertIn(self.key,queue(self.s,self.policy,'FULL')['to_fill'])

    def test_required_field_readback_and_actual_pdf(self):
        from job_runtime.answers import check_required_coverage
        from job_runtime.resumes import verify_attachment
        from hashlib import sha256
        root=self.path.parent
        (root/'form-answers.md').write_text('- Current location: Portland, OR')
        base=dict(op='form_step',owner=self.owner,token=self.token,job_key=self.key,
                  run_id=self.run['run_id'],page_ref='Contact',controls=[])
        field=dict(key='city',label='City',type='text',required=True,filled=True)
        first=dispatch(self.s,dict(base,fields=[dict(field,observed_value='Wrong')]))
        self.assertEqual(first['ops'],[])
        self.assertEqual(first['answers'],[])
        self.assertIn('conflicting',first['blocked'][0]['reason'])
        with self.assertRaises(Denied):
            check_required_coverage(first['required_fields'],first['answers'])
        verified=dispatch(self.s,dict(base,fields=[dict(field,observed_value='Portland')]))
        self.assertEqual(verified['ops'],[])
        check_required_coverage(verified['required_fields'],verified['answers'])
        with self.assertRaises(Denied):
            check_required_coverage(verified['required_fields'],[])
        library=root/'my-resumes'
        library.mkdir(); content=b'%PDF-fixture'; (library/'swe.pdf').write_bytes(content)
        sha=sha256(content).hexdigest()
        proof=dict(job_key=self.key,refs=['fixture:file'],method='browser_file',
                   observed_name='swe.pdf',observed_bytes=len(content),observed_sha256=sha)
        verify_attachment(root,self.s.job(self.key),'swe.pdf',sha,proof)
        for wrong in ({'observed_sha256':'0'*64},{'observed_name':'other.pdf'},{'job_key':'other-job'}):
            with self.assertRaises(Denied):
                verify_attachment(root,self.s.job(self.key),'swe.pdf',sha,dict(proof,**wrong))

    def observe_publication(self, *keys):
        from job_runtime.adapters import remember_employer_date
        for key in keys:
            link = self.s.job(key)['link']
            remember_employer_date(self.s,key,dict(link=link,posted_at=self.s.now(),
                posted_at_source='employer.JobPosting.datePosted'),verified_original=True)

    def test_production_entrypoint_requires_grouped_readback_and_keeps_one_receipt(self):
        from hashlib import sha256
        from job_runtime.terms import RECRUITING_CONSENT
        root=self.path.parent
        job=self.s.job(self.key)
        (root/'form-answers.md').write_text('- Current location: Portland, OR\n'+RECRUITING_CONSENT)
        clause='Applying to this job you are creating an account, agreeing to Terms & Conditions, Privacy Policy and permitting text/calls about your application'
        library=root/'my-resumes'
        library.mkdir(); pdf=b'%PDF-1.4 fixture'; sha=sha256(pdf).hexdigest()
        (library/'swe.pdf').write_bytes(pdf)
        (library/'manifest.json').write_text(json.dumps({'resumes':[{'file':'swe.pdf','sha256':sha}]}))
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")
        self.observe_publication(self.key)
        base=dict(owner=self.owner,token=self.token,run_id=self.run['run_id'])
        with patch('job_runtime.canary.rollout_ready',return_value=True):
            dispatch(self.s,dict(base,op='canary_enroll',job_key=self.key,channel='form'))
            form=dispatch(self.s,dict(base,op='form_step',job_key=self.key,page_ref='contact',controls=[],
                fields=[dict(key='city',label='City',type='text',required=True,filled=True,observed_value='Portland'),
                        dict(key='consent',label=clause,type='checkbox',required=True,checked=True)]))
            aid=dispatch(self.s,dict(base,op='prepare',job_key=self.key,channel='form',resume_hash=sha,review=self.review))['attempt_id']
            attach=dict(method='browser_file',job_key=self.key,observed_name='swe.pdf',observed_sha256=sha,
                        observed_bytes=len(pdf),refs=['fixture:resume'])
            click=dict(base,op='submit_started',attempt_id=aid,resume_hash=sha)
            with self.assertRaises(Denied):
                dispatch(self.s,dict(click,evidence=dict(self.evidence,answer_evidence=[],attachment=attach)))
            allowed=dispatch(self.s,dict(click,evidence=dict(self.evidence,answer_evidence=form['answers'],attachment=attach)))
            self.assertTrue(allowed['click_once'])
            pending=account_flow.value(self.s,account_flow.combined_key(job))
            self.assertEqual((pending['state'],pending['attempt_id']),('registration_pending',aid))
            after=dict(kind='history',url=job['link'],job_url=job['link'],title=job['title'],
                       quote='Application submitted',observed_at=self.s.now(),refs=['fixture:exact-history'])
            proven=dispatch(self.s,dict(base,op='form_evidence',job_key=self.key,action='submit_application',
                                       clicked_at=self.s.now(),after=after))
            dispatch(self.s,dict(base,op='reconcile',attempt_id=aid,**proven))
            self.assertEqual(canary.audit(self.s)['pending_accounts'],[{'job_key':self.key,'portal':account_flow.accounts.hostname(job['link'])}])
            with self.assertRaises(Denied):
                dispatch(self.s,dict(click,evidence=self.evidence))
            self.assertEqual(canary.state(self.s)['phase'],'halted')
            self.assertEqual(self.s.con.execute('SELECT COUNT(*) FROM applications').fetchone()[0],1)

    def test_unqualified_route_and_bad_canary_receipt_preserve_database(self):
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")
        self.observe_publication(self.key)
        with self.assertRaisesRegex(Denied,'grouped form'):
            canary.enroll(self.s,dict(job_key=self.key,owner=self.owner,token=self.token,run_id=self.run['run_id']))
        aid=self.prepare()
        with patch('job_runtime.canary.rollout_ready',return_value=True), self.s.tx():
            attempt=self.s.con.execute('SELECT * FROM jr_attempts WHERE id=?',(aid,)).fetchone()
            canary.register(self.s,attempt)
        self.submit(aid)
        with self.assertRaises(Denied):
            self.s.reconcile(self.owner,self.token,aid,'confirmed',{'refs':['fixture:thanks'],'observed_at':self.at.isoformat()})
        self.assertEqual(canary.state(self.s)['phase'],'halted')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],0)
        self.assertEqual(self.s.con.execute('SELECT state FROM jr_attempts').fetchone()[0],'submit_started')

    def test_five_distinct_openings_and_settled_quota_required_for_expansion(self):
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")
        keys=[self.key]+[self.job(company='Org '+str(i),suffix=str(20000+i)) for i in range(1,6)]
        self.observe_publication(*keys)
        q=dict(owner=self.owner,token=self.token,run_id=self.run['run_id'],channel='form')
        with patch('job_runtime.canary.rollout_ready',return_value=True):
            for key in keys[:5]:
                canary.enroll(self.s,dict(q,job_key=key))
            with self.assertRaisesRegex(Denied,'five-opening'):
                canary.enroll(self.s,dict(q,job_key=keys[5]))
            self.assertFalse(canary.audit(self.s)['can_expand'])
            with self.assertRaises(Denied):
                canary.expand(self.s,self.owner,self.token)
            # Separate SQL fixtures stand in for five previously fenced browser-click transactions.
            for i,key in enumerate(keys[:5]):
                job=self.s.job(key)
                aid='canary-'+str(i)
                with self.s.tx():
                    self.s.con.execute('INSERT INTO jr_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (aid,key,job['link'],self.run['run_id'],job['company_key'],'form','2026-11-12','a'*64,
                         self.policy['hash'],'prepared','{}',None,self.s.now()))
                    canary.register(self.s, self.s.con.execute('SELECT * FROM jr_attempts WHERE id=?',(aid,)).fetchone())
                    self.s.con.execute("UPDATE jr_attempts SET state='submit_started' WHERE id=?",(aid,))
                self.s.reconcile(self.owner,self.token,aid,'confirmed',dict(proof='portal_confirmation',receipt_kind='history',
                    refs=['fixture:history'],observed_at=self.s.now(),clicked_at=self.s.now(),quotes=['Application submitted'],
                    receipt_job_url=job['link'],receipt_title=job['title'],receipt_url=job['link']))
            self.assertFalse(canary.audit(self.s)['can_expand'])
            meter=Quota(self.path.parent)
            with meter._transaction() as db:
                db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)',('fixture','application',self.owner,1,2,1,10,'settled',2,1))
            self.s.con.execute('INSERT INTO jr_meta VALUES(?,?)',('quota:'+self.run['run_id'],'fixture'))
            report=canary.expand(self.s,self.owner,self.token)
            self.assertEqual((report['charged_points'],len(report['jobs']),canary.state(self.s)['phase']),(2,5,'expanded'))


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(PortalAccessAndRolloutTests(k) for k in PortalAccessAndRolloutTests.__dict__ if k.startswith('test_'))
