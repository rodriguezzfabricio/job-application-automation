from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from job_runtime.network import public_read

class NetworkTests(unittest.TestCase):
    def test_rate_limit_retries_exactly_twice_without_real_sleep(self):
        calls=[];delays=[]
        def opener(req,timeout):calls.append(req.full_url);raise HTTPError(req.full_url,429,'rate limit',{},None)
        with self.assertRaises(HTTPError) as error:public_read('https://example.test/',opener=opener,sleep=delays.append)
        error.exception.close()
        self.assertEqual(len(calls),3);self.assertEqual(delays,[30,30])
    def test_post_allowlist(self):
        with self.assertRaises(ValueError):public_read('https://example.test/jobs',data=b'{}',method='POST')

if __name__=='__main__':unittest.main()
