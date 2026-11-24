import unittest
from test_runtime import RuntimeTests
from job_runtime.store import Denied


class LegacyWorkerGuardTests(RuntimeTests):
    def test_resumed_legacy_worker_blocks_final_marker(self):
        attempt=self.prepare()
        lock=self.path.parent/'scratch'/'run.lock'
        lock.parent.mkdir()
        lock.write_text('{"mode":"FAST","pid":12345}')
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")
        with self.assertRaisesRegex(Denied,'legacy worker lock present'):
            self.s.submit_started(self.owner,self.token,attempt,'a'*64,self.evidence)
        self.assertEqual(self.s.con.execute('SELECT state FROM jr_attempts WHERE id=?',(attempt,)).fetchone()[0],'prepared')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],0)

    def test_unparseable_old_lock_is_not_assumed_stopped(self):
        attempt=self.prepare()
        lock=self.path.parent/'scratch'/'run.lock'
        lock.parent.mkdir()
        lock.write_text('old or incomplete state')
        with self.assertRaisesRegex(Denied,'verify stopped legacy worker'):
            self.s.submit_started(self.owner,self.token,attempt,'a'*64,self.evidence)
        self.assertTrue(lock.exists())


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(LegacyWorkerGuardTests(n) for n in LegacyWorkerGuardTests.__dict__ if n.startswith('test_'))
