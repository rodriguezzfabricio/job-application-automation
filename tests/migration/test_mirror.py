import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'stopgap-tools'))
import tracker
from job_runtime.adapters import mirror_patch
from job_runtime.store import Denied

class MirrorTests(unittest.TestCase):
    def test_all_seven_export_headers_have_merge_strategy(self):
        headers=dict(tracker.HEADERS)
        for tab,header in headers.items():
            with self.subTest(tab=tab):
                self.assertEqual(mirror_patch(tab,[header],[header])['cells'],[])
    def test_outcome_updates_existing_history_row(self):
        header=tracker.HEADERS['History'];old=[header,['2026-11-12','Employer','Software Engineer','applied','email']]
        new=[header,['2026-11-12','Employer','Software Engineer','interview','email']]
        self.assertEqual(mirror_patch('History',old,new)['cells'],[{'row':1,'col':3,'value':'interview'}])
    def test_row_identity_ambiguity_fails_without_clearing(self):
        row=['2026-11-12','Employer','Software Engineer','applied','email']
        with self.assertRaises(Denied):mirror_patch('History',[tracker.HEADERS['History'],row,row],[tracker.HEADERS['History'],row])
    def test_unchanged_parallel_receipts_allow_unrelated_append(self):
        header = tracker.HEADERS['History']
        email = ['2026-11-12','Employer','Engineer','applied','email']
        page = [*email[:4], 'confirmation page']
        addition = ['2026-11-24','Employer','Another opening','applied','portal history']
        before = [header, email, page]
        patch = mirror_patch('History', before, [header, page, email, addition])
        self.assertEqual({cell['row'] for cell in patch['cells']}, {3})
        with self.assertRaises(Denied):
            mirror_patch('History', before, [header, page, [*email[:3], 'rejected', 'email']])

class FormattedValueTests(unittest.TestCase):
    def test_rejected_hour_padding_does_not_append_duplicate(self):
        from job_runtime.adapters import append_only_patch
        h=['Found (ET)','Company','Title','Link'];row=['2026-11-12 7:23','Fixture','Role','https://example.test/1']
        after=[h,['2026-11-12 07:23',*row[1:]]]
        self.assertEqual(append_only_patch('Rejected',[h,row],after)['rows'],[])
    def test_formatted_counts_and_equivalent_time_do_not_rewrite(self):
        from job_runtime.adapters import mirror_patch
        h=['Company','Applications','Last researched','Research status']
        a=[h,['Fixture','2','2026-11-12 10:00','todo']]
        b=[h,['Fixture',2,'2026-11-12T15:00:00Z','todo']]
        self.assertEqual(mirror_patch('Companies',a,b)['cells'],[])

if __name__=='__main__':unittest.main()
