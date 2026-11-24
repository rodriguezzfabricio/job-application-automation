import json,unittest
from pathlib import Path
import test_polling as fixtures
from polling import poll_once
from semantic_segments import parse_segment_response

class SemanticPollingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PollingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.addCleanup(self.fixture.tearDown)
        self.calls=[]
        owner=self
        class Extractor:
            mode='segments'
            def extract(self,text,*,now):
                owner.calls.append(text)
                # The source text is the fixture's four-line description; never a profile.
                assert 'recipient@example' not in text
                response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Python']],['s2','none','unknown',[]],['s3','skill','preferred',['Rust']]]})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}}
                return parse_segment_response(response,text)
        self.extractor=Extractor()
    def run_poll(self,**kw):
        f=self.fixture
        return poll_once('example',f.profile,f.approval,f.output,get=f.get,now=fixtures.NOW,semantic_extractor=self.extractor,**kw)
    def test_opt_in_pipeline_binds_requirement_evidence_to_private_pdf_without_sending(self):
        result=self.run_poll(packet_limit=1)
        self.assertEqual(len(self.calls),1);self.assertEqual(result['external_messages_sent'],0)
        packet=Path(result['packets'][0]['directory']);manifest=json.loads((packet/'manifest.json').read_text())
        inspection=manifest['decision']['semantic_inspection'];self.assertEqual(inspection['evidence_rows'][0]['status'],'met')
        self.assertFalse(manifest['application_ready']);self.assertEqual(result['review'],1)
        from email.parser import BytesParser
        from email import policy
        message=BytesParser(policy=policy.default).parsebytes((packet/'message.eml').read_bytes())
        body=message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('Python experience required.',body);self.assertIn('Rust is preferred.',body)
        self.assertIn('Requirement evidence:',body);self.assertIn('UNKNOWN',body)
        before=(packet/'resume.pdf').read_bytes();again=self.run_poll(packet_limit=1)
        self.assertEqual(again['packets'][0]['status'],'cached_review');self.assertEqual((packet/'resume.pdf').read_bytes(),before)
    def test_failed_or_disabled_semantic_check_cannot_produce_a_packet(self):
        def fail(*args,**kw):raise ValueError('untrusted model output')
        self.extractor.extract=fail;result=self.run_poll()
        self.assertEqual(result['packets'],[]);self.assertEqual(result['held'],1);self.assertEqual(result['status'],'completed_with_holds')
        self.assertEqual(self.run_poll(packet_limit=0)['held'],0)
    def test_semantic_and_original_drafts_do_not_overwrite_each_other(self):
        f=self.fixture
        original=poll_once('example',f.profile,f.approval,f.output,get=f.get,now=fixtures.NOW)
        semantic=self.run_poll();self.assertNotEqual(original['packets'][0]['directory'],semantic['packets'][0]['directory'])
        self.assertTrue(Path(original['packets'][0]['directory']).exists())

    def test_inspection_review_can_downgrade_an_upstream_positive(self):
        from unittest.mock import patch
        with patch('polling.assess',return_value={'status':'eligible','reasons':[]}):result=self.run_poll()
        self.assertEqual(result['eligible'],0);self.assertEqual(result['review'],1)
        packet=Path(result['packets'][0]['directory']);manifest=json.loads((packet/'manifest.json').read_text())
        self.assertEqual(manifest['decision']['status'],'review')
