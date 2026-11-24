import unittest,copy,io,hashlib,tempfile
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from personal import render_resume,validate_evidence
import test_personal
from packet import digest,canonical
class PersonalTrustTests(unittest.TestCase):
 def setUp(self):
  self.seed=test_personal.PersonalTests();self.seed.setUp();self.p=self.seed.p;self.approval=self.seed.approval
 def tearDown(self):self.seed.tearDown()
 def test_sourceless_consistent_forgery_is_refused(self):
  self.p['facts']['f1']['claim_text']=self.p['variants'][0]['text']='Created unsupported revenue.'
  with self.assertRaises(ValueError):render_resume(self.p,{'id':'j'},['api'])
 def test_unknown_section_refused(self):
  self.p['groups']['work']['section']='research'
  with self.assertRaises(ValueError):render_resume(self.p,{'id':'j'},['api'])

 def test_each_rendered_field_is_bound(self):
  for field,value in [('name','Invented Name'),('email','wrong@example.invalid'),('skills',['Rust']),('education',{'institution':'Other','degree':'PhD','graduation_month':'2026-12'}),('groups',{'work':{'name':'Invented Employer','role':'Principal','dates':'2026','section':'experience'}})]:
   p=copy.deepcopy(self.p);p[field]=value
   with self.assertRaises(ValueError,msg=field):render_resume(p,{'id':'j'},['api'],approval_sha256=self.approval)
 def test_hash_inside_profile_cannot_self_approve(self):
  self.p['approval_sha256']=digest(canonical(self.p))
  with self.assertRaises(ValueError):render_resume(self.p,{'id':'j'},['api'])
 def test_deterministic_personal_bytes_across_clocks(self):
  from unittest.mock import patch
  with patch('reportlab.lib.utils.time.time',return_value=1700000000):a=render_resume(self.p,{'id':'j'},['api'],approval_sha256=self.approval)
  with patch('reportlab.lib.utils.time.time',return_value=1800000000):b=render_resume(self.p,{'id':'j'},['api'],approval_sha256=self.approval)
  self.assertEqual(a,b)
