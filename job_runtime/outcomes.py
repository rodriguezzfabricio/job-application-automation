"""Message-level outcome reconciliation; no fuzzy title merge or invented prior application."""
import datetime as dt
import json
import re
from .adapters import ingest_message,legacy
from .policy import ET,canonical_url,history_identity,timestamp
from .store import Denied
from .receipts import validate

def history_date(evidence, observed):
    """Prefer an explicit Date applied on the matched history row, in local calendar days."""
    if evidence.get('receipt_kind') != 'history' or not evidence.get('observed_applied_date'):
        return None
    try:
        day = dt.date.fromisoformat(evidence['observed_applied_date'])
    except (TypeError, ValueError):
        return None
    quote = str(evidence.get('history_date_quote', '')).strip()
    match = re.fullmatch(r'Date applied[\s,:]+(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2})', quote, re.I)
    if match and day <= observed.astimezone(ET).date():
        shown = match.group(1)
        if shown in (day.isoformat(), day.strftime('%m/%d/%y'), day.strftime('%m/%d/%Y'),
                     day.strftime('%-m/%-d/%y'), day.strftime('%-m/%-d/%Y')):
            return day.isoformat()
    return None

def reconcile_portal(store,job_key,evidence,owner,token):
    """Record an already-submitted external application without claiming our worker sent it."""
    if evidence.get('proof')!='portal_application_submitted' or not evidence.get('refs') or not evidence.get('quotes'):
        raise Denied('explicit portal submission evidence required')
    observed=timestamp(evidence.get('observed_at'))
    if observed is None or not 0 <= (store.clock()-observed).total_seconds() <= 300:
        raise Denied('fresh portal observation required')
    with store.tx():
        store.check(owner,token)
        j=store.job(job_key)
        try:
            validate(j,evidence)
        except ValueError as error:
            raise Denied(str(error)) from error
        own=store.con.execute("SELECT id,state FROM jr_attempts WHERE job_key=? AND state<>'not_submitted'",(job_key,)).fetchone()
        if own:raise Denied('worker attempt must use attempt reconciliation')
        prior=store.con.execute('SELECT app_key FROM applications WHERE job_key=?',(job_key,)).fetchone()
        if not prior:
            identity = history_identity(j['link'])
            for other in store.con.execute("SELECT app_key,link FROM applications WHERE link<>''"):
                try:
                    if history_identity(other['link'])==identity:
                        prior=other
                        break
                except ValueError:
                    continue
        if prior:
            store.resolve_legacy_holds(job_key,evidence)
            return {'app_key':prior[0],'created':False}
        stated = history_date(evidence, observed)
        at = stated or observed.astimezone(ET).date().isoformat()
        basis = 'portal_applied_date' if stated else 'first_observed_date'
        confirmed = 'portal history; reported application date' if stated else 'portal confirmation; date first observed'
        key='app|'+job_key+'|'+at
        store.con.execute('INSERT INTO applications(app_key,job_key,company,company_key,title,link,applied_at,submitted_by,confirmed_via,result,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (key,job_key,j['company'],j['company_key'],j['title'],j['link'],at,'unattributed',confirmed,'applied','portal:'+canonical_url(j['link']),store.now()))
        store.con.execute("UPDATE jobs SET status='Applied',updated_at=? WHERE job_key=?",(store.now(),job_key))
        run = store.con.execute("SELECT id FROM jr_runs WHERE owner=? AND token=? AND status='running'",
                                (owner,token)).fetchone()
        detail = dict(evidence, applied_at_basis=basis, submitted_by='unattributed', run_id=run[0] if run else None)
        store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                          (store.now(),'portal-submission-reconciled',job_key,json.dumps(detail)))
        store.resolve_legacy_holds(job_key,evidence)
    return {'app_key':key,'created':True,'submitted_by':'unattributed','date_basis':basis}

def apply_outcome(store,message_id,record,evidence,owner,token):
    if not evidence.get('refs') or not evidence.get('quotes'):raise Denied('email outcome requires observed source evidence')
    if record.get('result') not in ('applied','rejected','assessment','interview'):raise Denied('unsupported outcome')
    t=legacy('tracker');ck=t.company_key(record['company']);title=legacy('titles').title_key(record.get('title',''))
    def update(con):
        apps=[dict(r) for r in con.execute('SELECT * FROM applications WHERE company_key=?',(ck,))]
        matched=[]
        if record.get('link'):
            identity=canonical_url(record['link'])
            for a in apps:
                if a['link'] and canonical_url(a['link'])==identity:matched.append(a)

        identities={a['job_key'] for a in matched}
        if len(identities)>1:raise Denied('multiple requisitions match the email; reconcile before updating')
        if matched:
            prior=max(matched,key=lambda a:a['applied_at'])
            if title and title!='?' and legacy('titles').title_key(prior['title'])!=title:
                raise Denied('outcome title differs from the exact opening')
            if record['result']!='applied':
                incoming=record.get('result_at') or record['applied_at']
                old_at=timestamp(prior.get('result_at'));new_at=timestamp(incoming)
                stale=bool(old_at and (new_at is None or new_at<old_at))
                if not stale:
                    con.execute('UPDATE applications SET result=?,result_at=? WHERE app_key=?',(record['result'],incoming,prior['app_key']))
                con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                    (store.now(),'outcome-stale-preserved' if stale else 'application-result',prior['app_key'],json.dumps({'message_id':message_id,'record':record,'evidence':evidence})))
            return
        # Without an existing application, an outcome is not evidence for a new submission.
        if record['result']!='applied':raise Denied('outcome has no uniquely identified prior application')
        if not record.get('link') or not title or title=='?':raise Denied('new confirmation lacks a stable requisition identity')
        identity=canonical_url(record['link']);jobs=[dict(r) for r in con.execute('SELECT * FROM jobs WHERE company_key=?',(ck,)) if r['link'] and canonical_url(r['link'])==identity]
        if len(jobs)!=1:raise Denied('confirmation needs exact existing job reconciliation')
        j=jobs[0]
        if title != legacy('titles').title_key(j['title']):
            raise Denied('email role differs from the exact opening')
        try:
            validate(j,dict(evidence,proof='confirmation_email',receipt_kind='email',receipt_message_id=message_id))
        except ValueError as error:
            raise Denied(str(error)) from error
        if con.execute("SELECT 1 FROM jr_attempts WHERE job_key=? AND state IN ('prepared','submit_started')",(j['job_key'],)).fetchone():
            raise Denied('reconcile the pending attempt using the observed email')
        at=record['applied_at'];key='app|'+j['job_key']+'|'+at[:10]
        con.execute('INSERT OR IGNORE INTO applications(app_key,job_key,company,company_key,title,link,applied_at,submitted_by,confirmed_via,result,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(key,j['job_key'],j['company'],ck,j['title'],j['link'],at,'unattributed','confirmation email','applied',message_id,store.now()))
        con.execute("UPDATE jobs SET status='Applied',updated_at=? WHERE job_key=?",(store.now(),j['job_key']))
    return ingest_message(store,'gmail',message_id,dict(evidence,record=record),update,owner,token)
