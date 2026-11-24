import unittest
from policy import fit, novelty, validate_selection, budget_allowed, source_record, merge_key

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile={'skills':['Python','testing'],'locations':['NYC'],'needs_sponsorship':False,'graduation_year':2026}
        self.job={'id':'a','title':'New Grad Software Engineer','location':'New York, New York, United States','description':'Build software','required_skills':['Python'],'sponsorship':'unknown','graduation_years':[2026],'government_contracting':False,'min_years':0}
    def test_positive_fit_has_evidence(self):
        result=fit(self.profile,self.job)
        self.assertEqual(result['status'],'eligible');self.assertEqual(result['matched_skills'],['python'])
    def test_scalar_skill_fields_are_not_character_evidence(self):
        for p,j in [(dict(self.profile,skills='C++'),dict(self.job,required_skills=['C'])),(self.profile,dict(self.job,required_skills='Python'))]:
            self.assertEqual(fit(p,j)['status'],'review')
        self.assertFalse(validate_selection([{'id':'x','text':'Built C++ software','skills':'C++'}],['x']))
    def test_government_and_senior_reject(self):
        for changes in [{'government_contracting':True},{'title':'Senior Software Engineer'},{'description':'Active security clearance required'}]:
            self.assertEqual(fit(self.profile,dict(self.job,**changes))['status'],'reject')
    def test_member_of_technical_staff_is_not_a_staff_seniority_label(self):
        result=fit(self.profile,dict(self.job,title='Member of Technical Staff - New Grad (2027 Start)'))
        self.assertEqual(result['status'],'review');self.assertNotIn('excluded seniority or internship',result['reject_reasons']);self.assertNotIn('not a software role',result['reject_reasons'])
        self.assertIn('technical-staff role needs explicit software scope review',result['review_reasons'])
        explicit=fit(self.profile,dict(self.job,title='Member of Technical Staff - New Grad Software Engineer'))
        self.assertEqual(explicit['status'],'eligible')
    def test_technical_staff_alias_does_not_erase_explicit_senior_levels(self):
        for title in ['Senior Member of Technical Staff - New Grad','Principal Member of Technical Staff','Staff Member of Technical Staff - Software Engineer','Staff Software Engineer - New Grad','Technical Staff Software Engineer']:
            with self.subTest(title=title):self.assertEqual(fit(self.profile,dict(self.job,title=title))['status'],'reject')
        non_software=fit(self.profile,dict(self.job,title='Data Analyst'))
        self.assertEqual(non_software['status'],'reject')
        self.assertIn('not a software role',non_software['reject_reasons'])
    def test_unknown_and_canada_review(self):
        for changes in [{'government_contracting':None},{'location':'Remote, US'}]:
            self.assertEqual(fit(self.profile,dict(self.job,**changes))['status'],'review')
    def test_explicit_foreign_is_rejected(self):
        self.assertEqual(fit(self.profile,dict(self.job,location='Toronto, Canada'))['status'],'reject')
    def test_sponsorship_and_graduation(self):
        p=dict(self.profile,needs_sponsorship=True)
        self.assertEqual(fit(p,dict(self.job,sponsorship='unavailable'))['status'],'reject')
        self.assertEqual(fit(p,self.job)['status'],'review')
        self.assertEqual(fit(dict(self.profile,graduation_year=None),self.job)['status'],'review')
    def test_source_specific_hybrid(self):
        raw={'id':12,'title':'Early Career Software Engineer','absolute_url':'https://example.invalid/jobs/12','location':{'name':'NYC'},'content':'<p>This role works in-person 3 days a week.</p><p>Our company is remote-first.</p>'}
        j=source_record('greenhouse','example',raw)
        self.assertEqual(j['workplace'],'unknown');self.assertIn('in-person 3 days',j['description']);self.assertIsNone(j['government_contracting'])
    def test_bad_source_is_not_success(self):
        with self.assertRaises(ValueError):source_record('greenhouse','x',{'id':1,'title':'SWE'})
    def test_unapproved_selection_blocked(self):
        bank=[{'id':'b1','text':'Built a Python API','skills':['Python']}]
        self.assertTrue(validate_selection(bank,['b1']))
        for selected in [[],['b1','b1'],['invented']]:self.assertFalse(validate_selection(bank,selected))
    def test_exact_budget_and_overspend(self):
        self.assertTrue(budget_allowed(2500000,2400000,0,100000));self.assertFalse(budget_allowed(2500000,2400000,100000,1));self.assertFalse(budget_allowed(10,0,0,-1))
    def test_bootstrap_and_republication(self):
        now='2026-10-15T12:00:00Z';j={'id':'a','published_at':'2026-10-15T11:30:00Z','publication_semantics':'original'}
        self.assertTrue(novelty([],j,True,now));self.assertFalse(novelty(['a'],j,False,now));self.assertFalse(novelty([],dict(j,publication_semantics='last_published'),True,now));self.assertFalse(novelty([],dict(j,published_at='2026-10-15T13:00:00Z'),True,now));self.assertTrue(novelty([],dict(j,published_at=None),False,now))
    def test_cross_source_identity_exact_url(self):
        self.assertEqual(merge_key({'url':'https://example.invalid/j/1','id':'one'}),merge_key({'url':'https://example.invalid/j/1','id':'two'}));self.assertNotEqual(merge_key({'url':'https://example.invalid/j/1','id':'one'}),merge_key({'url':'https://example.invalid/j/2','id':'two'}))


class AdditionalFitTests(unittest.TestCase):
    setUp=PolicyTests.setUp
    def test_non_software_roles_reject(self):
        for title in ['Mechanical Engineer','Sales Engineer','Customer Support Engineer']:
            self.assertEqual(fit(self.profile,dict(self.job,title=title))['status'],'reject')
    def test_texas_and_florida_statewide_matching(self):
        for location in ['Houston, Texas, United States','Fort Lauderdale, FL, United States']:
            self.assertEqual(fit(dict(self.profile,locations=['Texas','Florida']),dict(self.job,location=location))['status'],'eligible')


class MetroAliasTests(unittest.TestCase):
    def test_nyc_and_dmv_aliases(self):
        from policy import location_matches
        self.assertTrue(location_matches('New York City, NY, United States',['NYC']))
        self.assertTrue(location_matches('Arlington, VA, United States',['DMV']))
        self.assertFalse(location_matches('Vancouver, Canada',['California']))

if __name__=='__main__':unittest.main()
