import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'stopgap-tools'))
import tracker
import title_filter
import poll_lists
from job_runtime.store import Store,Denied
from job_runtime.policy import UTC,ET,effective
from job_runtime.coordinator import next_action,finish
from job_runtime.cloud import phone_decision
from job_runtime.resumes import publish,recover

class IntegrationTests(unittest.TestCase):
    def test_full_and_fast_fake_end_to_end(self):
        for minute,mode in ((0,'FULL'),(20,'FAST')):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'tracker.sqlite';c=sqlite3.connect(path);c.executescript(tracker.SCHEMA);c.close()
                now=dt.datetime(2026,11,12,17,minute,tzinfo=UTC);s=Store(path,lambda:now);token=s.acquire('fake');run=s.start('fake',token,effective({}))
                seen=[]
                while True:
                    action=next_action(s,run['run_id'],'fake',token)
                    if action.get('finished'):break
                    seen.append(action['stage']);s.receipt('fake',token,action['id'],'succeeded',{'refs':['fake-adapter:'+action['stage']],'counts':{'input':0,'output':0}})
                self.assertEqual(seen,run['expected']);self.assertTrue(finish(s,run['run_id'],'fake',token)['success']);s.con.close()
    def test_failed_stage_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'t.sqlite';c=sqlite3.connect(p);c.executescript(tracker.SCHEMA);c.close();s=Store(p,lambda:dt.datetime(2026,11,12,17,tzinfo=UTC));tok=s.acquire('x');r=s.start('x',tok,effective({}));a=next_action(s,r['run_id'],'x',tok)
            s.receipt('x',tok,a['id'],'failed',{'refs':['fake:drive-error']},cursor='bad')
            self.assertEqual(next_action(s,r['run_id'],'x',tok)['stage'],'resume_sync')
            self.assertIsNone(s.con.execute('SELECT * FROM jr_cursors').fetchone());self.assertFalse(finish(s,r['run_id'],'x',tok)['success']);s.con.close()
    def test_phone_exact_boundaries_fake_sink_only(self):
        now=dt.datetime(2026,11,12,18,45,tzinfo=UTC)
        for age,expected in ((100,'fresh'),(100.01,'early-stale'),(160,'early-stale'),(160.01,'reminder')):
            status={'created_at':(now-dt.timedelta(minutes=age)).isoformat(),'title_time':'11:00','line':'x'*250,'phone_updates':'every run'}
            result=phone_decision(now,status);self.assertTrue(result['send']);self.assertTrue(result['dedup'].startswith(expected));self.assertLessEqual(len(result['text']),200)
            self.assertFalse(phone_decision(now,status,result['dedup'])['send'])
        self.assertFalse(phone_decision(now+dt.timedelta(hours=1))['send'])
    def test_phone_daily_and_dst(self):
        for date,utc_hour in ((dt.date(2026,7,1),22),(dt.date(2026,11,12),23)):
            now=dt.datetime.combine(date,dt.time(utc_hour,45),UTC)
            status={'created_at':now.isoformat(),'phone_updates':'daily','push_hour':18,'line':'fixture'}
            self.assertTrue(phone_decision(now,status)['send']);self.assertFalse(phone_decision(now-dt.timedelta(hours=1),dict(status,created_at=(now-dt.timedelta(hours=1)).isoformat()))['send'])
    def test_title_scope_no_sweden_false_positive(self):
        self.assertTrue(title_filter.decide('SWE New Grad','United States')['keep'])
        self.assertFalse(title_filter.decide('Software Engineer','Stockholm, SWE')['keep'])
        for title in ('Junior Data Scientist','Data Analyst I','Member of Technical Staff'):
            self.assertTrue(title_filter.decide(title,'US')['keep'],title)
    def test_resume_publish_and_interruption_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'source';lib=root/'library';src.mkdir();lib.mkdir()
            (src/'new.pdf').write_bytes(b'unchanged-new');(lib/'old.pdf').write_bytes(b'old');(lib/'manifest.json').write_text('{"old":true}')
            from job_runtime.backup import file_hash
            man={'resumes':[{'file':'new.pdf','sha256':file_hash(src/'new.pdf')}]}
            retired=publish(src,lib,man,lambda p:None)
            self.assertEqual((lib/'new.pdf').read_bytes(),b'unchanged-new');self.assertEqual((Path(retired)/'old.pdf').read_bytes(),b'old')
            (lib.parent/'resume-publish-journal.json').write_text(json.dumps({'backup':retired}));self.assertTrue(recover(lib));self.assertEqual(json.loads((lib/'manifest.json').read_text()),{'old':True})

if __name__=='__main__':unittest.main()
