import json,unittest
from source_access import collect_ashby,verify_application,validate_public_url

class SourceAccessTests(unittest.TestCase):
    def row(self):return {'title':'Software Engineer, New Grad','location':'NYC','applyUrl':'https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application','descriptionPlain':'Build Python software.','isListed':True,'employmentType':'FullTime'}
    def test_actual_shape_and_complete_snapshot_metadata(self):
        jobs=collect_ashby('example',lambda url,hosts:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z')
        self.assertEqual(len(jobs),1);self.assertTrue(jobs[0]['active']);self.assertEqual(jobs[0]['publication_semantics'],'last_published');self.assertEqual(len(jobs[0]['source_hash']),64)
    def test_one_malformed_row_fails_whole_snapshot(self):
        with self.assertRaises(ValueError):collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row(),{'title':'broken'}]}).encode(),'2026-10-15T22:00:00Z')
    def test_untrusted_urls_never_reach_transport(self):
        for url in ['http://jobs.ashbyhq.com/x','https://127.0.0.1/x','https://jobs.ashbyhq.com:8443/x','https://user@jobs.ashbyhq.com/x','https://evil.invalid/x']:
            with self.assertRaises(ValueError):validate_public_url(url,{'jobs.ashbyhq.com'})
    def test_application_verification_needs_listed_source_and_matching_identity(self):
        jobs=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z');job=jobs[0]
        result=verify_application(job,lambda *a:b'<html>Application</html>',now='2026-10-15T22:00:00Z');self.assertEqual(result['status'],'application_url_reachable')
        with self.assertRaises(ValueError):verify_application(dict(job,active=False),lambda *a:b'')
        with self.assertRaises(ValueError):verify_application(dict(job,url='https://evil.invalid/x'),lambda *a:b'')
    def test_real_feed_fields_cannot_certify_unparsed_requirements(self):
        from policy import fit
        job=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z')[0]
        job.update(government_contracting=False)
        result=fit({'skills':['Python'],'needs_sponsorship':False,'locations':['NYC']},job)
        self.assertEqual(result['status'],'review');self.assertIn('mandatory requirement extraction unverified',result['reasons'])
    def test_unrelated_new_job_does_not_change_existing_posting_digest(self):
        first=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z')
        second_row=dict(self.row(),applyUrl='https://jobs.ashbyhq.com/example/22345678-1234-1234-1234-123456789abc/application')
        second=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row(),second_row]}).encode(),'2026-10-15T22:15:00Z')
        self.assertEqual(first[0]['source_hash'],second[0]['source_hash'])
    def test_conflicting_and_malformed_ashby_identities_refused(self):
        from policy import source_record
        for fields in [{'jobUrl':'https://jobs.ashbyhq.com/example/22345678-1234-1234-1234-123456789abc'}, {'applyUrl':'https://jobs.ashbyhq.com/example/'+'-'*36+'/application'}, {'applyUrl':self.row()['applyUrl']+'#fragment'}, {'applyUrl':self.row()['applyUrl'].replace('.com/','.com:8443/')}]:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):source_record('ashby','example',dict(self.row(),**fields))
    def test_stale_or_missing_observation_cannot_certify_current_publication(self):
        j=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z')[0]
        for changed in [dict(j,source_observed_at=None),dict(j,source_observed_at='2001-01-01T00:00:00Z')]:
            with self.assertRaises(ValueError):verify_application(changed,lambda *a:b'<html>Application</html>')

    def test_path_suffix_cannot_change_the_application_identity(self):
        from policy import source_record
        base=self.row()['applyUrl'].removesuffix('/application')
        for suffix in ['/../../other/22345678-1234-1234-1234-123456789abc/application','/extra','/%2e%2e/application','//application']:
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):source_record('ashby','example',dict(self.row(),applyUrl=base+suffix))

    def test_application_identity_and_url_validation_precede_http(self):
        job=collect_ashby('example',lambda *a:json.dumps({'jobs':[self.row()]}).encode(),'2026-10-15T22:00:00Z')[0]
        variants=[dict(job,employer='other'),dict(job,url=job['url']+'/other'),dict(job,url=job['url'].replace('https://','https://@')),dict(job,url=job['url'].replace('example','exam\tple'))]
        for changed in variants:
            calls=[]
            def get(*args):calls.append(args);return b'<html>test</html>'
            with self.subTest(changed=changed['url'],employer=changed['employer']):
                with self.assertRaises(ValueError):verify_application(changed,get,now='2026-10-15T22:00:00Z')
                self.assertEqual(calls,[])
