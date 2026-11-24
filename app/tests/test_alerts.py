"""Source-to-qualified-package integration uses a labelled synthetic employer feed."""
import json,copy,unittest,tempfile
from pathlib import Path
from email import policy
from email.parser import BytesParser
import test_personal as fixtures
from packet import canonical,digest
from semantic_segments import parse_segment_response
try:
    from alerts import prepare_alert,validate_alert,alert_readiness
except ImportError:prepare_alert=validate_alert=alert_readiness=None

class AlertTests(unittest.TestCase):
    now='2026-10-16T10:00:00Z';identifier='11111111-1111-4111-8111-111111111111'
    def test_expired_unattempted_packet_can_be_reissued_without_overwriting_original(self):
        first=prepare_alert(self.profile,self.approval,self.source,self.identifier,self.extraction,self.root/'alerts',sender='sender@example.com',now=self.now,first_seen=self.now,get=self.get)
        original=(Path(first['directory'])/'message.eml').read_bytes()
        later='2026-10-17T10:01:00Z'
        second=prepare_alert(self.profile,self.approval,self.source,self.identifier,self.extraction,self.root/'alerts',sender='sender@example.com',now=later,first_seen=self.now,get=self.get)
        self.assertEqual(second['status'],'prepared');self.assertNotEqual(first['directory'],second['directory'])
        self.assertEqual((Path(first['directory'])/'message.eml').read_bytes(),original)
        self.assertTrue(validate_alert(second['directory'],self.profile,self.approval,self.source,later,get=self.get))
    def setUp(self):
        f=fixtures.PersonalTests();f.setUp();self.addCleanup(f.tearDown);self.p=f.p;self.root=Path(f.tmp.name);self.p.update(locations=['NYC'],needs_sponsorship=False,work_authorized_us=True,recipient_email='recipient@example.com')
        self.sha=digest(canonical(self.p));self.profile=self.root/'profile.json';self.approval=self.root/'approval.json'
        self.profile.write_bytes(canonical(self.p));self.approval.write_bytes(canonical({'authority':'synthetic fixture','profile_sha256':self.sha,'source_pdf_sha256':self.p['source']['sha256']}));self.profile.chmod(0o600);self.approval.chmod(0o600)
        self.text='This is a commercial software engineering role.\nRequirements\nPython required.'
        self.row={'title':'New Grad Software Engineer','applyUrl':'https://jobs.ashbyhq.com/example/'+self.identifier+'/application','location':'NYC','descriptionPlain':self.text,'isListed':True,'employmentType':'FullTime','publishedAt':'2026-10-16T09:40:00Z'}
        self.source={'board':'example','careers_url':'https://employer.example/careers','official_host':'employer.example','interval_seconds':1800}
        self.extraction=self.extract(self.text,[['s0','none','unknown',[]],['s1','none','unknown',[]],['s2','skill','mandatory',['Python']]])
    def extract(self,text,rows):return parse_segment_response({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':rows})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}},text)
    def get(self,url,hosts):
        if 'api.ashbyhq.com' in url:return canonical({'jobs':[self.row]})
        if 'employer.example' in url:return ('<html><a href="https://jobs.ashbyhq.com/example/'+self.identifier+'">job</a></html>').encode()
        return b'<html>Apply for this role</html>'
    def prepare(self,**kwargs):
        self.assertIsNotNone(prepare_alert)
        return prepare_alert(self.profile,self.approval,self.source,self.identifier,self.extraction,self.root/'alerts',sender='sender@example.com',now=self.now,first_seen=self.now,get=kwargs.pop('get',self.get),**kwargs)
    def test_complete_qualified_package_contains_exact_job_pdf_and_evidence(self):
        result=self.prepare();self.assertEqual(result['status'],'prepared');d=Path(result['directory']);m=json.loads((d/'manifest.json').read_text())
        self.assertTrue(m['application_ready']);self.assertEqual(m['decision']['status'],'eligible');self.assertTrue(validate_alert(d,self.profile,self.approval,self.source,self.now,get=self.get))
        msg=BytesParser(policy=policy.default).parsebytes((d/'message.eml').read_bytes());self.assertEqual(str(msg['To']),'recipient@example.com')
        self.assertEqual(list(msg.iter_attachments())[0].get_payload(decode=True),(d/'resume.pdf').read_bytes());self.assertIn(self.row['applyUrl'],msg.get_body(preferencelist=('plain',)).get_content())
    def test_official_career_link_absent_or_job_closed_prevents_package(self):
        def wrong(url,hosts):return b'<html>unrelated</html>' if 'employer.example' in url else self.get(url,hosts)
        with self.assertRaises(ValueError):self.prepare(get=wrong)
        self.row['isListed']=False
        with self.assertRaises(ValueError):self.prepare()
    def test_registered_official_job_page_verifies_dynamic_application_ownership(self):
        from alerts import current_opening
        source=dict(self.source,official_job_page_template='https://employer.example/careers/{id}');calls=[];requests=[]
        def get(url,hosts):calls.append(url);requests.append((url,set(hosts)));return self.get(url,hosts)
        job,proof=current_opening(source,self.identifier,get,self.now)
        self.assertEqual(proof['ownership_url'],'https://employer.example/careers/'+self.identifier)
        self.assertIn(proof['ownership_url'],calls);self.assertNotIn(source['careers_url'],calls);self.assertEqual(job['id'],'ashby:example:'+self.identifier)
        self.assertIn((proof['ownership_url'],{'employer.example'}),requests);self.assertEqual(proof['careers_url'],source['careers_url']);self.assertIsNone(proof['careers_page_sha256']);self.assertEqual(len(proof['ownership_page_sha256']),64)
    def test_untrusted_job_templates_and_noncanonical_ids_never_make_requests(self):
        from alerts import current_opening
        for template in ['http://employer.example/jobs/{id}','https://evil.example/jobs/{id}','https://employer.example@evil.example/jobs/{id}','https://employer.example/jobs/{id}?copy={id}','https://employer.example/jobs?next=/{id}','https://employer.example/jobs/{other}','https://employer.example/jobs/{id}/extra']:
            calls=[];source=dict(self.source,official_job_page_template=template)
            def get(url,hosts):calls.append(url);return self.get(url,hosts)
            with self.subTest(template=template),self.assertRaises(ValueError):current_opening(source,self.identifier,get,self.now)
            self.assertEqual(calls,[])
        calls=[];source=dict(self.source,official_job_page_template='https://employer.example/careers/{id}')
        with self.assertRaises(ValueError):current_opening(source,'../secrets',lambda *args:calls.append(args),self.now)
        self.assertEqual(calls,[])
        for identifier in [self.identifier+'/application',self.identifier+'/',self.identifier+'?x=1']:
            calls=[]
            with self.subTest(identifier=identifier),self.assertRaises(ValueError):current_opening(source,identifier,lambda *args:calls.append(args),self.now)
            self.assertEqual(calls,[])
    def test_official_job_page_must_link_the_same_opening(self):
        from alerts import current_opening
        source=dict(self.source,official_job_page_template='https://employer.example/careers/{id}');calls=[]
        def get(url,hosts):
            calls.append(url)
            if 'employer.example' in url:return b'<html><a href="https://jobs.ashbyhq.com/example/22222222-2222-4222-8222-222222222222">different opening</a></html>'
            return self.get(url,hosts)
        with self.assertRaises(ValueError):current_opening(source,self.identifier,get,self.now)
        self.assertEqual(calls,['https://employer.example/careers/'+self.identifier])
    def test_official_soft_404_is_not_ownership_proof_even_with_nav_link(self):
        from alerts import current_opening
        source=dict(self.source,official_job_page_template='https://employer.example/careers/{id}');calls=[]
        def get(url,hosts):
            calls.append(url)
            if 'employer.example' in url:return ('<html><h1>Page not found</h1><nav><a href="https://jobs.ashbyhq.com/example/'+self.identifier+'">all roles</a></nav></html>').encode()
            return self.get(url,hosts)
        with self.assertRaises(ValueError):current_opening(source,self.identifier,get,self.now)
        self.assertEqual(calls,['https://employer.example/careers/'+self.identifier])
    def test_commercial_description_does_not_certify_noncontracting(self):
        from alerts import current_opening
        self.row['descriptionPlain']=self.text+'\nSupports federal contracts.'
        job,proof=current_opening(self.source,self.identifier,self.get,self.now)
        self.assertIsNone(job['government_contracting']);self.assertEqual(job['description'],self.row['descriptionPlain'])
        extraction=self.extract(job['description'],[['s0','none','unknown',[]],['s1','none','unknown',[]],['s2','skill','mandatory',['Python']],['s3','contracting','mandatory',[]]])
        from qualification import qualify
        self.assertNotEqual(qualify(self.p,job,extraction,self.now,approval_sha256=self.sha)['status'],'eligible')
    def test_unknown_qualification_returns_review_without_files(self):
        self.text='Requirements\nRust required.';self.row['descriptionPlain']=self.text;self.extraction=self.extract(self.text,[['s0','none','unknown',[]],['s1','skill','mandatory',['Rust']]])
        r=self.prepare();self.assertEqual(r['status'],'review');self.assertNotIn('directory',r)
    def test_tampered_pdf_or_mime_or_profile_or_source_fails_readiness(self):
        r=self.prepare();d=Path(r['directory']);raw=(d/'resume.pdf').read_bytes();(d/'resume.pdf').write_bytes(raw+b'bad')
        self.assertFalse(validate_alert(d,self.profile,self.approval,self.source,self.now,get=self.get));(d/'resume.pdf').write_bytes(raw)
        self.row['descriptionPlain']+='\nRust required.'
        self.assertFalse(validate_alert(d,self.profile,self.approval,self.source,self.now,get=self.get))
    def test_duplicate_preparation_reuses_identical_immutable_package(self):
        a=self.prepare();b=self.prepare();self.assertEqual(a['directory'],b['directory']);self.assertEqual(b['status'],'cached')
    def test_timestamps_distinguish_provider_publication_and_detection(self):
        r=self.prepare();m=json.loads((Path(r['directory'])/'manifest.json').read_text())
        self.assertEqual(m['first_seen_at'],self.now);self.assertEqual(m['publication_semantics'],'last_published');self.assertIsNone(m['original_publication_at']);self.assertEqual(m['prepared_at'],self.now)
    def test_provider_workplace_type_overrides_unrelated_hybrid_text(self):
        from policy import source_record
        row=dict(self.row,workplaceType='Remote',descriptionPlain='Unlike our hybrid early career program, this role is fully remote.')
        self.assertEqual(source_record('ashby','example',row)['workplace'],'remote')
    def test_qualified_pdf_is_bound_to_job_in_metadata(self):
        import io
        from pypdf import PdfReader
        r=self.prepare();d=Path(r['directory']);m=json.loads((d/'manifest.json').read_text())
        self.assertIn(m['job_hash'],str(PdfReader(io.BytesIO((d/'resume.pdf').read_bytes())).metadata.subject))
    def test_frozen_real_employer_row_keeps_explicit_graduation_and_publication_semantics(self):
        from policy import source_record
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/ashby.sample.json').read_text());job=source_record('ashby','example',fixture['job'])
        self.assertEqual(job['id'],'ashby:example:99999999-0000-4000-8000-000000000001');self.assertEqual(job['title'],'Graduate Systems Engineer')
        self.assertIn('Applicants must complete a degree in May 2031.',job['description'])
        self.assertEqual(job['publication_semantics'],'last_published');self.assertIsNone(job['government_contracting']);self.assertFalse(fixture['provenance']['selected_fixture_is_complete_feed'])
    def test_literal_expected_span_is_independent_of_model_constructor(self):
        from semantic_segments import source_segments
        self.assertEqual([(s['start'],s['end'],s['text']) for s in source_segments('Requirements\nPython required.')],[(0,12,'Requirements'),(13,29,'Python required.')])
    def test_unsupported_rendered_skill_cannot_be_added_to_approved_document(self):
        from personal import render_resume
        self.p['skills'].append('Kubernetes');approval=digest(canonical(self.p))
        with self.assertRaises(ValueError):render_resume(self.p,{'description':'Kubernetes'},['api'],approval_sha256=approval)
    def test_visible_closed_application_page_blocks_even_if_feed_is_listed(self):
        def get(url,hosts):
            if 'jobs.ashbyhq.com' in url:return b'<html><body>This job is no longer accepting applications.</body></html>'
            return self.get(url,hosts)
        with self.assertRaises(ValueError):self.prepare(get=get)
    def test_closed_text_inside_script_is_not_a_visible_closed_marker(self):
        def get(url,hosts):
            if 'jobs.ashbyhq.com' in url:return b'<html><script>const x="job not found";</script><body>Apply now</body></html>'
            return self.get(url,hosts)
        self.assertEqual(self.prepare(get=get)['status'],'prepared')
