"""Source-envelope taxonomy tests (Q144): only valid envelopes may snapshot."""
import unittest

from source_access import EnvelopeError, collect_ashby
from packet import canonical


def feed(rows):
    return canonical({'jobs': rows})


class EnvelopeTests(unittest.TestCase):
    now = '2026-10-16T10:00:00Z'

    def row(self, **over):
        base = {'title': 'New Grad Software Engineer',
                'applyUrl': 'https://jobs.ashbyhq.com/example/11111111-1111-4111-8111-111111111111/application',
                'location': 'NYC', 'descriptionPlain': 'Requirements\nPython required.',
                'isListed': True, 'employmentType': 'FullTime', 'publishedAt': '2026-10-16T09:40:00Z'}
        base.update(over)
        return base

    def collect(self, payload):
        return collect_ashby('example', lambda url, hosts: payload, self.now)

    def test_valid_envelope_snapshots(self):
        jobs = self.collect(feed([self.row()]))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['id'], 'ashby:example:11111111-1111-4111-8111-111111111111')

    def test_malformed_json_body_is_malformed(self):
        with self.assertRaises(EnvelopeError) as ctx:
            self.collect(b'not json')
        self.assertEqual(ctx.exception.kind, 'malformed')

    def test_missing_jobs_body_is_unsupported(self):
        for payload in (canonical({'jobsx': []}), canonical({'jobs': 'not-a-list'}), b'[1,2,3]'):
            with self.subTest(payload=payload):
                with self.assertRaises(EnvelopeError) as ctx:
                    self.collect(payload)
                self.assertEqual(ctx.exception.kind, 'unsupported')

    def test_invalid_row_is_malformed(self):
        for bad in [{'isListed': 'yes', 'applyUrl': 'x', 'location': 'NYC', 'title': 'x', 'descriptionPlain': 'd'},
                    self.row(isListed='yes'),
                    self.row(location=42)]:
            with self.subTest(bad=bad):
                with self.assertRaises(EnvelopeError) as ctx:
                    self.collect(feed([bad]))
                self.assertEqual(ctx.exception.kind, 'malformed')

    def test_duplicate_identity_is_malformed(self):
        with self.assertRaises(EnvelopeError) as ctx:
            self.collect(feed([self.row(), self.row()]))
        self.assertEqual(ctx.exception.kind, 'malformed')

    def test_transport_failure_is_error_never_empty_success(self):
        def failing(url, hosts):
            raise EnvelopeError('error', 'http 429')
        with self.assertRaises(EnvelopeError) as ctx:
            collect_ashby('example', failing, self.now)
        self.assertEqual(ctx.exception.kind, 'error')

    def test_unknown_kind_refused(self):
        with self.assertRaises(ValueError):
            EnvelopeError('bogus', 'x')


if __name__ == '__main__':
    unittest.main()
