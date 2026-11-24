"""Qualified-alert admission and guarded handoff wiring (pipeline glue).

Connects the source-verified qualified alert package (alerts.py) to the corrected
single-file DeliveryEngine (engine.py). Admission re-verifies the current source
opening and approved profile before creating the immutable decision; handoff runs
the full network readiness pre-pass before claiming, then the engine re-validates
every pinned root after the lock with a fresh clock (E9-S-001..009). The default
transport is the local filesystem capture; real sending stays operator-gated.
"""
import json
import re
from pathlib import Path

from alerts import PACKET_VERSION, build_alert_message, current_opening, validate_alert
from engine import CAPTURED, HOLD, Hold
from packet import canonical, digest, job_fingerprint
from policy import instant, merge_key
from personal import render_resume, select_variants
from profile_store import load_profile
from qualification import qualify, VERSION

SHA = re.compile(r'[0-9a-f]{64}')


class PipelineHold(Exception):
    """Pipeline-level refusal with a typed reason; never converted to success."""


def load_alert(alert_dir):
    directory = Path(alert_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise PipelineHold('alert directory missing or symlink')
    manifest_path = directory / 'manifest.json'
    pdf_path = directory / 'resume.pdf'
    mime_path = directory / 'message.eml'
    if any(p.is_symlink() or not p.is_file() or p.stat().st_size > 4 * 1024 * 1024 for p in (manifest_path, pdf_path, mime_path)):
        raise PipelineHold('alert artifact bounds')
    manifest = json.loads(manifest_path.read_text())
    pdf = pdf_path.read_bytes()
    mime = mime_path.read_bytes()
    if manifest.get('packet_version') != PACKET_VERSION or manifest.get('application_ready') is not True:
        raise PipelineHold('not a qualified alert package')
    if not isinstance(manifest.get('qualified_at_epoch'), int):
        raise PipelineHold('qualified_at epoch missing')
    if digest(pdf) != manifest.get('pdf_sha256') or digest(mime) != manifest.get('email_sha256'):
        raise PipelineHold('alert artifact hash mismatch')
    if not isinstance(manifest.get('job'), dict) or not isinstance(manifest.get('source_registration'), dict):
        raise PipelineHold('alert manifest binding missing')
    return manifest, pdf, mime


def _guards(manifest, approval):
    """Local guard closures: pure recomputation over pinned bytes, no network."""
    job = manifest['job']
    first_seen = manifest['first_seen_at']
    prepared = manifest['prepared_at']
    key = digest(canonical({'job': manifest['job_hash'], 'profile': manifest['profile_hash'],
                            'sender': manifest['sender'], 'packet_version': PACKET_VERSION,
                            'qualification_policy': VERSION,
                            'prepared_day_utc': instant(prepared).date().isoformat()}))[:32]

    def eligibility(raw, profile):
        # Profile authority is the externally bound approval sha (validated by
        # load_profile and by the engine's pinned profile_sha), not a profile field.
        chosen = select_variants(profile, raw, 8, approval_sha256=approval)
        if not chosen or len(set(chosen)) != len(chosen):
            raise Hold('selection invalid at guard')
        return chosen

    def compose(raw, profile, chosen, created):
        if created != manifest['qualified_at_epoch']:
            raise Hold('packet generation epoch changed')
        pdf = render_resume(profile, raw, chosen, approval_sha256=approval,
                            job_binding=job_fingerprint(raw))
        mime = build_alert_message(raw, manifest['decision'], first_seen, prepared, key, pdf, profile, manifest['sender'])
        return pdf, mime

    return eligibility, compose


def admit_alert(engine, alert_dir, profile_path, approval_path, *, now, get=None, source=None):
    """Re-verify the current opening and approved profile, then admit immutably.

    created is the manifest's qualified_at epoch: the 600s deadline runs from the
    qualification moment and is never renewed here."""
    manifest, _, _ = load_alert(alert_dir)
    profile, approval = load_profile(profile_path, approval_path)
    if approval != manifest['profile_hash']:
        raise PipelineHold('current profile approval differs from alert binding')
    source = source if source is not None else manifest['source_registration']
    if source != manifest['source_registration']:
        raise PipelineHold('source registration differs from alert binding')
    identifier = manifest['job']['id'].rsplit(':', 1)[1]
    job, verification = current_opening(source, identifier, get, now)
    if job_fingerprint(job) != manifest['job_hash'] or merge_key(job) != manifest['canonical_job']:
        raise PipelineHold('current opening differs from qualified alert')
    decision = qualify(profile, job, manifest['extraction'], now, approval_sha256=approval)
    if decision != manifest['decision'] or decision['status'] != 'eligible':
        raise PipelineHold('re-qualification no longer eligible')
    engine.observe(manifest['canonical_job'], job, int(instant(now).timestamp()), eligible=True)
    ref = {'source_registration': source}
    eligibility, compose = _guards(manifest, approval)
    result = engine.admit(job_id=manifest['canonical_job'], raw=job, profile=profile, ref=ref,
                          eligibility=eligibility, compose=compose, created=manifest['qualified_at_epoch'])
    if result['status'] != 'QUALIFIED':
        raise PipelineHold('admission ' + result['status'] + ': ' + result.get('reason', ''))
    return result


def handoff_alert(engine, alert_dir, profile_path, approval_path, *, now, get=None,
                  source=None, clock=None, transport=None, setup=None, packet_id=None):
    """Readiness pre-pass (fresh network/qualification), then engine handoff.

    transport=None captures locally; the engine re-runs every authority and
    artifact guard after claiming, with the clock read after the lock."""
    manifest, _, _ = load_alert(alert_dir)
    if not validate_alert(alert_dir, profile_path, approval_path, source if source is not None else manifest['source_registration'], now, get=get):
        raise PipelineHold('alert readiness pre-pass failed')
    profile, approval = load_profile(profile_path, approval_path)
    eligibility, compose = _guards(manifest, approval)
    source = source if source is not None else manifest['source_registration']
    job = manifest['job']
    id_value = packet_id
    if id_value is None:
        with engine.conn() as c:
            row = c.execute("SELECT d.id FROM decisions d WHERE d.job=? ORDER BY d.generation DESC LIMIT 1",
                            (manifest['canonical_job'],)).fetchone()
            if row is None:
                raise PipelineHold('no admitted decision for this alert')
            id_value = row['id']
    state = engine.handoff(id_value, now=int(instant(now).timestamp()), clock=clock, transport=transport, setup=setup,
                           raw_loader=lambda job_id: job,
                           profile_loader=lambda: load_profile(profile_path, approval_path)[0],
                           ref_loader=lambda: {'source_registration': source},
                           eligibility=eligibility, compose=compose)
    if state in (CAPTURED, 'API_ACCEPTED', 'RETRY_WAIT', 'REJECTED', 'UNKNOWN', 'HOLD'):
        return {'state': state, 'id': id_value}
    raise PipelineHold('handoff ' + state)
