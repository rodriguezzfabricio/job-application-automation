"""Personal-profile adaptation and exact-source requirement inventory."""
import copy,unittest
from packet import canonical,digest
import test_personal as fixtures

try:
    from matching import candidate_context, assess, requirement_inventory
except ModuleNotFoundError:
    candidate_context=assess=requirement_inventory=None

class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PersonalTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.profile=self.fixture.p
        self.profile.update(locations=['NYC'],needs_sponsorship=False,earliest_start={'month':'2026-12','precision':'month','day':None},willing_to_relocate=True)
        self.approval=digest(canonical(self.profile))
        self.job={'id':'test-role','title':'New Grad Software Engineer','description':'Requirements\nPython experience required.\nPreferred Qualifications\nRust experience preferred.', 'location':'NYC','government_contracting':False,'employment_type':'FullTime','required_skills':['Python'],'start_date':'2027-01-01'}
    def test_missing_module_is_actual_missing_feature(self):
        self.assertIsNotNone(candidate_context,'Personal profile-to-matcher integration is missing')
    def test_verified_months_map_to_matcher_without_inventing_a_day(self):
        self.assertIsNotNone(candidate_context)
        context=candidate_context(self.profile,approval_sha256=self.approval)
        self.assertEqual(context['graduation_month'],'2026-12');self.assertEqual(context['availability_month'],'2026-12')
        self.assertNotIn('available_from_date',context);self.assertTrue(context['willing_to_relocate'])
        decision=assess(self.profile,self.job,'2026-10-15T12:00:00Z',approval_sha256=self.approval)
        self.assertEqual(decision['status'],'review');self.assertFalse(any('start' in reason for reason in decision['reasons']))
    def test_stale_approval_and_unsupported_precision_are_refused(self):
        self.assertIsNotNone(candidate_context)
        p=copy.deepcopy(self.profile);p['earliest_start']['day']=1
        with self.assertRaises(ValueError):candidate_context(p,approval_sha256=self.approval)
        with self.assertRaises(ValueError):candidate_context(p,approval_sha256=digest(canonical(p)))
    def test_inventory_preserves_offsets_and_preferred_distinction(self):
        self.assertIsNotNone(requirement_inventory)
        rows=requirement_inventory(self.job['description'])
        self.assertEqual([r['importance'] for r in rows],['mandatory','preferred'])
        for row in rows:self.assertEqual(self.job['description'][row['start']:row['end']],row['quote'])
    def test_unreviewed_real_requirements_are_not_qualified_by_keyword_hits(self):
        self.assertIsNotNone(assess)
        job=dict(self.job,requirements_status='unreviewed')
        result=assess(self.profile,job,'2026-10-15T12:00:00Z',approval_sha256=self.approval)
        self.assertEqual(result['status'],'review');self.assertFalse(result['requirements_verified'])
        self.assertTrue(result['requirement_inventory']);self.assertEqual(result['skill_evidence'][0]['evidence_ids'],['api'])
    def test_december_start_remains_uncertain(self):
        self.assertIsNotNone(assess)
        job=dict(self.job,start_date='2026-12-15')
        result=assess(self.profile,job,'2026-10-15T12:00:00Z',approval_sha256=self.approval)
        self.assertEqual(result['status'],'review')
    def test_no_sponsorship_never_creates_citizenship(self):
        self.assertIsNotNone(candidate_context)
        p=candidate_context(self.profile,approval_sha256=self.approval)
        self.assertNotIn('us_citizen',p)
        result=assess(self.profile,dict(self.job,description='US citizenship required.'),'2026-10-15T12:00:00Z',approval_sha256=self.approval)
        self.assertEqual(result['status'],'review')
    def test_heading_words_in_bullet_cannot_silently_downgrade_mandatory_section(self):
        self.assertIsNotNone(requirement_inventory)
        text='Requirements\nPreferred approach is Python but Java is required.\nSecurity clearance required.'
        rows=requirement_inventory(text);self.assertEqual([row['importance'] for row in rows],['unknown','mandatory'])
    def test_empty_or_oversized_descriptions_are_refused(self):
        self.assertIsNotNone(requirement_inventory)
        for value in ['',None,'x'*500001]:
            with self.assertRaises(ValueError):requirement_inventory(value)

    def test_ambiguous_or_conflicting_requirement_wording_is_unknown(self):
        self.assertIsNotNone(requirement_inventory)
        for text in ['Requirements\nPython is ideally required.','Preferred Qualifications\nRust is required.','Requirements\nOwn the service. Startup experience is a plus.','Requirements\nNo experience with Go is required.']:
            with self.subTest(text=text):self.assertEqual(requirement_inventory(text)[0]['importance'],'unknown')

    def test_untrusted_reviewed_flag_never_certifies_requirements(self):
        for changes in [{},{'source_kind':'ashby','requirements_status':'reviewed'}]:
            result=assess(self.profile,dict(self.job,**changes),'2026-10-15T12:00:00Z',approval_sha256=self.approval)
            self.assertEqual(result['status'],'review');self.assertFalse(result['requirements_verified'])

    def test_nested_graduation_conflict_is_refused(self):
        p=copy.deepcopy(self.profile);p['education']['graduation_year']=2027
        with self.assertRaises(ValueError):candidate_context(p,approval_sha256=digest(canonical(p)))

    def test_unheaded_lines_are_visible_unknowns_and_profile_is_immutable(self):
        text='Python experience.\nBS degree.'
        self.assertEqual([r['importance'] for r in requirement_inventory(text)],['unknown','unknown'])
        before=canonical(self.profile);context=candidate_context(self.profile,approval_sha256=self.approval)
        context['skills'].append('Rust');self.assertEqual(canonical(self.profile),before)

    def test_conflicting_legacy_months_are_refused(self):
        for field,value in [('available_from_month','2026-09'),('availability_month','2026-09'),('graduation_month','2027-05')]:
            p=copy.deepcopy(self.profile);p[field]=value
            with self.subTest(field=field), self.assertRaises(ValueError):candidate_context(p,approval_sha256=digest(canonical(p)))

    def test_explicitly_unverified_availability_is_removed_from_matching_context(self):
        p=copy.deepcopy(self.profile);p['unverified_fields']=['earliest_start'];p['available_from_month']='2026-12'
        before=canonical(p);context=candidate_context(p,approval_sha256=digest(canonical(p)))
        for field in ['earliest_start','available_from_month','availability_month','available_from_date']:self.assertNotIn(field,context)
        self.assertEqual(canonical(p),before)
        from eligibility import evaluate_clause
        self.assertEqual(evaluate_clause(context,'Must start on November 1, 2026.')['status'],'unknown')
    def test_invalid_uncertainty_metadata_is_refused(self):
        for fields in ['earliest_start',[None],['source']]:
            p=copy.deepcopy(self.profile);p['unverified_fields']=fields
            with self.assertRaises(ValueError):candidate_context(p,approval_sha256=digest(canonical(p)))
