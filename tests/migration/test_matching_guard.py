import unittest
from unittest.mock import patch
from test_runtime import RuntimeTests
from job_runtime.matching import commit_match
from job_runtime.store import Denied

class MatchingGuardTests(RuntimeTests):
    def test_partial_posting_cannot_be_committed(self):
        with self.assertRaisesRegex(Denied, 'complete observed'):
            commit_match(self.s,self.key,'snippet',{'gates':{}},{'refs':['fixture:snippet'],'description_complete':False},self.owner,self.token)
    def test_terminal_application_is_not_rematched(self):
        self.s.con.execute("UPDATE jobs SET status='Applied' WHERE job_key=?",(self.key,))
        with patch('job_runtime.matching.legacy') as library:
            with self.assertRaisesRegex(Denied,'terminal job'):
                commit_match(self.s,self.key,'full',{'gates':{}},{'refs':['fixture:full'],'description_complete':True},self.owner,self.token)
            library.assert_not_called()
        self.assertEqual(self.s.job(self.key)['resume'],'swe.pdf')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(MatchingGuardTests(name) for name in ('test_partial_posting_cannot_be_committed','test_terminal_application_is_not_rematched'))
