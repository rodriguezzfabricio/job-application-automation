import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'stopgap-tools'))
import tracker
from job_runtime.store import Store
from job_runtime.reporting import prepare, verify


class ReportingTests(unittest.TestCase):
    def test_confirmed_job_replaces_stale_handoff_without_touching_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 't.sqlite'
            c = sqlite3.connect(p); c.executescript(tracker.SCHEMA); c.close()
            s = Store(p)
            key, _ = tracker.upsert_job(s.con, {'company': 'Example', 'title': 'Software Engineer', 'link': 'https://example.test/1'})
            tracker.add_application(s.con, {'job_key': key, 'company': 'Example', 'title': 'Software Engineer', 'link': 'https://example.test/1', 'submitted_by': 'task', 'confirmed_via': 'portal'})
            old = [tracker.HEADERS['Today'], ['Last run', '', '', 'old summary', '', '', '', ''], ['Waiting on you', 'Example', 'Software Engineer', 'Please submit', 'https://example.test/1', '', 'fill now', key]]
            r = prepare(s, {'Today': old}, {'Today': 1}, 'Current verified summary')
            cells = r['patches']['Today']['cells']
            self.assertTrue(any(x['row'] == 2 and x['col'] == 0 and x['value'] == 'Applied today' for x in cells))
            self.assertTrue(any(x['row'] == 1 and x['col'] == 3 and x['value'] == 'Current verified summary' for x in cells))
            self.assertFalse(any(x['col'] == 6 for x in cells))
            s.con.close()

    def test_unknown_legacy_outcome_overrides_easy_apply_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 't.sqlite'
            c = sqlite3.connect(p); c.executescript(tracker.SCHEMA); c.close()
            s = Store(p)
            key, _ = tracker.upsert_job(s.con, {'company': 'Example', 'title': 'Software Engineer', 'link': 'https://example.test/1'})
            s.con.execute("INSERT INTO jr_backlog(provider,item_key,payload,first_seen,state) VALUES('legacy_unknown_submission',?,'{}',?,'pending')", (key, s.now()))
            old = [tracker.HEADERS['Today'], ['One click', 'Example', 'Software Engineer', 'Apply yourself', 'https://example.test/1', '', '', key]]
            r = prepare(s, {'Today': old}, {'Today': 1}, 'Under review')
            self.assertTrue(any(x['row'] == 1 and x['col'] == 0 and x['value'] == 'Outcome uncertain' for x in r['patches']['Today']['cells']))
            self.assertFalse(any(x['col'] == 6 for x in r['patches']['Today']['cells']))
            s.con.close()


    def test_grouped_requests_preserve_commands_and_detect_a_concurrent_row_move(self):
        from copy import deepcopy
        from unittest.mock import patch

        header = tracker.HEADERS['Jobs']
        old = [header, ['', 'A', 'Old A', '', '', 40, 'New', '', '', '', '', 'fill now', 'key-a'],
                       ['', 'B', 'Old B', '', '', 41, 'New', '', '', '', '', 'skip', 'key-b']]
        desired = deepcopy(old)
        for row in desired[1:]:
            row[1] += ' Company'
            row[2] = 'New ' + row[2]
            row[10] = 'new note'
            row[11] = 'ignored command'
        with patch('job_runtime.reporting.exports', return_value={'Jobs': desired}):
            plan = prepare(None, {'Jobs': old}, {'Jobs': 11}, '')
        self.assertEqual(len(plan['batches']), 1)
        batch = plan['batches'][0]
        self.assertEqual(batch['cell_count'], 6)
        self.assertEqual(len(batch['requests']), 2)
        self.assertEqual(len(batch['requests'][0]['updateCells']['rows']), 2)
        self.assertTrue(verify({'Jobs': old}, plan['expected'], batches_applied=0)['ok'])
        self.assertFalse(verify({'Jobs': [old[0], old[2], old[1]]}, plan['expected'], batches_applied=0)['ok'])
        self.assertFalse(verify({'Jobs': old}, plan['expected'])['ok'])
        desired[1][11] = 'concurrent user input'
        self.assertTrue(verify({'Jobs': desired}, plan['expected'])['ok'])
        desired[1][2] = 'one of the changed cells failed to write'
        self.assertFalse(verify({'Jobs': desired}, plan['expected'])['ok'])

    def test_large_mirror_generates_sequential_bounded_batches(self):
        from unittest.mock import patch

        header = tracker.HEADERS['Jobs']
        old = [header]
        desired = [header, *[['', 'A', 'Engineer', '', '', 40, 'New', '', '', '', '', '', f'key-{i}'] for i in range(400)]]
        with patch('job_runtime.reporting.exports', return_value={'Jobs': desired}):
            plan = prepare(None, {'Jobs': old}, {'Jobs': 11}, '')
        self.assertGreater(len(plan['batches']), 1)
        self.assertTrue(all(x['cell_count'] <= 1500 for x in plan['batches']))
        count = plan['batches'][0]['cell_count'] // 5
        self.assertTrue(verify({'Jobs': desired[:count + 1]}, plan['expected'], batches_applied=1)['ok'])
        self.assertFalse(verify({'Jobs': desired}, plan['expected'], batches_applied=1)['ok'])
        self.assertTrue(verify({'Jobs': desired}, plan['expected'])['ok'])


    def test_grouped_connector_snapshot_keeps_rows_and_checks_append_command(self):
        from unittest.mock import patch
        tab = 'Jobs'
        header = tracker.HEADERS[tab]
        original = {tab: [header]}
        after = ['', 'A', 'Engineer', '', '', 40, 'New', '', '', '', '', '', 'key-a']
        with patch('job_runtime.reporting.exports', return_value={tab: [header, after]}):
            plan = prepare(None, original, {tab: 11}, '')
        def response(*rows):
            return {'structuredContent': {'sheets': [{'properties': {'title': tab, 'sheetId': 11},
                     'data': [{'rowData': [{'values': [
                         {'effectiveValue': {'numberValue' if isinstance(v, int) else 'stringValue': v}} for v in row]}
                         for row in rows]}]}]}}
        self.assertTrue(verify(response(header), plan['expected'], batches_applied=0)['ok'])
        self.assertTrue(verify(response(header, after), plan['expected'])['ok'])
        after[11] = 'skip'
        self.assertFalse(verify(response(header, after), plan['expected'])['ok'])


if __name__ == '__main__': unittest.main()
