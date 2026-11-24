import datetime as dt
import unittest
from test_runtime import RuntimeTests
from job_runtime.operations import begin_cycle
from job_runtime.policy import effective
from job_runtime.store import Denied

class BootstrapTests(RuntimeTests):
    def test_legacy_lock_blocks_bootstrap_without_claiming_lease(self):
        self.s.release(self.owner,self.token)
        lock=self.path.parent/'scratch'/'run.lock'
        lock.parent.mkdir();lock.write_text('legacy owner')
        with self.assertRaisesRegex(Denied,'legacy worker lock'):
            begin_cycle(self.s,'next',self.packet)
        self.assertEqual(self.s.con.execute("SELECT owner FROM jr_leases").fetchone()[0],'')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM jr_runs').fetchone()[0],1)
    def test_production_start_requires_fresh_commands(self):
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")
        self.at+=dt.timedelta(minutes=20)
        with self.assertRaisesRegex(Denied,'re-read'):
            self.s.start(self.owner,self.token,effective({}))
    def test_early_scheduled_tick_maps_to_next_hour_full(self):
        self.s.heartbeat(self.owner,self.token,seconds=7200)
        self.at=dt.datetime(2026,11,12,17,58,28,tzinfo=dt.timezone.utc)
        r=self.s.start(self.owner,self.token,self.policy)
        self.assertEqual(r['slot'],'2026-11-12T13:00:00-05:00');self.assertEqual(r['mode'],'FULL')
    def test_early_fast_and_on_time_tick_share_slot(self):
        self.at+=dt.timedelta(minutes=18,seconds=28)
        r=self.s.start(self.owner,self.token,self.policy)
        self.at+=dt.timedelta(minutes=2)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.s.start(self.owner,self.token,self.policy)
        self.assertEqual(r['mode'],'FAST')
    def test_invalid_bootstrap_releases_lease_without_starting(self):
        self.s.release(self.owner,self.token)
        self.at+=dt.timedelta(minutes=20)
        with self.assertRaises(Denied):begin_cycle(self.s,'next',self.packet)
        self.assertEqual(self.s.con.execute("SELECT owner FROM jr_leases").fetchone()[0],'')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM jr_runs').fetchone()[0],1)
    def test_bootstrap_imports_commands_and_records_receipt(self):
        self.s.release(self.owner,self.token);self.at+=dt.timedelta(minutes=20)
        self.packet['observed_at']=self.at.isoformat()
        r=begin_cycle(self.s,'next',self.packet)
        self.assertEqual(self.s.con.execute("SELECT status FROM jr_actions WHERE run_id=? AND stage='commands'",(r['run_id'],)).fetchone()[0],'succeeded')
        self.s.release('next',r['token'])
    def test_empty_failed_run_can_resume_with_history_preserved(self):
        self.s.con.execute("UPDATE jr_runs SET status='partial',ended=? WHERE id=?",(self.s.now(),self.run['run_id']))
        self.s.release(self.owner,self.token)
        r=begin_cycle(self.s,'replacement',self.packet,self.run['run_id'],{'refs':['fixture:stopped'],'stopped_owner':self.owner})
        self.assertTrue(r['resumed']);self.assertEqual(r['run_id'],self.run['run_id'])
        self.assertEqual(self.s.con.execute("SELECT count(*) FROM events WHERE kind='codex-empty-run-resumed'").fetchone()[0],1)
        self.s.release('replacement',r['token'])
    def test_completed_stage_cannot_be_erased_by_empty_resume(self):
        from job_runtime.operations import record_stage
        record_stage(self.s,self.run['run_id'],'commands',self.owner,self.token,'succeeded',{'refs':['fixture:commands']})
        self.s.con.execute("UPDATE jr_runs SET status='partial',ended=? WHERE id=?",(self.s.now(),self.run['run_id']))
        self.s.release(self.owner,self.token)
        with self.assertRaisesRegex(Denied,'run has work'):
            begin_cycle(self.s,'replacement',self.packet,self.run['run_id'],{'refs':['fixture:stopped'],'stopped_owner':self.owner})


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(BootstrapTests(name) for name in BootstrapTests.__dict__ if name.startswith('test_'))
