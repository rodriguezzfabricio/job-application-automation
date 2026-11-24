from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from job_runtime.scoring import score

class ScoringTests(unittest.TestCase):
    def test_exact_shared_rubric_bands(self):
        r=score('Software Engineer New Grad',[True,True],1,'employer',True)
        self.assertEqual(r['score'],100)
        self.assertEqual(r['parts'],{'role':40,'requirements':25,'freshness':20,'apply_path':10,'company':5})
    def test_missing_required_skill_caps_points_without_eligibility_veto(self):
        r=score('Software Engineer',[True,True,True,False],10,'employer',True)
        self.assertEqual(r['parts']['requirements'],10);self.assertGreaterEqual(r['score'],35)
    def test_unknown_requirements_are_explicit_and_data_is_a_target(self):
        r=score('Data Analyst',[],50,'one_click')
        self.assertFalse(r['requirements_assessed']);self.assertEqual(r['score'],41)

if __name__=='__main__':unittest.main()
