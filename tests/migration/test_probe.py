"""The diagnostic can inspect a live worker's snapshot without acquiring its lease."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'stopgap-tools'))
import tracker
from job_runtime.probe import probe
from job_runtime.store import Store


class ProbeTests(unittest.TestCase):
    def test_occupied_lease_does_not_block_read_only_probe_or_change_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authority = root / 'tracker.sqlite'
            con = sqlite3.connect(authority)
            con.executescript(tracker.SCHEMA)
            con.close()
            store = Store(authority)
            store.acquire('existing-production-worker')
            before = '\n'.join(store.con.iterdump())
            result = probe(authority, root / 'output')
            self.assertEqual('\n'.join(store.con.iterdump()), before)
            self.assertEqual(result['snapshot_integrity'], 'ok')
            self.assertTrue(result['authority_read_only'])
            self.assertEqual(result['employer_submissions'], 0)
            self.assertEqual(json.loads((root / 'output/latest-probe.json').read_text()), result)
            store.con.close()

    def test_legacy_database_is_not_migrated_by_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authority = root / 'tracker.sqlite'
            con = sqlite3.connect(authority)
            con.executescript(tracker.SCHEMA)
            before = '\n'.join(con.iterdump())
            probe(authority, root / 'output')
            self.assertEqual('\n'.join(con.iterdump()), before)
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='jr_leases'").fetchone())
            con.close()


if __name__ == '__main__':
    unittest.main()
