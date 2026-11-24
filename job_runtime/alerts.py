"""Phone alerts the moment something needs the owner, ported from the Claude coordinator's
"TELL HIM AT ONCE" rules (owner 2026-11-12: "it fails if it doesn't even give me notifications about,
hey, we didn't finish this").

Messages go to the same outbox as stopgap-tools/notify.py: ~/Library/Application Support/job-watchdog/outbox,
which the local.jobtools.job-notify launchd agent texts to his phone (works even when the model is down).
The same key is sent at most once per 12 hours, shared with notify.py's sent-keys.json, so calling this after
every stage and again at finish never repeats a message.

Kinds (Claude wording):
  Finish this:      a form or Easy Apply left for him (handoff), key = job_key
  Sign in needed:   an account site moved to "Apply yourself", key = job_key
  <site> is blocked: a stage stopped on a login/CAPTCHA/rate-limit page, key = blocked <site> <date>
  <stage> did not finish: a failed stage, key = fail <stage> <date> <hour>
More than 4 of one kind in a call: one message with the first 4 and "+n more in the Today tab".
"""
import datetime as dt
import hashlib
import json
import os
import re
from .adapters import legacy
from .policy import ET, timestamp

NOT_FOR_HIM = re.compile(r'outside ?24|older than|freshness|duplicate|already applied|closed|repost|not eligible|'
                         r'time[ds]? ?out|timed out|chooser|crash|network|create account|sign ?in|log ?in', re.I)


REAL_BOX = os.path.expanduser('~/Library/Application Support/job-watchdog')


def box(store=None):
    """The real phone outbox only for the live tracker (its private root holds codex-migration/worker-location.json
    naming itself). Tests and dry-run copies write to their own scratch folder and never text his phone."""
    if os.environ.get('JOB_ALERT_BOX'):
        return os.environ['JOB_ALERT_BOX']
    if store is not None:
        root = os.path.realpath(str(store.path.parent))
        try:
            with open(os.path.join(root, 'codex-migration', 'worker-location.json')) as f:
                loc = json.load(f)
            if os.path.realpath(loc.get('private_root', '')) == root:
                return REAL_BOX
        except (OSError, ValueError):
            pass
        return os.path.join(root, 'scratch', 'alerts-outbox')
    return os.path.join(os.getcwd(), 'alerts-outbox-unbound')


def queue_phone(msg, key, hours=12.0, now=None, root=None):
    """notify.py's logic without its stdout: outbox file + shared 12 h de-duplication."""
    root = root or box(); out = os.path.join(root, 'outbox'); keys = os.path.join(root, 'sent-keys.json')
    now = now or dt.datetime.now()
    os.makedirs(out, exist_ok=True)
    try:
        with open(keys) as f:
            sent = json.load(f)
    except (OSError, ValueError):
        sent = {}
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    last = sent.get(h)
    if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < hours * 3600:
        return False
    sent[h] = now.isoformat(timespec='seconds')
    cutoff = now - dt.timedelta(days=3)
    sent = {k: v for k, v in sent.items() if dt.datetime.fromisoformat(v) > cutoff}
    tmp = keys + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sent, f)
    os.replace(tmp, keys)
    name = now.strftime('%Y%m%d-%H%M%S-') + h[:6] + '.txt'
    staged = os.path.join(root, '.' + name)
    with open(staged, 'w', encoding='utf-8') as f:
        f.write(re.sub(r'[\r\n]+', ' ', ' '.join(msg.split()))[:600])
    os.replace(staged, os.path.join(out, name))
    return True


def _short(text, n=80):
    t = ' '.join(str(text or '').split()).rstrip('. ')
    return t if len(t) <= n else t[:n - 1].rstrip() + '…'


def _job(store, key):
    r = store.con.execute('SELECT company,title,link FROM jobs WHERE job_key=?', (key,)).fetchone()
    return dict(r) if r else None


def collect(store, run_id):
    """What this run has to tell him, from the database (never from the model's memory)."""
    run = store.con.execute('SELECT * FROM jr_runs WHERE id=?', (run_id,)).fetchone()
    if not run:
        return {}
    started = timestamp(run['started'])
    now = store.clock().astimezone(ET)
    out = {'finish': [], 'signin': [], 'blocked': [], 'failed': []}
    seen = set()
    for r in store.con.execute("SELECT provider,item_key,payload,first_seen FROM jr_backlog WHERE provider IN ('application_handoff','fill') AND state='pending'"):
        first = timestamp(r['first_seen'])
        if not first or not started or first < started:
            continue
        try:
            p = json.loads(r['payload'] or '{}')
        except ValueError:
            p = {}
        key = p.get('job_key') or r['item_key']
        reason = p.get('handoff') or p.get('reason') or 'needs you'
        if NOT_FOR_HIM.search(str(reason)) or key in seen:
            continue
        j = _job(store, key)
        if not j:
            continue
        seen.add(key)
        out['finish'].append({'key': key, 'text': f"[Jobs] Finish this: {j['company']} - {j['title']}. {_short(reason)}. Tab is open in Brave; link in Today tab."})
    P = legacy('portals')
    for r in store.con.execute("SELECT key FROM events WHERE kind='codex-account-site' AND at>=?", (run['started'],)):
        j = _job(store, r['key'])
        if j and r['key'] not in seen:
            seen.add(r['key'])
            site = P.label(P.portal_of(j['link']), j['company'])
            out['signin'].append({'key': r['key'], 'text': f"[Jobs] Sign in needed: {j['company']} - {j['title']} on {site}. Sign in once in Brave (it fills your password); the agent applies within 20 min."})
    for a in store.con.execute('SELECT stage,status,result FROM jr_actions WHERE run_id=?', (run_id,)):
        try:
            res = json.loads(a['result'] or '{}')
        except ValueError:
            res = {}
        if not isinstance(res, dict):
            res = {}
        # "<site> is blocked" is for a whole SITE behind a login/CAPTCHA/rate limit (Claude's rule), i.e. the
        # discovery stages. A form or one Easy Apply that stopped is a per-job "Finish this", never "fill is blocked".
        if res.get('blocked') and a['stage'] in ('linkedin', 'handshake'):
            site = {'linkedin': 'LinkedIn', 'handshake': 'Handshake'}[a['stage']]
            out['blocked'].append({'key': f'blocked {site} {now:%Y-%m-%d}',
                                   'text': f"[Jobs] {site} is blocked: {_short(res['blocked'], 70)}. Nothing is applied there until you fix it."})
        elif a['status'] == 'failed':
            why = res.get('reason') or res.get('error') or 'stage failed'
            out['failed'].append({'key': f"fail {a['stage']} {now:%Y-%m-%d} {now:%H}",
                                  'text': f"[Jobs] {a['stage']} did not finish this run: {_short(why, 70)}."})
    return out


def send(store, run_id, now=None):
    """Queue this run's alerts; returns what was queued. Safe to call after any stage and again at finish."""
    queued = []
    for kind, items in collect(store, run_id).items():
        if kind in ('finish', 'signin') and len(items) > 4:
            head = '; '.join(i['text'].split(': ', 1)[1].split('.')[0] for i in items[:4])
            label = 'Finish these' if kind == 'finish' else 'Sign in needed'
            run = store.con.execute('SELECT slot FROM jr_runs WHERE id=?', (run_id,)).fetchone()
            items = [{'key': f"batch {kind} {run['slot'] if run else run_id}",
                      'text': f"[Jobs] {label}: {head}; +{len(items) - 4} more in the Today tab."}]
        for i in items:
            if queue_phone(i['text'], i['key'], now=now, root=box(store)):
                queued.append(i['text'])
    return {'queued': queued}
