import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'stopgap-tools'))
from test_runtime import RuntimeTests
from job_runtime.store import Denied
from job_runtime import adapters
from job_runtime.health import health_events
from job_runtime.policy import UTC

class LimitsTests(RuntimeTests):
    # Inherit fixture helpers, not tests; loader below excludes inherited cases.
    def test_independent_daily_caps(self):
        self.packet['settings'].update(max_fills_per_day=1,one_click_per_day=1)
        adapters.import_commands(self.s,self.packet,self.owner,self.token)
        self.prepare()
        second=self.job('Another','2');self.prepare(second,'one_click')
        third=self.job('Third','3')
        with self.assertRaisesRegex(Denied,'daily channel'):self.prepare(third)
        with self.assertRaisesRegex(Denied,'daily channel'):self.prepare(third,'one_click')
    def test_company_limit_shared_between_channels(self):
        a=self.job('Same','a',title='Software Engineer I');b=self.job('Same','b',title='Data Analyst I');c=self.job('Same','c',title='Junior Data Scientist')
        self.prepare(a);self.prepare(b,'one_click')
        with self.assertRaisesRegex(Denied,'company daily'):self.prepare(c)
    def test_finishing_counted_form_does_not_consume_second_daily_slot(self):
        self.packet['settings']['max_fills_per_day']=1
        adapters.import_commands(self.s,self.packet,self.owner,self.token)
        day=self.at.astimezone(__import__('job_runtime.policy',fromlist=['ET']).ET).date().isoformat()
        self.s.con.execute('UPDATE jobs SET notes=? WHERE job_key=?',('filled '+day,self.key))
        attempt=self.prepare()
        self.submit(attempt)
        other=self.job('Other','other')
        with self.assertRaisesRegex(Denied,'daily channel'):self.prepare(other)
    def test_actual_pdf_bytes_are_verified(self):
        lib=self.path.parent/'my-resumes';lib.mkdir();pdf=lib/'swe.pdf';pdf.write_bytes(b'original')
        sha=hashlib.sha256(pdf.read_bytes()).hexdigest();(lib/'manifest.json').write_text(json.dumps({'resumes':[{'file':'swe.pdf','sha256':sha}]}))
        a=self.s.prepare(self.owner,self.token,self.run['run_id'],self.key,'form',sha,self.review)
        pdf.write_bytes(b'changed')
        with self.assertRaisesRegex(Denied,'PDF bytes changed'):self.s.submit_started(self.owner,self.token,a,sha,self.evidence,production=False)
    def test_unknown_attempt_survives_ownership_recovery(self):
        a=self.prepare();self.submit(a);self.s.recover_stopped({'refs':['fixture:worker-stopped']});self.token=self.s.acquire(self.owner)
        self.assertEqual(self.s.con.execute('SELECT state FROM jr_attempts WHERE id=?',(a,)).fetchone()[0],'submit_started')
    def test_backlog_survives_and_exposes_age(self):
        adapters.defer(self.s,'descriptions','job',{'link':'https://example.test/job'},self.owner,self.token)
        self.at+=dt.timedelta(minutes=15)
        self.assertEqual(adapters.backlog(self.s,'descriptions')['oldest_age_seconds'],900)
    def test_ready_never_bypasses_seniority(self):
        a=self.job('Senior','s',title='Senior Software Engineer',status='Ready to fill')
        with self.assertRaisesRegex(Denied,'senior'):self.prepare(a)
    def test_midnight_revalidates_reservation(self):
        self.s.release(self.owner,self.token)
        self.at=dt.datetime(2026,11,13,4,59,50,tzinfo=UTC);self.token=self.s.acquire(self.owner)
        self.run=self.s.start(self.owner,self.token,self.policy)
        self.packet['observed_at']=self.at.isoformat();adapters.import_commands(self.s,self.packet,self.owner,self.token);a=self.prepare()
        self.at+=dt.timedelta(seconds=20);self.packet['observed_at']=self.at.isoformat();adapters.import_commands(self.s,self.packet,self.owner,self.token)
        with self.assertRaisesRegex(Denied,'ET day changed'):self.submit(a)

    def test_acceptance_grant_requires_stopped_legacy_worker(self):
        with self.assertRaises(Denied):
            self.s.acceptance_grant(self.owner,self.token,[self.key],{'refs':['fixture:paused'],'legacy_schedule_paused':True})
        g=self.s.acceptance_grant(self.owner,self.token,[self.key],{'refs':['fixture:stopped'],'legacy_schedule_paused':True,'legacy_stages_stopped':True})
        self.assertEqual(g['job_keys'],[self.key])
        self.s.end_acceptance(self.owner,self.token)
        self.assertIsNone(self.s.con.execute("SELECT * FROM jr_meta WHERE key='acceptance_grant'").fetchone())
    def test_resume_prepared_form_preserves_hash_and_attempt(self):
        a=self.prepare();self.s.release(self.owner,self.token);self.at+=dt.timedelta(minutes=20);self.token=self.s.acquire(self.owner)
        self.packet['observed_at']=self.at.isoformat();adapters.import_commands(self.s,self.packet,self.owner,self.token)
        self.run=self.s.start(self.owner,self.token,self.policy)
        r=self.s.adopt_prepared(self.owner,self.token,a,self.run['run_id'],{'refs':['fixture:existing-form'],'form_observed':True,'answers_preserved':True})
        self.assertEqual(r['attempt_id'],a);self.assertEqual(self.s.con.execute('SELECT resume_hash FROM jr_attempts').fetchone()[0],'a'*64)

def load_tests(loader,tests,pattern):
    return unittest.TestSuite([*(LimitsTests(name) for name in LimitsTests.__dict__ if name.startswith('test_')),loader.loadTestsFromTestCase(HealthTests)])

class HealthTests(unittest.TestCase):
    def test_fake_health_and_recovery(self):
        now=dt.datetime(2026,11,12,20,tzinfo=UTC);old=(now-dt.timedelta(hours=3)).isoformat()
        events=health_events(now,{'last_success_at':old,'browser_last_success_at':old},{},{})
        self.assertEqual({x['key'] for x in events},{'finish','browser'})
    def test_recovery_is_emitted_once_after_confirmed_delivery(self):
        now=dt.datetime(2026,11,12,20,tzinfo=UTC);old=(now-dt.timedelta(hours=3)).isoformat();fresh={'last_success_at':now.isoformat(),'browser_last_success_at':now.isoformat()}
        self.assertEqual(health_events(now,fresh,{}, {'finish':old})[0]['key'],'recovery')
        self.assertEqual(health_events(now,fresh,{}, {'finish':old,'recovery':now.isoformat()}),[])
