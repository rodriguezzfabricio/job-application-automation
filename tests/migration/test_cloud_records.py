import unittest
from test_runtime import RuntimeTests
from job_runtime.adapters import import_record
from job_runtime.store import Denied

class CloudRecordTests(RuntimeTests):
    def test_cloud_source_array_and_excerpt_import_idempotently(self):
        record={'company':'CloudCo','title':'Software Engineer I','link':'https://example.test/cloud/42','source':['https://list.test/','https://employer.test/'],'description_excerpt':'Observed excerpt only','posted_at':'2026-11-12','score':70}
        ack=import_record(self.s,'drive','rec42',record,self.owner,self.token)
        self.assertEqual(import_record(self.s,'drive','rec42',record,self.owner,self.token),ack)
        job=self.s.job(ack['job_key']);self.assertEqual(job['notes'],'Observed excerpt only');self.assertIn('https://employer.test/',job['source']);self.assertEqual(job['resume'],'')
    def test_invalid_record_rejected_before_write(self):
        with self.assertRaisesRegex(Denied,'company'):
            import_record(self.s,'drive','bad',{'link':'https://example.test/a','title':'Engineer'},self.owner,self.token)
        self.assertEqual(self.s.con.execute("SELECT count(*) FROM jr_imports WHERE record_id='bad'").fetchone()[0],0)

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(CloudRecordTests(name) for name in CloudRecordTests.__dict__ if name.startswith('test_'))
