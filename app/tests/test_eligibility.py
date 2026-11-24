"""Explicit clauses only: independently specified precision/type boundaries."""
import copy,unittest
try:
    from eligibility import evaluate_clause
except ImportError:evaluate_clause=None

class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.profile={'education':{'degree':'BS Computer Science','graduation_month':'2026-12'},'graduation_month':'2026-12','earliest_start':{'month':'2026-12','precision':'month','day':None},'internships_count':2,'experience_years':None,'commercial_fulltime_experience_years':None}
    def status(self,text,expected,profile=None):
        self.assertIsNotNone(evaluate_clause)
        result=evaluate_clause(profile or self.profile,text)
        self.assertEqual(result['status'],expected,(text,result));self.assertEqual(result['source_quote'],text)
        return result
    def test_explicit_month_graduation_window_includes_december(self):
        self.status('Must graduate between December 2026 and June 2027.','met')
        self.status('Graduation between January 2027 and June 2027 required.','not_met')
    def test_exact_graduation_day_overlap_remains_unknown(self):
        self.status('Graduation between December 15, 2026 and June 1, 2027 required.','unknown')
        self.status('Graduation between November 1, 2026 and December 31, 2026 required.','met')
    def test_seasons_and_reversed_or_invalid_windows_are_unknown(self):
        for text in ['Must graduate between Winter 2026 and Spring 2027.','Graduation between June 2027 and December 2026 required.','Graduation between February 30, 2026 and June 2027 required.']:
            self.status(text,'unknown')
    def test_year_cohorts_are_explicit_and_not_exact_graduation_dates(self):
        self.status('Must graduate in 2026 or 2027.','met')
        self.status('Must graduate in 2027.','not_met')
        self.assertNotIn('graduation_date',self.profile)
    def test_explicit_start_after_december_is_met(self):
        self.status('Must start on January 1, 2027.','met')
        self.status('Must start in January 2027.','met')
    def test_start_within_unknown_december_day_remains_unknown(self):
        self.status('Must start on December 15, 2026.','unknown')
        self.status('Must start in December 2026.','unknown')
    def test_start_before_availability_is_not_met(self):
        self.status('Must start by November 30, 2026.','not_met')
    def test_graduation_does_not_supply_missing_availability(self):
        profile=dict(self.profile);profile.pop('earliest_start')
        self.status('Must start on January 1, 2027.','unknown',profile)
    def test_future_degree_cohort_can_match_planned_degree_without_claiming_completion(self):
        result=self.status("Bachelor's degree in Computer Science, graduating between December 2026 and June 2027.",'met')
        self.assertIn('planned',result['explanation'])
        self.assertNotIn('degree_completed',self.profile['education'])
    def test_bare_degree_does_not_invent_completion(self):
        self.status("Bachelor's degree in Computer Science required.",'unknown')
        p={**self.profile,'education':{**self.profile['education'],'degree_completed':True}}
        self.status("Bachelor's degree in Computer Science required.",'met',p)
    def test_degree_discipline_or_level_not_assumed_equivalent(self):
        for text in ["Master's degree in Computer Science required.","Bachelor's degree in Electrical Engineering required.","Bachelor's degree in Computer Science or equivalent experience required."]:
            self.status(text,'unknown')
    def test_one_earned_degree_does_not_prove_absence_of_another(self):
        p={**self.profile,'education':{**self.profile['education'],'degree_completed':True}}
        self.status("Master's degree in Computer Science required.",'unknown',p)
    def test_internship_count_can_be_compared_without_inventing_duration(self):
        self.status('At least 2 internships required.','met')
        self.status('At least 3 internships required.','not_met')
        self.status('2 years of internship experience required.','unknown')
    def test_generic_and_fulltime_experience_are_separate(self):
        p={**self.profile,'experience_years':3}
        self.status('2 years of experience required.','met',p)
        self.status('2 years of full-time experience required.','unknown',p)
        p['commercial_fulltime_experience_years']=1
        self.status('2 years of full-time experience required.','unknown',p)
        self.status('2 years of commercial full-time experience required.','not_met',p)
        p['fulltime_experience_years']=1
        self.status('2 years of full-time experience required.','not_met',p)
    def test_invalid_numeric_profile_facts_are_unknown(self):
        for value in [True,-1,float('nan'),'2']:
            self.status('2 years of full-time experience required.','unknown',{**self.profile,'fulltime_experience_years':value})
    def test_partial_compound_or_negated_clauses_never_match(self):
        for text in ['Must start on January 1, 2027 and have a PhD.','Must graduate in 2026 unless employed elsewhere.','No prior experience required.',"Bachelor's degree in Computer Science required; security clearance required.",'2 years of experience required or equivalent projects.']:
            self.status(text,'unknown')
    def test_missing_bad_and_partial_profile_dates_are_unknown(self):
        for value in [None,'2026-13','December 2026','2026']:
            p={**self.profile,'graduation_month':value,'education':{**self.profile['education'],'graduation_month':value}}
            self.status('Must graduate in 2026.','unknown',p)
    def test_profile_is_immutable(self):
        import copy
        before=copy.deepcopy(self.profile)
        self.status('Must graduate in 2026.','met')
        self.assertEqual(self.profile,before)

    def test_month_only_by_deadline_is_ambiguous_and_uppercase_or_is_supported(self):
        self.status('Must start by December 2026.','unknown')
        self.status('Must graduate in 2026 OR 2027.','met')

    def test_explicit_minimum_experience_variants_and_fractional_years(self):
        p={**self.profile,'fulltime_experience_years':2+11/12}
        self.status('3+ years of full-time experience required.','not_met',p)
        self.status('At least 2 years of full-time experience required.','met',p)
    def test_standard_degree_abbreviations_are_format_aliases_only(self):
        for degree in ['B.S. in Computer Science','B.S., Computer Science','Bachelor of Science in Computer Science']:
            p={**self.profile,'education':{**self.profile['education'],'degree':degree}}
            self.status("Bachelor's degree in Computer Science, graduating between December 2026 and June 2027.",'met',p)

    def test_unknown_deadline_day_does_not_hide_clear_month_ordering(self):
        self.status('Must start by January 2027.','met')
        self.status('Must start by November 2026.','not_met')

    def test_top_level_graduation_cites_the_field_actually_used(self):
        result=self.status('Must graduate in 2026.','met',{'graduation_month':'2026-12'})
        self.assertEqual(result['profile_evidence_ids'],['profile:graduation_month'])
    def test_conflicting_graduation_months_never_choose_one(self):
        p={**self.profile,'education':{**self.profile['education'],'graduation_month':'2027-06'}}
        self.status('Must graduate in 2026.','unknown',p)
    def test_truthy_non_boolean_completion_and_invalid_internship_counts_stay_unknown(self):
        for value in ['in progress',1,'yes',[],{}]:
            self.status("Bachelor's degree in Computer Science required.",'unknown',{**self.profile,'education':{**self.profile['education'],'degree_completed':value}})
        for count in [True,False,'2',-1,float('nan')]:
            self.status('At least 1 internships required.','unknown',{**self.profile,'internships_count':count})

    def test_degree_uncertainty_cannot_hide_confirmed_cohort_mismatch(self):
        result=evaluate_clause(self.profile,"Master's degree in Computer Science, graduating between January 2027 and June 2027.")
        self.assertEqual(result['status'],'not_met')
        self.assertEqual(result['profile_evidence_ids'],['profile:education.graduation_month'])
    def test_upper_window_boundary_inside_candidate_month_stays_unknown(self):
        result=evaluate_clause(self.profile,'Graduation between November 1, 2026 and December 15, 2026 required.')
        self.assertEqual(result['status'],'unknown')
    def test_completed_other_degree_is_not_computer_science_equivalence(self):
        for degree in ['BA Mathematics','Bachelor of Science in Biology','Master of Science in Computer Science']:
            profile=copy.deepcopy(self.profile);profile['education'].update(degree=degree,degree_completed=True)
            self.assertEqual(evaluate_clause(profile,"Bachelor's degree in Computer Science required.")['status'],'unknown')

    def test_exact_combined_graduation_and_start_months(self):
        self.status('To be eligible for this role, you need to graduate in December 2026 and be able to start full time by January/February 2027.','met')
        self.status('To be eligible for this role, you need to graduate by Dec 2026 and be able to start FTE by January/February 2027.','met')
    def test_combined_clause_retains_all_candidate_deadline_boundaries(self):
        text='To be eligible for this role, you need to graduate in December 2026 and be able to start full time by January/February 2027.'
        for month,expected in [('2026-12','met'),('2027-01','unknown'),('2027-02','unknown'),('2027-03','not_met')]:
            p={**self.profile,'earliest_start':{'month':month,'precision':'month','day':None}}
            self.status(text,expected,p)
    def test_combined_known_false_conjunct_not_hidden_by_missing_other_fact(self):
        text='To be eligible for this role, you need to graduate in November 2026 and be able to start full time by January/February 2027.'
        p=copy.deepcopy(self.profile);p.pop('earliest_start');self.status(text,'not_met',p)
        p.pop('graduation_month');p['education'].pop('graduation_month');p['earliest_start']={'month':'2027-04','precision':'month','day':None}
        self.status(text,'not_met',p)
    def test_combined_employer_clause_never_drops_added_constraints_or_negation(self):
        stem='To be eligible for this role, you need to graduate in December 2026 and be able to start full time by January/February 2027'
        for text in [stem+' and hold US citizenship.',stem.replace('and be able','or be able')+'.',stem.replace('need to graduate','need not graduate')+'.',stem.replace('December 2026','Winter 2026')+'.',stem.replace('January/February 2027','January 2027/February 2028')+'.']:
            self.status(text,'unknown')
    def test_combined_unknown_availability_never_inferred_from_graduation(self):
        text='To be eligible for this role, you need to graduate by Dec 2026 and be able to start FTE by January/February 2027.'
        p=copy.deepcopy(self.profile);p.pop('earliest_start');self.status(text,'unknown',p)
    def test_by_graduation_month_does_not_change_exact_month_requirement(self):
        p=copy.deepcopy(self.profile);p['graduation_month']='2026-11';p['education']['graduation_month']='2026-11'
        self.status('To be eligible for this role, you need to graduate by Dec 2026 and be able to start FTE by January/February 2027.','met',p)
        self.status('To be eligible for this role, you need to graduate in Dec 2026 and be able to start FTE by January/February 2027.','not_met',p)
    def test_pursuing_degree_requires_explicit_enrollment_not_future_date(self):
        text="Pursuing a bachelor's or master’s degree in computer science, engineering, or another related field."
        self.status(text,'unknown')
        p=copy.deepcopy(self.profile);p['education']['enrollment_status']='in_progress';self.status(text,'met',p)
        p['education']['degree_completed']=True;self.status(text,'unknown',p)
    def test_pursuing_degree_never_invents_related_field_equivalence(self):
        text="Pursuing a bachelor's or master’s degree in computer science, engineering, or another related field."
        p=copy.deepcopy(self.profile);p['education'].update(enrollment_status='in_progress',degree='BA Mathematics')
        self.status(text,'unknown',p)
    def test_exact_previous_internship_fragment_has_count_evidence(self):
        self.status('Previous internship experience.','met')
        self.status('Previous internship experience.','unknown',{**self.profile,'internships_count':True})

    def test_pursuing_degree_rejects_malformed_completion_flag(self):
        text="Pursuing a bachelor's or master’s degree in computer science, engineering, or another related field."
        for flag in ['yes','in progress',1,0,[],{}]:
            p=copy.deepcopy(self.profile);p['education'].update(enrollment_status='in_progress',degree_completed=flag)
            self.status(text,'unknown',p)

    def test_descending_or_duplicate_slash_deadlines_remain_unclear(self):
        for months in ['December/January','January/January']:
            self.status('To be eligible for this role, you need to graduate in December 2026 and be able to start full time by '+months+' 2027.','unknown')
    def test_day_precise_or_malformed_availability_is_not_silently_rounded(self):
        for start in [{'month':'2027-01','precision':'day','day':5},{'month':'2027-01','precision':'month','day':5},{'month':'2026-12','day':None}]:
            self.status('Must start on January 1, 2027.','unknown',{**self.profile,'earliest_start':start})
    def test_withdrawal_cannot_leave_future_graduation_supported(self):
        p=copy.deepcopy(self.profile);p['education']['enrollment_status']='withdrawn'
        self.status('Must graduate in 2026.','unknown',p)
