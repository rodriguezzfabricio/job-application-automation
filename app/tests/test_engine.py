"""Delivery engine contract tests — Q038/Q141/Q155/Q162 + E9-S-001..009 invariants.

These tests encode the corrected delivery contracts in executable form. They use
explicitly synthetic jobs/profiles and injected compose/eligibility guards; no
network, transport, paid call, or real personal data is involved.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine import (API_ACCEPTED, ATTEMPTING, CAPTURED, HOLD, READY, REJECTED,
                    RETRY_WAIT, UNKNOWN, DeliveryEngine, canonical, digest)

VERSION = 'engine-tests-1'
RECIPIENT = 'demo@example.invalid'
T = 1792476000


def synthetic_profile(tag='p1', approved=True):
    return {'approved': approved, 'synthetic': True, 'tag': tag,
            'name': 'Synthetic Candidate', 'email': 'sample@example.invalid',
            'variants': [{'id': 'f1', 'text': 'Built a tested API.', 'facts': ['f1'], 'group': 'g1', 'skills': ['Python']},
                         {'id': 'f2', 'text': 'Shipped a React interface.', 'facts': ['f2'], 'group': 'g2', 'skills': ['React']}],
            'facts': {'f1': {'claim_text': 'Built a tested API.', 'group': 'g1'},
                      'f2': {'claim_text': 'Shipped a React interface.', 'group': 'g2'}},
            'groups': {'g1': 'PROJECTS', 'g2': 'PROJECTS'}, 'skills': ['Python', 'React']}


def synthetic_raw(job_id='job-1', listed=True, extra=''):
    return {'id': job_id, 'title': 'New Grad Software Engineer', 'isListed': listed,
            'descriptionPlain': 'Ship backend features with tests.' + extra,
            'applyUrl': 'https://jobs.example.test/' + job_id, 'location': 'Remote, US'}


def synthetic_ref():
    return {'source_policy_observation': {}, 'source_location_evidence': {'addressCountry': 'United States'}}


def compose_bytes(raw, profile, chosen, created):
    """Deterministic artifact builder with a STABLE MIME boundary (E9-S-002)."""
    identity = digest(canonical([raw, profile['tag'], chosen, created, VERSION]))
    boundary = 'jobassistant-' + identity[:32]
    pdf = b'%PDF-synthetic ' + canonical({'chosen': chosen, 'created': created})
    mime = (b'Content-Type: multipart/mixed; boundary="' + boundary.encode() + b'"\r\n\r\n'
            b'--' + boundary.encode() + b'\r\n' + pdf + b'\r\n--' + boundary.encode() + b'--\r\n')
    return pdf, mime


def pick_all(raw, profile):
    return ['f1', 'f2']


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_engine(self):
        return DeliveryEngine(self.root, version=VERSION, recipient=RECIPIENT)

    def observe_job(self, engine, job_id='job-1', raw=None):
        engine.observe(job_id, raw or synthetic_raw(job_id), T, eligible=True)
        return raw or synthetic_raw(job_id)

    def admit(self, engine, raw=None, profile=None, ref=None, created=T, fail_point=None):
        raw = raw or synthetic_raw()
        profile = profile or synthetic_profile()
        ref = ref or synthetic_ref()
        return engine.admit(job_id=raw['id'], raw=raw, profile=profile, ref=ref,
                            eligibility=pick_all, compose=compose_bytes, created=created,
                            fail_point=fail_point)

    def handoff_kwargs(self, engine, raw, profile, ref, **kw):
        base = dict(raw_loader=lambda job: raw,
                    profile_loader=lambda: profile, ref_loader=lambda: ref,
                    eligibility=pick_all, compose=compose_bytes)
        base.update(kw)
        return base

    def count(self, engine, table):
        with engine.conn() as c:
            return c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    # --- admission --------------------------------------------------------
    def test_qualified_admission_and_local_capture(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        self.assertEqual(result['status'], 'QUALIFIED')
        self.assertEqual(result['selected'], ['f1', 'f2'])
        state = engine.handoff(result['id'], now=T + 1, **self.handoff_kwargs(engine, raw, profile, ref))
        self.assertEqual(state, CAPTURED)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 1)
        self.assertEqual(self.count(engine, 'outbox'), 1)
        self.assertEqual(self.count(engine, 'reservations'), 1)

    def test_same_job_cannot_be_admitted_twice(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        first = self.admit(engine, raw)
        self.assertEqual(first['status'], 'QUALIFIED')
        second = self.admit(engine, raw, created=T + 1)
        self.assertEqual(second['status'], 'REVIEW')
        self.assertIn('still within its deadline', second['reason'])
        self.assertEqual(self.count(engine, 'outbox'), 1)

    def test_failed_insert_at_any_table_leaves_no_queue_state(self):
        for point in ('decision', 'packet', 'outbox'):
            with self.subTest(point=point):
                self.root = Path(self.tmp.name) / point
                engine = self.make_engine()
                raw = self.observe_job(engine)
                result = self.admit(engine, raw, fail_point=point)
                self.assertEqual(result['status'], 'HOLD')
                for table in ('decisions', 'packets', 'outbox'):
                    self.assertEqual(self.count(engine, table), 0)
                self.assertEqual(len(list((self.root / 'captures').iterdir())), 0)

    def test_unobserved_or_changed_source_blocks_admission(self):
        engine = self.make_engine()
        result = self.admit(engine)  # never observed
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(self.count(engine, 'outbox'), 0)
        raw = self.observe_job(engine)
        mutated = dict(raw, descriptionPlain=raw['descriptionPlain'] + ' clearance required')
        result = self.admit(engine, mutated)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(self.count(engine, 'outbox'), 0)

    def test_revoked_authority_prevents_new_admission(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        engine.revoke_approval()
        result = self.admit(engine, raw)
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(self.count(engine, 'outbox'), 0)

    # --- deadline and clock ----------------------------------------------
    def test_after_setup_expiry_blocks_and_never_attempts(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        state = engine.handoff(result['id'], now=T,
                               setup=lambda: T + 600,
                               **self.handoff_kwargs(engine, raw, profile, ref))
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)
        # Never-attempted expiry leaves the immutable row intact; the sweeper
        # terminal-holds it with a typed event (no silent drop, no extension).
        self.assertEqual(engine.status().get(READY, 0), 1)
        self.assertEqual(engine.expire_stale(T + 600), 1)
        self.assertEqual(engine.status().get(HOLD, 0), 1)
        self.assertEqual(engine.handoff(result['id'], now=T + 601,
                                        **self.handoff_kwargs(engine, raw, profile, ref)), HOLD)

    def test_late_integer_clock_inside_deadline_captures(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        state = engine.handoff(result['id'], now=T + 599,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, CAPTURED)

    def test_clock_advancing_after_reservation_blocks(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        ticks = iter([T + 1, T + 600])
        state = engine.handoff(result['id'], now=T + 1, clock=lambda: next(ticks),
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)

    def test_replace_cannot_extend_original_expiry(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        with engine.conn() as c:
            row = dict(c.execute('SELECT * FROM decisions').fetchone())
            row['expires'] = T + 1200
            values = tuple(row[k] for k in ('id', 'job', 'generation', 'prior_id', 'source_sha',
                                            'profile_sha', 'ref_sha', 'policy', 'created',
                                            'expires', 'selected'))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)', values)
        state = engine.handoff(result['id'], now=T + 601,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)

    # --- revocation and changed roots -------------------------------------
    def test_profile_change_during_setup_blocks(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        changed = dict(profile, approved=False)
        state = engine.handoff(result['id'], now=T,
                               setup=lambda: T + 1, profile_loader=lambda: changed,
                               raw_loader=lambda job: raw, ref_loader=lambda: ref,
                               eligibility=pick_all, compose=compose_bytes)
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)

    def test_revocation_between_reservation_and_capture_blocks(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        holder = {'revoked': False}

        def revoke():
            holder['revoked'] = True

        def loader():
            return synthetic_profile(approved=False) if holder['revoked'] else profile

        state = engine.handoff(result['id'], now=T + 1, before_send=revoke,
                               raw_loader=lambda job: raw, profile_loader=loader,
                               ref_loader=lambda: ref, eligibility=pick_all, compose=compose_bytes)
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)

    def test_authoritative_revocation_survives_restart(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        engine.revoke_approval()
        reopened = DeliveryEngine(self.root, version=VERSION, recipient=RECIPIENT)
        state = reopened.handoff(result['id'], now=T + 1,
                                 **self.handoff_kwargs(reopened, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, HOLD)

    def test_artifact_change_between_guards_blocks(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)

        def corrupt():
            key = engine.fetch_object
            with engine.conn() as c:
                pdf_sha = c.execute('SELECT pdf_sha FROM packets').fetchone()[0]
            (self.root / 'objects' / pdf_sha).write_bytes(b'unapproved PDF bytes')

        state = engine.handoff(result['id'], now=T + 1, before_send=corrupt,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, HOLD)
        self.assertEqual(len(list((self.root / 'captures').glob('*.eml'))), 0)

    # --- transport outcome classification --------------------------------
    def test_retryable_429_then_accepted_within_same_deadline(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        attempts = [{'state': 'rejected', 'http_status': 429, 'retry_after': '1'},
                    {'state': 'api_accepted', 'provider_message_id': 'msg-1'}]
        transport = type('T', (), {'send': lambda self, mime: attempts.pop(0)})()
        state = engine.handoff(result['id'], now=T + 1, transport=transport,
                               **self.handoff_kwargs(engine, raw, profile, ref))
        self.assertEqual(state, RETRY_WAIT)
        self.assertEqual(engine.status().get(RETRY_WAIT, 0), 1)
        # Immediate retry is refused by backoff; same deadline still applies.
        attempts.insert(0, {'state': 'api_accepted', 'provider_message_id': 'msg-1'})
        self.assertEqual(engine.handoff(result['id'], now=T + 2, transport=transport,
                                        **self.handoff_kwargs(engine, raw, profile, ref)), HOLD)
        attempts.insert(0, {'state': 'api_accepted', 'provider_message_id': 'msg-1'})
        state = engine.handoff(result['id'], now=T + 61, transport=transport,
                               **self.handoff_kwargs(engine, raw, profile, ref))
        self.assertEqual(state, API_ACCEPTED)
        # Deadline never extended: a late attempt still refuses.
        self.assertEqual(engine.handoff(result['id'], now=T + 599, transport=transport,
                                        **self.handoff_kwargs(engine, raw, profile, ref)), HOLD)

    def test_permanent_rejection_is_terminal(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        transport = type('T', (), {'send': lambda self, mime: {'state': 'rejected', 'http_status': 403}})()
        state = engine.handoff(result['id'], now=T + 1, transport=transport,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, REJECTED)
        self.assertEqual(engine.handoff(result['id'], now=T + 2, transport=transport,
                                        **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref())), HOLD)

    def test_unknown_outcome_is_terminal_for_auto_retry(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        transport = type('T', (), {'send': lambda self, mime: {'state': 'unknown'}})()
        state = engine.handoff(result['id'], now=T + 1, transport=transport,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, UNKNOWN)
        reopened = DeliveryEngine(self.root, version=VERSION, recipient=RECIPIENT)
        self.assertEqual(reopened.handoff(result['id'], now=T + 2,
                                          **self.handoff_kwargs(reopened, raw, synthetic_profile(), synthetic_ref())), HOLD)

    def test_crash_after_claim_recovered_as_unknown_terminal(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        with engine.conn() as c:
            c.execute("UPDATE outbox SET state=? WHERE id=?", (ATTEMPTING, result['id']))
        reopened = DeliveryEngine(self.root, version=VERSION, recipient=RECIPIENT)
        self.assertEqual(reopened.status().get(UNKNOWN, 0), 1)
        self.assertEqual(reopened.handoff(result['id'], now=T + 2,
                                          **self.handoff_kwargs(reopened, raw, synthetic_profile(), synthetic_ref())), HOLD)

    # --- caps -------------------------------------------------------------
    def test_daily_cap_holds_fourth_packet(self):
        engine = self.make_engine()
        profile, ref = synthetic_profile(), synthetic_ref()
        admitted, raw_by_job = [], {}
        for index in range(4):
            job_id = 'job-%d' % index
            raw = self.observe_job(engine, job_id)
            raw_by_job[job_id] = raw
            result = self.admit(engine, raw, profile, ref, created=T + index)
            self.assertEqual(result['status'], 'QUALIFIED')
            admitted.append(result)
        states = []
        for result in admitted:
            raw = raw_by_job[result['selected'] and 'job-%d' % admitted.index(result)]
            job_id = raw['id']
            states.append(engine.handoff(result['id'], now=T + 10,
                                         raw_loader=lambda job, r=raw: r, profile_loader=lambda: profile,
                                         ref_loader=lambda: ref, eligibility=pick_all, compose=compose_bytes))
        self.assertEqual(states.count(CAPTURED), 3)
        self.assertEqual(states.count(HOLD), 1)
        self.assertEqual(self.count(engine, 'reservations'), 3)

    # --- Q141 never-attempted generations ---------------------------------
    def test_expired_never_attempted_regenerates_linked_generation(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        first = self.admit(engine, raw)
        self.assertEqual(first['generation'], 1)
        # Deadline expired, never attempted -> a new fully qualified generation
        # is explicitly linked, with its own fresh 600s deadline.
        second = self.admit(engine, raw, created=T + 601)
        self.assertEqual(second['status'], 'QUALIFIED')
        self.assertEqual(second['generation'], 2)
        with engine.conn() as c:
            prior = c.execute('SELECT prior_id FROM decisions WHERE id=?', (second['id'],)).fetchone()[0]
            self.assertEqual(prior, first['id'])
            gen1 = c.execute("SELECT o.state FROM outbox o JOIN decisions d ON d.id=o.id WHERE d.generation=1").fetchone()[0]
            self.assertEqual(gen1, READY)
        state = engine.handoff(second['id'], now=T + 602,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, CAPTURED)

    def test_attempted_history_blocks_regeneration(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        first = self.admit(engine, raw)
        state = engine.handoff(first['id'], now=T + 1,
                               **self.handoff_kwargs(engine, raw, synthetic_profile(), synthetic_ref()))
        self.assertEqual(state, CAPTURED)
        later = self.admit(engine, raw, created=T + 900)
        self.assertEqual(later['status'], 'REVIEW')
        self.assertIn('no regeneration', later['reason'])
        self.assertEqual(self.count(engine, 'outbox'), 1)

    def test_prior_generation_still_within_deadline_blocks_regeneration(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        first = self.admit(engine, raw)
        self.assertEqual(first['status'], 'QUALIFIED')
        second = self.admit(engine, raw, created=T + 60)
        self.assertEqual(second['status'], 'REVIEW')
        self.assertIn('still within its deadline', second['reason'])

    # --- queue management -------------------------------------------------
    def test_candidates_respect_deadline_backoff_and_ttl(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        result = self.admit(engine, raw)
        self.assertIn(result['id'], engine.candidates(T + 1))
        self.assertEqual(engine.candidates(T + 601), [])
        changed = engine.expire_stale(T + 601)
        self.assertEqual(changed, 1)
        self.assertEqual(engine.status().get(HOLD, 0), 1)

    def test_source_row_replacement_refused(self):
        engine = self.make_engine()
        self.observe_job(engine)
        with self.assertRaises(sqlite3.IntegrityError):
            engine.observe('job-1', synthetic_raw('job-1', extra=' changed'), T + 1, eligible=True)

    def test_deterministic_compose_regenerates_identical_artifacts(self):
        engine = self.make_engine()
        raw = self.observe_job(engine)
        profile, ref = synthetic_profile(), synthetic_ref()
        result = self.admit(engine, raw, profile, ref)
        with engine.conn() as c:
            row = c.execute('SELECT * FROM packets WHERE id=?', (result['id'],)).fetchone()
        pdf, mime = compose_bytes(raw, profile, ['f1', 'f2'], T)
        self.assertEqual(engine.fetch_object(row['pdf_sha']), pdf)
        self.assertEqual(engine.fetch_object(row['mime_sha']), mime)
        # E9-S-002: the MIME boundary must be deterministic for identical content.
        mime2 = compose_bytes(raw, profile, ['f1', 'f2'], T)[1]
        self.assertEqual(mime, mime2)


if __name__ == '__main__':
    unittest.main()
