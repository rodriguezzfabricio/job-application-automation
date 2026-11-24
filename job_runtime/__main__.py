"""JSON request/receipt protocol. All artifacts belong under stopgap-private/."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from . import adapters
from .store import Store, Denied

def dispatch(s, q):
    op=q['op']; owner=q.get('owner'); token=q.get('token')
    if op=='next':
        from .coordinator import next_action
        return next_action(s,q['run_id'],owner,token)
    if op=='finish':
        from .coordinator import finish
        return finish(s,q['run_id'],owner,token,q.get('blocker',''))
    if op=='acquire': return {'token':s.acquire(owner)}
    if op=='heartbeat': s.heartbeat(owner,token); return {'renewed':True}
    if op=='release': s.release(owner,token); return {'released':True}
    if op=='start': return s.start(owner,token,q['policy'])
    if op=='begin_cycle':
        from .operations import begin_cycle
        return begin_cycle(s,owner,q['packet'],q.get('resume_empty_run'),q.get('stopped_evidence'))
    if op in ('learning','learning_fix'):
        from . import learning
        s.check(owner,token)
        return learning.trial(s,q) if op=='learning_fix' else learning.export(s,q.get('run_id'))
    if op=='plan':
        from .planner import plan, apply_walls
        s.check(owner,token)
        out=plan(s,s.policy_snapshot(),q['mode'],q.get('observed_auth'))
        out['walled']=apply_walls(s,out['to_wall'],owner,token)
        return out
    if op=='commands': return adapters.import_commands(s,q['packet'],owner,token)
    if op=='action': return s.action(q['run_id'],q['stage'],q['kind'],q['input'],q['idempotency_key'])
    if op=='receipt': s.receipt(owner,token,q['action_id'],q['status'],q['evidence'],q.get('cursor')); return {'recorded':True}
    if op=='stage_receipt':
        from .operations import record_stage
        return record_stage(s,q['run_id'],q['stage'],owner,token,q['status'],q['evidence'],q.get('cursor'))
    if op=='import_feed':
        from .operations import import_feed_batch
        return import_feed_batch(s,json.loads(Path(q['report_file']).read_text()),owner,token,q['report_file'],q.get('run_id'))
    if op=='mark_opened':
        from .planner import mark_opened
        return {'marked':mark_opened(s,q['keys'],owner,token)}
    if op=='alerts':
        from . import alerts
        s.check(owner,token)
        return alerts.send(s,q['run_id'])
    if op=='tab_sweep':
        from .sweep import sweep
        return sweep(s,owner,token,q.get('tabs'),bool(q.get('complete')))
    if op=='cards_ingest':
        from .cards import ingest
        return ingest(s,q['source'],q['cards'],owner,token,int(q.get('limit',12)))
    if op=='cards_next':
        from .cards import next_batch, queue_size
        from .cards import budget_left
        return {'queue':queue_size(s,q['source']),'daily_cap_left':budget_left(s,q['source']),'batch':next_batch(s,q['source'],int(q.get('limit',12)))}
    if op=='cards_js':
        from .cards import readers
        return readers(q['source'])
    if op=='job_closed':
        from .cards import job_closed
        return job_closed(s,q['job_key'],q.get('evidence') or {},owner,token)
    if op=='card_commit':
        from .cards import commit
        return commit(s,q['source'],q['id'],q['read'],owner,token)
    if op=='match_batch_prepare':
        from .match_batch import prepare
        return prepare(s,q['run_id'],q['keys'],int(q.get('limit',8)),owner,token)
    if op=='match_batch_commit':
        from .match_batch import commit
        return commit(s,q['run_id'],owner,token)
    if op=='match':
        from .matching import commit_match
        return commit_match(s,q['job_key'],Path(q['posting_file']).read_text(),q['requirements'],q['evidence'],owner,token)
    if op=='form_reader':
        from .browser_form import reader
        return reader()
    if op=='opening_history':
        from .opening_history import check
        return check(s,q)
    if op=='publication_verify':
        from .opening_history import verify_publication
        return verify_publication(s,q)
    if op=='recovery_verify':
        from .opening_history import verify_recovery
        return verify_recovery(s,q)
    if op=='form_session':
        from .form_session import start
        return start(s,q)
    if op=='form_packet':
        from .form_step import worker_plan, requisition_id
        from .backup import file_hash
        job=s.job(q['job_key'])
        policy=s.policy_snapshot()
        name=policy.get('resume_only') or job.get('resume') or ''
        pdf=None
        if name:
            if name!=Path(name).name: raise Denied('invalid selected resume')
            path=s.path.parent/'my-resumes'/name
            sha=file_hash(path)
            s.verify_pdf(job,sha,required=True,policy=policy)
            pdf={'name':name,'path':str(path.resolve()),'bytes':path.stat().st_size,'sha256':sha}
        return {'job_key':job['job_key'],'opening':job['link'],'requisition':job.get('requisition_id') or requisition_id(job['link']),
                'fields':q['fields'],'plan':worker_plan(s,q),'pdf':pdf}
    if op=='form_step':
        from .form_step import worker_plan
        return worker_plan(s,q)
    if op=='form_evidence':
        from .form_step import check_evidence
        s.check(owner,token)
        job=s.job(q['job_key'])
        return check_evidence(dict(q,opening=dict(job, job_url=job['link'])))
    if op=='form_question':
        from .form_step import park_question
        return park_question(s,q)
    if op=='judge':
        from .judgment import decide
        return decide(s,q)
    if op in ('account_decide','account_saved_autofill','account_code_challenge','account_verified','account_begin','account_register_started','account_exists','account_mail_filter','account_match_mail'):
        from . import account_flow
        return {'account_verified':account_flow.verification_used,'account_decide':account_flow.decision,'account_saved_autofill':account_flow.saved_autofill_used,'account_code_challenge':account_flow.code_challenge,'account_register_started':account_flow.register_started,
                'account_exists':account_flow.account_exists,'account_begin': account_flow.begin, 'account_mail_filter': account_flow.verification_filter,
                'account_match_mail': account_flow.verification_match}[op](s,q)
    if op=='canary_enroll':
        from . import canary
        return canary.enroll(s,q)
    if op=='canary_status':
        from . import canary
        return canary.audit(s)
    if op=='canary_expand':
        from . import canary
        return canary.expand(s,owner,token)
    if op=='mirror_verify':
        from .reporting import verify
        s.check(owner,token)
        return verify(q['snapshots'], q['expected'], batches_applied=q.get('batches_applied'))
    if op=='mirror_prepare':
        from .reporting import prepare
        s.check(owner,token)
        return prepare(s,q['snapshots'],q['sheet_ids'],q['summary'])
    if op=='status_artifact':
        from .operations import status_artifact
        s.check(owner,token)
        return status_artifact(s,q['run_id'],q.get('summary'))
    if op=='prepare': return {'attempt_id':s.prepare(owner,token,q['run_id'],q['job_key'],q['channel'],q['resume_hash'],q['review'])}
    if op=='acceptance_grant': return s.acceptance_grant(owner,token,q['job_keys'],q['evidence'])
    if op=='end_acceptance': s.end_acceptance(owner,token);return {'ended':True}
    if op=='adopt_prepared': return s.adopt_prepared(owner,token,q['attempt_id'],q['run_id'],q['evidence'])
    if op=='submit_started': return s.submit_started(owner,token,q['attempt_id'],q['resume_hash'],q['evidence'])
    if op=='reconcile': s.reconcile(owner,token,q['attempt_id'],q['outcome'],q['evidence']);return {'recorded':True}
    if op=='reconcile_portal':
        from .outcomes import reconcile_portal
        return reconcile_portal(s,q['job_key'],q['evidence'],owner,token)
    if op=='audit': return s.audit(q['run_id'])
    if op=='account_fill_observed':
        from .account_flow import filled_observed
        return filled_observed(s,q)
    if op=='account_mark':
        from .account_flow import mark_observed
        return mark_observed(s,q)
    if op=='accounts':
        from . import accounts
        s.check(owner,token)
        private=accounts.private_of(s);default=accounts.load(private).get('default') or {}
        waiting=sorted(accounts.open_portals(s))
        return {'file':str(Path(private)/accounts.FILE),
                'has_default':bool(default.get('email') and default.get('password')),
                'agent':[p for p in waiting if accounts.has_credentials(private,p)],
                'human':[p for p in waiting if not accounts.has_credentials(private,p)]}
    if op=='import_record': return adapters.import_record(s,q['provider'],q['record_id'],q['record'],owner,token)
    if op=='cache_get': return {'value':adapters.cache_get(s,q['key'])}
    if op=='cache_put': adapters.cache_put(s,q['key'],q['value'],owner,token);return {'cached':True}
    if op=='usage': adapters.record_usage(s,q['run_id'],q['stage'],q['usage'],owner,token);return {'recorded':True}
    if op=='notify':
        result=adapters.queue_notification(s,q['dedup'],q['payload'],owner,token)
        if result.get('id') and result.get('state') in ('queued','sending','delivered'):
            from . import learning
            with s.tx():
                s.check(owner,token)
                issue=learning.classify('operation/notify',"'dedup'")[0]
                learning.verified(s,issue,'stage/notify','The corrected request entered the notification queue with a duplicate-prevention key.')
        return result
    if op=='status':
        return {'production_owner':s.con.execute("SELECT value FROM jr_meta WHERE key='production_owner'").fetchone()[0],
                'pending_attempts':[dict(r) for r in s.con.execute("SELECT id,job_key,state,updated FROM jr_attempts WHERE state IN ('prepared','submit_started')")],
                'leases':[dict(r) for r in s.con.execute('SELECT * FROM jr_leases')]}
    raise ValueError('unsupported operation: '+op)

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--db',required=True);ap.add_argument('request',help='JSON path, or - for stdin');a=ap.parse_args()
    try:
        with (sys.stdin if a.request=='-' else open(a.request)) as f: q=json.load(f)
        private=str(Path(a.db).resolve().parent)
        configured=os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT')
        if configured and str(Path(configured).resolve())!=private:
            raise Denied('database and private-state roots disagree')
        os.environ['JOB_AUTOMATION_PRIVATE_ROOT']=private
        s=Store(a.db)
        try: result=dispatch(s,q)
        finally: s.con.close()
        print(json.dumps({'ok':True,'result':result}))
        return 0
    except (Denied,ValueError,KeyError,sqlite3.IntegrityError) as e:
        print(json.dumps({'ok':False,'error':str(e),'retryable':False}));return 2

if __name__=='__main__': sys.exit(main())
