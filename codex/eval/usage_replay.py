"""Replay locally supplied usage records."""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or os.path.join(ROOT, 'stopgap-private'), 'tracker.sqlite')
A_UNC, B_CAC = 39.74e-6, 1.298e-6          # replaced by the fit when --section includes fit
CTX_CAP = 180000
MEMORY_SAVED_FLOOR = 3600                  # BRIEF (~2.5k tokens) + trimmed memory (~1k) + worker-location stays


def load(day):
    out = {}
    for f in sorted(glob.glob(os.path.expanduser('~/.codex/sessions/%s/*.jsonl' % day))):
        sid = os.path.basename(f)[-42:-6]
        meta = {'file': f, 'sid': sid, 'model': None, 'trigger': None, 'account': None}
        ev = []
        for line in open(f, encoding='utf-8', errors='ignore'):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p = d.get('type'), d.get('payload') or {}
            pt = p.get('type')
            if t == 'session_meta':
                meta['account'] = p.get('creator_account_id')
            elif t == 'event_msg' and pt == 'thread_settings_applied':
                meta['model'] = (p.get('thread_settings') or {}).get('model')
            elif t == 'event_msg' and pt == 'task_started':
                meta['trigger'] = (p.get('turn_attribution') or {}).get('turn_trigger')
            elif t == 'token_usage_record':
                ev.append(('usage', d['timestamp'], p['usage']))
            elif t == 'event_msg' and pt == 'token_count':
                rl = p.get('rate_limits') or {}
                if rl.get('primary'):
                    ev.append(('rl', d['timestamp'], {'pct': rl['primary']['used_percent'], 'resets': rl['primary']['resets_at'],
                                                      'week': (rl.get('secondary') or {}).get('used_percent'),
                                                      'week_resets': (rl.get('secondary') or {}).get('resets_at')}))
            elif t == 'response_item' and pt in ('function_call', 'custom_tool_call'):
                ev.append(('call', d['timestamp'], {'name': p.get('name'), 'call_id': p.get('call_id'),
                                                    'input': (p.get('arguments') if pt == 'function_call' else p.get('input')) or ''}))
            elif t == 'response_item' and pt in ('function_call_output', 'custom_tool_call_output'):
                o = p.get('output')
                txt = ''.join((x.get('text') or '') for x in o if isinstance(x, dict)) if isinstance(o, list) else (o if isinstance(o, str) else json.dumps(o))
                ev.append(('out', d['timestamp'], {'call_id': p.get('call_id'), 'text': txt}))
        out[sid] = (meta, ev)
    return out


def lstsq(X, y):
    """Least squares by normal equations with partial pivoting (no numpy in the worker venv)."""
    k = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(k)] + [sum(r[i] * v for r, v in zip(X, y))] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(A[r][c]))
        A[c], A[p] = A[p], A[c]
        if abs(A[c][c]) < 1e-12:
            continue
        for r in range(k):
            if r != c:
                f = A[r][c] / A[c][c]
                A[r] = [a - f * b for a, b in zip(A[r], A[c])]
    return [A[i][k] / A[i][i] if abs(A[i][i]) > 1e-12 else 0.0 for i in range(k)]


def fit(S):
    reqs = []
    for sid, (m, e) in S.items():
        pending = None
        for x in e:
            if x[0] == 'usage':
                if pending:
                    reqs.append(pending)
                pending = [x[1], x[2], None]
            elif x[0] == 'rl' and pending is not None and pending[2] is None:
                pending[2] = x[2]
        if pending:
            reqs.append(pending)
    reqs.sort(key=lambda r: r[0])
    accounts = {}
    for sid, (m, e) in S.items():
        accounts[m['account']] = accounts.get(m['account'], 0) + 1
    wins = {}
    for r in reqs:
        if r[2]:
            wins.setdefault(round(r[2]['resets'] / 600), []).append(r)
    keys = sorted(wins)
    rows, y = [], []
    for k in keys:
        cu = cc = co = n = 0
        for r in wins[k]:
            u = r[1]
            cu += u['input_tokens'] - u['cached_input_tokens']; cc += u['cached_input_tokens']; co += u['output_tokens']; n += 1
            rows.append((k, cu, cc, co, n)); y.append(r[2]['pct'])
    mean = sum(y) / len(y)
    res = {}
    for name, cols in (('total input', 'T'), ('uncached input', 'U'), ('cached input', 'C'), ('requests', 'N'),
                       ('output', 'O'), ('uncached + cached', 'UC'), ('uncached + cached + output', 'UCO')):
        X = []
        for (k, cu, cc, co, n) in rows:
            f = {'U': cu / 1e6, 'C': cc / 1e6, 'O': co / 1e6, 'N': n / 1e6, 'T': (cu + cc) / 1e6}     # per million
            X.append([1.0 if k == kk else 0.0 for kk in keys] + ([f['T']] if cols == 'T' else [f[c] for c in cols]))
        coef = lstsq(X, y)
        pred = [sum(a * b for a, b in zip(r, coef)) for r in X]
        r2 = 1 - sum((a - b) ** 2 for a, b in zip(y, pred)) / sum((a - mean) ** 2 for a in y)
        res[name] = (r2, sum(abs(a - b) for a, b in zip(y, pred)) / len(y), [c / 1e6 for c in coef[len(keys):]])
    print('5-hour meter vs cumulative tokens (per-window intercepts; %d requests, %d windows, session accounts %s)' % (len(y), len(keys), accounts))
    for name, (r2, mae, coef) in res.items():
        print('  %-28s R2=%.4f  mean abs error=%.2f points  coef=%s' % (name, r2, mae, ['%.3g' % c for c in coef]))
    weekly = []
    for k in keys:
        w0, w1 = wins[k][0][2], wins[k][-1][2]
        if w0['week'] is not None and w1['week'] is not None and w1['pct'] - w0['pct'] >= 30:
            weekly.append((w1['pct'] - w0['pct'], w1['week'] - w0['week']))
    if weekly:
        print('  weekly points per 5-hour point, windows with >=30 points used: %s -> %.3f (one account: a week holds ~%.1f windows)' % (
            weekly, sum(b for a, b in weekly) / sum(a for a, b in weekly), sum(a for a, b in weekly) / max(1, sum(b for a, b in weekly))))
    return res['uncached + cached'][2]


ERR = re.compile(r'"ok\\?": ?false|Traceback \(most recent|Script error|Script failed|Error: Playwright|Timed out after|already part of browser session|"exit_code":[12],', re.I)
DISC = re.compile(r'linkedin\.com/jobs/(view|search)|job-search|cards_js|cards_next|card_commit|cards_ingest|page_js|list_js|page_selector|'
                  r'list_selector|job-result-card|jobs/search-results|componentkey|stage (handshake|linkedin)', re.I)
SHEETREAD = re.compile(r'get_spreadsheet_range\s*\(')


def category(c, begun):
    s = c['input']
    if c['name'] == 'js':
        if re.search(r'documentation\.get', s): return 'cua: docs'
        if DISC.search(s): return 'discovery: CUA reads'
        if re.search(r'getState|createBrowserTab|getTab\(', s) and not re.search(r'\.fill\(|\.click\(|setInputFiles|filechooser|selectOption', s):
            return 'cua: tab setup (+docs blob)'
        return 'forms: CUA'
    if c['name'] != 'exec':
        return 'other tool'
    if not begun:
        if re.search(r'codex/jr begin|begin_cycle', s): return 'startup: begin'
        if SHEETREAD.search(s): return 'startup: 5-range read+save'
        if re.search(r'cat [^"]*(BRIEF|memory\.md|RUNBOOK|WORKER|worker-location)|ALL_TOOLS|clock__curr_time|codex/jr -h', s):
            return 'startup: BRIEF/memory/tool lookup'
        return 'startup: other'
    if re.search(r'memory\.md', s) and re.search(r'apply_patch|>>|Update File|Add File|appendFile', s): return 'finish: memory.md write'
    if re.search(r'codex/jr (finish|audit|alerts)|"op":\s*"(finish|audit)"|\bfinish\(', s): return 'finish/audit/alerts'
    if re.search(r'batch_update_spreadsheet|update_spreadsheet|update_values|append_values', s): return 'mirror: sheet write'
    if re.search(r'upload_file|google_drive_search|status_artifact|create_file|get_file_metadata', s): return 'mirror: STATUS upload'
    if re.search(r'mirror_prepare|MIRROR\.md|"op":\s*"mirror', s): return 'mirror: prepare'
    if SHEETREAD.search(s): return 'sheet re-read (fresh/mirror)'
    if re.search(r'codex/jr (prepare|submit_started|reconcile|fresh|job_closed|adopt)|"op":\s*"(prepare|submit_started|reconcile|job_closed)|submit_started|form-answers|facts\.md', s):
        return 'forms: jr submit ops'
    if re.search(r'match_batch|rubric-overrides|match-prompt|match_resume|"op":\s*"match|codex/jr match|match-requirements', s): return 'match'
    if DISC.search(s): return 'discovery: jr card ops'
    if re.search(r'gmail_|poll_lists|import_feed|feeds\.py|LOCAL\.md|intake', s): return 'intake/feeds'
    if re.search(r'(cat|sed -n|rg|nl -ba|head|tail|less|grep)\b[^"]{0,300}(job_runtime|stopgap-tools/[a-z_]+\.py|__main__)', s): return 'mid-run source read'
    if re.search(r'(cat|sed -n|rg|nl -ba|head|tail|grep)\b[^"]{0,300}\.md\b', s): return 'mid-run doc read'
    if re.search(r'codex/jr (stage|plan|brief|resume_sync|tab_sweep|heartbeat|status|defer|notify|mark_opened)|resume_sync|record_stage|"op":\s*"(plan|next|notify|stage)', s):
        return 'jr stage/plan/brief/notify'
    if 'ALL_TOOLS' in s: return 'tool lookup (mid-run)'
    return 'exec other (python/shell)'


def requests(S, sid):
    m, e = S[sid]
    outs = {x[2]['call_id']: x[2] for x in e if x[0] == 'out'}
    reqs, pend, rl = [], [], None
    for x in e:
        if x[0] == 'call':
            pend.append(x[2])
        elif x[0] == 'usage':
            reqs.append({'ts': x[1], 'u': x[2], 'calls': pend, 'rl': None}); pend = []
        elif x[0] == 'rl' and reqs and reqs[-1]['rl'] is None:
            reqs[-1]['rl'] = x[2]
    begun, prev_err, rows = False, False, []
    for i, r in enumerate(reqs):
        u = r['u']; U = u['input_tokens'] - u['cached_input_tokens']; C = u['cached_input_tokens']
        cats, err, mem = [], False, False
        for c in r['calls']:
            cats.append(category(c, begun))
            if re.search(r'codex/jr begin|begin_cycle', c['input']):
                begun = True
            if re.search(r'cat [^"]*memory\.md', c['input']) and not begun:
                mem = True
            o = outs.get(c['call_id'])
            if o and ERR.search(o['text'][:6000]):
                err = True
        rows.append({'i': i, 'ts': r['ts'], 'U': U, 'C': C, 'ctx': u['input_tokens'], 'cat': cats[0] if cats else 'final message / hook turn',
                     'err': err, 'after_err': prev_err, 'mem_read': mem, 'rl': r['rl']})
        prev_err = err
    for i, row in enumerate(rows):
        row['growth'] = max(rows[i + 1]['ctx'] - row['ctx'], 0) if i + 1 < len(rows) else 0
    return m, rows


def price(rows):
    return sum(A_UNC * r['U'] + B_CAC * r['C'] for r in rows)


def production_runs(S, since):
    con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True); con.row_factory = sqlite3.Row
    runs = []
    for run in con.execute("SELECT * FROM jr_runs WHERE started >= ? ORDER BY started", (since,)):
        if run['owner'] in S and any(x[0] == 'usage' for x in S[run['owner']][1]):
            applied = con.execute("SELECT count(*) FROM jr_attempts WHERE run_id=? AND state='confirmed'", (run['id'],)).fetchone()[0]
            runs.append((dict(run), applied))
    return runs


def categories(S, runs):
    agg, tot, n = {}, 0.0, 0
    for run, _ in runs:
        m, rows = requests(S, run['owner'])
        if m['model'] != 'gpt-6.1-sol':
            continue
        n += 1
        N = len(rows)
        for r in rows:
            a = agg.setdefault(r['cat'], [0, 0.0, 0, 0.0])
            cost = A_UNC * r['U'] + B_CAC * r['C']
            a[0] += 1; a[1] += cost; a[2] += r['growth']; a[3] += r['growth'] * (A_UNC + B_CAC * max(N - r['i'] - 2, 0))
            tot += cost
    err = sum(A_UNC * r['U'] + B_CAC * r['C'] for run, _ in runs for r in requests(S, run['owner'])[1]
              if r['after_err'] and S[run['owner']][0]['model'] == 'gpt-6.1-sol')
    print('Sol runs: %d, modeled %.1f%% of a 5-hour window (%.1f%%/run); requests right after an op error: %.1f%%' % (n, tot, tot / max(n, 1), err))
    print('| category | requests | cost (% of 5h) | share | context tokens added | carried cost |')
    print('|---|---:|---:|---:|---:|---:|')
    for k, (c, cost, g, carry) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        print('| %s | %d | %.1f | %.1f%% | %dk | %.1f |' % (k, c, cost, 100 * cost / tot, g // 1000, carry))


def run_table(S, runs):
    print('| slot | mode | model | requests | measured % | modeled % | applied | startup % | tail % | CUA docs carried % | first request % | peak ctx |')
    print('|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
    for run, applied in runs:
        m, rows = requests(S, run['owner'])
        rl = [x[2]['pct'] for x in S[run['owner']][1] if x[0] == 'rl']
        N = len(rows)
        start = sum(A_UNC * r['U'] + B_CAC * r['C'] for r in rows if r['cat'].startswith('startup'))
        tail = sum(A_UNC * r['U'] + B_CAC * r['C'] for r in rows if r['cat'].startswith(('mirror', 'finish', 'final')))
        docs = sum(r['growth'] * (A_UNC + B_CAC * max(N - r['i'] - 2, 0)) for r in rows if r['cat'].startswith('cua: tab setup'))
        print('| %s | %s | %s | %d | %.0f | %.1f | %d | %.1f | %.1f | %.1f | %.2f | %dk |' % (
            run['slot'][11:16], run['mode'], (m['model'] or '?').replace('gpt-6.1-', ''), N, (rl[-1] - rl[0]) if rl else 0,
            price(rows), applied, start, tail, docs, A_UNC * rows[0]['U'] + B_CAC * rows[0]['C'], max(r['ctx'] for r in rows) // 1000))


def replay_run(rows, fresh_ctx):
    """Re-price one run: memory trim shrinks the first doc read; past CTX_CAP the remaining requests move to a
    fresh run whose context starts at fresh_ctx (that run's startup is the scheduled next run, not extra)."""
    saved = 0
    out = []
    for r in rows:
        if r['mem_read']:
            saved = max(0, r['growth'] - MEMORY_SAVED_FLOOR)
            out.append(dict(r))
            continue
        out.append(dict(r, ctx=r['ctx'] - saved, C=max(0, r['C'] - saved)))
    cut = next((i for i, r in enumerate(out) if r['ctx'] >= CTX_CAP), None)
    moved = 0
    if cut is not None:
        base = out[cut]['ctx']
        for r in out[cut:]:
            if r['cat'].startswith(('mirror', 'finish', 'final')):
                continue                                # the wrap-up does these anyway
            r['C'] = max(0, fresh_ctx + (r['ctx'] - base) - r['U'])
            r['ctx'] = fresh_ctx + (r['ctx'] - base)
            moved += 1
    return price(out), saved, moved


def replay(S, runs):
    from job_runtime import pace
    print('| slot | measured % | modeled now % | memory trim saves (tokens/request) | requests moved by 180k wrap-up | replayed % | pace gate at start |')
    print('|---|---:|---:|---:|---:|---:|---|')
    tot_now = tot_new = 0.0
    sol_runs = 0
    for run, applied in runs:
        m, rows = requests(S, run['owner'])
        rl0 = next((r['rl'] for r in rows if r['rl']), None)
        gate = '-'
        if rl0:
            at = dt.datetime.fromisoformat(rows[0]['ts'].replace('Z', '+00:00'))
            meters = {'5h': {'used': rl0['pct'], 'resets_at': rl0['resets'], 'window_min': 300}}
            if rl0.get('week') is not None:
                meters['week'] = {'used': rl0['week'], 'resets_at': rl0['week_resets'], 'window_min': 10080}
                first = min((x for (mm, e) in S.values() for x in e if x[0] == 'rl' and x[2].get('week_resets')
                             and abs(x[2]['week_resets'] - rl0['week_resets']) <= 3600), key=lambda x: x[1], default=None)
                if first:
                    meters['day_start'] = {'used': first[2]['week'], 'at': first[1].replace('Z', '+00:00')}
            d = pace.decide(meters, at)
            gate = '%s (5h %.0f%%; week %s)' % (d['mode'], rl0['pct'], '%.0f spent today of %.1f, %.1f allowed by then' % (
                d['week']['spent_today'], d['week']['per_day'], d['week']['allowed_now']) if d.get('week') and d['week'].get('per_day') else '?')
        fresh = rows[min(6, len(rows) - 1)]['ctx']
        new, saved, moved = replay_run(rows, fresh)
        now = price(rows)
        rl = [x[2]['pct'] for x in S[run['owner']][1] if x[0] == 'rl']
        print('| %s %s | %.0f | %.1f | %d | %d | %.1f | %s |' % (run['slot'][11:16], (m['model'] or '').replace('gpt-6.1-', ''),
                                                             (rl[-1] - rl[0]) if rl else 0, now, saved, moved, new, gate))
        if m['model'] == 'gpt-6.1-sol':
            tot_now += now; tot_new += new; sol_runs += 1
    print('Sol runs: modeled now %.1f%%, replayed %.1f%% (%.1f%% less) over %d runs' % (tot_now, tot_new, 100 * (1 - tot_new / tot_now), sol_runs))


def weekly(S, runs, cost_cut):
    """Weekly points: measured per window, per application, and the projection under the new schedule."""
    account = os.environ.get('JOB_USAGE_ACCOUNT_ID')
    if not account:
        raise ValueError('set JOB_USAGE_ACCOUNT_ID for a weekly account report')
    recs = sorted((x[1], x[2]) for (m, e) in S.values() if m['account'] == account
                  for x in e if x[0] == 'rl' and x[2].get('week') is not None)
    spent, seg = 0.0, []
    for (t0, a), (t1, b) in zip(recs, recs[1:]):
        if b.get('week_resets') and a.get('week_resets') and abs(b['week_resets'] - a['week_resets']) <= 3600 and b['week'] >= a['week']:
            spent += b['week'] - a['week']
    sol = [(r, n) for r, n in runs if S[r['owner']][0]['model'] == 'gpt-6.1-sol']
    pct = 0.0
    for r, n in sol:
        rl = [x[2]['pct'] for x in S[r['owner']][1] if x[0] == 'rl']
        pct += (rl[-1] - rl[0]) if rl else 0
    apps = sum(n for r, n in sol)
    ratio = 0.16                                   # weekly points per 5-hour point (fit section: 0.155-0.18)
    per_app = pct * ratio / apps if apps else None
    print('weekly meter: %.0f points spent today across the day\'s windows (server resets excluded)' % spent)
    print('Sol runs since --since: %d runs, %d applications, %.0f%% of 5h measured = %.1f weekly points, %.2f points/application' % (
        len(sol), apps, pct, pct * ratio, per_app or 0))
    if per_app:
        print('one account, even pace (100 points/week): %.0f applications/week at today\'s cost per application' % (100 / per_app))
        new = per_app * (1 - cost_cut)
        print('projected cost/application %.2f points (-%.0f%%): %.0f applications/week; each extra account adds the same' % (
            new, 100 * cost_cut, 100 / new))


def main():
    global A_UNC, B_CAC
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', default=dt.datetime.now(ET).strftime('%Y/%m/%d'))
    ap.add_argument('--since', default='14:00')
    ap.add_argument('--section', default='all')
    ap.add_argument('--cost-cut', type=float, default=0.084,
                    help='projected cut in cost per application: 0.064 replayed (memory trim + 180k wrap-up) + ~0.02 '
                         'estimated from op-error examples (USAGE_OPTIMIZATION_2026-11-13.md)')
    a = ap.parse_args()
    S = load(a.day)
    if a.section in ('fit', 'all'):
        coef = fit(S)
        A_UNC, B_CAC = coef[0], coef[1]
        print('  price used below: %.2f%% per 1M uncached, %.3f%% per 1M cached input tokens\n' % (A_UNC * 1e6, B_CAC * 1e6))
    y, mo, d = a.day.split('/')
    since = dt.datetime.strptime('%s-%s-%s %s' % (y, mo, d, a.since), '%Y-%m-%d %H:%M').replace(tzinfo=ET).astimezone(dt.timezone.utc).isoformat()
    runs = production_runs(S, since)
    if a.section in ('categories', 'all'):
        categories(S, runs); print()
    if a.section in ('runs', 'all'):
        run_table(S, runs); print()
    if a.section in ('replay', 'all'):
        replay(S, runs)
    if a.section in ('weekly', 'all'):
        print()
        weekly(S, runs, a.cost_cut)


if __name__ == '__main__':
    main()
