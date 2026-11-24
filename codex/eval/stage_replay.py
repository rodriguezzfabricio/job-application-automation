"""Guarded stage calibration and private, read-only job-application replay."""
import argparse
import datetime as dt
import hashlib
import fcntl
import json
import os
import sys
import subprocess
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from job_runtime.model_routes import (BASELINE, CANDIDATES, FALLBACK, evidence_path, input_digest, qualified,
                                      request_subscription, require_subscription)
from job_runtime.pace import SESSIONS, session_file
import stage_pilot
from stage_archive import PORTALS, SENSITIVE, grade_episode, load_episodes, manifest_path, split_batches

DEFAULT_ROOT = Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or Path(__file__).resolve().parents[2] / 'stopgap-private')


def state_path(root):
    return root / 'codex-runtime' / 'stage-replay' / 'results.json'


def load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def persist(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', prefix='.replay-', dir=path.parent, delete=False) as f:
        filename = f.name
        json.dump(value, f, indent=2, sort_keys=True)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(filename, path)



def should_call(attempts, retry):
    return not attempts or (retry and not attempts[-1]['complete'] and attempts[-1].get('state') != 'started')


def recover_interrupted(state, quota, path):
    """Under the exclusive run lock, settle a lost CLI before permitting an explicit retry."""
    changed = False
    for phase in ('pilot', 'replay'):
        for entry in state.get(phase, {}).values():
            for attempts in entry['arms'].values():
                if not attempts or attempts[-1].get('state') != 'started':
                    continue
                incomplete = attempts[-1]
                reservation = incomplete['reservation']
                settlement = quota.finish(reservation)
                state.setdefault('settlements', {})[reservation] = settlement
                incomplete['state'] = 'interrupted_unverified'
                incomplete['complete'] = False
                changed = True
    if changed:
        persist(path, state)


def fallback_call(key, entry, subset, retry):
    if subset is None:
        return None
    candidates = entry['arms'].get('candidate') or []
    if not candidates or not candidates[-1].get('complete'):
        return None
    grade = candidates[-1]['grade']
    missed = sorted(case for case in grade if not correct(grade, case))
    if not missed:
        return None
    attempts = entry['arms'].setdefault('fallback', [])
    if not should_call(attempts, retry):
        return None
    prompt, images, grade_subset = subset(set(missed))
    metadata = {'cases': missed, 'input_sha256': input_digest(prompt, images)}
    if entry.setdefault('fallback_input', metadata) != metadata:
        raise ValueError(f'Fallback inputs changed for {key}; preserve the original result')
    return key, 'fallback', FALLBACK, prompt, images, grade_subset, attempts


def run_arms(private_root, prepared, *, phase, retry, max_calls, budget):
    from job_runtime.quota import Quota, QuotaUnavailable
    path = state_path(private_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_name('run.lock')
    lock.touch(mode=0o600, exist_ok=True)
    with lock.open() as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load(path, {'schema': 1, 'pilot': {}, 'replay': {}})
        quota = Quota(private_root)
        recover_interrupted(state, quota, path)
        calls = []
        entries = {}
        for key, stage, prompt, images, grade, subset in prepared:
            digest = input_digest(prompt, images)
            entry = state[phase].setdefault(key, {'stage': stage, 'input_sha256': digest, 'arms': {}})
            if entry['input_sha256'] != digest:
                raise ValueError(f'Inputs changed for {key}; preserve the original results in a separate run')
            entries[key] = (entry, subset)
            arms = [('baseline', BASELINE), ('candidate', CANDIDATES[stage])]
            if phase == 'replay' and key.startswith('batch-2:'):
                arms.reverse()
            for arm, route in arms:
                attempts = entry['arms'].setdefault(arm, [])
                if should_call(attempts, retry):
                    calls.append((key, arm, route, prompt, images, grade, attempts))
            fallback = fallback_call(key, entry, subset, retry)
            if fallback:
                calls.append(fallback)
        if not calls:
            return summarize(state)
        codex = require_subscription()
        reservation = quota.begin('benchmark', budget=budget)
        status = None
        try:
            for index in range(max_calls):
                if index >= len(calls):
                    break
                key, arm, route, prompt, images, grade, attempts = calls[index]
                opened_at = time.time()
                before = quota.check(reservation)
                attempt = {'reservation': reservation, 'state': 'started', 'complete': False, 'model': route.model,
                           'effort': route.effort, 'meter_before': before, 'started_at': opened_at}
                attempts.append(attempt)
                persist(path, state)
                response = request_subscription(codex, route, prompt, images)
                raw = response.pop('response')
                account = response.pop('account_id', None)
                attempt['account_sha256'] = hashlib.sha256(account.encode()).hexdigest() if account else None
                attempt.update(response)
                attempt['grade'] = grade(raw)
                if phase == 'replay':
                    attempt['proposal'] = bounded_proposal(raw, attempt['grade'])
                attempt['complete'] = (isinstance(raw, dict) and response['returncode'] == 0
                                       and not response['tools_used'] and response.get('served_model') == route.model
                                       and response.get('served_effort') == route.effort)
                attempt['state'] = 'returned' if attempt['complete'] else 'failed_or_unverified'
                try:
                    attempt['meter_after'] = quota.check(reservation, account_id=account, authorize_next=False)
                    attempt['ended_at'] = time.time()
                    attempt['overlap'] = overlapping_usage(account, response['session_id'], opened_at, attempt['ended_at'])
                except QuotaUnavailable:
                    attempt['meter_after'] = None
                    attempt['complete'] = False
                    attempt['state'] = 'failed_or_unverified'
                    persist(path, state)
                    raise
                persist(path, state)
                print(json.dumps({'phase': phase, 'case': key, 'arm': arm, 'complete': attempt['complete'],
                                  'grade': attempt['grade'], 'usage': attempt['usage']}), flush=True)
                if arm == 'candidate' and attempt['complete']:
                    entry, subset = entries[key]
                    fallback = fallback_call(key, entry, subset, retry)
                    if fallback:
                        calls.insert(index + 1, fallback)
        except QuotaUnavailable as error:
            status = str(error)
        finally:
            settlement = quota.finish(reservation)
            state.setdefault('settlements', {})[reservation] = settlement
            persist(path, state)
        return {**summarize(state), 'stop': status, 'settlement': settlement}



def log_events(path):
    with Path(path).open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def overlapping_usage(account, session_id, before, after):
    if not account or not session_id:
        return None
    try:
        root = Path(SESSIONS)
        if not root.is_dir():
            return None
        for path in root.glob('*/*/*/rollout-*.jsonl'):
            if session_id in path.name or path.stat().st_mtime < before:
                continue
            events = log_events(path)
            first = next(events, {})
            if (first.get('payload') or {}).get('creator_account_id') != account:
                continue
            for event in events:
                if event.get('type') != 'token_usage_record':
                    continue
                stamp = dt.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                if before <= stamp <= after:
                    return True
        return False
    except (OSError, ValueError, TypeError, KeyError):
        return None


def regrade_pilot(private_root):
    path = state_path(private_root)
    lock = path.with_name('run.lock')
    with lock.open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load(path, {})
        entry = state.get('pilot', {}).get('writing') or {}
        with tempfile.TemporaryDirectory(prefix='jr-regrade-') as folder:
            prompt, images = stage_pilot.prepare('writing', Path(folder))
            if input_digest(prompt, images) != entry.get('input_sha256'):
                raise ValueError('Recorded writing inputs do not match the corrected grading case')
        for attempts in entry['arms'].values():
            for attempt in attempts:
                if not attempt.get('complete') or attempt.get('grade_revision') == 'writing-2':
                    continue
                path_to_log = session_file(attempt.get('session_id'))
                if not path_to_log:
                    raise ValueError('Writing session unavailable for offline grading')
                messages = []
                for event in log_events(path_to_log):
                    payload = event.get('payload') or {}
                    if event.get('type') == 'response_item' and payload.get('role') == 'assistant':
                        messages.extend(content.get('text') for content in payload.get('content', [])
                                        if isinstance(content, dict) and content.get('text'))
                answer = json.loads(messages[-1])
                attempt['grade_history'] = [*attempt.get('grade_history', []), attempt.get('grade')]
                attempt['grade'] = stage_pilot.grade('writing', answer)
                attempt['grade_revision'] = 'writing-2'
                attempt['grading_note'] = 'p-w2 clearance is supplied in the shared prompt, so unknown is false.'
        persist(path, state)
    return summarize(state)


def summarize(state):
    return {phase: {key: {arm: {'attempts': len(attempts),
                                'complete': bool(attempts and attempts[-1]['complete']),
                                'grade': attempts[-1].get('grade') if attempts else None}
                          for arm, attempts in entry['arms'].items()}
                    for key, entry in state.get(phase, {}).items()}
            for phase in ('pilot', 'replay')}



def arm_usage(attempts, settlements, accounts):
    if not attempts:
        return None
    deltas = []
    for attempt in attempts:
        before, after = attempt.get('meter_before'), attempt.get('meter_after')
        settled = settlements.get(attempt.get('reservation')) or {}
        account = attempt.get('account_sha256')
        if not (before and after and settled.get('status') == 'settled' and attempt.get('overlap') is False
                and isinstance(attempt.get('usage'), dict) and account):
            return None
        if before['reset_at'] != after['reset_at'] or after['reset_at'] != settled['reset_at']:
            return None
        accounts.add((account, before['reset_at']))
        delta = after['observed_delta'] - before['observed_delta']
        if delta <= 0:
            return None
        deltas.append(delta)
    return sum(deltas)


def correct(grade, case):
    scores = grade.get(case)
    return isinstance(scores, dict) and bool(scores) and all(value is True for value in scores.values())



def cascade_scores(cheap_grade, escalation):
    missed = {case for case in cheap_grade if not correct(cheap_grade, case)}
    if not missed:
        return cheap_grade, 0
    if not escalation or not escalation[-1].get('complete'):
        return None
    patch = escalation[-1].get('grade')
    if not isinstance(patch, dict) or patch.keys() != missed:
        return None
    return {**cheap_grade, **patch}, len(missed)



def bounded_proposal(raw, grade):
    if not isinstance(raw, dict):
        return {}
    proposal = {}
    for case, fields in grade.items():
        row = raw.get(case)
        if not isinstance(row, dict):
            continue
        record = {key: row[key] for key in fields if key in row}
        serialized = json.dumps(record, ensure_ascii=False)
        if len(serialized) <= 8_000 and not SENSITIVE.search(serialized):
            proposal[case] = record
    return proposal


def execution_parity(root, batch, records, stage, fallback_cases):
    from stage_fixture_suite import execute_composition
    base = {s: records[s]['baseline'][-1].get('proposal') for s in CANDIDATES}
    candidate = records[stage]['candidate'][-1].get('proposal')
    if not isinstance(candidate, dict) or any(not isinstance(proposal, dict) for proposal in base.values()):
        return None
    if fallback_cases:
        correction = records[stage]['fallback'][-1].get('proposal')
        if not isinstance(correction, dict):
            return None
        candidate = {**candidate, **correction}
    outputs = {}
    for arm, stages in (('baseline', base), ('candidate', {**base, stage: candidate})):
        proposals = {e['id']: {s: prediction.get(e['id'], {}) for s, prediction in stages.items()} for e in batch}
        try:
            outputs[arm] = execute_composition(root, batch, proposals)
        except (OSError, RuntimeError, ValueError, KeyError, TimeoutError, subprocess.SubprocessError):
            return None
    return outputs


def evaluate_routes(root, state):
    """Compose each stage substitution with the other two incumbents, then compare per-portal outcomes."""
    episodes, diagnostics = load_episodes(root)
    path = state_path(root)
    corpus = manifest_path(root).with_name('suite.json')
    proof = {'schema': 1, 'results_sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
             'suite_sha256': hashlib.sha256(corpus.read_bytes()).hexdigest() if corpus.is_file() else None,
             'archive': diagnostics, 'stages': {}}
    try:
        batches = split_batches(episodes)
    except ValueError as error:
        proof['hold'] = str(error)
        return proof
    for stage, route in CANDIDATES.items():
        pilot = state.get('pilot', {}).get(stage, {}).get('arms', {})
        pilot_passed = all(pilot.get(arm) and pilot[arm][-1].get('complete') and
                           all(pilot[arm][-1].get('grade', {}).get(case, False) for case, _, _ in stage_pilot.PILOT[stage])
                           for arm in ('baseline', 'candidate'))
        entry = {'model': route.model, 'effort': route.effort, 'pilot_passed': bool(pilot_passed), 'batches': []}
        fallback_observed = False
        for i, batch in enumerate(batches, start=1):
            selected = {s: state.get('replay', {}).get(f'batch-{i}:{s}', {}) for s in CANDIDATES}
            records = {s: part.get('arms', {}) for s, part in selected.items()}
            metrics = {'batch_id': hashlib.sha256(''.join(e['id'] for e in batch).encode()).hexdigest(),
                       'matched_cases': len(batch), 'quality_passed': False, 'comparable': False}
            entry['batches'].append(metrics)
            arms = [('baseline', s) for s in records] + [('candidate', stage)]
            if not all(records[s].get(arm) and records[s][arm][-1].get('complete') for arm, s in arms):
                continue
            scores = {(arm, s): records[s][arm][-1]['grade'] for arm, s in arms}
            repaired = cascade_scores(scores['candidate', stage], records[stage].get('fallback', []))
            if repaired is None:
                continue
            scores['candidate', stage], fallback_cases = repaired
            fallback_observed |= bool(fallback_cases)
            metrics['fallback_cases'] = fallback_cases
            execution = execution_parity(root, batch, records, stage, fallback_cases)
            if execution is None:
                metrics['execution_parity'] = None
                continue
            local_count = sum(e.get('source_kind') == 'local-browser-fixture' for e in batch)
            if local_count == 0 or any(len(results) != local_count for results in execution.values()):
                metrics['execution_parity'] = None
                continue
            metrics['execution_parity'] = {arm: sum(results.values()) for arm, results in execution.items()}
            metrics['local_execution_cases'] = local_count
            by_portal = {p: {'baseline': 0, 'candidate': 0, 'baseline_confirmed': 0,
                             'candidate_confirmed': 0, 'verified': 0} for p in PORTALS}
            for e in batch:
                row = by_portal[e['portal']]
                row['verified'] += 1
                fixed = all(correct(scores['baseline', s], e['id']) for s in records if s != stage)
                for arm in ('baseline', 'candidate'):
                    executed = execution[arm].get(e['id']) if e.get('source_kind') == 'local-browser-fixture' else True
                    completed = executed is True and fixed and correct(scores[arm, stage], e['id'])
                    row[arm] += int(completed)
                    row[arm + '_confirmed'] += int(completed and e['gold']['outcome'] == 'confirmed')
            totals = {arm: sum(row[arm + '_confirmed'] for row in by_portal.values())
                      for arm in ('baseline', 'candidate')}
            quality = (all(execution['candidate'].values()) and
                       all(correct(scores['candidate', stage], e['id']) for e in batch) and
                       all(value['candidate'] >= value['baseline'] and
                           value['candidate_confirmed'] >= value['baseline_confirmed'] for value in by_portal.values()))
            accounts = set()
            histories = {arm: records[stage][arm] for arm in ('baseline', 'candidate')}
            if fallback_cases:
                histories['candidate'] = [*histories['candidate'], *records[stage]['fallback']]
            costs = {arm: arm_usage(history, state.get('settlements', {}), accounts)
                     for arm, history in histories.items()}
            comparable = len(accounts) == 1 and all(costs.values()) and all(totals.values())
            metrics.update(by_portal=by_portal, quality_passed=quality, comparable=bool(comparable))
            if comparable:
                for arm in ('baseline', 'candidate'):
                    metrics[f'{arm}_cost_per_correct'] = costs[arm] / totals[arm]
        entry['qualified'] = qualified(entry)
        entry['fallback_on_miss'] = entry['qualified'] and fallback_observed
        proof['stages'][stage] = entry
    return proof



def verify_suite(root):
    episodes, archive = load_episodes(root)
    batches = split_batches(episodes)
    from stage_fixture_suite import execute_composition
    outcomes = {}
    execution_count = 0
    for number, batch in enumerate(batches, start=1):
        rows = {p: {} for p in PORTALS}
        for e in batch:
            row = rows[e['portal']]
            gold = e['gold']
            outcome = gold['outcome']
            row[outcome] = row.get(outcome, 0) + 1
            matches = {'classification': 'eligible', 'writing': 'answers', 'visual': 'observed_opening'}
            for stage, field in matches.items():
                if not all(grade_episode(stage, gold, gold).values()):
                    raise ValueError(f'Gold cannot be recovered for {e["id"]}/{stage}')
                wrong = {**gold, field: None}
                if all(grade_episode(stage, gold, wrong).values()):
                    raise ValueError(f'Replay accepted missing {field} for {e["id"]}/{stage}')
            if grade_episode('visual', gold, {**gold, 'submit_count': gold['submit_count'] + 1})['duplicate']:
                raise ValueError(f'Replay accepted a duplicate submit for {e["id"]}')
            if not gold['handoff_reason'] and grade_episode('visual', gold, {**gold, 'handoff_reason': 'Fake stop'})['avoidable_handoff']:
                raise ValueError(f'Replay accepted an avoidable handoff for {e["id"]}')
        control = {e['id']: {stage: e['gold'] for stage in CANDIDATES} for e in batch}
        execution = execute_composition(root, batch, control)
        local_count = sum(e.get('source_kind') == 'local-browser-fixture' for e in batch)
        if len(execution) != local_count or not all(execution.values()):
            raise ValueError(f'The sandbox control did not replay all original browser outcomes in batch {number}')
        execution_count += sum(execution.values())
        outcomes[f'batch-{number}'] = rows
    return {'episodes': len(episodes), 'source_mix': archive['sources'], 'portal_totals': archive['verified'],
            'outcomes': outcomes, 'sandbox_control_passed': execution_count, 'local_grade_checks': 'passed'}


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('validate', 'pilot', 'replay', 'report', 'promote', 'regrade-pilot', 'seed-archive', 'build-suite', 'verify-suite'))
    ap.add_argument('--private-root', type=Path, default=Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT', DEFAULT_ROOT)))
    ap.add_argument('--stage', choices=tuple(CANDIDATES))
    ap.add_argument('--max-calls', type=int, default=6)
    ap.add_argument('--budget', type=int, default=1, help='weekly points to reserve within the shared benchmark cap')
    ap.add_argument('--retry', action='store_true', help='retry terminal failures and retain all prior usage')
    ap.add_argument('--subscription', action='store_true', help='authorize guarded calls against the existing subscription')
    return ap


def main():
    ap = parser()
    args = ap.parse_args()
    if args.command == 'verify-suite':
        print(json.dumps(verify_suite(args.private_root), indent=2))
        return
    if args.command == 'build-suite':
        from stage_fixture_suite import build_suite
        print(json.dumps({'suite_episodes': build_suite(args.private_root, persist)}, indent=2))
        return
    if args.command == 'seed-archive':
        from stage_archive_seed import export
        print(json.dumps({'annotated_episodes': export(args.private_root, persist)}, indent=2))
        return
    if args.command == 'regrade-pilot':
        print(json.dumps(regrade_pilot(args.private_root), indent=2))
        return
    if args.command in ('report', 'promote'):
        state = load(state_path(args.private_root), {})
        promotion = evaluate_routes(args.private_root, state)
        if args.command == 'promote':
            persist(evidence_path(args.private_root), promotion)
        print(json.dumps({**summarize(state), 'promotion': promotion}, indent=2))
        return
    if args.command in ('pilot', 'replay') and not args.subscription:
        ap.error('a model benchmark requires explicit --subscription')
    if not 1 <= args.max_calls <= 100:
        ap.error('--max-calls must be 1–100')
    try:
        with tempfile.TemporaryDirectory(prefix='jr-replay-') as tmp:
            folder = Path(tmp)
            prepared = []
            if args.command in ('pilot', 'validate'):
                for stage in (args.stage,) if args.stage else ('classification', 'visual', 'writing'):
                    prompt, images = stage_pilot.prepare(stage, folder)
                    prepared.append((stage, stage, prompt, images,
                                     lambda output, stage=stage: stage_pilot.grade(stage, output), None))
            if args.command == 'validate':
                archive = load_episodes(args.private_root)[1]
                print(json.dumps({'archive': archive, 'pilot': {key: {'cases': len(stage_pilot.PILOT[stage]),
                                                  'images': len(images), 'input_sha256': input_digest(prompt, images)}
                                            for key, stage, prompt, images, _, _ in prepared}}, indent=2))
                return
            if args.command == 'replay':
                from stage_archive import prepare_replay
                prepared = prepare_replay(args.private_root, args.stage, folder)
            result = run_arms(args.private_root, prepared, phase=args.command, retry=args.retry,
                              max_calls=args.max_calls, budget=args.budget)
            if args.command == 'replay':
                promotion = evaluate_routes(args.private_root, load(state_path(args.private_root), {}))
                persist(evidence_path(args.private_root), promotion)
                result['promotion'] = promotion
            print(json.dumps(result, indent=2))
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        ap.exit(2, f'{error}\n')


if __name__ == '__main__':
    main()
