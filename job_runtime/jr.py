"""Run one leased application worker. Results are saved in its private run directory.

  jr gate                         Preview queue and available capacity
  jr begin SNAPSHOT.json          Start from a fresh dashboard snapshot
  jr brief                        Show remaining stages and queued openings
  jr stage STAGE STATUS JSON      Record a stage outcome and its evidence
  jr form_session JSON            Arm the grouped form driver for one opening
  jr prepare JSON                 Verify final review and attachment evidence
  jr submit_started JSON          Record the durable marker before an authorized click
  jr reconcile JSON               Record the observed outcome of that attempt
  jr finish '{}'                  Close an accounted-for run
  jr OPERATION [JSON|@FILE]       Run another operation under the current lease
"""
import datetime as dt
import glob
import json
import os
import re
import sys
from pathlib import Path
from . import pace
from .store import Denied
from .policy import digest, timestamp
from .quota import APPLICATION_BUDGET, APPLICATION_STARTUP, PRIVATE_ROOT, Quota, QuotaUnavailable
from .usage import read_live_meters

PRIVATE = Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or
               PRIVATE_ROOT)
os.environ['JOB_AUTOMATION_PRIVATE_ROOT'] = str(PRIVATE)
LIMIT = 2500
read_meters = read_live_meters
# Keep a bounded local worker memory file.
MEMORY = Path(os.environ.get('JR_AUTOMATION_MEMORY') or
              PRIVATE / 'codex-runtime' / 'memory.md')
MEMORY_KEEP_CHARS = 4000
MEMORY_HEAD = '(Older run notes: memory-archive.md in this folder. The database is the record; read it with codex/jr.)\n'
LEAN_DEFERS = ('handshake', 'linkedin')
# RUNBOOK "Finish without losing user edits": the mirror tabs and their sheet ids (Settings is read-only, not exported)
MIRROR_TABS = ('Today', 'Jobs', 'Applied', 'History', 'Recruiters', 'Companies', 'Rejected')


def mirror_sheet_ids():
    path = PRIVATE / 'dashboard-sheet-ids.json'
    if not path.is_file():
        raise Denied('provide sheet_ids or configure dashboard-sheet-ids.json in the private directory')
    ids = json.loads(path.read_text())
    if (not isinstance(ids, dict) or set(ids) != set(MIRROR_TABS)
            or any(type(v) is not int or v < 0 for v in ids.values()) or len(set(ids.values())) != len(ids)):
        raise Denied('invalid dashboard sheet IDs')
    return ids
MIRROR_READBACK_MAX_AGE = 60
# One example per op whose evidence shape runs got wrong on 11-13 (each miss cost 2-6 calls of source reading)
OP_EXAMPLES = {
    'prepare': '{"job_key":"<jobs.job_key>","channel":"one_click|form","resume_hash":"<sha256 of the attached PDF>",'
               '"review":{"refs":["<posting url>"],"requirements_verified":true}}  (one_click = LinkedIn Easy Apply or '
               'Handshake Quick Apply; form = employer site). Needs `codex/jr fresh <5-range file>` within 60 s before.',
    'submit_started': '{"attempt_id":"<from prepare>","resume_hash":"<same sha256>","evidence":{"refs":["<form url>"],'
                      '"attachment_verified":true,"answers_verified":true,"answer_evidence":[{"field":"<label>","value":"<entered>",'
                      '"refs":["<form url>"],"kind":"literal","source":"form-answers.md","quote":"<saved line>"}]}}  Include attachment:{job_key,refs,method:"browser_file",observed_name,observed_bytes,observed_sha256}. Kinds: literal '
                      '(source+quote), rule (rule name), plain (meaning), prefilled (observed_value, preserved:true). Needs '
                      '`codex/jr fresh <file>` within 60 s before; then ONE click.',
    'reconcile': '{"attempt_id":"...","outcome":"confirmed|not_submitted|unknown","evidence":{"refs":["<page url>"],'
                 '"observed_at":"<ISO time>"}}  For confirmation use the full evidence returned by form_evidence. '
                 'Explicit non-submission requires a matching opening and an observed application failure.',
    'job_closed': '{"job_key":"...","evidence":{"refs":["<page url>"],"quote":"No longer accepting applications"}}',
    'notify': '{"dedup":"<stable key>","payload":{"text":"<message>"}}',
    'mirror_prepare': '@<file with {"snapshots":<grouped dashboard read>,"summary":"<one line>"}>',
    'mirror_verify': '@<file with {"snapshots":<grouped dashboard read>,"expected":<saved plan expected>,"batches_applied":0,"read_at":"<ISO time>"}>',
}
DISCOVERY_MINUTES = 5          # RUNBOOK: discovery gets about 5 minutes per source
BLOCKED_SITE = re.compile(r'login|log in|sign.?in|logged out|captcha|unusual activity|rate.?limit|too many requests|429|'
                          r'usage limit|checkpoint|not reachable|browser (is )?(down|closed)', re.I)
BROWSER_STAGES = ('handshake', 'linkedin', 'one_click', 'fill')
OWNERSHIP = re.compile(r'already part of browser session|another (active )?(chat|session)|tab ownership|browser ownership|'
                       r'owned by (another|other)|belongs to (another|other)|saved (browser )?(tabs?|session)', re.I)


def compact(x, depth=0):
    if isinstance(x, dict):
        return {k: compact(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        head = [compact(v, depth + 1) for v in list(x)[:5]]
        return head + (['... +%d more' % (len(x) - 5)] if len(x) > 5 else [])
    if isinstance(x, str) and len(x) > 200:
        return x[:200] + '...'
    return x


def emit(result, full_path=None):
    text = json.dumps(compact(result), default=str, ensure_ascii=False)
    if len(text) > LIMIT:
        text = text[:LIMIT] + '...'
    if full_path:
        text += '\n(full: %s)' % full_path
    print(text)


def sheets(d):
    """{sheet: rows} from any shape a run saves: Promise.allSettled results (with or without sheet_name),
    {sheet: rows}, {sheet: {values}}, or grouped CellData with a separate Settings read."""
    snaps = d.get('snapshots', d) if isinstance(d, dict) else d
    out = {}
    def rows(v):
        v = v.get('value', v) if isinstance(v, dict) and 'status' in v else v
        v = (v.get('result') or v) if isinstance(v, dict) and 'result' in v else v
        v = v.get('structuredContent', v) if isinstance(v, dict) else v
        return v.get('values', v) if isinstance(v, dict) else v
    if isinstance(snaps, dict) and ('sheets' in snaps or 'sheets' in (snaps.get('structuredContent') or {})):
        from .reporting import grid_snapshots
        out = grid_snapshots(snaps)
        if 'settings' in d:
            out['Settings'] = rows(d['settings'])
    elif isinstance(snaps, list):
        for r in snaps:
            v = r.get('value', r) if isinstance(r, dict) else r
            name = v.get('sheet_name') if isinstance(v, dict) else None
            sc = rows(r)
            if not name and isinstance(v, dict):
                rng = ((v.get('result') or v).get('structuredContent') or (v.get('result') or v) or {}).get('range', '')
                name = str(rng).split('!')[0].strip("'")
            if name:
                out[name] = sc
    else:
        for k, v in snaps.items():
            out[k] = rows(v)
    missing = [k for k in ('Settings', 'Today', 'Jobs', 'Recruiters', 'Companies') if not isinstance(out.get(k), list)]
    if missing:
        raise ValueError('snapshot file lacks rows for: ' + ', '.join(missing))
    return out


def store():
    from .store import Store
    return Store(str(PRIVATE / 'tracker.sqlite'))


def open_run(s):
    """The run this worker holds: newest run.json whose run has not ended."""
    files = sorted(glob.glob(str(PRIVATE / 'codex-runtime' / 'runs' / '*' / 'run.json')), key=os.path.getmtime, reverse=True)
    owner = os.environ.get('CODEX_THREAD_ID')
    for f in files[:20]:
        try:
            state = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        if owner and state.get('owner') != owner:
            continue
        row = s.con.execute('SELECT ended FROM jr_runs WHERE id=?', (state.get('run_id'),)).fetchone()
        if row is not None and not row['ended']:
            return state
    return None


def save(state, op, result):
    if not state:
        return None
    d = PRIVATE / 'codex-runtime' / 'runs' / state['run_id'] / 'ops'
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob('*.json'))) + 1
    p = d / ('%03d-%s.json' % (n, op))
    p.write_text(json.dumps(result, default=str, ensure_ascii=False, indent=1))
    return str(p)


def usage(meters=None):
    if meters is None:
        meters = read_meters()
    out = {}
    for name in ('5h', 'week'):
        if name in meters:
            out[name + '_used_pct'] = meters[name]['used']
            out[name + '_resets'] = dt.datetime.fromtimestamp(meters[name]['resets_at'], pace.ET).strftime('%a %H:%M')
    return out


def worker_quota(s):
    return Quota(PRIVATE, meter=read_meters, clock=lambda: s.clock().timestamp())


def quota_reservation(s, state):
    row = s.con.execute('SELECT value FROM jr_meta WHERE key=?', ('quota:' + state['run_id'],)).fetchone()
    return row['value'] if row else None


def quota_status(s, state, meters=None, *, applications=False):
    rid = quota_reservation(s, state)
    if not rid:
        return {}  # a previously started run may still close cleanly
    try:
        ledger=worker_quota(s)
        if applications and s.codex_production():
            return ledger.check_ready(rid,reading=meters)
        return ledger.check(rid,reading=meters)
    except QuotaUnavailable as exc:
        return {'why': str(exc), 'wrap_up': True}


def start_quota(s, owner, *, reserve=True, queue=None):
    meters = read_meters()
    d = {'mode': 'skip', 'why': 'verified current weekly Codex meter unavailable'}
    rid = None
    try:
        q = worker_quota(s)
        meters = q.pace_reading(meters)
        ready, reviews = readiness(s) if queue is None else queue
        d = pace.decide(meters, s.clock(), ready + reviews)
        if reviews and not ready and d['mode'] == 'lean':
            d.update(mode='skip',why='only optional qualification reviews remain while account usage is ahead of pace')
        d.update(ready_openings=ready,qualification_reviews=reviews)
        if d['mode'] != 'skip':
            if not owner:
                raise QuotaUnavailable('worker session identity unavailable')
            rid = q.begin('application', budget=APPLICATION_BUDGET, startup=APPLICATION_STARTUP,
                          owner=owner, reading=meters,reserve=reserve)
    except QuotaUnavailable as exc:
        d.update(mode='skip', why=str(exc))
    return d, meters, rid


def trim_memory(path=None, keep=None):
    """Keep the automation memory file short. Entries start with a date; the newest ones that fit in `keep`
    characters stay, older ones are appended unchanged to memory-archive.md beside it. Never raises."""
    p = Path(path or MEMORY)
    keep = keep or MEMORY_KEEP_CHARS
    try:
        text = p.read_text()
    except OSError:
        return None
    body = text[len(MEMORY_HEAD):] if text.startswith(MEMORY_HEAD) else text
    if len(body) <= keep:
        return {'trimmed': 0}
    entries = re.split(r'\n(?=\d{4}-\d{2}-\d{2}[T ])', body)
    kept, size = [], 0
    for e in reversed(entries):
        if kept and size + len(e) + 1 > keep:
            break
        kept.append(e)
        size += len(e) + 1
    kept.reverse()
    old = entries[:len(entries) - len(kept)]
    if not old:
        return {'trimmed': 0}
    try:
        archive = p.with_name('memory-archive.md')
        with open(archive, 'a') as f:
            f.write(('\n' if archive.exists() and archive.stat().st_size else '') + '\n'.join(old).rstrip('\n') + '\n')
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(MEMORY_HEAD + '\n'.join(kept))
        os.replace(tmp, p)
    except OSError:
        return None
    return {'trimmed': len(old), 'kept': len(kept), 'archive': str(archive)}


def skip_record(d, owner=None):
    """A scheduled-skips record like the connector-blocked ones, so the watchdog names the cause."""
    folder = PRIVATE / 'codex-runtime' / 'scheduled-skips'
    folder.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    f = folder / ('%s-usage-pace.json' % now.strftime('%Y-%m-%dT%H%M%SZ'))
    f.write_text(json.dumps({'at': now.isoformat(), 'automation_id': 'job-applications-local-shadow', 'owner': owner,
                             'blocker': 'ChatGPT usage limit pace gate: ' + d['why'], 'pace': pace.summary(d),
                             'lease_acquired': False, 'cycle_started': False, 'submissions': 0, 'learning_issue':'usage_pace',
                             'next_action': 'Preserve deterministic feed and receipt work. Recheck allowance next run.'}, indent=2))
    return str(f)


def run_pace(s, run_id):
    from .coordinator import run_pace as stored
    return stored(s, run_id)


def record_pace(s, state, d, meters):
    """Save the run's pace (stop hook and finish read it); a lean run defers browser discovery at once, with the
    reason, so no model call is spent on it. Its card queues stay in jr_backlog for the next normal run."""
    rid = state['run_id']
    s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)', ('pace:' + rid, json.dumps(
        {'mode': d['mode'], 'why': d['why'], 'at': s.now(), 'meters': {k: meters.get(k) for k in ('5h', 'week') if meters.get(k)}})))
    s.con.commit()
    deferred = []
    rollout=s.path.parent/'codex-runtime/first-live-slot.json'
    first=rollout.exists() and json.loads(rollout.read_text()).get('slot')==state.get('slot')
    if d['mode'] != 'lean' or first:
        return deferred
    from .operations import record_stage
    from .cards import queue_size
    for stage in LEAN_DEFERS:
        if stage in state.get('expected', []):
            record_stage(s, rid, stage, state['owner'], state['token'], 'deferred',
                         {'refs': ['usage pace gate (job_runtime/pace.py): ' + d['why']], 'pace': pace.summary(d),
                          'reason': 'lean run: discovery deferred so the allowance goes to ready applications; the card '
                                    'queue stays in jr_backlog', 'queue_left': queue_size(s, stage), 'observed_at': s.now()})
            deferred.append(stage)
    return deferred


def past_cap(s, state):
    """The stop hook's wrap-up point (run time cap or context cap) is reached: open stages may be deferred."""
    from .stop_hook import RUN_CAP_MINUTES
    from .policy import timestamp
    row = s.con.execute('SELECT started FROM jr_runs WHERE id=?', (state['run_id'],)).fetchone()
    started = timestamp(row['started']) if row else None
    if started and (s.clock() - started).total_seconds() / 60 >= RUN_CAP_MINUTES.get(state.get('mode'), 35):
        return True
    ctx = pace.session_context(state.get('owner'))
    return bool(ctx and ctx >= pace.CONTEXT_WRAP_TOKENS)


def pending_receipts(store):
    return [dict(r) for r in store.con.execute("""SELECT a.id AS attempt_id,j.company,j.title,j.link
               FROM jr_attempts a JOIN jobs j ON j.job_key=a.job_key WHERE a.state='submit_started' ORDER BY a.updated""")]


def fresh_readiness(store, owner, packet):
    """Apply fresh Dashboard commands to an isolated in-memory snapshot before reserving."""
    from copy import copy
    from contextlib import closing
    import sqlite3
    from .adapters import import_commands

    preview = copy(store)
    with closing(sqlite3.connect(':memory:',isolation_level=None)) as db:
        store.con.backup(db)
        db.row_factory = sqlite3.Row
        preview.con = db
        from .operations import take_over_dead_lease
        take_over_dead_lease(preview)
        token = preview.acquire(owner)
        import_commands(preview,packet,owner,token)
        return readiness(preview)


def readiness(s):
    """Count eligible routes and optional reviews before opening any reservation."""
    from . import canary
    from .planner import plan
    from .store import Denied
    cohort = canary.state(s)
    if s.codex_production():
        try:
            canary.assert_owner(s)
        except Denied:
            return 0,0
        if cohort['phase'] == 'waiting' and not canary.rollout_ready(s):
            return 0,0
    if cohort['phase'] == 'halted' or s.con.execute("SELECT 1 FROM jr_attempts WHERE state='submit_started' LIMIT 1").fetchone():
        return 0, 0
    policy = s.policy_snapshot(max_age=10 ** 9)
    queue = plan(s,policy,'FULL')
    pending = {r['item_key']:json.loads(r['payload'] or '{}') for r in s.con.execute(
        "SELECT item_key,payload FROM jr_backlog WHERE provider='requirements' AND state='pending'")}
    day = s.clock().astimezone(pace.ET).date().isoformat()
    ready = reviews = 0
    for stage in ('to_fill','to_signin','to_one_click','to_match'):
        for key in queue[stage]:
            job = s.job(key)
            channel = 'one_click' if stage == 'to_one_click' or re.search('easy apply only|quick apply only',job.get('notes') or '',re.I) else 'form'
            try:
                s._budget(job,policy,day,channel,None)
            except Denied:
                continue
            if stage == 'to_match':
                data = pending.get(key,{})
                if not data.get('description_complete') or not Path(data.get('description_file') or '').is_file():
                    continue
                if not data.get('verified_review'):
                    reviews += 1
                    continue
            ready += 1
    return ready,reviews


def brief(s, state):
    from .planner import plan
    from .cards import queue_size, budget_left
    if not state:
        return {'run': None, 'usage': usage()}
    q = plan(s, state['policy'], state['mode'])

    def jobs(keys, n=6):
        out = []
        for k in keys[:n]:
            k = k['job_key'] if isinstance(k, dict) else k
            try:
                j = s.job(k)
            except Exception:
                continue
            out.append({'job_key': k, 'link': j['link'], 'company': j['company'], 'title': (j['title'] or '')[:60], 'resume': j['resume'],
                        'note': (j['notes'] or '')[:90]})
        return out
    done = {r['stage']: r['status'] for r in s.con.execute('SELECT stage,status FROM jr_actions WHERE run_id=?', (state['run_id'],))}
    stored = run_pace(s, state['run_id'])
    meters = read_meters()
    use = usage(meters)
    quota = quota_status(s, state, meters,applications=True)
    lean = pace.live_mode(stored, meters if not quota.get('wrap_up') else None, s.clock())
    if lean=='run' and quota.get('ready_only'):
        lean='lean'
    ctx = pace.session_context(state.get('owner'))
    return {'run_id': state['run_id'], 'mode': state['mode'],
            'pace': lean + ('' if lean == 'run' else ': apply to ready jobs first; no card reading'),
            'context_k': ctx // 1000 if ctx else None,
            'wrap_up_now': bool(quota.get('wrap_up') or (ctx and ctx >= pace.CONTEXT_WRAP_TOKENS)),
            'quota': quota,
            'stages_left': [x for x in state.get('expected', []) if x not in done],
            'to_one_click': jobs(q['to_one_click'], 8), 'to_fill': jobs(q['to_fill'], 4),
            'to_match': len(q['to_match']), 'walled_to_open': jobs(q['walled_to_open'], 4),
            'to_signin': jobs(q.get('to_signin', []), 2), 'signin': q.get('signin', {}),
            'cards': {src: {'queue': queue_size(s, src), 'cap_left': budget_left(s, src)} for src in ('linkedin', 'handshake')},
            'rules': ['browser tabs: open your own with await cua.createBrowserTab("1", url); never getTab an old id; a '
                      '"tab already part of browser session" error is not a blocker',
                      'do not end the turn while stages_left or queues are non-empty; finish only after the work',
                      'print little; use jr for runtime steps'],
            'usage': use}


def mirror_record(s, key):
    row = s.con.execute('SELECT value FROM jr_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else {}


def verify_mirror_batch(s, key, q, dispatch):
    from .store import Denied
    with s.tx():
        active = mirror_record(s, key)
        if not active or active['hash'] != digest(q.get('expected')):
            raise Denied('mirror verification requires the latest prepared plan')
        applied = q.get('batches_applied', active['batches'])
        if type(applied) is not int or applied < 0 or applied not in (active['checked'], active['checked'] + 1):
            raise Denied('verify the mirror preflight and each batch in order')
        observed = timestamp(q.get('read_at'))
        prepared = timestamp(active['prepared_at'])
        if not observed or observed < prepared or not 0 <= (s.clock() - observed).total_seconds() <= MIRROR_READBACK_MAX_AGE:
            raise Denied('fresh grouped dashboard read timestamp required')
        result = dispatch(s, q)
        active['verified_at'] = None
        if result['ok']:
            active['checked'] = applied
            if applied == active['batches']:
                active['verified_at'] = observed.isoformat()
                from .learning import verified
                verified(s,'dashboard_rows','stage/mirror','All planned dashboard cells passed the live Drive readback.')
        else:
            active['checked'] = min(active['checked'], applied - 1)
        s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES (?,?)', (key, json.dumps(active)))
        return result


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    from .store import Denied
    cmd, rest = argv[0], argv[1:]
    s = store()
    starter = None
    state = None
    q = {}
    try:
        state = open_run(s)
        if cmd == 'pending_receipts':
            emit(pending_receipts(s))
            return 0
        if cmd == 'reconcile_receipt':
            # This short lease permits exactly one observation of a previously marked
            # click. It never returns a token or starts an application run.
            from .operations import take_over_dead_lease
            owner = os.environ.get('CODEX_THREAD_ID')
            if not owner or not rest or not rest[0].startswith('@'):
                raise Denied('real chat ID and observed receipt evidence file required')
            case = json.loads(Path(rest[0][1:]).read_text())
            if case['outcome'] not in ('confirmed','not_submitted','unknown'):
                raise Denied('invalid receipt outcome')
            take_over_dead_lease(s)
            token = s.acquire(owner)
            try:
                row = s.con.execute("SELECT state FROM jr_attempts WHERE id=?",(case['attempt_id'],)).fetchone()
                if not row or row['state']!='submit_started':
                    raise Denied('a pending durable submission marker is required')
                s.reconcile(owner,token,case['attempt_id'],case['outcome'],case['evidence'])
            finally:
                s.release(owner,token)
            emit({'attempt_id':case['attempt_id'],'outcome':case['outcome']})
            return 0
        if cmd == 'daily':
            from .shortlist import save_daily
            emit(save_daily(s))
            return 0
        if cmd == 'usage':
            emit(usage())
            return 0
        if cmd == 'brief':
            emit(brief(s, state))
            return 0
        if cmd == 'gate':                      # the run's first call: is there allowance for a run at all?
            owner = os.environ.get('CODEX_THREAD_ID')
            from . import learning
            from .canary import release_expired
            from .operations import take_over_dead_lease
            take_over_dead_lease(s)
            release_expired(s)
            from .canary import expand_settled
            expand_settled(s)
            if learning.capture_unfinished(s):
                learning.export(s)
            d, _, _ = start_quota(s, owner,reserve=not s.codex_production())
            out = pace.summary(d)
            if d['mode'] == 'skip':
                out['skip_record'] = skip_record(d, owner)
                if s.codex_production():
                    from .shortlist import save_daily
                    out['daily'] = save_daily(s)
                    out['pending_receipts'] = pending_receipts(s)
                out['learning']=learning.export(s)
                out['next'] = ('SKIP funded work. Preserve a deterministic public feed snapshot and review already saved '
                               'exact receipts as described in the automation prompt. No application clicks or new '
                               'model judgments. Report the saved discovery and the pace reason.')
            elif d['mode'] == 'lean':
                out['next'] = ('LEAN: start normally (sheets, then jr begin). begin defers LinkedIn/Handshake discovery; '
                               'apply to the ready Easy Apply jobs and forms first, then audit, mirror, finish.')
            else:
                out['next'] = 'RUN: start normally (sheets, then jr begin).'
            trim_memory()                        # skip runs never reach begin; the next run reads a short memory.md
            emit(out)
            return 0
        if cmd == 'begin':
            from .operations import packet, begin_cycle
            from .adapters import check_command_packet
            snap_file = Path(rest[0])
            d = json.loads(snap_file.read_text())
            snaps = sheets(d)
            owner = os.environ.get('CODEX_THREAD_ID') or (rest[1] if len(rest) > 1 else None)
            if state and state.get('owner') == owner:
                emit(brief(s, state))
                return 0
            queue = None
            command_packet = None
            if s.codex_production():
                command_packet = packet(snaps,(d.get('read_at') or d.get('observed_at')) if isinstance(d,dict) else None,
                                        (PRIVATE/'rubric-overrides.md').read_text(),str(snap_file))
                check_command_packet(s,command_packet)
                queue = fresh_readiness(s,owner,command_packet)
            gate, meters, starter = start_quota(s, owner, queue=queue)
            if gate['mode'] == 'skip':           # code-enforced: no run starts that would die at the limit
                raise Denied('usage pace SKIP (%s): no funded run started. Preserve deterministic discovery and '
                             'saved history for the next funded worker. Skip record: %s' % (gate['why'], skip_record(gate, owner)))
            try:
                command_packet=command_packet or packet(snaps,(d.get('read_at') or d.get('observed_at')) if isinstance(d,dict) else None,
                                      (PRIVATE/'rubric-overrides.md').read_text(),str(snap_file))
                check_command_packet(s,command_packet)
                state = begin_cycle(s, owner, command_packet)
            except Denied as error:
                if str(error) in ('fresh command packet required','all editable tabs required') or str(error).startswith('missing command headers:'):
                    starter=None  # The same chat can reuse its existing hold with a fresh dashboard read.
                raise
            s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES (?,?)', ('quota:' + state['run_id'], starter))
            from .learning import startup_succeeded
            startup_succeeded(s,owner,state['run_id'])
            s.con.commit()
            starter = None
            deferred = record_pace(s, state, gate, meters)
            trim_memory()
            out = {'run_id': state['run_id'], 'mode': state['mode'], 'slot': state['slot'], 'pace': pace.summary(gate)}
            if deferred:
                out['deferred_by_pace'] = deferred
            out['brief'] = brief(s, state)
            emit(out)
            return 0
        if not state:
            raise Denied('no open run: call `jr begin <snapshot.json>` first')
        if cmd == 'quota':
            emit(quota_status(s, state,applications=True) or {'wrap_up': True, 'why': 'worker reservation unavailable'})
            return 0
        ready_only=False
        if cmd in ('cards_next', 'match_batch_prepare', 'prepare', 'submit_started', 'account_begin',
                   'account_register_started', 'learning_fix'):
            available = quota_status(s,state,applications=cmd not in ('cards_next','learning_fix'))
            ready_only=available.get('ready_only',False)
            if not available or available.get('wrap_up'):
                raise Denied('usage limit: %s. Close the current stages and finish.' %
                             available.get('why', 'worker reservation unavailable'))
        if cmd == 'fresh':                     # fresh editable-tab read before a final submit or the mirror
            from .operations import packet
            from .adapters import import_commands
            snap_file = Path(rest[0])
            d = json.loads(snap_file.read_text())
            snaps = sheets(d)
            r = import_commands(s, packet(snaps, d.get('read_at') or d.get('observed_at'),
                                          (PRIVATE / 'rubric-overrides.md').read_text(), str(snap_file)), state['owner'], state['token'])
            emit({'imported': True, 'policy_hash': (r.get('policy') or {}).get('hash')}, save(state, 'fresh', r))
            return 0
        mirror_key = 'mirror_plan:' + state['run_id']
        if cmd == 'stage':
            from .operations import record_stage
            ev = json.loads(rest[2]) if len(rest) > 2 else {}
            if rest[:2] == ['mirror', 'succeeded']:
                active = mirror_record(s, mirror_key)
                if active or quota_reservation(s, state):
                    verified = timestamp(active.get('verified_at'))
                    if not verified or not 0 <= (s.clock() - verified).total_seconds() <= MIRROR_READBACK_MAX_AGE:
                        raise Denied('current final mirror readback required before recording success')
                    ev['mirror_readback'] = {'verified_at': active['verified_at'], 'batches': active['batches'],
                                             'plan_hash': active['hash']}
            lean = rest[0] in ('handshake', 'linkedin') and ('usage limit' in json.dumps(ev)
                                                             or past_cap(s, state)
                                                             or run_pace(s, state['run_id']).get('mode') == 'lean')
            if (rest[0] in ('handshake', 'linkedin') and rest[1] in ('deferred', 'failed') and not lean
                    and not BLOCKED_SITE.search(json.dumps(ev))):
                from .cards import queue_size, budget_left
                left, cap = queue_size(s, rest[0]), budget_left(s, rest[0])
                row = s.con.execute('SELECT value FROM jr_meta WHERE key=?', ('discovery_start:%s:%s' % (state['run_id'], rest[0]),)).fetchone()
                used = (s.clock() - timestamp(row['value'])).total_seconds() / 60 if row else 0
                if left and cap and used < DISCOVERY_MINUTES:
                    raise Denied('%d %s cards are still queued and only %.1f of %d discovery minutes are used, so this stage is not '
                                 'deferred yet. Read the next batch: `codex/jr cards_next \'{"source":"%s"}\'`, then for EACH card navigate '
                                 'your own tab to its url and read it with the cards_js page_js (a domSnapshot is not the posting text), then '
                                 '`codex/jr card_commit`. Loop in as few calls as you can; defer only on a login/CAPTCHA/rate-limit page.'
                                 % (left, rest[0], used, DISCOVERY_MINUTES, rest[0]))
            if rest[0] in BROWSER_STAGES and rest[1] in ('deferred', 'failed') and OWNERSHIP.search(json.dumps(ev)):
                raise Denied('a tab-ownership error is not a reason to skip %s: that tab belongs to another session, so open your '
                             'own with `await cua.createBrowserTab("1", url)` and do the work there; record this stage only after you '
                             'tried a new tab' % rest[0])
            r = record_stage(s, state['run_id'], rest[0], state['owner'], state['token'], rest[1], ev)
            emit(r, save(state, 'stage-' + rest[0], r))
            return 0
        if cmd == 'resume_sync':               # deterministic stage: copy his resume folder unchanged, record the stage
            import subprocess
            root = Path(__file__).resolve().parents[1]
            r = subprocess.run([sys.executable, str(root / 'stopgap-tools' / 'resume_sync.py'), '--apply'],
                               capture_output=True, text=True, timeout=300, cwd=str(root / 'stopgap-tools'))
            try:
                rep = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                rep = {'error': (r.stderr or r.stdout)[-300:]}
            from .operations import record_stage
            ok = r.returncode == 0 and 'error' not in rep
            record_stage(s, state['run_id'], 'resume_sync', state['owner'], state['token'], 'succeeded' if ok else 'failed',
                         {'refs': ['resume_sync.py --apply'], 'report': compact(rep), 'observed_at': s.now()})
            emit({'stage': 'resume_sync', 'ok': ok, 'report': rep}, save(state, 'resume_sync', rep))
            return 0
        if cmd in ('cards_ingest', 'cards_next', 'card_commit'):      # start this source's discovery clock
            try:
                src = json.loads(rest[0][1:] if False else rest[0]).get('source', '') if rest and not rest[0].startswith('@') else ''
            except ValueError:
                src = ''
            if src:
                key = 'discovery_start:%s:%s' % (state['run_id'], src)
                if not s.con.execute('SELECT 1 FROM jr_meta WHERE key=?', (key,)).fetchone():
                    s.con.execute('INSERT INTO jr_meta VALUES(?,?)', (key, s.now()))
                    s.con.commit()
        from .__main__ import dispatch
        q = {}
        if rest:
            q = json.loads(Path(rest[0][1:]).read_text()) if rest[0].startswith('@') else json.loads(rest[0])
        for k in ('owner', 'token', 'run_id', 'mode'):
            q.setdefault(k, state[k])
        if cmd == 'mirror_prepare' and isinstance(q.get('snapshots'), dict):
            if s.con.execute("SELECT 1 FROM jr_actions WHERE run_id=? AND stage='mirror' AND status<>'pending'",
                             (state['run_id'],)).fetchone():
                raise Denied('mirror stage already has its run receipt')
            if any(tab in q['snapshots'] for tab in MIRROR_TABS):
                q['snapshots'] = {k: v for k, v in q['snapshots'].items() if k in MIRROR_TABS}
            if 'sheet_ids' not in q:
                q['sheet_ids'] = mirror_sheet_ids()
            q.setdefault('summary', '')
        if cmd == 'match_batch_prepare' and 'keys' not in q:      # the plan's match queue, so the model never lists keys
            from .planner import plan
            q['keys'] = [k['job_key'] if isinstance(k, dict) else k for k in plan(s, state['policy'], state['mode'])['to_match']]
        if cmd=='match_batch_prepare' and ready_only:
            local={r['item_key'] for r in s.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='requirements' AND state='pending'")
                   if json.loads(r['payload'] or '{}').get('verified_review')}
            q['keys']=[key for key in q.get('keys',[]) if key in local]
        q['op'] = cmd
        rid = quota_reservation(s, state) if cmd == 'finish' else None
        quota = quota_status(s, state,applications=True) if rid else {}
        if quota.get('wrap_up') and not q.get('blocker'):
            q['blocker'] = 'usage limit: ' + quota['why']
        try:
            r = verify_mirror_batch(s, mirror_key, q, dispatch) if cmd == 'mirror_verify' else dispatch(s, q)
            if cmd=='learning' and r.get('proposed_fix'):
                balance=quota_status(s,state)
                if not balance or balance.get('wrap_up'):
                    r['proposed_fix']=None
        finally:
            if rid:
                ended = s.con.execute('SELECT ended FROM jr_runs WHERE id=?', (state['run_id'],)).fetchone()
                if ended and ended['ended']:
                    try:
                        settlement = worker_quota(s).finish(rid)
                    except QuotaUnavailable as exc:
                        settlement = {'status': 'uncertain', 'why': str(exc)}
                    save(state, 'quota', settlement)
                    if s.codex_production():
                        from .canary import expand_settled
                        from .shortlist import save_daily
                        expand_settled(s)
                        save_daily(s)
        if cmd == 'mirror_prepare' and quota_reservation(s, state) and set(r['ranges']) != set(MIRROR_TABS):
            raise Denied('mirror planning requires all seven dashboard tabs')
        path = save(state, cmd, r)
        if cmd!='finish':
            from . import learning
            try:
                learning.operation_succeeded(s,state,cmd,q.get('job_key'),r)
            except Denied:
                pass  # If the lease ended after a successful operation, its saved receipt still stands.
        if cmd == 'mirror_prepare':
            active = {'hash': digest(r['expected']), 'batches': len(r['batches']), 'checked': -1,
                      'prepared_at': s.now(), 'verified_at': None}
            s.con.execute('INSERT OR REPLACE INTO jr_meta VALUES (?,?)', (mirror_key, json.dumps(active)))
            r = {'ranges': r['ranges'], 'batches': len(r['batches']),
                 'cells': sum(batch['cell_count'] for batch in r['batches'])}
        emit(r, path)
        return 0
    except (Denied, ValueError, KeyError, IndexError) as e:
        from . import learning
        try:
            learning.operation_error(s,state,cmd,os.environ.get('CODEX_THREAD_ID'),str(e),q.get('job_key') if isinstance(q,dict) else None)
        except (Denied,OSError,ValueError):
            pass
        hint = ''
        if 'unsupported operation' in str(e):
            hint = (' | `jr stage <name> <status> <json>` records a stage; ops: next finish plan match match_batch_prepare '
                    'match_batch_commit cards_js cards_ingest cards_next card_commit job_closed prepare fresh submit_started reconcile '
                    'reconcile_portal alerts audit mark_opened tab_sweep import_feed notify status; built in: begin brief usage '
                    'resume_sync fresh stage. An op error is never a reason to end the run: fix the call, or skip that stage, and go on.')
        if isinstance(e, KeyError):
            hint = ' | missing field %s for `%s`' % (e, cmd) + hint
        if cmd in OP_EXAMPLES and re.search(r'missing field|evidence|required|channel|proof|quote', str(e) + hint):
            hint += ' | example: codex/jr %s \'%s\'' % (cmd, OP_EXAMPLES[cmd])
        if re.search(r're-read current Settings', str(e)):
            hint += (' | in ONE exec call: read the five ranges, save them with apply_patch, then '
                     '`codex/jr fresh <file> && codex/jr %s ...`' % cmd)
        print(json.dumps({'ok': False, 'error': str(e)[:600] + hint}))
        return 2
    finally:
        if starter:
            try:
                worker_quota(s).finish(starter)
            except QuotaUnavailable:
                pass  # the durable hold persists
        _close(s)


def _close(s):
    s.con.close()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
