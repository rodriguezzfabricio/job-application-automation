import unittest
from job_runtime.store import Denied
from test_runtime import RuntimeTests


class SubmissionProofTests(RuntimeTests):
    def test_wrong_or_generic_confirmation_never_releases_click(self):
        aid = self.prepare()
        self.submit(aid)
        job = self.s.job(self.key)
        base = dict(proof='portal_confirmation', job_key=self.key, job_url=job['link'],
                    observed_at=self.at.isoformat(), refs=['fixture:visible'], receipt_kind='history',
                    receipt_title=job['title'], receipt_job_url=job['link'], receipt_url=job['link'])
        for fields in [
            {'quotes': ['Customer Success Engineer']},
            {'quotes': ['Thank you for registering. Your candidate account is ready.']},
            {'quotes': ['We have not received your application']},
            {'quotes': ['Application submitted'], 'receipt_job_url': job['link'] + '/other'},
            {'quotes': ['Application submitted'], 'receipt_title': 'Another opening'},
        ]:
            with self.subTest(fields=fields):
                with self.assertRaises(Denied):
                    self.s.reconcile(self.owner, self.token, aid, 'confirmed', dict(base, **fields))
                with self.assertRaises(Denied):
                    self.submit(aid)
                self.assertEqual(self.s.con.execute('SELECT COUNT(*) FROM applications').fetchone()[0], 0)
        self.s.reconcile(self.owner, self.token, aid, 'confirmed', dict(base, quotes=['Application received']))
        self.assertEqual(self.s.audit(self.run['run_id'])['confirmed_submissions'], 1)

    def test_self_labelled_non_submission_does_not_authorize_retry(self):
        aid = self.prepare()
        self.submit(aid)
        with self.assertRaises(Denied):
            self.s.reconcile(self.owner, self.token, aid, 'not_submitted',
                             dict(proof='explicit_portal_non_submission', refs=['fixture:inbox'],
                                  observed_at=self.at.isoformat(), quotes=['No matching mail']))
        with self.assertRaises(Denied):
            self.submit(aid)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SubmissionProofTests(name) for name in SubmissionProofTests.__dict__ if name.startswith('test_'))
