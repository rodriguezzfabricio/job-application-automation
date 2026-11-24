import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'stopgap-tools'))
import feeds
from test_runtime import RuntimeTests
from job_runtime.adapters import import_record
from job_runtime.store import Denied

class EmployerDateTests(RuntimeTests):
    def record(self,date):
        j=self.s.job(self.key)
        return {'company':j['company'],'title':j['title'],'link':j['link'],'posted_at':date,
                'posted_at_source':'employer.JobPosting.datePosted','posted_at_url':j['link']}
    def test_primary_date_corrects_listing_without_losing_user_state(self):
        self.s.con.execute("UPDATE jobs SET your_call='fill now' WHERE job_key=?",(self.key,))
        import_record(self.s,'employer','one',self.record('2026-10-20'),self.owner,self.token)
        row=self.s.job(self.key)
        self.assertEqual((row['posted_at'],row['status'],row['resume'],row['your_call']),('2026-10-20','Matched','swe.pdf','fill now'))
        import_record(self.s,'employer','one',self.record('2026-11-12'),self.owner,self.token)
        self.assertEqual(self.s.job(self.key)['posted_at'],'2026-10-20')
    def test_more_precise_same_day_is_refinement_not_reposting(self):
        import_record(self.s,'employer','one',self.record('2026-11-12'),self.owner,self.token)
        import_record(self.s,'employer','one',self.record('2026-11-12T10:15:00-05:00'),self.owner,self.token)
        import_record(self.s,'employer','one',self.record('2026-11-12'),self.owner,self.token)
        self.assertEqual(self.s.job(self.key)['posted_at'],'2026-11-12T10:15:00-05:00')
    def test_reused_record_id_cannot_change_requisition(self):
        r=self.record('2026-11-12');import_record(self.s,'employer','one',r,self.owner,self.token)
        with self.assertRaisesRegex(Denied,'identity changed'):
            import_record(self.s,'employer','one',dict(r,link='https://example.test/jobs/other'),self.owner,self.token)

class FeedSourceTests(unittest.TestCase):
    def test_old_employer_date_wins_and_long_requirement_tail_survives(self):
        desc='&lt;p&gt;'+('Synthetic requirement context. '*400)+'Required citizenship at the end.&lt;/p&gt;'
        raw=json.dumps({'@graph':[{'@type':['Thing','JobPosting'],'datePosted':'2026-10-20','description':desc}]})
        body="<script nonce='fixture' type='application/ld+json'>"+raw+'</script>'
        row={'link':'https://example.test/job','posted_at':'2026-11-12','description':'Incomplete old excerpt'}
        with patch.object(feeds,'get',return_value=(body,row['link'])):r=feeds.fetch_description(row)
        self.assertEqual(r['posted_at'],'2026-10-20');self.assertEqual(r['listing_posted_at'],'2026-11-12')
        self.assertTrue(r['description_complete']);self.assertGreater(len(r['description']),8000)
        self.assertIn('Required citizenship at the end.',r['description']);self.assertNotIn('<p>',r['description'])
    def test_workday_date_and_full_description_are_kept(self):
        row={'link':'https://example.test/job','posted_at':'2026-11-12','wd':('https://fixture.wd1.myworkdayjobs.com','fixture','External','/job/role_R1')}
        with patch.object(feeds,'get_json',return_value={'jobPostingInfo':{'startDate':'2026-11-02','jobDescription':'<p>'+('Text '*2000)+'</p>'}}):r=feeds.fetch_description(row)
        self.assertEqual(r['posted_at'],'2026-11-02');self.assertTrue(r['description_complete']);self.assertGreater(len(r['description']),8000)
    def test_resolved_url_keeps_unknown_posting_ids_and_tenant(self):
        with patch.object(feeds.subprocess,'run',return_value=SimpleNamespace(stdout='200 https://example.test/job?posting_id=AB&tenant=XY&utm_source=fixture')):
            self.assertEqual(feeds.resolve('https://example.test/redirect'),'https://example.test/job?posting_id=AB&tenant=XY')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite([*(EmployerDateTests(n) for n in EmployerDateTests.__dict__ if n.startswith('test_')),loader.loadTestsFromTestCase(FeedSourceTests)])
