"""Batch matching: code supplies postings and commits; the model only writes requirements."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from test_runtime import RuntimeTests
from job_runtime import adapters, match_batch
from job_runtime.match_batch import ats_target, hard_gate, prepare, commit
from job_runtime.policy import gate

POSTING = ('We build payments software. You will design APIs and services in Python and Go. '
           'Requirements: experience with Python, SQL and REST APIs; familiarity with AWS. ') * 6


class MatchBatchTests(RuntimeTests):
    def queued(self, company, suffix, text=None, complete=True):
        key = self.job(company, suffix, resume='')
        if text is not None:
            f = self.path.parent / f'{suffix}.txt'
            f.write_text(text)
            adapters.defer(self.s, 'requirements', key, {'description_file': str(f), 'description_complete': complete,
                                                         'source_ref': 'fixture:feed'}, self.owner, self.token)
        return key

    def test_batch_uses_saved_full_text_and_writes_one_file(self):
        a = self.queued('Alpha', 'a', POSTING)
        b = self.queued('Beta', 'b', POSTING)
        out = prepare(self.s, self.run['run_id'], [a, b], 8, self.owner, self.token)
        self.assertEqual((out['batch'], out['screened'], out['missing']), (2, 0, 0))
        text = Path(out['batch_text']).read_text()
        self.assertIn('job_key: ' + a, text)
        self.assertIn('job_key: ' + b, text)
        self.assertIn('professional_years_required', text)

    def test_limit_and_already_matched_rows(self):
        keys = [self.queued(f'C{i}', f'c{i}', POSTING) for i in range(4)]
        self.s.con.execute("UPDATE jobs SET resume='swe.pdf' WHERE job_key=?", (keys[0],))
        out = prepare(self.s, self.run['run_id'], keys, 2, self.owner, self.token)
        self.assertEqual([i['job_key'] for i in json.loads((Path(out['output_file']).parent / 'match-batch.json').read_text())['items']], keys[1:3])

    def test_explicit_citizenship_requirement_is_screened_without_model(self):
        k = self.queued('Defense Co', 'd', POSTING + ' Applicants must be a U.S. citizen due to contract requirements.')
        out = prepare(self.s, self.run['run_id'], [k], 8, self.owner, self.token)
        self.assertEqual((out['batch'], out['screened']), (0, 1))
        self.assertEqual(self.s.job(k)['status'], 'Not eligible')
        self.assertIn('citizenship_required', self.s.job(k)['reason'])

    def test_unreadable_site_is_reported_not_guessed(self):
        k = self.queued('Agg', 'agg', 'Apply now', complete=False)
        self.s.con.execute("UPDATE jobs SET link='https://aplyr.ai/jobs/123' WHERE job_key=?", (k,))
        out = prepare(self.s, self.run['run_id'], [k], 8, self.owner, self.token)
        self.assertEqual(out['missing'], 1)
        self.assertIn('browser', out['missing_detail'][0]['reason'])

    def test_missing_text_is_fetched_from_job_board_and_saved(self):
        k = self.queued('Gh', 'gh')
        with patch.object(match_batch, 'fetch_text', return_value=(POSTING, 'https://boards-api.greenhouse.io/x', '')):
            out = prepare(self.s, self.run['run_id'], [k], 8, self.owner, self.token)
        self.assertEqual(out['batch'], 1)
        payload = json.loads(self.s.con.execute("SELECT payload FROM jr_backlog WHERE provider='requirements' AND item_key=?", (k,)).fetchone()[0])
        self.assertTrue(payload['description_complete'])
        self.assertEqual(Path(payload['description_file']).read_text(), POSTING)

    def test_commit_matches_skips_and_reports_without_stopping(self):
        a, b, c = (self.queued(n, n.lower(), POSTING) for n in ('A1', 'B1', 'C1'))
        prepare(self.s, self.run['run_id'], [a, b, c], 8, self.owner, self.token)
        out_dir = self.path.parent / 'codex-runtime' / 'runs' / self.run['run_id']
        (out_dir / 'match-requirements.json').write_text(json.dumps({a: {'track': 'backend', 'requirements': [], 'gates': {}},
                                                                     b: {'skip': 'page shell'}}))
        fake = {'status': 'Matched', 'score': {'score': 70}, 'resume': 'swe.pdf', 'gate': ''}
        with patch('job_runtime.matching.commit_match', return_value=fake) as cm:
            out = commit(self.s, self.run['run_id'], self.owner, self.token)
        self.assertEqual(cm.call_count, 1)
        self.assertTrue(cm.call_args.args[4]['description_complete'])
        self.assertEqual((out['matched'], out['skipped'], len(out['errors'])), (1, 1, 1))
        self.assertIn('model skip', self.s.con.execute("SELECT payload FROM jr_backlog WHERE item_key=?", (b,)).fetchone()[0])

    def test_old_gate_names_are_enforced(self):
        j = self.s.job(self.key)
        j['requirements'] = {'required_years': 5}
        self.assertEqual(gate(j, self.policy, self.at), 'more than 3 required years')
        j['requirements'] = {'government_required': True}
        self.assertEqual(gate(j, self.policy, self.at), 'government_role_required')
        j['requirements'] = {'required_years': 3, 'government_required': False}
        self.assertEqual(gate(j, self.policy, self.at), '')
        j['requirements'] = {'role': 'other'}
        self.assertEqual(gate(j, self.policy, self.at), 'not a target role (posting)')
        j['requirements'] = {'role': 'swe'}
        self.assertEqual(gate(j, self.policy, self.at), '')


class PureTests(unittest.TestCase):
    def test_ats_targets(self):
        self.assertEqual(ats_target('https://job-boards.greenhouse.io/examplecompany/jobs/9004579879'), {'gh': ('examplecompany', '9004579879')})
        self.assertEqual(ats_target('https://job-boards.greenhouse.io/embed/job_app?for=sampleco&token=9002071393'), {'gh': ('sampleco', '9002071393')})
        self.assertEqual(ats_target('https://jobs.lever.co/acme/0b9f1e2a-1111-2222-3333-444455556666')['lever'][0], 'acme')
        self.assertEqual(ats_target('https://jobs.ashbyhq.com/acme/0b9f1e2a-1111-2222-3333-444455556666/application')['ashby'][0], 'acme')
        self.assertEqual(ats_target('https://jobs.smartrecruiters.com/ExampleBoard/90042746440007-data'), {'sr': ('ExampleBoard', '90042746440007')})
        wd = ats_target('https://examplea.wd5.myworkdayjobs.com/en-US/Careers/job/US---Example-City/Associate-Data-Engineer_REF598269W')['wd']
        self.assertEqual(wd, ('https://examplea.wd5.myworkdayjobs.com', 'examplea', 'Careers', '/job/US---Example-City/Associate-Data-Engineer_REF598269W'))
        self.assertIsNone(ats_target('https://aplyr.ai/jobs/1'))

    def test_hard_gate_only_on_explicit_requirements(self):
        self.assertEqual(hard_gate('Must possess an active Secret clearance.')[0], 'clearance_required')
        self.assertEqual(hard_gate('U.S. citizenship is required for this role.')[0], 'citizenship_required')
        self.assertEqual(hard_gate('CLEARANCE REQUIRED FOR START: Yes')[0], 'clearance_required')
        for ok in ('Applicants must be U.S. citizens or permanent residents.',
                   'US Citizenship or Permanent Residency is required.',
                   'Government security clearance may be required in the future.',
                   'Some positions will require current U.S. Citizenship due to contract requirements.',
                   'Ability to obtain an S//SAR level security clearance, if required by the program.',
                   'This role requires access to export-controlled data; applicants must be U.S. persons.',
                   'Security clearance preferred.',
                   'A security clearance is not required.',
                   'We do not discriminate on the basis of citizenship status.',
                   'Experience with Python is required.'):
            self.assertEqual(hard_gate(ok), (None, None), ok)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(MatchBatchTests(n) for n in MatchBatchTests.__dict__ if n.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(PureTests))
    return suite
