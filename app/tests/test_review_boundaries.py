import unittest
from policy import fit,instant
class ReviewBoundaryTests(unittest.TestCase):
    def test_extreme_timezone_is_value_error(self):
        with self.assertRaises(ValueError):instant('0001-01-01T00:00:00+01:00')
    def test_malformed_flags_and_nonfinite_years_are_review(self):
        p={'skills':[],'locations':['NYC'],'needs_sponsorship':False}
        j={'title':'New Grad Software Engineer','location':'NYC','description':'Build software.','government_contracting':False,'employment_type':'FullTime'}
        self.assertEqual(fit(p,j)['status'],'eligible')
        for change in [{'active':'false'},{'is_listed':'false'},{'min_years':float('nan')},{'min_years':False}]:
            with self.subTest(change=change):self.assertEqual(fit(p,dict(j,**change))['status'],'review')
