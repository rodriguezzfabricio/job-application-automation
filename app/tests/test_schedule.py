import unittest,tempfile,plistlib,os
from pathlib import Path
try:
    from schedule import write_agent
except ImportError:write_agent=None

class ScheduleTests(unittest.TestCase):
    def test_launch_agent_uses_absolute_arguments_and_no_shell_or_secret_values(self):
        self.assertIsNotNone(write_agent)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);config=root/'service.json';config.write_text('{}');config.chmod(0o600);entry=root/'main.py';entry.write_text('');destination=root/'local.jobtools.job-assistant.plist'
            write_agent(config,Path('/usr/bin/python3'),entry,destination,root/'logs')
            p=plistlib.loads(destination.read_bytes());self.assertEqual(p['ProgramArguments'],['/usr/bin/python3',str(entry),'service','--config',str(config)]);self.assertEqual(p['StartInterval'],300);self.assertTrue(p['RunAtLoad']);self.assertFalse(p['KeepAlive']);self.assertEqual(destination.stat().st_mode&0o777,0o600)
    def test_existing_agent_is_never_overwritten(self):
        self.assertIsNotNone(write_agent)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dest=root/'existing.plist';dest.write_text('existing');config=root/'service.json';config.write_text('{}');config.chmod(0o600);entry=root/'main.py';entry.write_text('')
            with self.assertRaises(FileExistsError):write_agent(config,Path('/usr/bin/python3'),entry,dest,root/'logs')
            self.assertEqual(dest.read_text(),'existing')
