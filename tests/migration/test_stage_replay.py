import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from job_runtime.model_routes import BASELINE, CANDIDATES, evidence_path, qualified, run_stage, select_route

EVAL = Path(__file__).resolve().parents[2] / 'codex' / 'eval'
sys.path.insert(0, str(EVAL))
from stage_archive import PORTALS, grade_episode, load_episodes, split_batches
from stage_fixture_suite import browser_path, build_suite, execute_composition
from stage_replay import arm_usage, cascade_scores, fallback_call, persist, recover_interrupted, should_call
sys.path.pop(0)


class StageReplayTest(unittest.TestCase):
    def proof(self):
        return {'pilot_passed': True, 'batches': [
            {'batch_id': str(batch), 'matched_cases': 20, 'quality_passed': True, 'comparable': True,
             'baseline_cost_per_correct': 0.2, 'candidate_cost_per_correct': 0.1, 'local_execution_cases': 18,
             'execution_parity': {'candidate': 18, 'baseline': 18}} for batch in (1, 2)]}

    def test_tie_unknown_and_single_batch_fail_closed(self):
        self.assertTrue(qualified(self.proof()))
        for field, value in [('candidate_cost_per_correct', 0.2), ('candidate_cost_per_correct', None),
                             ('matched_cases', 19), ('quality_passed', False), ('comparable', False), ('execution_parity', None)]:
            proof = self.proof()
            proof['batches'][1][field] = value
            self.assertFalse(qualified(proof))
        proof = self.proof()
        proof['batches'].pop()
        self.assertFalse(qualified(proof))

    def test_runner_falls_back_only_after_qualified_miss_and_requires_served_model(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = evidence_path(root)
            path.parent.mkdir(parents=True)
            for filename in ('results.json', 'suite.json'):
                path.with_name(filename).write_text('{}')
            digest = hashlib.sha256(b'{}').hexdigest()
            stage = 'classification'
            candidate = CANDIDATES[stage]
            proof = {**self.proof(), 'model': candidate.model, 'effort': candidate.effort}
            state = {'schema': 1, 'results_sha256': digest, 'suite_sha256': digest, 'stages': {stage: proof}}
            path.write_text(json.dumps(state))
            self.assertEqual(select_route(root, stage).model, candidate.model)
            calls = []

            def request(route, _prompt, _images):
                calls.append(route.model)
                served = BASELINE if route.model == candidate.model else route
                return {'response': {'valid': True}, 'served_model': served.model, 'served_effort': served.effort,
                        'returncode': 0, 'tools_used': False}

            returned = run_stage(root, stage, 'minimal', validate=lambda answer: answer == {'valid': True},
                                 approved_request=request)
            self.assertEqual(calls, [candidate.model, BASELINE.model])
            self.assertEqual(returned['response'], {'valid': True})
            path.with_name('results.json').write_text('{"updated":true}')
            self.assertEqual(select_route(root, stage), BASELINE)

    def test_exact_opening_duplicate_and_false_handoff_are_separate(self):
        gold = {'observed_opening': 'one', 'outcome': 'confirmed', 'receipt': 'R-1',
                'submit_count': 1, 'handoff_reason': '', 'action': 'submit'}
        correct = grade_episode('visual', gold, gold)
        self.assertTrue(all(correct.values()))
        wrong = grade_episode('visual', gold, {**gold, 'observed_opening': 'different', 'submit_count': 2,
                                               'handoff_reason': 'optional field'})
        self.assertFalse(wrong['observed_opening'])
        self.assertFalse(wrong['duplicate'])
        self.assertFalse(wrong['avoidable_handoff'])

    def test_batches_are_balanced_and_keep_employers_together(self):
        cases = [{'portal': portal, 'id': f'{portal}-{i}', 'opening_group': f'{portal}-{i}', 'employer_group': f'E-{i}',
                  'gold': {'outcome': 'confirmed' if i % 2 else 'blocked'}}
                 for portal in PORTALS for i in range(10)]
        left, right = split_batches(cases)
        self.assertEqual([len(left), len(right)], [20, 20])
        self.assertEqual(split_batches(list(reversed(cases))), [left, right])
        self.assertFalse({e['employer_group'] for e in left} & {e['employer_group'] for e in right})
        for batch in (left, right):
            for portal in PORTALS:
                self.assertEqual(sum(e['portal'] == portal for e in batch), 5)

    def test_paid_cost_requires_nonzero_nonoverlapping_meter_including_retries(self):
        costs = {'r': {'status': 'settled', 'reset_at': 73}}
        row = {'reservation': 'r', 'account_sha256': 'a', 'overlap': False, 'usage': {'input_tokens': 123},
               'meter_before': {'reset_at': 73, 'observed_delta': 0}, 'meter_after': {'reset_at': 73, 'observed_delta': 1}}
        self.assertEqual(arm_usage([row, row], costs, set()), 2)
        self.assertIsNone(arm_usage([row, {**row, 'overlap': True}], costs, set()))
        self.assertIsNone(arm_usage([row, {**row, 'meter_after': row['meter_before']}], costs, set()))

    def test_fallback_only_receives_missed_cases_and_requires_subset_grades(self):
        grade = {'correct': {'opening': True}, 'wrong': {'opening': False}}
        entry = {'arms': {'candidate': [{'complete': True, 'grade': grade}]}}
        got = []

        def subset(ids):
            got.append(ids)
            return 'only the original missed input', (), lambda _response: {'wrong': {'opening': True}}

        call = fallback_call('batch-1:visual', entry, subset, retry=False)
        self.assertEqual(got, [{'wrong'}])
        self.assertEqual(call[1], 'fallback')
        self.assertEqual(entry['fallback_input']['cases'], ['wrong'])
        self.assertIsNone(cascade_scores(grade, []))
        self.assertIsNone(cascade_scores(grade, [{'complete': True, 'grade': {'correct': {'opening': True}}}]))
        self.assertEqual(cascade_scores(grade, [{'complete': True, 'grade': {'wrong': {'opening': True}}}]),
                         ({'correct': {'opening': True}, 'wrong': {'opening': True}}, 1))
        entry['arms']['candidate'][-1]['grade'] = {'correct': {'opening': True}}
        self.assertIsNone(fallback_call('batch-1:visual', entry, subset, retry=False))

    def test_interrupted_attempt_keeps_charge_and_requires_explicit_retry(self):
        class Ledger:
            def finish(self, reservation):
                self.closed = reservation
                return {'status': 'uncertain', 'charged': 1}

        ledger = Ledger()
        attempt = {'reservation': 'existing', 'state': 'started', 'complete': False}
        state = {'replay': {'entry': {'arms': {'candidate': [attempt]}}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'results.json'
            recover_interrupted(state, ledger, path)
            self.assertEqual(ledger.closed, 'existing')
            self.assertEqual(state['settlements']['existing']['charged'], 1)
            self.assertEqual(json.loads(path.read_text()), state)
        self.assertEqual(attempt['state'], 'interrupted_unverified')
        self.assertFalse(should_call([attempt], retry=False))
        self.assertTrue(should_call([attempt], retry=True))

    def test_mismatched_receipt_after_a_click_stays_unknown_without_retry(self):
        gold = {'observed_opening': 'different', 'outcome': 'unknown', 'receipt': '',
                'submit_count': 1, 'handoff_reason': '', 'action': 'submit'}
        self.assertTrue(all(grade_episode('visual', gold, gold).values()))
        seen = grade_episode('visual', gold, {**gold, 'outcome': 'confirmed', 'receipt': 'wrong-opening-proof'})
        self.assertFalse(seen['outcome'])
        self.assertFalse(seen['receipt'])
        not_submitted = grade_episode('visual', gold, {**gold, 'outcome': 'not_submitted', 'submit_count': 0})
        self.assertFalse(not_submitted['outcome'])
        retried = grade_episode('visual', gold, {**gold, 'submit_count': 2})
        self.assertFalse(retried['duplicate'])

    def test_sandbox_verifies_actions_and_pdf_bytes_not_claims(self):
        try:
            browser_path()
        except RuntimeError:
            self.skipTest('Chromium is unavailable')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build_suite(root, persist)
            episodes, _ = load_episodes(root)
            successes = [e for e in episodes if e['gold']['outcome'] == 'confirmed'][:3]
            duplicate = next(e for e in episodes if e['gold']['outcome'] == 'already_applied')
            cases = [*successes, duplicate]
            proposals = {e['id']: {s: dict(e['gold']) for s in CANDIDATES} for e in cases}
            proposals[successes[1]['id']]['visual'].pop('action')
            proposals[successes[2]['id']]['writing']['pdf'] = 'operations-pdf'
            proposals[duplicate['id']]['visual']['action'] = 'submit'
            self.assertEqual(execute_composition(root, cases, proposals),
                             {cases[0]['id']: True, cases[1]['id']: False, cases[2]['id']: False, cases[3]['id']: False})


if __name__ == '__main__':
    unittest.main()
