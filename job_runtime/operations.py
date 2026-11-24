"""Small deterministic operations for a host worker; no browser or connector transport."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from .adapters import IdentityConflict, legacy, import_commands, import_record, defer
from .health import cause
from .policy import ET, timestamp, canonical_url
from .store import Denied


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    pending=path.with_suffix(path.suffix+'.tmp')
    pending.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    os.replace(pending,path)


def packet(snapshots,observed_at,overrides,source_ref):
    """The host supplies time captured when the final connector read completed."""
    settings={str(r[0]):r[1] for r in snapshots['Settings'][1:] if len(r)>1 and r[0]}
    return {'settings':settings,'commands':{k:snapshots[k] for k in ('Today','Jobs','Recruiters','Companies')},
            'observed_at':observed_at,'source_ref':source_ref,
            'overrides_hash':hashlib.sha256(overrides.encode()).hexdigest()}


def record_stage(store,run_id,stage,owner,token,status,evidence,cursor=None):
    run=store.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
    if not run or stage not in json.loads(run['expected']):raise Denied('unexpected stage')
    from .coordinator import KINDS
    existing=store.con.execute('SELECT * FROM jr_actions WHERE run_id=? AND stage=? ORDER BY rowid LIMIT 1',(run_id,stage)).fetchone()
    action=dict(existing) if existing else store.action(run_id,stage,KINDS[stage],{'stage':stage},run_id+'|'+stage)
    store.receipt(owner,token,action['id'],status,evidence,cursor)
    return {'stage':stage,'status':status,'action_id':action['id']}


DEAD_LEASE_MINUTES=15
SILENT_OWNER_MINUTES=10


def owner_silent_minutes(owner):
    """Minutes since the lease owner's Codex session log (rollout-*-<thread id>.jsonl) last changed; None if unknown."""
    import glob,time
    if not owner or not str(owner).replace('-','').isalnum():return None
    hits=glob.glob(os.path.expanduser('~/.codex/sessions/*/*/*/rollout-*%s.jsonl'%owner))
    if not hits:return None
    return (time.time()-max(os.path.getmtime(h) for h in hits))/60


def take_over_dead_lease(store):
    """Claude parity (its coordinator took over a run lock older than 75 minutes): a lease that expired more than
    DEAD_LEASE_MINUTES ago belongs to a run that stopped (runs heartbeat every 10 minutes and a lease lasts 30), most
    often a Codex usage limit mid-run (2026-11-13 16:19). Without this every later run stopped at startup until a
    human recovered it. A live or only just expired lease is never touched."""
    lease=store.con.execute("SELECT * FROM jr_leases WHERE name='worker'").fetchone()
    if not lease or not lease['owner']:return None
    late=store.clock().timestamp()-lease['expires']
    silent=owner_silent_minutes(lease['owner'])
    # 2026-11-13 18:20: a run died at the usage limit at 18:29; its lease ran to 18:53, so with the 15-minute grace
    # the 18:40 and 19:00 runs were blocked. The owner's own Codex session log is written on every tool call:
    # 10 silent minutes there means the owner run is gone, whatever its lease says.
    if late<DEAD_LEASE_MINUTES*60 and not (silent is not None and silent>=SILENT_OWNER_MINUTES):return None
    evidence={'refs':['jr_leases worker owner=%s token=%s expired %.0f min ago; owner session log silent %s min'%(
                  lease['owner'],lease['token'],late/60,'?' if silent is None else '%.0f'%silent)],
              'reason':'auto takeover: owner run stopped (lease expired over %d minutes ago, or its session log silent %d minutes)'%(
                  DEAD_LEASE_MINUTES,SILENT_OWNER_MINUTES),
              'observed_at':store.now()}
    store.recover_stopped(evidence)
    return evidence


def begin_cycle(store, owner, command_packet, resume_empty_run=None, stopped_evidence=None):
    """One checked startup operation: never create a production run from defaults."""
    if (store.path.parent/'scratch'/'run.lock').exists():
        raise Denied('legacy worker lock exists; verify that worker stopped before cleanup')
    take_over_dead_lease(store)
    token=store.acquire(owner)
    try:
        policy=import_commands(store,command_packet,owner,token)['policy']
        run=store.resume_empty_run(resume_empty_run,owner,token,policy,stopped_evidence or {}) if resume_empty_run else store.start(owner,token,policy)
        record_stage(store,run['run_id'],'commands',owner,token,'succeeded',
                     {'refs':[command_packet['source_ref']],'policy_hash':policy['hash']})
        state=dict(run,owner=owner,token=token,policy=policy)
        write_json(store.path.parent/'codex-runtime'/'runs'/run['run_id']/'run.json',state)
        return state
    except BaseException:
        store.release(owner,token)
        raise


def import_feed_batch(store,report,owner,token,source_ref,run_id=None):
    """Persist every returned candidate and full description before any queue limit."""
    store.check(owner,token);root=store.path.parent/'codex-runtime'/'postings';root.mkdir(parents=True,exist_ok=True)
    if run_id:
        from . import learning
        with store.tx():
            store.check(owner,token)
            active=store.con.execute('SELECT 1 FROM jr_runs WHERE id=? AND owner=? AND token=?',(run_id,owner,token)).fetchone()
            if active:
                learning.feed_errors(store,run_id,report.get('stats',{}))
    acks=[]
    held=[]
    for item in report.get('postings',[]):
        record=dict(item);identity=canonical_url(record['link']);filename=hashlib.sha256(identity.encode()).hexdigest()
        artifact=root/(filename+'.json');write_json(artifact,dict(record,source_ref=source_ref,observed_at=store.now()))
        if record.get('description'):(root/(filename+'.txt')).write_text(record['description'])
        # Title and date checks have run in feeds.py. Requirement scoring remains a separate stage.
        if record.get('score') is None:
            from .scoring import score
            when=timestamp(record.get('posted_at'));age=(store.clock()-when).total_seconds()/3600 if when else None
            record['score']=score(record['title'],[],age,'employer',True)['score']
        try:
            ack=import_record(store,record.get('source','feed'),identity,record,owner,token)
        except IdentityConflict as error:
            conflict=dict(link=identity,company=record['company'],title=record['title'],reason=str(error),source_ref=source_ref)
            defer(store,'public_identity',identity,conflict,owner,token)
            held.append(conflict)
            continue
        job=store.job(ack['job_key'])
        if job['status'] not in ('Applied','Rejected by me','Not eligible','Stale') and not job.get('resume'):
            backlog=store.con.execute("SELECT payload FROM jr_backlog WHERE provider='requirements' AND item_key=? AND state='pending'",(ack['job_key'],)).fetchone()
            prior=json.loads(backlog[0]) if backlog else {}
            # The public shortlist holds the validated original text and its offline review.
            # An independent feed import with incomplete or identical data preserves that evidence.
            if not prior.get('verified_review') or (record.get('description_complete') is True and
                    hashlib.sha256((record.get('description') or '').encode()).hexdigest()!=prior['verified_review'].get('posting_sha256')):
                if record.get('description_complete') is True or not prior.get('description_complete'):
                    defer(store,'requirements',ack['job_key'],{'artifact':str(artifact),'description_file':str(root/(filename+'.txt')),
                          'description_complete':record.get('description_complete') is True,'source_ref':source_ref},owner,token)
        acks.append(ack)
    return {'committed':len(acks),'acks':acks,'held':held,'stats':report.get('stats',{}),'source_ref':source_ref}


def publish_health(store,run_id,audit):
    """Keep the installed watchdog compatible without crediting failed work."""
    root=store.path.parent;run=dict(store.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone())
    statefile=root/'desktop-state.json'
    try:state=json.loads(statefile.read_text())
    except (FileNotFoundError,ValueError):state={}
    done={r['stage']:r['completed'] for r in store.con.execute("SELECT stage,completed FROM jr_actions WHERE run_id=? AND status='succeeded'",(run_id,))}
    state.update(last_completed_at=run['ended'],last_run_id=run_id,last_run_status=run['status'],provider='codex')
    # Name why, not only that: the watchdog reads this state file and has no database.
    state['cause']=cause(store,run_id,audit)
    if audit['success']:state['last_success_at']=run['ended']
    browser=[]
    for action in store.con.execute("SELECT * FROM jr_actions WHERE run_id=? AND status='succeeded'",(run_id,)):
        evidence=json.loads(action['result']) if action['result'] else {}
        if action['stage'] in ('handshake','linkedin','one_click','fill') and (evidence.get('browser_observed') is True or evidence.get('cards_observed',0)>0):
            browser.append(action['completed'])
    if browser:state['browser_last_success_at']=max(browser)
    if 'intake' in done:state['last_email_check_at']=done['intake']
    if 'feeds' in done:state['last_discovery_at']=done['feeds']
    if 'fill' in done and run['mode']=='FULL':state['last_full_fill_at']=done['fill']
    write_json(statefile,state)
    write_json(root/'scratch'/'last-run.json',{'provider':'codex','run_id':run_id,'mode':run['mode'],'started_at':run['started'],
              'finished_at':run['ended'],'audit':audit,'reports':{r['stage']:{'status':r['status'],'evidence':json.loads(r['result']) if r['result'] else None} for r in store.con.execute('SELECT * FROM jr_actions WHERE run_id=?',(run_id,))}})
    # Installers explicitly configure this optional compatibility destination.
    loc=root/'codex-migration'/'worker-location.json'
    if loc.exists():
        destination=json.loads(loc.read_text()).get('watchdog_state')
        if destination:
            write_json(destination,{'provider':'codex','mode':run['mode'],'started_at':run['started'],'finished_at':run['ended'],'status':run['status']})
            write_json(Path(destination).parent/'desktop-state.json',state)
    return state


def publish_started(store, run_id):
    """Expose the active run so the watchdog can distinguish stuck from idle."""
    location = store.path.parent/'codex-migration'/'worker-location.json'
    if not location.exists():
        return
    destination = json.loads(location.read_text()).get('watchdog_state')
    if destination:
        run = store.con.execute('SELECT * FROM jr_runs WHERE id=?', (run_id,)).fetchone()
        write_json(destination, {'provider':'codex', 'run_id':run_id, 'mode':run['mode'],
                                 'started_at':run['started'], 'finished_at':None, 'status':'running'})


def status_artifact(store,run_id,summary=None):
    run=store.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
    if not run:raise Denied('run missing')
    p=json.loads(run['policy']);audit=store.audit(run_id)
    line=summary or legacy('tracker').phone_line(store.con,store.clock().astimezone(ET).strftime('%H:%M'))
    if not isinstance(line,str):line=json.dumps(line,ensure_ascii=False)
    if store.codex_production():
        from .shortlist import report_day, summary as daily_summary
        line = '[Jobs] '+daily_summary(report_day(store))+' '+line
    if not audit['success']:
        pending=','.join(audit['missing'])
        detail='Check: '+(pending or 'uncertain receipt or stage failure')+'.'
        if line.startswith('[Jobs ') and '] ' in line:
            label,body=line.split('] ',1)
            line=label+'] Partial. '+body.rstrip()+' '+detail
        else:
            line='[Jobs] Partial. '+line.removeprefix('[Jobs] ')+' '+detail
    text=line[:200]+'\n'+json.dumps({'phone_updates':p['phone_updates'],'push_hour':p['push_hour']})+'\n'
    path=store.path.parent/'codex-runtime'/'runs'/run_id/'STATUS.txt';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    return {'file':str(path),'name':'STATUS '+store.clock().astimezone(ET).strftime('%Y-%m-%d %H:%M')+' Codex','audit':audit}
