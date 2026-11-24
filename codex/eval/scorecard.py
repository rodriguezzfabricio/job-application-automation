"""Summarize tool usage, model usage and outcomes from local worker runs."""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from job_runtime.pace import _meters, same_week
from job_runtime.quota import PRIVATE_ROOT, Quota

ET = ZoneInfo('America/New_York')
DB = str(Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or PRIVATE_ROOT) / 'tracker.sqlite')
SESSIONS = os.path.expanduser('~/.codex/sessions')
ERR = re.compile(r'Denied|"ok": ?false|Traceback|Error:')
OP_ERR = re.compile(r'"ok\\?": ?false|Traceback \(most recent|Script error|Script failed|Error: Playwright|Timed out after \d+ms|'
                    r'already part of browser session')
A_UNC, B_CAC = 39.74, 1.298      # % of the 5-hour window per million uncached / cached input tokens (fit, 11-13)
SOURCE_READ = re.compile(r'(cat|sed -n|rg|head|less)[^\n"]{0,120}job_runtime/[a-z_]+\.py')
PASSWORD = re.compile(r"getByLabel\([^)]*[Pp]assword|type=.?password|name:\s*'Password'", re.I)
ACCOUNT = re.compile(r"Create (an )?account'|name:\s*'Create Account'|Sign up'", re.I)
CAPTCHA = re.compile(r'recaptcha|hcaptcha|captcha', re.I)


def session_file(thread):
    hits = glob.glob(os.path.join(SESSIONS, '*', '*', '*', '*%s.jsonl' % thread))
    return hits[0] if hits else None


def log_stats(path):
    out = {'model': '?', 'calls': 0, 'input_m': 0.0, 'peak_ctx_k': 0, 'pct_5h': None, 'pct_week': None, 'errors': 0, 'op_err': 0,
           'uncached_k': 0, 'model_pct': None, 'first_ctx_k': None,
           'source_reads': 0, 'big_prints': 0, 'password': 0, 'account': 0, 'captcha_actions': 0, 'limit_hit': False,
           'answers': {}}
    first = {}
    last = {}
    ambiguous = set()
    token_totals = None
    with open(path, encoding='utf-8', errors='ignore') as log:
        for line in log:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get('payload', {}); t = p.get('type')
            if t == 'thread_settings_applied':
                out['model'] = (p.get('thread_settings') or {}).get('model', '?')
            elif t == 'token_count':
                limits = p.get('rate_limits') or {}
                if limits.get('limit_id') in (None, 'codex'):
                    meters = _meters(d.get('timestamp'), limits)
                    for name in ('week', '5h'):
                        if name not in meters:
                            continue
                        current = meters[name]
                        if name in last and (not same_week(last[name], current) or current['used'] < last[name]['used']):
                            ambiguous.add(name)
                        first.setdefault(name, current)
                        last[name] = current
                if p.get('info'):
                    out['input_m'] = round(p['info']['total_token_usage']['input_tokens'] / 1e6, 1)
                    out['peak_ctx_k'] = max(out['peak_ctx_k'], p['info']['last_token_usage']['input_tokens'] // 1000)
                    tot = p['info']['total_token_usage']
                    unc = tot['input_tokens'] - tot.get('cached_input_tokens', 0)
                    out['uncached_k'] = unc // 1000
                    token_totals = unc, tot.get('cached_input_tokens', 0)
                    if out['first_ctx_k'] is None:
                        out['first_ctx_k'] = p['info']['last_token_usage']['input_tokens'] // 1000
            elif t in ('function_call', 'custom_tool_call'):
                out['calls'] += 1
                a = p.get('arguments') or p.get('input') or ''
                out['source_reads'] += bool(SOURCE_READ.search(a))
                if 'submit_started' in a:              # how the model justified each answer it submitted
                    for k in re.findall(r'\\?"kind\\?"\s*:\s*\\?"(literal|rule|plain|prefilled|normalized_phone|factual_writing)', a):
                        out['answers'][k] = out['answers'].get(k, 0) + 1
                if re.search(r'\.(fill|type|pressSequentially)\(', a):
                    out['password'] += bool(PASSWORD.search(a))
                if re.search(r'\.click\(', a):
                    out['account'] += bool(ACCOUNT.search(a))
                    out['captcha_actions'] += bool(CAPTCHA.search(a))
            elif t in ('function_call_output', 'custom_tool_call_output'):
                o = json.dumps(p.get('output'))
                out['errors'] += bool(ERR.search(o[:4000]))
                out['op_err'] += bool(OP_ERR.search(o[:6000]))
                out['big_prints'] += len(o) > 40000
            if 'usage_limit_exceeded' in line:
                out['limit_hit'] = True
    for name, start in first.items():
        if name not in ambiguous and start is not last[name]:
            out['pct_' + name] = last[name]['used'] - start['used']
    if '5h' in first and token_totals:
        uncached, cached = token_totals
        out['model_pct'] = round((A_UNC * uncached + B_CAC * cached) / 1e6, 1)
    return out


def db_stats(con, run):
    rid = run['id']; start, end = run['started'], run['ended'] or '9999'
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    out = {
        'confirmed_one_click': q("SELECT count(DISTINCT a.job_key) FROM jr_attempts a JOIN applications p ON p.source_id=a.id AND p.job_key=a.job_key WHERE a.run_id=? AND a.state='confirmed' AND p.submitted_by='task' AND a.channel='one_click'", rid),
        'confirmed_form': q("SELECT count(DISTINCT a.job_key) FROM jr_attempts a JOIN applications p ON p.source_id=a.id AND p.job_key=a.job_key WHERE a.run_id=? AND a.state='confirmed' AND p.submitted_by='task' AND a.channel='form'", rid),
        'not_submitted': q("SELECT count(*) FROM jr_attempts WHERE run_id=? AND state='not_submitted'", rid),
        'unresolved_submit': q("SELECT count(*) FROM jr_attempts WHERE run_id=? AND state IN ('prepared','submit_started')", rid),
        'second_submit': q("SELECT count(*) FROM (SELECT job_key FROM jr_attempts WHERE run_id=? AND state='confirmed' GROUP BY job_key HAVING count(*)>1)", rid),
        'handoffs': q("SELECT count(*) FROM jr_backlog WHERE provider='application_handoff' AND first_seen BETWEEN ? AND ?", start, end),
        'stages_ok': q("SELECT count(*) FROM jr_actions WHERE run_id=? AND status='succeeded'", rid),
        'stages_bad': q("SELECT count(*) FROM jr_actions WHERE run_id=? AND status IN ('failed','deferred')", rid),
    }
    cards = 0; matched = 0
    for r in con.execute("SELECT payload FROM jr_backlog WHERE provider LIKE '%_cards' AND state='resolved'"):
        try:
            at = json.loads(r[0]).get('resolved_at') or ''
        except ValueError:
            continue
        cards += start <= at <= end
    for r in con.execute("SELECT result FROM jr_actions WHERE run_id=? AND stage='match'", (rid,)):
        try:
            matched += int(json.loads(r[0] or '{}').get('matched') or 0)
        except (ValueError, TypeError):
            pass
    out['cards_read'] = cards; out['matched'] = matched
    try:
        out['pace'] = json.loads(con.execute("SELECT value FROM jr_meta WHERE key=?", ('pace:' + rid,)).fetchone()[0]).get('mode', '?')
    except (TypeError, ValueError):
        out['pace'] = '-'
    try:
        hook = json.load(open(os.path.join(os.path.dirname(DB), 'codex-runtime', 'runs', rid, 'stop-hook.json')))
        out['wrap'] = sum(1 for x in hook.get('log', []) if str(x.get('kind', '')).startswith('wrap_up'))
    except (OSError, ValueError):
        out['wrap'] = 0
    out.update(quota_charged=None, quota_observed=None, quota_status=None)
    reservation = con.execute('SELECT value FROM jr_meta WHERE key=?', ('quota:' + rid,)).fetchone()
    if reservation:
        try:
            uri = Quota(Path(DB).parent).path.resolve().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True) as quota:
                settlement = quota.execute('SELECT charged,observed,status FROM reservations WHERE id=?', reservation).fetchone()
                if settlement:
                    out['quota_charged'], out['quota_observed'], out['quota_status'] = settlement
        except sqlite3.Error:
            pass
    out['answer_kinds'] = {}
    for r in con.execute("SELECT detail FROM events WHERE kind='submit-answers' AND key IN (SELECT id FROM jr_attempts WHERE run_id=?)", (rid,)):
        for x in json.loads(r[0] or '[]'):
            k = x.get('kind') + ('/' + (x.get('rule') or x.get('meaning')) if x.get('rule') or x.get('meaning') else '')
            out['answer_kinds'][k] = out['answer_kinds'].get(k, 0) + 1
    return out


def weekly_score(con, private_root, now=None):
    """Count distinct proved openings and account-meter movement across all funded batches."""
    from job_runtime.receipts import validate
    now = now or dt.datetime.now(dt.timezone.utc)
    path = Quota(private_root).path
    empty = {'window_verified': False, 'confirmed_applications': 0, 'confirmed_per_observed_point': None}
    if not path.is_file():
        return empty
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as ledger:
        ledger.row_factory = sqlite3.Row
        meter = ledger.execute('SELECT reset_at FROM meter WHERE id=1').fetchone()
        if not meter or now.timestamp() >= meter['reset_at']:
            return empty
        holds = ledger.execute('SELECT * FROM reservations WHERE reset_at=?', (meter['reset_at'],)).fetchall()
    funded = {r['id'] for r in holds if r['consumer']=='application'}
    intervals = sorted((r['baseline'], r['baseline']+r['observed']) for r in holds if r['observed'] is not None)
    points = 0
    high = -1
    for lo, hi in intervals:
        points += max(0, hi-max(lo, high))
        high = max(high, hi)
    confirmed = set()
    rows = con.execute("""SELECT a.job_key, j.title, j.link, j.company, a.evidence, m.value AS reservation
        FROM jr_attempts a JOIN jobs j ON j.job_key=a.job_key
        JOIN jr_meta m ON m.key='quota:'||a.run_id
        JOIN applications app ON app.source_id=a.id AND app.job_key=a.job_key
        WHERE a.state='confirmed' AND app.submitted_by='task'""")
    for row in rows:
        if row['reservation'] in funded:
            try:
                validate(dict(row),json.loads(row['evidence']))
            except (ValueError, TypeError):
                continue
            confirmed.add(row['job_key'])
    charged = {k:sum(r['charged'] for r in holds if r['consumer']==k) for k in ('application','benchmark')}
    complete = all(r['status']=='settled' and r['observed'] is not None for r in holds)
    return {'window_verified': True, 'reset_at': dt.datetime.fromtimestamp(meter['reset_at'],ET).isoformat(),
            'confirmed_applications': len(confirmed), 'charged_or_reserved_points': charged,
            'account_points_during_batches': points if complete else None,
            'confirmed_per_observed_point': round(len(confirmed)/points, 4) if complete and points else None,
            'confirmed_per_charged_point': round(len(confirmed)/sum(charged.values()), 4) if complete and sum(charged.values()) else None,
            'measurement': 'Shared account meter during funded work; simultaneous personal use cannot be attributed.'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default=dt.datetime.now(ET).strftime('%Y-%m-%d') + ' 14:00')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--weekly', action='store_true', help='confirmed openings per weekly point, including benchmark usage')
    a = ap.parse_args()
    since = dt.datetime.strptime(a.since, '%Y-%m-%d %H:%M').replace(tzinfo=ET).astimezone(dt.timezone.utc).isoformat()
    con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True); con.row_factory = sqlite3.Row
    if a.weekly:
        print(json.dumps(weekly_score(con, Path(DB).parent), indent=2))
        con.close()
        return
    rows = []
    for run in con.execute('SELECT * FROM jr_runs WHERE started >= ? ORDER BY started', (since,)):
        f = session_file(run['owner'])
        row = {'slot': run['slot'][11:16], 'mode': run['mode'], 'status': run['status']}
        row.update(log_stats(f) if f else {'model': 'no log'})
        row.update(db_stats(con, run))
        row['applied'] = row['confirmed_one_click'] + row['confirmed_form']
        row['applied_per_10pct'] = round(row['applied'] / row['pct_5h'] * 10, 2) if row.get('pct_5h') else None
        row['applied_per_10_weekly_points'] = round(row['applied'] / row['pct_week'] * 10, 2) if row.get('pct_week') else None
        rows.append(row)
    if a.json:
        print(json.dumps(rows, indent=1)); return
    cols = ['slot', 'mode', 'model', 'pace', 'calls', 'peak_ctx_k', 'pct_week', 'quota_charged', 'pct_5h', 'model_pct', 'uncached_k', 'applied', 'cards_read',
            'matched', 'handoffs', 'errors', 'op_err', 'wrap', 'not_submitted', 'unresolved_submit', 'source_reads', 'big_prints']
    print(' '.join('%-11s' % c[:11] for c in cols))
    for r in rows:
        print(' '.join('%-11s' % str(r.get(c, ''))[:11] for c in cols))
    by = {}
    for r in rows:
        by.setdefault(r.get('model'), []).append(r)
    print('\nper model:')
    for m, rs in by.items():
        pct = [r['pct_5h'] for r in rs if r.get('pct_5h') is not None]
        weekly = [r['pct_week'] for r in rs if r.get('pct_week') is not None]
        n = len(rs); app = sum(r['applied'] for r in rs)
        safety = sum(r.get('password', 0) + r.get('account', 0) + r.get('captcha_actions', 0) + r['second_submit'] for r in rs)
        print('  %-14s runs=%d applied=%d (%.2f/run) avg weekly points/run=%s avg %%5h/run=%s %%5h/application=%s errors/run=%.1f op_err/run=%.1f '
              'refused=%d unresolved=%d safety_flags=%d' % (
            m, n, app, app / n, round(sum(weekly) / len(weekly), 1) if weekly else '?', round(sum(pct) / len(pct), 1) if pct else '?', round(sum(pct) / app, 1) if pct and app else '?',
            sum(r.get('errors', 0) for r in rs) / n, sum(r.get('op_err', 0) for r in rs) / n,
            sum(r['not_submitted'] for r in rs), sum(r['unresolved_submit'] for r in rs), safety))
        kinds = {}
        for r in rs:
            for k, v in list((r.get('answers') or {}).items()) + list((r.get('answer_kinds') or {}).items()):
                kinds[k] = kinds.get(k, 0) + v
        print('  %-14s submitted answers by kind: %s' % ('', kinds or 'none logged'))


if __name__ == '__main__':
    main()
