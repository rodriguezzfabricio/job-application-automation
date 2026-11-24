import tempfile
import unittest
from pathlib import Path

from job_runtime.form_step import check_evidence, plan, requisition_id


OPENING = {
    'job_key': 'same-employer|engineer|1234',
    'job_url': 'https://jobs.example.test/jobs/1234/customer-success-engineer',
    'title': 'Customer Success Engineer',
    'requisition_id': '1234',
}


class FormStepTests(unittest.TestCase):
    def test_plan_uses_named_saved_answers_and_blocks_credentials_and_attestation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'form-answers.md').write_text(
                '- Email: candidate@example.test\n- Authorized to work in the US: Yes\n'
                '- [allow] ordinary non-sensitive yes/no questions\n'
            )
            import json
            (root / 'form-options.json').write_text(json.dumps([{
                'field':'Are you authorized to work in the US?', 'meaning':'sample_work_answer',
                'value':'Yes', 'quote':'- Authorized to work in the US: Yes'}]))
            fields = [
                {'key': 'email', 'label': 'Email', 'type': 'email', 'required': True},
                {'key': 'work', 'label': 'Are you authorized to work in the US?', 'type': 'radio',
                 'options': [{'label': 'Yes', 'value': 'Y'}, {'label': 'No', 'value': 'N'}]},
                {'key': 'terms', 'label': 'I agree to the terms', 'type': 'checkbox', 'required': True},
                {'key': 'password', 'label': 'Email', 'kind': 'text', 'type': 'password', 'required': True},
                {'key': 'unknown', 'label': 'Can you work any shift?', 'type': 'radio', 'required': True},
            ]
            request = dict(job_key='fixture', fields=fields,
                           controls=[{'key': 'submit', 'label': 'Submit application'}])
            result = plan(request, root)
            self.assertEqual(result['ops'], [
                {'key': 'email', 'mode': 'text', 'value': 'candidate@example.test'},
                {'key': 'work', 'mode': 'radio', 'value': 'Y'},
            ])
            self.assertEqual([f['key'] for f in result['blocked']], ['terms', 'password', 'unknown'])
            self.assertIsNone(result['navigation'])
            guarded = plan(dict(request, captcha=True), root)
            self.assertEqual(guarded['ops'], [])
            self.assertIsNone(guarded['navigation'])

    def test_history_requires_role_and_exact_opening(self):
        history = {'kind': 'history', 'url': 'https://jobs.example.test/my/applications',
                   'job_url': OPENING['job_url'], 'title': OPENING['title'],
                   'quote': 'Submitted', 'refs': ['observed row'], 'observed_at': '2026-11-24T12:00:00-05:00'}
        self.assertEqual(check_evidence({'opening': OPENING, 'after': history})['outcome'], 'confirmed')
        wrong = dict(history, job_url=history['job_url'].replace('1234', '1235'))
        self.assertEqual(check_evidence({'opening': OPENING, 'after': wrong})['outcome'], 'unknown')
        no_opening = dict(history, job_url=None)
        self.assertEqual(check_evidence({'opening': OPENING, 'after': no_opening})['outcome'], 'unknown')

    def test_receipt_requires_final_action_and_new_non_account_evidence(self):
        receipt = {'kind': 'receipt', 'url': 'https://jobs.example.test/jobs/1234/confirmation',
                   'title': OPENING['title'], 'requisition_id': '1234', 'refs': ['observed receipt'],
                   'quote': 'We received your application.', 'observed_at': '2026-11-24T12:00:00-05:00'}
        form = {'kind': 'form', 'url': OPENING['job_url'], 'quote': 'Submit application',
                'observed_at': '2026-11-24T11:59:59-05:00'}
        self.assertEqual(check_evidence(dict(opening=OPENING, after=receipt, before=form))['outcome'], 'unknown')
        observation = dict(opening=OPENING, after=receipt, before=form, action='submit_application')
        self.assertEqual(check_evidence(observation)['outcome'], 'confirmed')
        thank_you = dict(receipt, url='https://jobs.example.test/register/thank-you',
                          quote='Thank you for your interest. Your account has been created.')
        self.assertEqual(check_evidence(dict(observation, after=thank_you))['outcome'], 'unknown')
        posting = dict(receipt, url=OPENING['job_url'], quote='Customer Success Engineer: Apply now.')
        self.assertEqual(check_evidence(dict(observation, after=posting))['outcome'], 'unknown')
        failure = dict(receipt, quote='Your application was not submitted.')
        self.assertEqual(check_evidence(dict(observation, after=failure))['outcome'], 'unknown')

    def test_requisition_extraction_preserves_provider_id_and_rejects_title_suffix(self):
        self.assertEqual(requisition_id(OPENING['job_url']), '1234')
        self.assertEqual(requisition_id('https://tenant.example.test/job/Anywhere/Engineer_R1234'), 'R1234')
        self.assertIsNone(requisition_id('https://tenant.example.test/job/Anywhere/Engineer-2026'))


if __name__ == '__main__':
    unittest.main()
