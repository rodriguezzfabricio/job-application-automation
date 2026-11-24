#!/usr/bin/env python3
"""CAPTCHA evidence for the worker (codex/stages/CAPTCHA.md).

The stage doc ships the commands and the receipt fields the audit reads, so this tool
exists to make both mechanical: one append-only record per challenge, and an honest
count that never inflates "seen" into "resolved". Nothing here clicks anything; the
model does that and calls `record`.

Usage:
  captcha.py policy --run-id RUN          effective bounds for this run (read once)
  captcha.py record --run-id RUN --stage fill --host H --provider turnstile \
      --action clicked --outcome passed [--attempts N] [--elapsed-seconds N] \
      [--token-observed] [--job-key K] [--ref R] [--note TEXT]
  captcha.py summary --run-id RUN         {"captchas_seen", "captchas_resolved", "captchas_handed_off"}

`record` prints the stored record; pass its `refs` into the stage receipt as `captcha_refs`.
The private root comes from JOB_AUTOMATION_PRIVATE_ROOT (or --private on the command line).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ACTIONS = ('detected', 'clicked', 'solved_grid', 'handed_off', 'blocked', 'skipped')
OUTCOMES = ('passed', 'failed', 'handoff', 'detected_only', 'blocked')
REQUIRED = ('run_id', 'stage', 'host', 'provider', 'action', 'outcome')
BOUND_KEYS = ('captcha_policy', 'captcha_max_attempts', 'captcha_budget_seconds', 'captcha_max_per_run', 'captcha_solver')

sys.path.insert(0, str(Path(__file__).resolve().parent))


def private_root(explicit=None):
    if explicit:
        return str(Path(explicit).resolve())
    import runtime_paths
    return runtime_paths.PRIVATE


def db_path(explicit=None, private=None):
    return str(Path(explicit).resolve()) if explicit else os.path.join(private_root(private), 'tracker.sqlite')


def events_path(private):
    return Path(private) / 'codex-runtime' / 'captcha' / 'events.jsonl'


def read_events(private):
    p = events_path(private)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a half-written tail line does not lose the rest
    return out


def record(private=None, **fields):
    """Append one challenge record and return it, including citable refs."""
    private = private_root(private)
    missing = [k for k in REQUIRED if not str(fields.get(k) or '').strip()]
    if missing:
        raise ValueError('record needs: ' + ', '.join(missing))
    if fields['action'] not in ACTIONS:
        raise ValueError('unknown action %r (one of %s)' % (fields['action'], ', '.join(ACTIONS)))
    if fields['outcome'] not in OUTCOMES:
        raise ValueError('unknown outcome %r (one of %s)' % (fields['outcome'], ', '.join(OUTCOMES)))
    existing = read_events(private)
    n = len(existing) + 1
    key = '%s|%s|%s|%s' % (fields['run_id'], n, fields['host'], fields.get('job_key') or fields.get('ref') or '')
    row = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'seq': n,
           'attempts': int(fields.get('attempts') or 0), 'elapsed_seconds': float(fields.get('elapsed_seconds') or 0),
           'token_observed': bool(fields.get('token_observed')),
           'refs': ['captcha:' + hashlib.sha256(key.encode()).hexdigest()[:12], '%s:%s' % (fields['host'], fields['provider'])]}
    for name in ('run_id', 'stage', 'host', 'provider', 'action', 'outcome', 'job_key', 'ref', 'note'):
        if fields.get(name) is not None:
            row[name] = fields[name]
    p = events_path(private)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, sort_keys=True) + '\n')
    return row


def summary(private=None, run_id=None):
    """Honest per-run counts. `seen` is one per recorded challenge, not per attempt."""
    rows = [r for r in read_events(private_root(private)) if r.get('run_id') == run_id]
    return {'run_id': run_id, 'captchas_seen': len(rows),
            'captchas_resolved': sum(1 for r in rows if r.get('outcome') == 'passed'),
            'captchas_handed_off': sum(1 for r in rows if r.get('outcome') == 'handoff')}


def policy(db=None, run_id=None, private=None):
    """The effective bounds stored with the run: never invent them, never re-read per challenge."""
    from job_runtime.policy import effective
    path = db_path(db, private)
    if not Path(path).is_file():
        raise ValueError('no tracker database at ' + path)
    con = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    try:
        row = con.execute('SELECT policy FROM jr_runs WHERE id=?', (run_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError('unknown run_id ' + str(run_id))
    stored = json.loads(row[0])
    eff = effective({k: v for k, v in stored.items() if k != 'hash'})
    out = dict((k, eff[k]) for k in BOUND_KEYS)
    out['policy_hash'] = stored.get('hash') or eff['hash']
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('policy', 'record', 'summary'))
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--private')
    ap.add_argument('--db')
    ap.add_argument('--stage')
    ap.add_argument('--host')
    ap.add_argument('--provider')
    ap.add_argument('--action')
    ap.add_argument('--outcome')
    ap.add_argument('--attempts', type=int)
    ap.add_argument('--elapsed-seconds', type=float)
    ap.add_argument('--token-observed', action='store_true')
    ap.add_argument('--job-key')
    ap.add_argument('--ref')
    ap.add_argument('--note')
    a = ap.parse_args(argv)
    names = {'run_id': a.run_id, 'stage': a.stage, 'host': a.host, 'provider': a.provider,
             'action': a.action, 'outcome': a.outcome, 'attempts': a.attempts,
             'elapsed_seconds': a.elapsed_seconds, 'token_observed': a.token_observed,
             'job_key': a.job_key, 'ref': a.ref, 'note': a.note}
    if a.command == 'policy':
        result = policy(a.db, a.run_id, a.private)
    elif a.command == 'summary':
        result = summary(a.private, a.run_id)
    else:
        result = record(a.private, **names)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ValueError as e:
        print(json.dumps({'ok': False, 'error': str(e)}))
        sys.exit(2)
