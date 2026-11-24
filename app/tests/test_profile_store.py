import json,os,unittest,signal,io
from pathlib import Path
import test_personal as fixtures
from packet import canonical,digest
from profile_store import load_profile
from personal import validate_evidence
from reportlab.pdfgen.canvas import Canvas

class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PersonalTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown);self.root=Path(self.fixture.tmp.name)
        self.profile=self.root/'profile.json';self.approval=self.root/'approval.json';self.profile.write_bytes(canonical(self.fixture.p));self.approval.write_text(json.dumps({'profile_sha256':digest(self.profile.read_bytes()),'source_pdf_sha256':self.fixture.p['source']['sha256'],'authority':'user-selected original resume; source-text reuse'}));os.chmod(self.profile,0o600);os.chmod(self.approval,0o600)
    def test_external_approval_and_original_pdf_are_bound(self):
        p,sha=load_profile(self.profile,self.approval);self.assertEqual(sha,digest(canonical(p)))
        p['name']='Other person';self.profile.write_bytes(canonical(p))
        with self.assertRaises(ValueError):load_profile(self.profile,self.approval)
    def test_unprotected_or_symlinked_approval_refused(self):
        os.chmod(self.approval,0o644)
        with self.assertRaises(ValueError):load_profile(self.profile,self.approval)
        os.chmod(self.approval,0o600);link=self.root/'link.json';link.symlink_to(self.approval)
        with self.assertRaises((ValueError,OSError)):load_profile(self.profile,link)
    def test_original_source_tamper_is_not_blessed_by_profile_digest(self):
        Path(self.fixture.p['source']['path']).write_bytes(b'changed source')
        with self.assertRaises(ValueError):load_profile(self.profile,self.approval)

    def test_fifos_are_refused_without_blocking(self):
        def deadline(*unused): raise TimeoutError('file read blocked')
        old=signal.signal(signal.SIGALRM,deadline)
        self.addCleanup(signal.signal,signal.SIGALRM,old)
        fifo=self.root/'pipe';os.mkfifo(fifo,0o600)
        for kind in ['profile','approval','source']:
            with self.subTest(kind=kind):
                signal.setitimer(signal.ITIMER_REAL,1)
                try:
                    with self.assertRaises(ValueError):
                        if kind=='profile':load_profile(fifo,self.approval)
                        elif kind=='approval':load_profile(self.profile,fifo)
                        else:
                            self.fixture.p['source']['path']=str(fifo)
                            validate_evidence(self.fixture.p,approval_sha256=digest(canonical(self.fixture.p)))
                finally:signal.setitimer(signal.ITIMER_REAL,0)

    def test_empty_source_claim_is_not_evidence(self):
        p=self.fixture.p;out=io.BytesIO();canvas=Canvas(out);canvas.showPage();canvas.save()
        source=Path(p['source']['path']);source.write_bytes(out.getvalue());p['source']['sha256']=digest(source.read_bytes())
        for variant in p['variants']:
            variant['text']='   ';p['facts'][variant['facts'][0]]['claim_text']='   '
        with self.assertRaises(ValueError):validate_evidence(p,approval_sha256=digest(canonical(p)))
