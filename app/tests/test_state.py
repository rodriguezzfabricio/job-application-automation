import concurrent.futures
import tempfile
from pathlib import Path
import unittest
from state import Store

class StateTests(unittest.TestCase):
    def setUp(self):tmp_root=Path(__file__).resolve().parents[1]/'.test-tmp';tmp_root.mkdir(exist_ok=True);self.tmp=tempfile.TemporaryDirectory(dir=tmp_root);self.db=Path(self.tmp.name)/'state.sqlite';self.s=Store(self.db)
    def tearDown(self):self.tmp.cleanup()
    def test_unique_immutable_intent(self):
        self.assertTrue(self.s.enqueue('p1','j1',b'first'))
        self.assertFalse(self.s.enqueue('p1','j1',b'changed'))
        self.assertEqual(self.s.intent('p1','j1')['payload'],b'first')
    def test_concurrent_claim_once(self):
        self.s.enqueue('p1','j1',b'first')
        def call(_):return Store(self.db).claim('p1','j1','2026-10-15T12:00:00Z')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(call,range(4)))
        self.assertEqual(sum(results),1)
    def test_uncertain_never_blindly_retries(self):
        self.s.enqueue('p1','j1',b'first');self.s.claim('p1','j1','2026-10-15T12:00:00Z')
        self.s.recover('2026-10-15T12:10:00Z')
        self.assertEqual(self.s.intent('p1','j1')['status'],'delivery_unknown')
        self.assertFalse(self.s.claim('p1','j1','2026-10-15T12:11:00Z'))
    def test_accepted_requires_claim(self):
        self.s.enqueue('p1','j1',b'first')
        with self.assertRaises(ValueError):self.s.finish('p1','j1','capture_accepted')
        self.s.claim('p1','j1','2026-10-15T12:00:00Z');self.s.finish('p1','j1','capture_accepted')
        self.assertEqual(self.s.intent('p1','j1')['status'],'capture_accepted')
    def test_daily_limit_includes_uncertain(self):
        for i in range(3):self.s.enqueue('p1',str(i),b'x')
        self.assertTrue(self.s.claim('p1','0','2026-10-15T12:00:00Z',daily_cap=2))
        self.assertTrue(self.s.claim('p1','1','2026-10-15T12:00:00Z',daily_cap=2))
        self.assertFalse(self.s.claim('p1','2','2026-10-15T12:00:00Z',daily_cap=2))
    def test_budget_concurrency(self):
        def call(i):return Store(self.db).reserve('2026-10',str(i),60,100)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(call,range(4)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.s.budget('2026-10')['reserved'],60)
    def test_budget_negative_and_duplicate(self):
        self.assertFalse(self.s.reserve('2026-10','a',-1,100))
        self.assertTrue(self.s.reserve('2026-10','a',100,100))
        self.assertFalse(self.s.reserve('2026-10','a',1,100))
        self.assertFalse(self.s.reserve('2026-10','b',1,100))
    def test_source_failure_is_not_empty_success(self):
        self.s.snapshot('source',['j'],'2026-10-15T12:00:00Z')
        self.s.snapshot('source',None,'2026-10-15T12:15:00Z',error='HTTP429')
        self.assertTrue(self.s.job('source','j')['active'])
        self.s.snapshot('source',[],'2026-10-15T12:30:00Z')
        self.assertTrue(self.s.job('source','j')['active'])
        self.s.snapshot('source',[],'2026-10-15T12:45:00Z')
        self.assertFalse(self.s.job('source','j')['active'])
    def test_stale_health_and_first_seen_preserved(self):
        self.s.snapshot('source',['j'],'2026-10-15T12:00:00Z')
        self.s.snapshot('source',['j'],'2026-10-15T12:15:00Z')
        self.assertEqual(self.s.job('source','j')['first_seen'],'2026-10-15T12:00:00+00:00')
        self.assertEqual(self.s.health('source','2026-10-15T13:01:00Z'),'stale')

    def test_recent_failure_is_not_reported_as_healthy(self):
        self.s.snapshot('source',['j'],'2026-10-15T12:00:00Z')
        self.s.snapshot('source',None,'2026-10-15T12:01:00Z',error='HTTP429')
        self.assertEqual(self.s.health('source','2026-10-15T12:01:00Z'),'failed')
        self.s.snapshot('source',['j'],'2026-10-15T12:02:00Z')
        self.assertEqual(self.s.health('source','2026-10-15T12:02:00Z'),'healthy')


    def test_reversed_snapshots_do_not_change_newer_jobs(self):
        self.s.snapshot('source',['j'],'2026-10-15T12:00:00Z')
        before=self.s.job('source','j')
        for ids in [[],['j'],[]]:
            with self.assertRaises(ValueError):self.s.snapshot('source',ids,'2026-10-15T11:00:00Z')
        self.assertEqual(self.s.job('source','j'),before)

class MonthlyLimitTests(unittest.TestCase):
    setUp=StateTests.setUp
    tearDown=StateTests.tearDown
    def test_month_limit_survives_day_reset(self):
        self.s.enqueue('p','a',b'a');self.s.enqueue('p','b',b'b')
        self.assertTrue(self.s.claim('p','a','2026-10-15T23:00:00Z',monthly_cap=1))
        self.assertFalse(self.s.claim('p','b','2026-10-16T01:00:00Z',monthly_cap=1))

if __name__=='__main__':unittest.main()
