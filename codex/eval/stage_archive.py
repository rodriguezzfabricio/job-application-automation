"""Validate locally reviewed, redacted historical artifacts before submitting replay inputs to any model."""
import collections
import hashlib
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from stage_pilot import POLICY, screenshot

PORTALS = ('linkedin', 'handshake', 'public', 'account')
RESULTS = {'confirmed', 'blocked', 'disqualified', 'already_applied', 'not_submitted', 'unknown'}
ROUTES = {'classification', 'writing', 'visual'}
PROVENANCE = {'posting', 'decision', 'terminal', 'form', 'facts', 'pdf'}
SAFE_REF = re.compile(r'^[\w./-]+$')
SENSITIVE = re.compile(r'(?:https?://\S+|/Users/|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\b(?:\d{3}-){2}\d{4}\b|'
                       r'\b\d{3}-\d{2}-\d{4}\b|(?i:password|passport number|social security number)\s*[:=]\s*\S+)')


def stable_id(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:20]


def archive_file(root, rel):
    if not SAFE_REF.fullmatch(rel):
        raise ValueError('invalid source reference')
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError('missing local archive evidence')
    if target.name in ('board-cookies.json', 'portal-accounts.json', 'hunter.key') or 'chrome-profile' in target.parts:
        raise ValueError('credential material is outside the replay corpus')
    return target


def portal_bucket(link):
    host = urlparse(link).hostname or ''
    if host == 'linkedin.com' or host.endswith('.linkedin.com'):
        return 'linkedin'
    if host == 'joinhandshake.com' or host.endswith('.joinhandshake.com'):
        return 'handshake'
    if any(token in host for token in ('workday', 'taleo', 'icims', 'dayforce', 'oraclecloud',
                                       'successfactors', 'workforcenow', 'brassring', 'paycom', 'my.ukg')):
        return 'account'
    return 'public'


def archive_inventory(root):
    """Count discoverable evidence, keeping tracker labels separate from verified replay gold."""
    database = root / 'tracker.sqlite'
    if not database.is_file():
        return {}
    counts = {portal: collections.Counter() for portal in PORTALS}
    with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute('SELECT a.state, j.link FROM jr_attempts a JOIN jobs j ON a.job_key=j.job_key'):
            counts[portal_bucket(row['link'])]['runtime_' + row['state']] += 1
        for row in con.execute("SELECT DISTINCT job_key,link FROM applications WHERE confirmed_via <> '' AND job_key <> ''"):
            counts[portal_bucket(row['link'])]['ledger_receipt_candidates'] += 1
        for row in con.execute("SELECT DISTINCT key FROM events WHERE kind='application-handoff'"):
            counts[portal_bucket(row['key'])]['handoff_candidates'] += 1
    return {portal: dict(count) for portal, count in counts.items()}


def manifest_path(root):
    return root / 'codex-runtime' / 'stage-replay' / 'episodes.json'


def verify_source(root, item):
    path = archive_file(root, item['file'])
    if 'event' in item:
        if path.name != 'tracker.sqlite' or type(item['event']) is not int:
            raise ValueError('invalid archived event reference')
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as con:
            row = con.execute('SELECT kind,key,detail FROM events WHERE id=?', (item['event'],)).fetchone()
            if not row or row[0] not in ('codex-hard-gate', 'job-set', 'application-handoff'):
                raise ValueError('archived event cannot substantiate this annotation')
        raw = json.dumps(row, ensure_ascii=False).encode()
    else:
        raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item.get('sha256'):
        raise ValueError('archived source changed after annotation')
    quote = item.get('quote')
    if quote and quote not in raw.decode(errors='replace'):
        raise ValueError('annotation quote is absent from archived source')


def verify_episode(root, record):
    data, gold = record['input'], record['gold']
    if record['portal'] not in PORTALS or gold['outcome'] not in RESULTS:
        raise ValueError('unrecognized episode labels')
    if not record['opening_group'] or not record['employer_group'] or not record.get('reviewer'):
        raise ValueError('opening, employer and independent review are required')
    if not re.fullmatch(r'[a-z0-9-]{3,50}', record['id']):
        raise ValueError('invalid replay ID')
    if SENSITIVE.search(json.dumps(data, ensure_ascii=False)):
        raise ValueError('unredacted model input')
    sources = record['sources']
    minimum = {'posting', 'decision', 'terminal'}
    if gold['outcome'] == 'confirmed':
        minimum |= {'form', 'facts', 'pdf'}
    if not minimum <= sources.keys() or not sources.keys() <= PROVENANCE:
        raise ValueError('episode lacks original posting, decision, form, answer/PDF, or terminal evidence')
    for item in sources.values():
        verify_source(root, item)
    if not isinstance(gold['blocks'], list) or not isinstance(gold['answers'], dict) or not isinstance(gold['claims'], dict):
        raise ValueError('gold output must annotate disqualifiers, required fields and claim evidence')
    if set(gold['answers']) != set(data.get('required_fields', [])) - set(gold.get('unknown_fields', [])):
        raise ValueError('gold must resolve every required field or prove the corresponding genuine block')
    if gold['outcome'] == 'confirmed' and (gold['blocks'] or gold.get('unknown_fields') or not gold.get('pdf') or
                                          not gold.get('receipt') or gold['observed_opening'] != data['opening']):
        raise ValueError('confirmation requires exact opening, verified PDF and no unresolved requirements')
    if gold['outcome'] == 'blocked' and not gold.get('handoff_reason'):
        raise ValueError('blocked episode must identify the legitimate user action')
    if gold['outcome'] == 'disqualified' and not gold['blocks']:
        raise ValueError('disqualified episode has no disqualifier')
    if gold['outcome'] in {'confirmed', 'already_applied'} and not gold.get('receipt'):
        raise ValueError('application proof is missing')
    if gold['outcome'] == 'already_applied' and gold['observed_opening'] != data['opening']:
        raise ValueError('another opening cannot prove a duplicate')
    if gold['outcome'] not in {'disqualified', 'not_submitted', 'unknown'} and gold['observed_opening'] != data['opening']:
        raise ValueError('terminal observation does not belong to the exact opening')
    if gold['pdf'] and gold['pdf'] not in data.get('pdf_options', []):
        raise ValueError('selected PDF is absent from the approved set')
    if gold.get('unknown_fields') and gold['outcome'] != 'blocked':
        raise ValueError('unanswered required fields must be recorded as a legitimate block')
    if not isinstance(gold['submit_count'], int) or gold['submit_count'] != int(gold['outcome'] in ('confirmed', 'unknown')):
        raise ValueError('invalid submit count for outcome')
    if gold.get('action') != ('submit' if gold['outcome'] in ('confirmed', 'unknown') else 'stop'):
        raise ValueError('gold lacks an independently observed application action')
    if gold['outcome'] == 'unknown' and gold.get('receipt'):
        raise ValueError('uncertain outcome cannot carry a verified receipt')
    if not isinstance(data.get('visual'), list) or len(data['visual']) != 7:
        raise ValueError('redacted terminal observation cannot be reconstructed')
    return record


def load_episodes(root, *, historical_only=False):
    suite = manifest_path(root).with_name('suite.json')
    path = suite if suite.is_file() and not historical_only else manifest_path(root)
    if not path.is_file():
        return [], {'manifest': 'missing independently annotated archive', 'verified': {p: 0 for p in PORTALS},
                    'archive_candidates': archive_inventory(root)}
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != 1:
        raise ValueError('unsupported episodes manifest')
    episodes, excluded = [], []
    for record in manifest.get('episodes', []):
        try:
            episodes.append(verify_episode(root, record))
        except (OSError, ValueError, TypeError, KeyError) as error:
            excluded.append({'id': record.get('id'), 'reason': str(error)})
    counts = collections.Counter(e['portal'] for e in episodes)
    source_counts = collections.Counter(e.get('source_kind', 'historical-archive') for e in episodes)
    return episodes, {'manifest': 'verified', 'verified': {p: counts[p] for p in PORTALS}, 'excluded': excluded,
                      'sources': dict(source_counts),
                      'archive_candidates': archive_inventory(root)}


def split_batches(episodes):
    """Build two balanced batches without moving an opening or employer across the split."""
    openings = set()
    employers = {}
    for e in sorted(episodes, key=lambda x: (stable_id(x['employer_group']), stable_id(x['opening_group']))):
        if e['opening_group'] in openings:
            raise ValueError('duplicate opening group in held-out corpus')
        openings.add(e['opening_group'])
        employers.setdefault(e['employer_group'], []).append(e)
    for portal in PORTALS:
        rows = [e for e in episodes if e['portal'] == portal]
        if len(rows) != 10:
            raise ValueError('held-out replay needs exactly ten verified openings per portal')
        confirmed = sum(e['gold']['outcome'] == 'confirmed' for e in rows)
        if not 2 <= confirmed <= 8:
            raise ValueError(f'held-out replay needs successes and legitimate stops for {portal} in each batch')
    groups = list(employers.values())
    features = [tuple(sum(e['portal'] == p and (e['gold']['outcome'] == 'confirmed') == success for e in group)
                      for p in PORTALS for success in (True, False)) for group in groups]
    totals = tuple(map(sum, zip(*features)))

    @lru_cache(None)
    def route(i, left):
        if i == len(groups):
            if all(left[n] + left[n + 1] == 5 for n in range(0, len(left), 2)) and all(left):
                return ()
            return None
        feature = features[i]
        assigned = tuple(a + b for a, b in zip(left, feature))
        right = tuple(a - b for a, b in zip(totals, assigned))
        if all(assigned[n] + assigned[n + 1] <= 5 and min(right[n:n + 2]) >= 1
               for n in range(0, len(left), 2)):
            result = route(i + 1, assigned)
            if result is not None:
                return (0, *result)
        result = route(i + 1, left)
        return None if result is None else (1, *result)

    assignment = route(0, (0,) * len(totals))
    if assignment is None:
        raise ValueError('the independently reviewed corpus cannot balance both batches without employer overlap')
    return [[e for pos, group in enumerate(groups) if assignment[pos] == batch for e in group] for batch in (0, 1)]


def grade_episode(stage, gold, answer):
    answer = answer if isinstance(answer, dict) else {}
    keys = {
        'classification': ('blocks', 'requirements', 'eligible'),
        'writing': ('answers', 'unknown_fields', 'pdf', 'claims'),
        'visual': ('observed_opening', 'outcome', 'receipt', 'submit_count', 'handoff_reason', 'action'),
    }[stage]
    grade = {key: key in answer and type(answer[key]) is type(gold[key]) and answer[key] == gold[key] for key in keys}
    if stage == 'visual':
        grade['duplicate'] = type(answer.get('submit_count')) is int and answer['submit_count'] <= gold['submit_count']
        grade['avoidable_handoff'] = not answer.get('handoff_reason') or bool(gold['handoff_reason'])
    return grade


TASKS = {
    'classification': 'Return eligible (boolean), blocks (sorted exact applicable disqualifier IDs), and requirements '
                      '(sorted list of required skill quotes copied verbatim, empty when disqualified).',
    'writing': 'Answer each required field using only its applicable saved fact, with the exact recorded option. '
               'Return answers (object), unknown_fields (sorted list), pdf (one approved PDF ID or empty string), and claims '
               '(object mapping free-text field ID to its exact source-grounded sentence). Use the role-appropriate PDF or empty '
               'string when there is no form data. Skip fields that genuinely lack evidence.',
    'visual': 'Inspect the screenshots in order. Return observed_opening (ID), outcome (confirmed|blocked|disqualified|'
              'already_applied|not_submitted|unknown), receipt (exact verified receipt ID or empty string), and submit_count '
              '(integer counting the clicks already observed). If a Submit was clicked but evidence refers to another opening, '
              'record unknown with no verified receipt and do not retry. A mismatched opening before any click is not_submitted. '
              'handoff_reason is the exact blocking heading or empty string. Also return action (submit|stop) as the decision '
              'for the original pre-submit state. Use submit only to proceed once on an approved, eligible original opening. '
              'Application unavailable or no longer accepting is disqualified, with no user handoff. No live browser actions.',
}

KEYS = {
    'classification': ('opening', 'posting'),
    'writing': ('opening', 'posting', 'required_fields', 'facts', 'pdf_options', 'questions'),
    'visual': ('opening', 'portal_state_before', 'attempt_status'),
}


def prepare_replay(root, selected_stage, folder):
    episodes, diagnostics = load_episodes(root)
    try:
        batches = split_batches(episodes)
    except ValueError as error:
        raise ValueError(f'{error}. Verified counts {diagnostics["verified"]}.') from error
    prepared = []
    for i, episodes in enumerate(batches, start=1):
        for stage in ('classification', 'writing', 'visual'):
            if selected_stage and stage != selected_stage:
                continue
            images = ({e['id']: screenshot(folder, e['id'], e['input']['visual']) for e in episodes}
                      if stage == 'visual' else {})

            def subset(case_ids, stage=stage, episodes=episodes, images=images):
                selected = [e for e in episodes if e['id'] in case_ids]
                inputs = {e['id']: {key: e['input'][key] for key in KEYS[stage] if key in e['input']} for e in selected}
                attachments = tuple(images[e['id']] for e in selected) if images else ()
                prompt = (POLICY + '\n' + TASKS[stage] + '\nReply with a single object keyed by case ID. Use no tools.\n'
                          + json.dumps(inputs, ensure_ascii=False))

                def grade(response):
                    result = response if isinstance(response, dict) else {}
                    return {e['id']: grade_episode(stage, e['gold'], result.get(e['id'])) for e in selected}

                return prompt, attachments, grade

            prompt, attachments, grade = subset({e['id'] for e in episodes})
            prepared.append((f'batch-{i}:{stage}', stage, prompt, attachments, grade, subset))
    return prepared
