"""Today-tab and tab hygiene, ported from the Claude worker's `tabs.py sweep` (run at the start of every mirror).

1. A job that needed him (Press Submit / Apply yourself / One click) and is more than 72 h past its posting
   becomes Stale, so it leaves the Today tab. (Codex never did this: Today still showed Nov 9-11 jobs.)
2. Given the open Brave tabs (from CUA), tabs whose jobs are all finished are returned to close, and so are
   duplicate tabs of the same page. Codex closes its own unmarked tabs at the end of a run; this covers the
   tabs it marked for him and his own.
3. A filled form that lost its tab (Awaiting submit, not expired, no submit in doubt) goes back to the fill
   queue once, noted "refilled once". It gets no queue priority (planner owner_go): only his "fill now" does.
4. Writes scratch/open-tabs.json (open job keys and sites with an application page open), which the planner
   uses, as Claude's plan.py did, to treat a site as signed in while his application page is open.

Limit (2026-11-13): Claude's tabs.py read and closed every Brave tab with AppleScript. Codex's WORKER.md forbids
AppleScript, and CUA lists only the current session's tabs, so tabs left by earlier runs are not closed here.
"""
import json
import os
import re
from .adapters import legacy
from .policy import ET


def tab_key(url, TB):
    """tabs.py norm, plus: an application/confirmation page counts as its posting, Paylocity Apply = details,
    and a Workday page is keyed by its requisition slug (the apply URL adds a locale and location path)."""
    u = re.sub(r'(?i)/jobs/apply/', '/jobs/details/', str(url or ''))                    # Paylocity
    u = re.sub(r'(?i)(joinhandshake\.com)/job-search/(\d+)', r'\1/jobs/\2', u)         # Handshake search pane
    u = re.sub(r'(?i)/(apply|application|confirmation)(/[^?#]*)?(?=[?#]|$)', '', u)
    n = TB.norm(u)
    if 'greenhouse.io/' in n and re.search(r'/jobs/\d+', n):
        n = n.replace('job-boards.greenhouse.io', 'boards.greenhouse.io').split('?')[0]
    if 'myworkdayjobs.com' in n and '/job/' in n:
        host, _, path = n.partition('/')
        n = host + '/' + path.split('?')[0].rstrip('/').split('/')[-1]
    return n


def sweep(store, owner, token, tabs=None, complete=False):
    """complete=True only when `tabs` is every Brave tab, not just this session's CUA tabs: a lost-form
    requeue on a partial list would refill forms that are still open in his Brave."""
    T = legacy('tracker'); TB = legacy('tabs')
    now = store.clock().astimezone(ET)
    stamp = now.strftime('%Y-%m-%d %H:%M')
    out = {'expired': [], 'close': [], 'duplicates': [], 'requeued': [], 'tabs_checked': tabs is not None}
    with store.tx():
        store.check(owner, token)
        for j in [dict(r) for r in store.con.execute('SELECT * FROM jobs')]:
            if T.today_section(j) and T.is_expired(j, now):
                store.con.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE job_key=?',
                                  ('Stale', f'not done within {T.TODAY_HOURS} h of posting; dropped from Today {stamp}', store.now(), j['job_key']))
                store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                                  (store.now(), 'job-set', j['job_key'], json.dumps({'status': 'Stale', 'by': 'sweep'})))
                out['expired'].append(j['company'])
        if tabs is None:
            return out
        jobs = {r['job_key']: dict(r) for r in store.con.execute('SELECT * FROM jobs')}
        index = {}
        for k, j in jobs.items():
            for u in (j['link'], T.form_url(j['notes'])):
                n = tab_key(u, TB)
                if n:
                    index.setdefault(n, set()).add(k)
        open_keys, seen_urls = set(), set()
        for t in tabs:
            tid, url = t.get('id'), str(t.get('url') or '')
            n = tab_key(url, TB)
            keys = index.get(n, set()) if n else set()
            if not keys:
                continue                                   # not a job page: never his own tabs (sheet, mail, chat)
            if n in seen_urls:
                out['duplicates'].append(tid)
                continue
            seen_urls.add(n)
            if all(jobs[k]['status'] in T.FINAL for k in keys):
                out['close'].append(tid)
            else:
                open_keys |= keys
        for k, j in jobs.items():
            n = j['notes'] or ''
            if not complete or j['status'] != 'Awaiting submit' or k in open_keys or T.is_expired(j, now):
                continue
            if re.search(r'submitting \d|submit result unknown|refilled once', n):
                continue
            store.con.execute('UPDATE jobs SET status=?,notes=?,updated_at=? WHERE job_key=?',
                              ('Ready to fill', f'form tab gone {stamp}: refilled once; {n}'[:1900], store.now(), k))
            out['requeued'].append(j['company'])
    path = os.path.join(str(store.path.parent), 'scratch', 'open-tabs.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    portal_rows = [(0, t.get('id'), str(t.get('url') or '')) for t in tabs]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'at': stamp, 'open_job_keys': sorted(open_keys), 'portal_tabs': TB.portal_tabs(portal_rows)}, f)
    return out
