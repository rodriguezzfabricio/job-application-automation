import datetime as dt
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from job_runtime import learning
from job_runtime.store import Denied
from test_runtime import RuntimeTests


class LearningTests(RuntimeTests):
    def test_replay_dedup_and_real_success_then_recurrence(self):
        rid=self.run['run_id']
        for _ in range(2):
            learning.observe(self.s,rid,self.key,'question','Private autofill left the credential fields empty.')
        learning.observe(self.s,rid,'second opening','question','Private autofill hotkey failed.')
        one=learning.review(self.s)[0]
        self.assertEqual((one['run_count'],one['open_count']),(1,2))
        self.assertIsNone(learning.next_fix([one]))
        self.at+=dt.timedelta(minutes=3)
        learning.observe(self.s,'later',self.key,'question','Private autofill hotkey failed.')
        self.assertEqual(learning.next_fix(learning.review(self.s))['issue'],'private_autofill')
        learning.verified(self.s,'private_autofill',self.key,'Original form and signed-in control observed.')
        self.assertEqual(learning.review(self.s)[0]['open_count'],1)
        self.at+=dt.timedelta(minutes=1)
        learning.observe(self.s,'still later',self.key,'question','Private autofill hotkey failed.')
        self.assertEqual(learning.review(self.s)[0]['open_count'],2)

    def test_private_values_and_links_are_excluded_from_exports(self):
        raw='City? Email jane@example.invalid. Password: example-secret Token=secret456. Verification code 762714. https://example.invalid/activate?s=private'
        learning.observe(self.s,self.run['run_id'],self.key,'question',raw)
        self.assertEqual(learning.export(self.s,self.run['run_id'])['owner_questions'],1)
        root=self.s.path.parent/'codex-runtime'
        for path in [root/'learning'/'review.md',root/'learning'/'openings.csv',root/'runs'/self.run['run_id']/'learning.json']:
            for forbidden in ['jane@','example-secret','secret456','762714','s=private']:
                self.assertNotIn(forbidden,path.read_text())
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_startup_skip_and_new_packet_use_no_extra_model_call(self):
        learning.operation_error(self.s,None,'begin',self.owner,'fresh command packet required')
        learning.operation_error(self.s,None,'begin',self.owner,'fresh command packet required')
        self.assertEqual(learning.review(self.s)[0]['run_count'],1)
        learning.startup_succeeded(self.s,self.owner,self.run['run_id'])
        self.assertEqual(learning.review(self.s)[0]['state'],'Verified')
        dest=self.s.path.parent/'codex-runtime'/'scheduled-skips'/'fixture-usage-pace.json'
        dest.parent.mkdir(parents=True);dest.write_text(json.dumps({'owner':'other','learning_issue':'usage_pace','at':self.s.now(),'blocker':'weekly allowance'}))
        self.assertEqual(learning.short_summary(self.s)['open_issues'],1)

    def test_improvement_budget_and_test_result_are_separate_from_live_proof(self):
        rid=self.run['run_id']
        q=dict(owner=self.owner,token=self.token,run_id=rid,issue='private_autofill',test_file='test_learning.py',change='Validate the original employer form.')
        for run in (rid,'earlier'):
            learning.observe(self.s,run,self.key,'question','Private autofill left the account form empty.')
        self.s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',('pace:'+rid,json.dumps({'mode':'lean'})))
        with self.assertRaises(Denied):learning.trial(self.s,q)
        self.s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',('pace:'+rid,json.dumps({'mode':'run'})))
        with patch('job_runtime.learning.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'Ran 2 tests in 0.055s\nOK')) as run:
            self.assertEqual(learning.trial(self.s,q)['result'],'passed')
            self.assertTrue(learning.trial(self.s,q)['already_recorded'])
            self.assertEqual(run.call_count,1)
            self.assertEqual(learning.review(self.s)[0]['state'],'Test passed · awaiting live proof')
        self.assertEqual(learning.short_summary(self.s)['open_issues'],1)

    def test_recurrence_after_live_check_in_one_run_reopens_once(self):
        rid=self.run['run_id']
        learning.observe(self.s,rid,self.key,'question','Private autofill left fields empty.')
        self.at+=dt.timedelta(seconds=2)
        learning.verified(self.s,'private_autofill',self.key,'Original form reopened signed in.')
        self.at+=dt.timedelta(seconds=2)
        learning.observe(self.s,rid,self.key,'question','Private autofill failed again.')
        result=learning.review(self.s)[0]
        self.assertEqual((result['run_count'],result['open_count']),(1,1))
        self.assertIn('again',result['subjects'][self.key]['detail'])

    def test_stage_summary_does_not_keep_a_verified_opening_open(self):
        rid=self.run['run_id']
        why='Private autofill left the credential fields empty.'
        learning.observe(self.s,rid,self.key,'question',why)
        learning.stage(self.s,rid,'fill','handoff',{'reason':why})
        learning.verified(self.s,'private_autofill',self.key,'Private fill and original signed-in opening verified.')
        self.assertEqual(learning.export(self.s,rid)['open_issues'],0)
        path=self.s.path.parent/'codex-runtime'/'runs'/rid/'learning.json'
        self.assertEqual(len(json.loads(path.read_text())),2)

    def test_two_runs_share_one_daily_trial(self):
        rid=self.run['run_id']
        self.s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',('pace:'+rid,json.dumps({'mode':'run'})))
        self.s.con.execute('INSERT INTO jr_learning_trials VALUES(?,?,?,?,?,?)',('prior','private_autofill',self.s.now(),'test_learning.py','Checked the field.','failed'))
        for run in (rid,'prior'):
            learning.observe(self.s,run,self.key,'question','Private autofill left fields empty.')
        q=dict(owner=self.owner,token=self.token,run_id=rid,issue='private_autofill',test_file='test_learning.py',change='Rechecked the field.')
        with self.assertRaisesRegex(Denied,'daily'):
            learning.trial(self.s,q)

    def test_an_interrupted_funded_run_is_reported_once_at_next_gate(self):
        rid=self.run['run_id']
        self.s.con.execute('INSERT INTO jr_meta VALUES (?,?)',('quota:'+rid,'fixture-reservation'))
        self.assertFalse(learning.capture_unfinished(self.s))
        self.at+=dt.timedelta(hours=2)
        self.assertTrue(learning.capture_unfinished(self.s))
        self.assertFalse(learning.capture_unfinished(self.s))
        self.assertEqual(learning.review(self.s)[0]['run_count'],1)

    def test_partial_feed_success_retains_each_failed_source(self):
        from job_runtime.operations import import_feed_batch
        data={'postings':[],'stats':{'boards':{'fetched':7,'errors':['example/public: HTTP Error 404: Not Found']},
                                    'workday':{'fetched':0,'errors':['example/site: HTTP Error 403: Forbidden']}}}
        for _ in range(2):
            import_feed_batch(self.s,data,self.owner,self.token,'fixture:feed',self.run['run_id'])
        found=learning.review(self.s)
        self.assertEqual([(f['kind'],f['run_count']) for f in found],[('Discovery source',1)]*2)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(LearningTests(name) for name in LearningTests.__dict__ if name.startswith('test_'))
