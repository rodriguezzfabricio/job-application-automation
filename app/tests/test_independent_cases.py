"""Independent same-family normative synthetic cases, not market accuracy."""
import copy,json,unittest
from pathlib import Path
import test_personal as fixtures
from packet import canonical,digest
from qualification import qualify
from semantic_segments import parse_segment_response

class IndependentReferenceTests(unittest.TestCase):
    def test_independently_authored_policy_cases(self):
        cases=json.loads((Path(__file__).resolve().parents[1]/'fixtures/independent-cases.json').read_text())['cases']
        self.assertEqual(len(cases),12)
        for case in cases:
            with self.subTest(case=case['id']):
                f=fixtures.PersonalTests();f.setUp()
                try:
                    profile=f.p;profile.update(locations=['NYC'],needs_sponsorship=False,work_authorized_us=True,internships_count=2,willing_to_relocate=True,earliest_start={'month':'2026-12','precision':'month','day':None});profile.update(case['profile_patch'])
                    job={'id':'synthetic:reference','title':'New Grad Software Engineer','description':case['description'],'location':'NYC','employment_type':'FullTime','active':True,'is_listed':True,'government_contracting':False,'employer':'Example commercial employer','required_skills':[]};job.update(case['job_patch'])
                    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':case['segments']})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}}
                    extraction=parse_segment_response(response,case['description']);decision=qualify(profile,job,extraction,'2026-10-16T10:00:00Z',approval_sha256=digest(canonical(profile)))
                    print(json.dumps({'reference_case':case['id'],'expected':case['expected'],'actual':decision['status']}))
                    self.assertEqual(decision['status'],case['expected'],case['why'])
                finally:f.tearDown()
