"""Durable bounded stage dispatch; host executes typed actions and submits receipts."""
import json
import re
from .policy import timestamp
from .store import Denied

KINDS={'commands':'drive','resume_sync':'deterministic','intake':'gmail','feeds':'deterministic',
       'handshake':'browser','linkedin':'browser','plan':'deterministic','match':'model',
       'one_click':'browser','fill':'browser','recruiters':'browser','audit':'deterministic','mirror':'drive'}

def next_action(store,run_id,owner,token):
    store.check(owner,token)
    run=store.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
    if not run or (run['owner'],run['token'])!=(owner,token):raise Denied('wrong run owner')
    expected=json.loads(run['expected'])
    for stage in expected:
        actions=store.con.execute('SELECT * FROM jr_actions WHERE run_id=? AND stage=?',(run_id,stage)).fetchall()
        if actions:
            pending=[dict(a) for a in actions if a['status']=='pending']
            if pending:return pending[0]
            # A failed source must not strand every later source, mirror, or lease.
            # Its receipt remains failed/deferred, so audit cannot claim full success.
            continue
        return store.action(run_id,stage,KINDS[stage],{'stage':stage,'policy_hash':json.loads(run['policy'])['hash'],
                            'instructions':'codex/stages/APPLICATIONS.md' if stage in ('fill','one_click') else 'codex/stages/MIRROR.md' if stage=='mirror' else 'codex/stages/LOCAL.md',
                            'deadline':store.clock().timestamp()+900},run_id+'|'+stage)
    return {'finished':True,'audit':store.audit(run_id)}

MIN_RUN_MINUTES={'FAST':14,'FULL':18}   # slot minutes the worker must use while productive work remains
MAX_PUSHES=4                            # finish refuses at most this many times per run, so it can never loop


def run_pace(store,run_id):
    """The usage pace `jr begin` recorded for this run ({} when none): mode run|lean, why, meters."""
    row=store.con.execute('SELECT value FROM jr_meta WHERE key=?',('pace:'+str(run_id),)).fetchone()
    try:return json.loads(row['value']) if row else {}
    except (ValueError,TypeError):return {}


def remaining_work(store,run_id,lean=None):
    """Productive work still open, counted from the database (never from the model's own account of it).

    Why (2026-11-13): instructions alone did not stop runs ending after 5 of 20 minutes with
    "deferred: remaining cards" while a queue of postings, ready Easy Apply jobs and fills sat idle.
    A lean run (job_runtime/pace.py: the usage allowance is ahead of an even spend) does not count card
    reading as work left: its allowance goes to ready Easy Apply jobs and forms first.
    """
    from . import cards
    from .planner import plan
    if lean is None:lean=run_pace(store,run_id).get('mode')=='lean'
    out={}
    blocked=set()
    for a in store.con.execute("SELECT stage,result FROM jr_actions WHERE run_id=? AND stage IN ('linkedin','handshake')",(run_id,)):
        try:
            if json.loads(a['result'] or '{}').get('blocked'):blocked.add(a['stage'])
        except (ValueError,AttributeError):pass
    for source in ('linkedin','handshake'):
        if lean or source in blocked:continue
        n=min(cards.queue_size(store,source),cards.budget_left(store,source))
        if n>0:out[source+' cards to read']=n
    mode=store.con.execute('SELECT mode FROM jr_runs WHERE id=?',(run_id,)).fetchone()['mode']
    q=plan(store,store.policy_snapshot(),mode)
    tried={r['job_key'] for r in store.con.execute('SELECT job_key FROM jr_attempts WHERE run_id=?',(run_id,))}
    backlog={r['item_key']:r['payload'] for r in store.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='requirements' AND state='pending'")}
    ready=0
    for key in q['to_match']:
        try:p=json.loads(backlog.get(key) or '{}')
        except ValueError:continue
        if p.get('description_complete') is True:ready+=1
    if ready:out['postings with full text to match']=ready
    for name,keys in (('Easy Apply / quick apply jobs ready',q['to_one_click']),('forms ready to fill',q['to_fill'])):
        n=len([k for k in keys if k not in tried])
        if n:out[name]=n
    return out


# Only these stop the whole run. One job's problem (an arbitration approval, one failed Easy Apply, an unknown
# citizenship answer) is a per-job handoff, not a reason to leave other queued work (2026-11-13 17:29 run did that).
RUN_BLOCKER=re.compile(r'usage limit|usage_limit|out of credits|rate.?limit|too many requests|unusual activity|'
                       r'captcha|browser (is )?(not reachable|unreachable|closed|down)|brave (is )?(not reachable|closed|down)|'
                       r'cua (unavailable|not available)|signed out|logged out|login wall|sign.?in wall|session expired|'
                       r'site (is )?down|service unavailable|internet|network (down|error)',re.I)


def finish(store,run_id,owner,token,blocker=''):
    """End the run. While productive work remains and the slot is mostly unused, refuse (twice at most)
    with the list of what is left: that answer is an instruction to continue, not a failure.
    `blocker` ("usage limit", "login wall", ...) records a real stop and skips the check."""
    if not RUN_BLOCKER.search(str(blocker or '')):
        run=store.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
        started=timestamp(run['started']) if run else None
        elapsed=(store.clock()-started).total_seconds()/60 if started else 999
        key='finish_pushes:'+run_id
        row=store.con.execute('SELECT value FROM jr_meta WHERE key=?',(key,)).fetchone()
        pushes=int(row['value']) if row else 0
        if run and elapsed<MIN_RUN_MINUTES.get(run['mode'],14) and pushes<MAX_PUSHES:
            try:left=remaining_work(store,run_id)
            except Denied:left={}      # no usable settings snapshot: this check never stops a run from finishing
            if left:
                with store.tx():
                    store.check(owner,token)
                    store.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',(key,str(pushes+1)))
                raise Denied('WORK REMAINS after %.0f of %d minutes: %s. Do this work now (RUNBOOK: handshake/linkedin loop, match batches, '
                             'one_click, fill), then call finish again. If a site is blocked (login, CAPTCHA, rate limit, usage limit), put '
                             'it in that stage receipt as `blocked` or pass `blocker` to finish. A problem with ONE job (arbitration approval, a '
                             'failed Easy Apply, an unknown answer) is a per-job application_handoff, not a blocker: do the other work. '
                             'A browser-tab error ("tab N is already part of browser session X", another chat owns it) is NEVER a blocker: '
                             'that tab is not yours, so open your own with `await cua.createBrowserTab("1", url)` (never getTab on an old id) '
                             'and do the work in it.'
                             %(elapsed,MIN_RUN_MINUTES.get(run['mode'],14),'; '.join('%d %s'%(n,k) for k,n in left.items())))
    # Claude parity safety net: expire 72 h Today items and send any alert the stages did not (12 h de-dup).
    # Tabs are not read here: WORKER.md forbids AppleScript, and CUA lists only this session's tabs.
    try:
        from .sweep import sweep
        sweep(store,owner,token,None)
    except Exception:
        pass
    try:
        from . import alerts
        alerts.send(store,run_id)
    except Exception:
        pass
    report=store.audit(run_id)
    with store.tx():
        store.check(owner,token)
        run=store.con.execute('SELECT * FROM jr_runs WHERE id=? AND owner=? AND token=?',(run_id,owner,token)).fetchone()
        if not run:raise Denied('wrong run owner')
        store.con.execute('UPDATE jr_runs SET status=?,ended=? WHERE id=? AND owner=? AND token=?',('succeeded' if report['success'] else 'partial',store.now(),run_id,owner,token))
        from . import learning
        learning.verified(store,'stage-finish','run/'+run_id,'The same worker run saved a finish receipt after reconciliation.')
    from .operations import publish_health
    publish_health(store,run_id,report)
    try:
        from . import learning
        report['learning']=learning.export(store,run_id)
    finally:
        store.release(owner,token)
    return report
