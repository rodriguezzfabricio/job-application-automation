import copy
import hashlib
import io
import unittest
from email import policy as mail_policy
from email.parser import BytesParser
from pypdf import PdfReader
from packet import build_packet, check_packet

PROFILE={'synthetic':True,'approved':True,'version':'sample-v1','name':'José Example (SAMPLE)','email':'sample@example.invalid','education':'Sample University | B.S. Computer Science | 2026','skills':['Python','SQL','Testing'],'groups':{'api':'Course Project - API','ui':'Course Project - Web UI'},'facts':{'f1':{'group':'api'},'f2':{'group':'ui'}},'variants':[{'id':'api-tests','group':'api','facts':['f1'],'text':'Built a Python API with SQLite transactions and integration tests.','skills':['Python','SQL','Testing']},{'id':'ui-form','group':'ui','facts':['f2'],'text':'Implemented JavaScript form validation and accessible error messages.','skills':['JavaScript']}]}
JOB={'synthetic':True,'id':'sample-api-001','employer':'Sample Employer','title':'New Grad Software Engineer','url':'https://example.invalid/jobs/sample-api-001','location':'NYC','description':'Build APIs and tests','source_hash':'a'*64}

class PacketTests(unittest.TestCase):
    def test_scalar_profile_skills_cannot_enter_direct_packet(self):
        bad=copy.deepcopy(PROFILE);bad['skills']='C++';bad['variants'][0]['skills']=['C']
        with self.assertRaises(ValueError):build_packet(bad,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
    def test_pdf_and_email_exact_attachment(self):
        packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        self.assertTrue(check_packet(packet,PROFILE,JOB))
        pdf=packet['pdf'];reader=PdfReader(io.BytesIO(pdf));self.assertEqual(len(reader.pages),1)
        text=reader.pages[0].extract_text();self.assertIn('José Example',text);self.assertIn('SQLite',text);self.assertNotIn('JavaScript form',text)
        message=BytesParser(policy=mail_policy.default).parsebytes(packet['email'])
        attachments=list(message.iter_attachments());self.assertEqual(len(attachments),1);self.assertEqual(attachments[0].get_payload(decode=True),pdf)
        self.assertEqual(packet['manifest']['pdf_sha256'],hashlib.sha256(pdf).hexdigest())
    def test_job_specific_variants_differ(self):
        first=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        second=build_packet(PROFILE,dict(JOB,id='sample-ui-002',url='https://example.invalid/jobs/sample-ui-002'),['ui-form'],'demo@example.invalid','2026-10-15T12:00:00Z')
        self.assertNotEqual(first['pdf'],second['pdf']);self.assertNotEqual(first['manifest']['selected_ids'],second['manifest']['selected_ids'])
    def test_missing_source_hash_blocked(self):
        with self.assertRaises(ValueError):build_packet(PROFILE,dict(JOB,source_hash=None),['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
    def test_tamper_and_wrong_job_blocked(self):
        packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        self.assertFalse(check_packet(dict(packet,pdf=packet['pdf']+b'changed'),PROFILE,JOB))
        self.assertFalse(check_packet(packet,PROFILE,dict(JOB,id='wrong')))
    def test_unapproved_fact_or_recipient_blocked(self):
        bad=copy.deepcopy(PROFILE);bad['variants'][0]['facts']=['invented']
        with self.assertRaises(ValueError):build_packet(bad,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        with self.assertRaises(ValueError):build_packet(PROFILE,JOB,['api-tests'],'real@example.com','2026-10-15T12:00:00Z')
    def test_recipient_tamper_rejected(self):
        packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        packet['manifest']['recipient']='real@example.com'
        msg=BytesParser(policy=mail_policy.SMTP).parsebytes(packet['email']);msg.replace_header('To','real@example.com');packet['email']=msg.as_bytes()
        self.assertFalse(check_packet(packet,PROFILE,JOB))
    def test_forged_pdf_text_with_recomputed_hash_rejected(self):
        from packet import digest,canonical
        forged=copy.deepcopy(PROFILE);forged['variants'][0]['text']='Reduced production latency by 90 percent without evidence.'
        packet=build_packet(forged,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        packet['manifest']['profile_hash']=digest(canonical(PROFILE))
        self.assertFalse(check_packet(packet,PROFILE,JOB))
    def test_overflow_and_missing_glyph_quarantine(self):
        bad=copy.deepcopy(PROFILE);bad['education']='Education experience '*350
        with self.assertRaises(ValueError):build_packet(bad,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')
        bad=copy.deepcopy(PROFILE);bad['name']='Sample \U0001f680'
        with self.assertRaises(ValueError):build_packet(bad,JOB,['api-tests'],'demo@example.invalid','2026-10-15T12:00:00Z')

if __name__=='__main__':unittest.main()
