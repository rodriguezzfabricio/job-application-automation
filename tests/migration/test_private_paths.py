import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]

class PrivatePathsTests(unittest.TestCase):
    def test_separate_checkout_uses_one_private_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,JOB_AUTOMATION_PRIVATE_ROOT=tmp,PYTHONPATH=str(ROOT/'stopgap-tools')+os.pathsep+str(ROOT))
            code='''import json,tracker,plan,feeds,cover_letter,email_find,seen_links,resume_sync
print(json.dumps([tracker.DB_PATH,tracker.LAST_RUN_PATH,tracker.OPEN_TABS_PATH,plan.DB_PATH,feeds.SEEN_PATH,cover_letter.OUT,cover_letter.CONFIG,email_find.KEY_PATH,email_find.CACHE_PATH,seen_links.PATH,resume_sync.LIB,resume_sync.DB]))'''
            paths=json.loads(subprocess.check_output([sys.executable,'-c',code],env=env,cwd=tmp,text=True))
            self.assertTrue(all(Path(p).is_relative_to(Path(tmp).resolve()) for p in paths),paths)
    def test_cli_rejects_mismatched_private_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,JOB_AUTOMATION_PRIVATE_ROOT=tmp,PYTHONPATH=str(ROOT))
            result=subprocess.run([sys.executable,'-m','job_runtime','--db','/different/private/tracker.sqlite','-'],input='{"op":"status"}',text=True,capture_output=True,env=env,cwd=tmp)
            self.assertEqual(result.returncode,2)
            self.assertIn('roots disagree',result.stdout)
