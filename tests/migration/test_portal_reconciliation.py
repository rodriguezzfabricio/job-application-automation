from test_runtime import RuntimeTests
from job_runtime.outcomes import reconcile_portal,apply_outcome
from job_runtime.store import Denied
from job_runtime.adapters import import_record,import_commands
import unittest

class PortalReconciliationTests(RuntimeTests):
    def proof(self):
        return {'proof':'portal_application_submitted','refs':['fixture:portal'],'quotes':['Application submitted'],
                'observed_at':self.at.isoformat(),'job_url':self.s.job(self.key)['link'],
                'receipt_title':self.s.job(self.key)['title'],'receipt_job_url':self.s.job(self.key)['link'],
                'receipt_url':self.s.job(self.key)['link'],'receipt_kind':'history'}
    def test_external_submission_is_idempotent_and_unattributed(self):
        self.assertTrue(reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)['created'])
        self.assertFalse(reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)['created'])
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],1)
        self.assertEqual(self.s.con.execute('SELECT submitted_by FROM applications').fetchone()[0],'unattributed')
        self.assertEqual(self.s.audit(self.run['run_id'])['confirmed_submissions'],0)
        self.assertEqual(self.s.audit(self.run['run_id'])['reconciled_prior_submissions'],1)
        with self.assertRaisesRegex(Denied,'not actionable|already applied'):self.prepare()
    def test_workday_alias_receipt_stays_with_its_own_employer_site_and_requisition(self):
        from job_runtime.policy import history_identity
        base='https://acme.wd5.myworkdayjobs.com'
        url=base+'/en-US/External/job/City/Software-Engineer_JR1234-1'
        alias=base+'/external/job/Software-Engineer_JR1234/apply'
        self.assertEqual(history_identity(url),history_identity(alias))
        self.assertNotEqual(history_identity(url),history_identity(alias.replace('JR1234','JR1235')))
        self.assertNotEqual(history_identity(url),history_identity(alias.replace('/external/','/division2/')))
        shared='https://wd5.myworkdaysite.com/recruiting/acme/external/job/Software-Engineer_JR1234'
        self.assertNotEqual(history_identity(shared),history_identity(shared.replace('/acme/','/another/')))
        key=self.job(company='Acme',link=url)
        other=self.job(company='Legacy alias',link=alias)
        self.s.con.execute('UPDATE jobs SET company=?,company_key=? WHERE job_key=?',('Acme',self.s.job(key)['company_key'],other))
        proof=dict(self.proof(),job_url=url,receipt_job_url=alias,receipt_url=alias,receipt_title=self.s.job(key)['title'])
        self.assertTrue(reconcile_portal(self.s,key,proof,self.owner,self.token)['created'])
        self.assertTrue(self.s.already_applied(self.s.job(other)))

    def test_displayed_history_date(self):
        from datetime import timedelta
        from job_runtime.outcomes import history_date
        day = (self.at - timedelta(days=1)).date()
        proof = dict(self.proof(), observed_applied_date=day.isoformat(), history_date_quote=day.strftime('Date applied, %m/%d/%y'))
        self.assertEqual(reconcile_portal(self.s,self.key,proof,self.owner,self.token)['date_basis'],'portal_applied_date')
        self.assertEqual(self.s.con.execute('SELECT applied_at FROM applications').fetchone()[0], day.isoformat())
        self.assertIsNone(history_date(dict(proof, history_date_quote='Application sent yesterday'), self.at))
    def test_lever_apply_alias_has_one_receipt_and_one_outcome(self):
        posting='https://jobs.lever.co/example/11111111-2222-3333-4444-555555555555'
        keys=[self.job(company='Same Company',link=posting), self.job(company='Legacy row',suffix='other')]
        self.s.con.execute("UPDATE jobs SET company=?,company_key=?,link=? WHERE job_key=?",('Same Company',self.s.job(keys[0])['company_key'],posting+'/apply',keys[1]))
        proof=dict(self.proof(),receipt_title='Software Engineer',receipt_job_url=posting,receipt_url=posting,receipt_company='Same Company')
        proof.pop('job_url',None)
        self.assertTrue(reconcile_portal(self.s,keys[0],proof,self.owner,self.token)['created'])
        self.assertFalse(reconcile_portal(self.s,keys[1],proof,self.owner,self.token)['created'])
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],1)
        with self.assertRaises(Denied):reconcile_portal(self.s,keys[0],dict(proof,receipt_company='Another employer'),self.owner,self.token)

    def test_portal_receipt_clears_only_its_legacy_hold(self):
        from job_runtime.adapters import defer
        defer(self.s,'legacy_unknown_submission',self.key,{'job_key':self.key},self.owner,self.token)
        other=self.job(suffix='other',title='Data Engineer')
        defer(self.s,'legacy_unknown_submission',other,{'job_key':other},self.owner,self.token)
        reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)
        self.assertEqual([h['job_key'] for h in self.s.legacy_unknown_holds()],[other])
    def test_earlier_email_receipt_does_not_credit_worker(self):
        aid=self.prepare();self.submit(aid)
        proof={'refs':['fixture:email'],'observed_at':self.at.isoformat(),'proof':'confirmation_email',
               'quotes':['Your application was received'], 'receipt_kind':'email', 'receipt_url':'https://mail.google.com/mail/u/0/#inbox/message1','receipt_message_id':'message1',
               'receipt_title':self.s.job(self.key)['title'],'receipt_job_url':self.s.job(self.key)['link'],
               'submitted_by':'unattributed','submitted_at':'2026-11-12T14:00:00Z',
               'attribution_reason':'Receipt predates this worker attempt.'}
        self.s.reconcile(self.owner,self.token,aid,'confirmed',proof)
        app=dict(self.s.con.execute('SELECT * FROM applications').fetchone())
        self.assertEqual(app['submitted_by'],'unattributed')
        self.assertEqual(app['applied_at'],'2026-11-12T09:00:00-05:00')
        self.assertEqual(app['source_id'],aid)
        self.assertEqual(self.s.audit(self.run['run_id'])['confirmed_submissions'],0)
        self.assertFalse(self.s.audit(self.run['run_id'])['inconsistent_submission_receipts'])
    def test_wrong_requisition_and_own_attempt_do_not_reconcile(self):
        with self.assertRaises(Denied):reconcile_portal(self.s,self.key,dict(self.proof(),job_url='https://example.test/another'),self.owner,self.token)
        self.prepare()
        with self.assertRaisesRegex(Denied,'attempt reconciliation'):reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)
    def test_older_outcome_never_overwrites_newer_outcome(self):
        reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)
        self.s.con.execute("UPDATE applications SET result='interview',result_at='2026-11-12'")
        j=self.s.job(self.key)
        r={'company':j['company'],'title':j['title'],'link':j['link'],'result':'rejected','applied_at':'2026-11-01','result_at':'2026-11-05'}
        evidence={'refs':['fixture:old-email'],'quotes':['pursue other candidates']}
        self.assertTrue(apply_outcome(self.s,'old-message',r,evidence,self.owner,self.token))
        self.assertEqual(tuple(self.s.con.execute('SELECT result,result_at FROM applications').fetchone()),('interview','2026-11-12'))
        self.assertFalse(apply_outcome(self.s,'old-message',r,evidence,self.owner,self.token))
        self.assertEqual(self.s.con.execute("SELECT count(*) FROM events WHERE kind='outcome-stale-preserved'").fetchone()[0],1)
    def test_exact_different_requisition_never_title_merges(self):
        reconcile_portal(self.s,self.key,self.proof(),self.owner,self.token)
        obj={'company':'Example','title':'Software Engineer','link':'https://example.test/jobs/different-requisition'}
        second=import_record(self.s,'fixture','different-requisition',obj,self.owner,self.token)['job_key'];j=self.s.job(second)
        record={'company':j['company'],'title':j['title'],'link':j['link'],'result':'applied','applied_at':'2026-11-12'}
        apply_outcome(self.s,'new-requisition-confirmation',record,{'refs':['fixture:email'],'quotes':['received your application'],'observed_at':self.at.isoformat(),
            'receipt_title':j['title'],'receipt_job_url':j['link'],'receipt_url':'https://mail.google.com/mail/u/0/#inbox/message2'},self.owner,self.token)
        self.assertEqual(self.s.con.execute('SELECT count(DISTINCT job_key) FROM applications').fetchone()[0],2)
    def test_dashboard_done_uses_exact_key_for_same_title_query_ids(self):
        obj={'company':'Query Employer','title':'Software Engineer','link':'https://example.test/jobs?posting_id=one'}
        first=import_record(self.s,'fixture','query-one',obj,self.owner,self.token)['job_key']
        second=import_record(self.s,'fixture','query-two',dict(obj,link='https://example.test/jobs?posting_id=two'),self.owner,self.token)['job_key']
        self.packet['commands']['Jobs']=[['Your call','job_key'],['applied myself',second]]
        import_commands(self.s,self.packet,self.owner,self.token)
        self.assertEqual(self.s.con.execute('SELECT job_key FROM applications').fetchone()[0],second)
        self.assertNotEqual(self.s.job(first)['status'],'Applied')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(PortalReconciliationTests(n) for n in PortalReconciliationTests.__dict__ if n.startswith('test_'))
