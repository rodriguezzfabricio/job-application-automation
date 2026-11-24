import json
import unittest
from test_runtime import RuntimeTests
from job_runtime.operations import record_stage,publish_health

class HealthStateTests(RuntimeTests):
    def test_empty_queue_does_not_fake_a_browser_observation(self):
        record_stage(self.s,self.run['run_id'],'one_click',self.owner,self.token,'succeeded',{'refs':['fixture:queue'],'queue_size':0})
        self.s.con.execute("UPDATE jr_runs SET status='partial',ended=? WHERE id=?",(self.s.now(),self.run['run_id']))
        state=publish_health(self.s,self.run['run_id'],self.s.audit(self.run['run_id']))
        self.assertNotIn('browser_last_success_at',state)
        self.assertEqual(state['last_completed_at'],self.s.now())
    def test_real_browser_observation_advances_its_health_cursor(self):
        record_stage(self.s,self.run['run_id'],'one_click',self.owner,self.token,'succeeded',{'refs':['fixture:portal'],'browser_observed':True})
        self.s.con.execute("UPDATE jr_runs SET status='partial',ended=? WHERE id=?",(self.s.now(),self.run['run_id']))
        state=publish_health(self.s,self.run['run_id'],self.s.audit(self.run['run_id']))
        self.assertEqual(state['browser_last_success_at'],self.s.now())

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HealthStateTests(name) for name in HealthStateTests.__dict__ if name.startswith('test_'))
