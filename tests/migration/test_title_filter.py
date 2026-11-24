"""Title filter: hardware and field-engineering titles are dropped; software titles keep passing."""
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'stopgap-tools'))
from title_filter import decide

class TitleFilterTests(unittest.TestCase):
    def test_non_software_titles_are_dropped(self):
        for t in ('Laser Test Engineer','Optical Hardware Test Engineer','Product Quality Engineer, Intelligence Systems',
                  'RF Engineer I - New Grad (R6108)','Quality Engineer I','Vehicle Structures Engineer I - 2026 Graduate',
                  'Electrical Engineer I','Manufacturing Engineer','Process Engineer','Controls Engineer'):
            self.assertFalse(decide(t,'Remote, US')['keep'],t)
    def test_software_titles_still_pass(self):
        for t in ('Software Engineer, New Grad','Software Test Engineer','Data Engineer I','Backend Engineer I',
                  'Hardware Software Integration Developer','AI Engineer','Associate Software Developer','Full Stack Engineer',
                  'Cloud Engineer I','SDET I','Software Engineer I, Backend (Continuous Delivery)','Junior Data Analyst'):
            self.assertTrue(decide(t,'Remote, US')['keep'],t)

if __name__=='__main__':unittest.main()
