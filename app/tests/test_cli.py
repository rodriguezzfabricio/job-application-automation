import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class CLITests(unittest.TestCase):
    def test_semantic_cli_is_explicit_and_requires_real_poll_inputs(self):
        root=Path(__file__).resolve().parents[1]
        help_result=subprocess.run([sys.executable,str(root/'main.py'),'poll','--help'],text=True,capture_output=True)
        self.assertIn('--semantic',help_result.stdout)
        invalid=subprocess.run([sys.executable,str(root/'main.py'),'demo','--semantic','deepseek'],text=True,capture_output=True)
        self.assertNotEqual(invalid.returncode,0);self.assertIn('only valid for poll',invalid.stderr)

    def test_demo_repeat_status_and_sample_record(self):
        root=Path(__file__).resolve().parents[1];tmp=root/'.test-tmp';tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp) as directory:
            cmd=[sys.executable,str(root/'main.py'),'demo','--output',directory]
            first=subprocess.run(cmd,text=True,capture_output=True);self.assertEqual(first.returncode,0,first.stderr)
            result=json.loads(first.stdout);self.assertEqual([r['status'] for r in result['packets']],['capture_accepted','capture_accepted'])
            self.assertEqual(result['sample_job']['workplace'],'unknown');self.assertEqual(result['sample_job']['status'],'review')
            self.assertEqual(len(list(Path(directory).glob('*/message.eml'))),2)
            second=subprocess.run(cmd,text=True,capture_output=True);self.assertEqual(second.returncode,0,second.stderr);self.assertEqual([r['status'] for r in json.loads(second.stdout)['packets']],['duplicate','duplicate'])
            status=subprocess.run([sys.executable,str(root/'main.py'),'status','--output',directory],text=True,capture_output=True);self.assertEqual(status.returncode,0,status.stderr);self.assertEqual(json.loads(status.stdout)['intents']['capture_accepted'],2)

    def test_outbox_status_and_disabled_worker_do_not_send(self):
        root=Path(__file__).resolve().parents[1];tmp=root/'.test-tmp';tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp) as directory:
            args=[sys.executable,str(root/'main.py'),'outbox','--output',directory]
            status=subprocess.run(args,text=True,capture_output=True);self.assertEqual(status.returncode,0,status.stderr);self.assertEqual(json.loads(status.stdout)['queue'],{})
            disabled=subprocess.run(args+['--outbox-action','drain'],text=True,capture_output=True);self.assertEqual(disabled.returncode,0,disabled.stderr);self.assertEqual(json.loads(disabled.stdout)['status'],'disabled')
            missing=subprocess.run(args+['--outbox-action','drain','--send'],text=True,capture_output=True);self.assertNotEqual(missing.returncode,0);self.assertIn('requires',missing.stderr)


class LauncherTests(unittest.TestCase):
    def test_launcher_status(self):
        root=Path(__file__).resolve().parents[1]
        self.assertTrue((root/'run-demo').exists(),'launcher missing')
        p=subprocess.run([str(root/'run-demo'),'status'],text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(json.loads(p.stdout)['live_sending_available'])

if __name__=='__main__':unittest.main()
