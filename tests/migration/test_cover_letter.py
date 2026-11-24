import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'stopgap-tools'))
import cover_letter

class CoverLetterTests(unittest.TestCase):
    def test_readable_pdf_and_distinct_attempt_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config.json';config.write_text(json.dumps({'name':'Synthetic Candidate','contact':'candidate@example.test'}))
            old=(cover_letter.CONFIG,cover_letter.OUT);cover_letter.CONFIG=str(config);cover_letter.OUT=str(root/'output')
            outputs=[]
            try:
                for attempt in ('a','b'):
                    body=root/(attempt+'.txt');body.write_text(' '.join(['This synthetic candidate has only the fictional qualifications supplied for this local fixture.']*8))
                    stdout=io.StringIO()
                    with contextlib.redirect_stdout(stdout):code=cover_letter.main(['cover_letter.py','Fixture Employer','Fixture Engineer',str(body)])
                    self.assertEqual(code,0,stdout.getvalue());result=json.loads(stdout.getvalue());outputs.append(result['path'])
                    text=subprocess.check_output(['pdftotext',result['path'],'-'],text=True)
                    self.assertIn('Synthetic Candidate',text);self.assertIn('Fixture Engineer',text);self.assertIn('Fixture Employer',text)
                self.assertNotEqual(*outputs)
            finally:cover_letter.CONFIG,cover_letter.OUT=old

if __name__=='__main__':unittest.main()
