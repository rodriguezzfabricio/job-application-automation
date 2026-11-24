"""End-to-end pipeline tests: qualified alert -> corrected engine -> local capture.

Synthetic profile/source fixtures only; no network, transport, paid calls or
real personal data. Encoding the E9 probe composition pass on the app modules.
"""
import json
import unittest
import tempfile
from pathlib import Path
from email import policy
from email.parser import BytesParser

import test_personal as fixtures
from engine import CAPTURED, HOLD, DeliveryEngine
from packet import canonical, digest
from pipeline import PipelineHold, admit_alert, handoff_alert, load_alert
from alerts import PACKET_VERSION, prepare_alert
from semantic_segments import parse_segment_response


class PipelineTests(unittest.TestCase):
    now = '2026-10-16T10:00:00Z'
    identifier = '11111111-1111-4111-8111-111111111111'

    def setUp(self):
        f = fixtures.PersonalTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.p = f.p
        self.root = Path(f.tmp.name)
        self.p.update(locations=['NYC'], needs_sponsorship=False, work_authorized_us=True,
                      recipient_email='recipient@example.com')
        self.sha = digest(canonical(self.p))
        self.profile = self.root / 'profile.json'
        self.approval = self.root / 'approval.json'
        self.profile.write_bytes(canonical(self.p))
        self.approval.write_bytes(canonical({'authority': 'synthetic fixture', 'profile_sha256': self.sha,
                                             'source_pdf_sha256': self.p['source']['sha256']}))
        self.profile.chmod(0o600)
        self.approval.chmod(0o600)
        self.text = 'This is a commercial software engineering role.\nRequirements\nPython required.'
        self.row = {'title': 'New Grad Software Engineer',
                    'applyUrl': 'https://jobs.ashbyhq.com/example/' + self.identifier + '/application',
                    'location': 'NYC', 'descriptionPlain': self.text, 'isListed': True,
                    'employmentType': 'FullTime', 'publishedAt': '2026-10-16T09:40:00Z'}
        self.source = {'board': 'example', 'careers_url': 'https://employer.example/careers',
                       'official_host': 'employer.example', 'interval_seconds': 1800}
        self.extraction = self.extract(self.text, [['s0', 'none', 'unknown', []],
                                                   ['s1', 'none', 'unknown', []],
                                                   ['s2', 'skill', 'mandatory', ['Python']]])
        self.engine = DeliveryEngine(self.root / 'engine', version=PACKET_VERSION,
                                     recipient='recipient@example.com')

    def extract(self, text, rows):
        return parse_segment_response({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'segments': rows})}}],
                                       'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}, text)

    def get(self, url, hosts):
        if 'api.ashbyhq.com' in url:
            return canonical({'jobs': [self.row]})
        if 'employer.example' in url:
            return ('<html><a href="https://jobs.ashbyhq.com/example/' + self.identifier + '">job</a></html>').encode()
        return b'<html>Apply for this role</html>'

    def prepare(self, now=None):
        return prepare_alert(self.profile, self.approval, self.source, self.identifier,
                             self.extraction, self.root / 'alerts', sender='sender@example.com',
                             now=now or self.now, first_seen=self.now, get=self.get)

    def admitted(self, now=None):
        result = self.prepare(now)
        self.assertEqual(result['status'], 'prepared')
        admission = admit_alert(self.engine, result['directory'], self.profile, self.approval,
                                now=now or self.now, get=self.get)
        self.assertEqual(admission['status'], 'QUALIFIED')
        return result['directory'], admission

    def test_qualified_alert_admits_and_captures_locally(self):
        alert_dir, admission = self.admitted()
        state = handoff_alert(self.engine, alert_dir, self.profile, self.approval,
                              now='2026-10-16T10:00:01Z', get=self.get, packet_id=admission['id'])
        self.assertEqual(state['state'], CAPTURED)
        capture = next((self.root / 'engine' / 'captures').glob('*.eml'))
        msg = BytesParser(policy=policy.default).parsebytes(capture.read_bytes())
        self.assertEqual(str(msg['To']), 'recipient@example.com')
        attachments = list(msg.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_payload(decode=True), (Path(alert_dir) / 'resume.pdf').read_bytes())
        # E9-S-002: byte-identical deterministic regeneration.
        self.assertEqual(capture.read_bytes(), (Path(alert_dir) / 'message.eml').read_bytes())
        self.assertEqual(self.engine.status().get(CAPTURED, 0), 1)

    def test_same_job_cannot_be_admitted_twice_within_deadline(self):
        alert_dir, admission = self.admitted()
        with self.assertRaises(PipelineHold) as ctx:
            admit_alert(self.engine, alert_dir, self.profile, self.approval, now=self.now, get=self.get)
        self.assertIn('still within its deadline', str(ctx.exception))
        self.assertEqual(len(list((self.root / 'engine' / 'captures').iterdir())), 0)

    def test_source_change_fails_readiness_before_claim(self):
        alert_dir, admission = self.admitted()

        def changed(url, hosts):
            if 'api.ashbyhq.com' in url:
                return canonical({'jobs': [dict(self.row, descriptionPlain=self.text + '\nRust required.')]})
            return self.get(url, hosts)

        with self.assertRaises(PipelineHold):
            handoff_alert(self.engine, alert_dir, self.profile, self.approval,
                          now='2026-10-16T10:00:01Z', get=changed, packet_id=admission['id'])
        self.assertEqual(len(list((self.root / 'engine' / 'captures').iterdir())), 0)
        with self.engine.conn() as c:
            self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0], 'READY')

    def test_profile_change_fails_readiness_before_claim(self):
        alert_dir, admission = self.admitted()
        self.p['email'] = 'other@example.com'
        self.profile.write_bytes(canonical(self.p))
        self.approval.write_bytes(canonical({'authority': 'synthetic fixture',
                                             'profile_sha256': digest(canonical(self.p)),
                                             'source_pdf_sha256': self.p['source']['sha256']}))
        with self.assertRaises(PipelineHold):
            handoff_alert(self.engine, alert_dir, self.profile, self.approval,
                          now='2026-10-16T10:00:01Z', get=self.get, packet_id=admission['id'])
        self.assertEqual(len(list((self.root / 'engine' / 'captures').iterdir())), 0)

    def test_expired_deadline_holds_without_capture(self):
        alert_dir, admission = self.admitted()
        state = handoff_alert(self.engine, alert_dir, self.profile, self.approval,
                              now='2026-10-16T10:10:01Z', get=self.get, packet_id=admission['id'])
        self.assertEqual(state['state'], HOLD)
        self.assertEqual(len(list((self.root / 'engine' / 'captures').iterdir())), 0)

    def test_expired_never_attempted_packet_regenerates_fresh_generation(self):
        alert_dir, first = self.admitted()
        later = '2026-10-17T10:01:00Z'  # next UTC day: fresh qualification, new generation
        refreshed = self.prepare(later)
        self.assertEqual(refreshed['status'], 'prepared')
        self.assertNotEqual(refreshed['directory'], alert_dir)
        second = admit_alert(self.engine, refreshed['directory'], self.profile, self.approval,
                             now=later, get=self.get)
        self.assertEqual(second['status'], 'QUALIFIED')
        self.assertEqual(second['generation'], 2)
        with self.engine.conn() as c:
            prior = c.execute('SELECT prior_id FROM decisions WHERE id=?', (second['id'],)).fetchone()[0]
            self.assertEqual(prior, first['id'])
        state = handoff_alert(self.engine, refreshed['directory'], self.profile, self.approval,
                              now='2026-10-17T10:01:01Z', get=self.get, packet_id=second['id'])
        self.assertEqual(state['state'], CAPTURED)

    def test_tampered_artifact_refused_at_load(self):
        alert_dir, _ = self.admitted()
        (Path(alert_dir) / 'resume.pdf').write_bytes(b'tampered')
        with self.assertRaises(PipelineHold):
            load_alert(alert_dir)

    def test_revocation_blocks_handoff_even_after_prepass(self):
        alert_dir, admission = self.admitted()
        self.engine.revoke_approval()
        state = handoff_alert(self.engine, alert_dir, self.profile, self.approval,
                              now='2026-10-16T10:00:01Z', get=self.get, packet_id=admission['id'])
        self.assertEqual(state['state'], HOLD)
        self.assertEqual(len(list((self.root / 'engine' / 'captures').iterdir())), 0)


if __name__ == '__main__':
    unittest.main()
