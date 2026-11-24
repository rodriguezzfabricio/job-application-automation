import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path=Path(__file__).resolve().parents[2]/'ops/job-watchdog/watchdog.py'
spec=importlib.util.spec_from_file_location('watchdog',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class WatchdogTests(unittest.TestCase):
    def test_partial_finished_run_is_not_reported_as_stuck(self):
        now=dt.datetime(2026,11,13,2,0,tzinfo=dt.timezone.utc)
        state={'last_completed_at':now.isoformat(),'last_run_status':'partial','browser_last_success_at':now.isoformat()}
        self.assertEqual(module.decisions(now,state,{'finished_at':now.isoformat()})[0][0],'partial')
    def test_old_active_run_is_reported_as_stuck(self):
        now=dt.datetime(2026,11,13,2,0,tzinfo=dt.timezone.utc)
        started=(now-dt.timedelta(minutes=46)).isoformat()
        state={'last_completed_at':now.isoformat(),'last_run_status':'succeeded','browser_last_success_at':now.isoformat()}
        self.assertEqual(module.decisions(now,state,{'mode':'FAST','started_at':started,'finished_at':None})[0][0],'stuck')
    def test_fake_spool_deduplicates_without_claiming_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);now=dt.datetime(2026,11,13,2,0,tzinfo=dt.timezone.utc)
            self.assertEqual(module.run_once(p,now)['queued'],['stopped'])
            self.assertEqual(module.run_once(p,now)['queued'],[])
            self.assertEqual(len(list((p/'outbox').iterdir())),1)
            self.assertEqual(json.loads((p/'codex-alerts.json').read_text())['stopped']['delivery'],'pending')
