import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'stopgap-tools'))
import tracker
from job_runtime.policy import UTC, ET, effective, canonical_url, gate
from job_runtime.store import Store, Denied
from job_runtime import adapters
from job_runtime.planner import plan

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'tracker.sqlite'
        c=sqlite3.connect(self.path);c.executescript(tracker.SCHEMA);c.close()
        self.at=dt.datetime(2026,11,12,17,0,tzinfo=UTC)
        self.s=Store(self.path,lambda:self.at);self.owner='fixture';self.token=self.s.acquire(self.owner)
        self.packet={'settings':{'manual_companies':['Example Studio','Example Media'], 'held_companies':['Sample Labs']},'observed_at':self.at.isoformat(),'source_ref':'fixture:sheet',
                     'commands':{'Today':[['Section','Mark done / skip','job_key']], 'Jobs':[['Your call','job_key']], 'Recruiters':[['Status','person_key']], 'Companies':[['Research status','Company']]}}
        self.policy=adapters.import_commands(self.s,self.packet,self.owner,self.token)['policy']
        self.run=self.s.start(self.owner,self.token,self.policy)
        self.key=self.job()
        self.review={'refs':['fixture:job-page'],'requirements_verified':True,'requirements':{}}
        self.evidence={'refs':['fixture:form-review'],'attachment_verified':True,'answers_verified':True,'required_unknown':[],'consent_pending':[]}
    def tearDown(self): self.s.con.close();self.tmp.cleanup()
    def job(self,company='Example',suffix='1',**extra):
        obj=dict(company=company,title='Software Engineer',link='https://example.test/jobs/'+suffix,score=75,status='Matched',posted_at=self.at.isoformat(),resume='swe.pdf');obj.update(extra)
        return tracker.upsert_job(self.s.con,obj)[0]
    def prepare(self,key=None,channel='form'): return self.s.prepare(self.owner,self.token,self.run['run_id'],key or self.key,channel,'a'*64,self.review)
    def submit(self,aid,**extra):return self.s.submit_started(self.owner,self.token,aid,'a'*64,dict(self.evidence,**extra),production=False)
    def test_ac01_crash_marker_survives_reopen_and_blocks_retry(self):
        a=self.prepare();self.submit(a);self.s.con.close();self.s=Store(self.path,lambda:self.at)
        with self.assertRaises(Denied):self.submit(a)
        with self.assertRaises(sqlite3.IntegrityError):self.prepare()
        self.assertEqual(self.s.con.execute('SELECT state FROM jr_attempts').fetchone()[0],'submit_started')
    def test_ac02_expired_lease_never_stolen(self):
        other=Store(self.path,lambda:self.at+dt.timedelta(hours=2))
        with self.assertRaises(Denied):other.acquire('other')
        other.recover_stopped({'refs':['fixture:process-stopped']});tok=other.acquire('other')
        with self.assertRaises(Denied):self.s.heartbeat(self.owner,self.token)
        other.release('other',tok);other.con.close()
    def test_duplicate_slot(self):
        with self.assertRaises(sqlite3.IntegrityError):self.s.start(self.owner,self.token,self.policy)
    def test_ac03_pause_changes_after_prepare(self):
        a=self.prepare();self.packet['settings']['paused']='yes';adapters.import_commands(self.s,self.packet,self.owner,self.token)
        with self.assertRaisesRegex(Denied,'paused'):self.submit(a)
    def test_ac03_skip_import_once_and_no_replay(self):
        self.packet['commands']['Jobs'].append(['skip',self.key]);out=adapters.import_commands(self.s,self.packet,self.owner,self.token)
        self.assertEqual(self.s.job(self.key)['status'],'Rejected by me')
        self.assertEqual(adapters.import_commands(self.s,self.packet,self.owner,self.token)['changes']['Jobs']['applied'],0)
    def test_ac03_mirror_never_writes_editable_cells(self):
        current=[['Your call','job_key','Score'],['fill now',self.key,70]]
        result=adapters.mirror_patch('Jobs',current,[current[0],['',self.key,75]])
        self.assertEqual(result['cells'],[{'row':1,'col':2,'value':75}])
    def test_ac04_query_ids_preserved_and_tracking_removed(self):
        self.assertNotEqual(canonical_url('https://e.test/?jobId=A'),canonical_url('https://e.test/?jobId=B'))
        self.assertEqual(canonical_url('https://e.test/?company=ABC&jobId=A&utm_source=x'),canonical_url('https://e.test/?jobId=A&company=ABC'))
    def test_ac04_cloud_reimport_and_distinct_requisitions(self):
        record=dict(company='Other',title='Software Engineer',link='https://e.test/?jobId=A',posted_at=self.at.isoformat())
        x=adapters.import_record(self.s,'drive','rec1',record,self.owner,self.token)
        self.assertEqual(x,adapters.import_record(self.s,'drive','rec1',record,self.owner,self.token))
        y=adapters.import_record(self.s,'drive','rec2',dict(record,link='https://e.test/?jobId=B'),self.owner,self.token)
        self.assertNotEqual(x['job_key'],y['job_key'])
    def test_ac05_current_policy(self):
        p=effective({});self.assertEqual((p['max_fills_per_day'],p['one_click_per_day'],p['one_click_per_run']),(40,40,8))
        j=self.s.job(self.key);j['requirements']={'professional_years_required':3};self.assertEqual(gate(j,p,self.at),'')
        j['requirements']['professional_years_required']=4;self.assertIn('3 required',gate(j,p,self.at))
    def test_ac06_fifty_hours_only_one_click(self):
        j=self.s.job(self.key);j['posted_at']=(self.at-dt.timedelta(hours=50)).isoformat()
        self.assertEqual(gate(j,self.policy,self.at,'one_click'),'');self.assertIn('freshness',gate(j,self.policy,self.at,'form'))
    def test_ac07_changed_attachment(self):
        a=self.prepare()
        with self.assertRaisesRegex(Denied,'attachment changed'):self.s.submit_started(self.owner,self.token,a,'b'*64,self.evidence,production=False)
    def test_ac09_failed_command_packet_rolls_back(self):
        self.packet['commands']['Jobs'].append(['skip',self.key]);self.packet['commands']['Companies']=[['bad']]
        with self.assertRaises(Denied):adapters.import_commands(self.s,self.packet,self.owner,self.token)
        self.assertEqual(self.s.job(self.key)['status'],'Matched')
    def test_ac10_new_message_same_thread_processed_once(self):
        updates=[]
        for mid in ('m1','m1','m2'):adapters.ingest_message(self.s,'gmail',mid,{'thread':'t1'},lambda c:updates.append(mid),self.owner,self.token)
        self.assertEqual(updates,['m1','m2'])
    def test_ac11_ready_company_holds_and_pause(self):
        for company in ('Example Studio','Example Media','Sample Labs'):
            k=self.job(company,company,status='Ready to fill')
            with self.assertRaises(Denied):self.prepare(k)
        j=self.s.job(self.key);j['company']='Example Independent Ventures';self.assertEqual(gate(j,self.policy,self.at),'')
    def test_ac12_required_unknown_blocks_submit(self):
        a=self.prepare()
        for flag in ('required_unknown','consent_pending','auth_required','captcha','payment'):
            with self.assertRaises(Denied):self.submit(a,**{flag:['unknown']})
    def test_ac13_missing_stages_fail_audit(self): self.assertFalse(self.s.audit(self.run['run_id'])['success'])
    def test_audit_rejects_claimed_submission_without_database_evidence(self):
        a=self.s.action(self.run['run_id'],'fill','browser',{},'fill-claim')
        self.s.receipt(self.owner,self.token,a['id'],'succeeded',{'refs':['fixture:claimed-click'],'confirmed_submissions':1})
        report=self.s.audit(self.run['run_id']);self.assertEqual(report['confirmed_submissions'],0);self.assertEqual(report['inconsistent_submission_receipts'],[a['id']])
    def test_ac14_confirmation_atomic_and_idempotent(self):
        a=self.prepare();self.submit(a);proof=dict(refs=['fixture:confirmation'], quotes=['Application submitted'], observed_at=self.at.isoformat(),
            proof='portal_confirmation', receipt_kind='history', receipt_title=self.s.job(self.key)['title'],
            receipt_url=self.s.job(self.key)['link'], receipt_job_url=self.s.job(self.key)['link'])
        self.s.reconcile(self.owner,self.token,a,'confirmed',proof);self.s.reconcile(self.owner,self.token,a,'confirmed',proof)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],1)
        self.assertEqual(self.s.job(self.key)['status'],'Applied')
    def test_unknown_email_absence_never_allows_retry(self):
        a=self.prepare();self.submit(a)
        with self.assertRaises(Denied):self.s.reconcile(self.owner,self.token,a,'not_submitted',{'refs':['no-email'],'observed_at':self.at.isoformat()})
    def test_ac15_cache_uses_description_and_manifest(self):
        k=adapters.cache_key('description','manifest');adapters.cache_put(self.s,k,{'must':['Python']},self.owner,self.token)
        self.assertEqual(adapters.cache_get(self.s,k),{'must':['Python']});self.assertIsNone(adapters.cache_get(self.s,adapters.cache_key('changed','manifest')))
    def test_notification_failed_is_not_delivered(self):
        n=adapters.queue_notification(self.s,'health',{'text':'fixture only'},self.owner,self.token)
        adapters.notification_result(self.s,n['id'],'failed',{'refs':['fake:failed']},self.owner,self.token)
        self.assertEqual(adapters.queue_notification(self.s,'health',{},self.owner,self.token)['state'],'failed')
    def test_usage_unavailable_is_null(self):
        adapters.record_usage(self.s,self.run['run_id'],'match',{'source':'host meter unavailable'},self.owner,self.token)
        self.assertIsNone(self.s.con.execute('SELECT input_tokens FROM jr_usage').fetchone()[0])
    def test_production_submit_disabled_before_cutover(self):
        a=self.prepare()
        with self.assertRaisesRegex(Denied,'cutover'):self.s.submit_started(self.owner,self.token,a,'a'*64,self.evidence)
    def test_stale_policy_packet_blocks_click(self):
        a=self.prepare();self.at+=dt.timedelta(seconds=61)
        with self.assertRaisesRegex(Denied,'re-read'):self.submit(a)
    def test_full_fast_routing_and_dst_slot(self):
        self.assertEqual(self.run['mode'],'FULL');self.assertEqual(plan(self.s,self.policy,'FAST')['to_fill'],[self.key])
        self.at=dt.datetime(2026,11,12,17,20,tzinfo=UTC);r=self.s.start(self.owner,self.token,self.policy);self.assertEqual(r['mode'],'FAST')
        self.assertNotIn('feeds',r['expected']);self.assertIn('commands',r['expected'])
    def test_new_email_outcome_updates_application_once(self):
        from job_runtime.outcomes import apply_outcome
        a=self.prepare();self.submit(a);proof=dict(refs=['fixture:confirmation'], quotes=['Application submitted'], observed_at=self.at.isoformat(),
            proof='portal_confirmation', receipt_kind='history', receipt_title=self.s.job(self.key)['title'],
            receipt_url=self.s.job(self.key)['link'], receipt_job_url=self.s.job(self.key)['link'])
        self.s.reconcile(self.owner,self.token,a,'confirmed',proof)
        rec={'company':'Example','title':'Software Engineer','link':self.s.job(self.key)['link'],'applied_at':self.at.isoformat(),'result':'interview','result_at':'2026-11-14'}
        evidence={'refs':['gmail:message2'],'quotes':['We would like to schedule an interview.']}
        self.assertTrue(apply_outcome(self.s,'message2',rec,evidence,self.owner,self.token))
        self.assertFalse(apply_outcome(self.s,'message2',rec,evidence,self.owner,self.token))
        row=self.s.con.execute('SELECT * FROM applications').fetchone();self.assertEqual(row['result'],'interview');self.assertEqual(row['submitted_by'],'task')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],1)
    def test_unmatched_rejection_does_not_invent_application(self):
        from job_runtime.outcomes import apply_outcome
        with self.assertRaises(Denied):apply_outcome(self.s,'unknown-rejection',{'company':'Unknown','title':'Software Engineer','result':'rejected','applied_at':self.at.isoformat()},{'refs':['gmail:unknown'],'quotes':['unfortunately']},self.owner,self.token)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],0)

if __name__=='__main__': unittest.main()
