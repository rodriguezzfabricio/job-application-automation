"""Exercise public boundaries, not just upstream matching guards."""
import copy,hashlib,json,unittest
from email.parser import BytesParser
from email import policy as mail_policy
from packet import build_packet,check_packet,canonical,digest
from personal import select_variants,render_resume
from policy import fit
from test_packet import PROFILE,JOB
import test_personal as personal_fixtures

class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.personal=personal_fixtures.PersonalTests();self.personal.setUp();self.addCleanup(self.personal.tearDown)
    def test_every_public_document_boundary_rejects_bad_job_skill_fields(self):
        for key,bads in [('required_skills',['C++',None,{},[None],[''],[1]]),('preferred_skills',['C#',None,{},[' ']]),('required_skill_groups',['C++',['C++'],[[]],[[None]],[[' ']],None])]:
            for bad in bads:
                job=dict(JOB,**{key:bad})
                with self.subTest(field=key,value=bad):
                    with self.assertRaises(ValueError):build_packet(PROFILE,job,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
                    with self.assertRaises(ValueError):select_variants(self.personal.p,job,1,approval_sha256=self.personal.approval)
                    with self.assertRaises(ValueError):render_resume(self.personal.p,job,['api'],approval_sha256=self.personal.approval)
    def test_candidate_skill_collections_cannot_bypass_document_boundaries(self):
        for bad in ['C++',None,{},[''],[1],[['Python']]]:
            for variant in [False,True]:
                p=copy.deepcopy(PROFILE);personal=copy.deepcopy(self.personal.p)
                if variant:p['variants'][0]['skills']=bad;personal['variants'][0]['skills']=bad
                else:p['skills']=bad;personal['skills']=bad
                with self.subTest(value=bad,variant=variant):
                    with self.assertRaises(ValueError):build_packet(p,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
                    with self.assertRaises(ValueError):select_variants(personal,JOB,1,approval_sha256=digest(canonical(personal)))
    def test_rehashed_malformed_requirements_still_fail_verification(self):
        for fields in [{'required_skills':{'Python':True}},{'required_skill_groups':[[]]},{'required_skill_groups':[['Python',None]]}]:
            packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z');job=dict(JOB,**fields)
            material={k:v for k,v in job.items() if k not in {'source_observed_at','first_seen_at','last_seen_at','known_ids','bootstrap'}}
            encode=lambda value:json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
            packet['manifest']['job_hash']=hashlib.sha256(encode(material)).hexdigest()
            msg=BytesParser(policy=mail_policy.SMTP).parsebytes(packet['email']);msg.replace_header('Message-ID','<'+hashlib.sha256(encode(packet['manifest'])).hexdigest()[:32]+'@example.invalid>');packet['email']=msg.as_bytes()
            self.assertFalse(check_packet(packet,PROFILE,job))
    def test_matching_rejects_invalid_preferred_schema_without_rejecting_missing_preference(self):
        p=dict(PROFILE,locations=['NYC'],needs_sponsorship=False)
        j=dict(JOB,government_contracting=False,required_skills=['Python'],preferred_skills=['Rust'])
        self.assertEqual(fit(p,j)['status'],'eligible')
        self.assertIn('invalid skill evidence schema',fit(p,dict(j,preferred_skills='Rust'))['reasons'])

    def test_rebound_packet_still_requires_valid_url_and_source_hash(self):
        from packet import job_fingerprint
        original=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        self.assertTrue(check_packet(original,PROFILE,JOB))
        for fields in [{'source_hash':None},{'url':'https://jobs.ashbyhq.com/example/'+'-'*36+'/application'}]:
            job=dict(JOB,**fields);packet=copy.deepcopy(original);manifest=packet['manifest']
            manifest.update(job_url=job['url'],job_hash=job_fingerprint(job),source_hash=job.get('source_hash'))
            message=BytesParser(policy=mail_policy.SMTP).parsebytes(packet['email'])
            body=message.get_body(preferencelist=('plain',));body.set_content(body.get_content().replace(JOB['url'],job['url']))
            message.replace_header('Message-ID','<'+digest(canonical(manifest))[:32]+'@example.invalid>');packet['email']=message.as_bytes()
            with self.subTest(fields=fields):self.assertFalse(check_packet(packet,PROFILE,job))

class SourceIdentityTests(unittest.TestCase):
    def test_documented_ashby_shape_without_id_is_accepted(self):
        from policy import source_record
        raw={'title':'Software Engineer, New Grad','jobUrl':'https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc','applyUrl':'https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application','descriptionPlain':'Build software.','location':'NYC','isListed':True,'employmentType':'FullTime'}
        result=source_record('ashby','example',raw)
        self.assertEqual(result['id'],'ashby:example:12345678-1234-1234-1234-123456789abc')
        self.assertEqual(result['publication_semantics'],'last_published')
    def test_unknown_source_and_cross_employer_ashby_identity_refused(self):
        from policy import source_record
        raw={'id':1,'title':'SWE','applyUrl':'https://jobs.ashbyhq.com/other/12345678-1234-1234-1234-123456789abc/application'}
        with self.assertRaises(ValueError):source_record('unknown','example',raw)
        with self.assertRaises(ValueError):source_record('ashby','example',raw)

class EligibilityCorrections(unittest.TestCase):
    def profile(self):return dict(PROFILE,locations=['NYC'],needs_sponsorship=False)
    def job(self):return dict(JOB,government_contracting=False,required_skills=['Python'],employment_type='FullTime')
    def test_lead_role_is_excluded(self):
        self.assertEqual(fit(self.profile(),dict(self.job(),title='Lead Software Engineer'))['status'],'reject')
    def test_no_sponsorship_does_not_establish_citizenship(self):
        for phrase in ['Must be a U.S. citizen.','US citizenship required.','United States citizenship required.','U.S. citizens only.','Applicants must be U.S. citizens.','U.S. citizenship is mandatory.','U.S. citizens are required.']:
            job=dict(self.job(),description=phrase)
            self.assertEqual(fit(self.profile(),job)['status'],'review')
            self.assertEqual(fit(dict(self.profile(),us_citizen=False),job)['status'],'reject')

    def test_citizenship_alternatives_do_not_falsely_reject_noncitizen(self):
        job=dict(self.job(),description='All applicants must be U.S. citizens or permanent residents.')
        self.assertEqual(fit(dict(self.profile(),us_citizen=False),job)['status'],'review')

    def test_malformed_citizenship_requirement_cannot_approve(self):
        for value in ['true',1,{},[]]:
            self.assertEqual(fit(self.profile(),dict(self.job(),us_citizenship_required=value))['status'],'review')

    def test_unrelated_skill_alternative_does_not_override_citizenship(self):
        job=dict(self.job(),description='Applicants must be U.S. citizens. Work with Python or Rust.')
        self.assertEqual(fit(dict(self.profile(),us_citizen=False),job)['status'],'reject')
    def test_requirements_have_evidence_and_preferred_is_nonblocking(self):
        decision=fit(self.profile(),dict(self.job(),preferred_skills=['Rust']))
        evidence=decision['skill_evidence'];self.assertEqual(evidence[0]['status'],'met');self.assertEqual(evidence[0]['evidence_ids'],['api-tests'])
        self.assertEqual(evidence[-1]['importance'],'preferred');self.assertEqual(evidence[-1]['status'],'unknown');self.assertEqual(decision['status'],'eligible')
    def test_or_group_evidence_reaches_archive_packet_selection(self):
        import test_runner as fixtures
        fixture=fixtures.RunnerTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        job=dict(fixture.job,required_skills=[],required_skill_groups=[['Python','Go']])
        fixture.store.snapshot('archive-fixture',[job['id']],job['source_observed_at'],hashes={job['id']:__import__('packet').job_fingerprint(job)})
        result=fixture.runner.process(fixture.profile,job,'2026-10-15T12:00:00Z')
        self.assertEqual(result['status'],'capture_accepted');self.assertEqual(result['selected_ids'],['api-tests'])

class RoleScopeAndPrivacy(unittest.TestCase):
    def test_employer_name_alone_does_not_establish_government_contract(self):
        p=dict(PROFILE,locations=['NYC'],needs_sponsorship=False)
        job=dict(JOB,employer='Lockheed Martin',government_contracting=False,required_skills=['Python'],description='Commercial product engineering.')
        result=fit(p,job);self.assertEqual(result['status'],'review');self.assertNotIn('excluded employer',result['reject_reasons'])
    def test_personal_pdf_does_not_expose_internal_job_or_fact_ids(self):
        fixture=personal_fixtures.PersonalTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        from pypdf import PdfReader
        import io
        pdf=render_resume(fixture.p,dict(JOB,id='PRIVATE-INTERNAL-JOB-123'),['api'],approval_sha256=fixture.approval)
        metadata=str(PdfReader(io.BytesIO(pdf)).metadata);self.assertNotIn('PRIVATE-INTERNAL-JOB-123',metadata);self.assertNotIn('selected_ids',metadata)

class OptionalSkillDefault(unittest.TestCase):
    def test_omitted_skills_and_empty_skills_render_consistently(self):
        fixture=personal_fixtures.PersonalTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        empty=copy.deepcopy(fixture.p);empty['skills']=[];missing=copy.deepcopy(empty);del missing['skills']
        a=render_resume(empty,JOB,['api'],approval_sha256=digest(canonical(empty)))
        b=render_resume(missing,JOB,['api'],approval_sha256=digest(canonical(missing)))
        self.assertEqual(a,b)

class RequiredCoverageTests(unittest.TestCase):
    def fixture(self):
        import test_runner as fixtures
        f=fixtures.RunnerTests();f.setUp();self.addCleanup(f.tearDown)
        f.profile['skills']+=['Rust']
        f.profile['facts']['rust']={'group':'api'}
        f.profile['variants'] += [dict(f.profile['variants'][0],id='api-tests-2'),{'id':'rust','group':'api','facts':['rust'],'text':'Built a Rust service.','skills':['Rust']}]
        j=dict(f.job,required_skills=[],required_skill_groups=[['Python','Go'],['Rust','Java']])
        f.store.snapshot('archive-fixture',[j['id']],j['source_observed_at'],hashes={j['id']:__import__('packet').job_fingerprint(j)})
        return f,j
    def test_every_required_or_group_survives_packet_selection(self):
        f,j=self.fixture();result=f.runner.process(f.profile,j,'2026-10-15T12:00:00Z');self.assertEqual(result['status'],'capture_accepted');self.assertIn('rust',result['selected_ids'])
    def test_direct_packet_cannot_omit_a_required_group(self):
        f,j=self.fixture()
        with self.assertRaises(ValueError):build_packet(f.profile,j,['api-tests','api-tests-2'],'demo@example.invalid','2026-10-15T12:00:00Z')
