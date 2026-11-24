"""Per-opening history checkpoint before resuming stalled applications."""
import json
import re
from urllib.parse import urlsplit
from .form_step import normalized_url, requisition_id
from .receipts import trusted_origin
from .policy import opening_identity, history_identity, timestamp, publication_value
from .store import Denied


def recovery_holds(store):
    return {row['item_key']:json.loads(row['payload']) for row in store.con.execute(
        "SELECT item_key,payload FROM jr_backlog WHERE provider='recovery_verification' AND state='pending'")}


def link_publication(store,job,proof):
    """Bind a platform listing to the original requisition actually linked from that listing."""
    from .adapters import original_date_source,remember_employer_date
    from .accounts import allowed
    link = normalized_url(job['link'])
    source = normalized_url(proof.get('employer_url'))
    observed = timestamp(proof.get('observed_at'))
    date = timestamp(publication_value(proof.get('employer_posted_at'),observed))
    req = str(proof.get('employer_requisition') or '').strip().casefold()
    if (not source or not observed or not date or not 0 <= (store.clock()-observed).total_seconds() <= 300
            or date > observed or normalized_url(proof.get('source_posting')) != link
            or not req or not proof.get('date_quote') or not proof.get('job_identity_quote')
            or source not in [normalized_url(ref) for ref in proof.get('refs',[])]
            or not original_date_source(store,dict(job,link=source),'employer.JobPosting.datePosted',source)):
        raise Denied('fresh publication date on the exact original employer posting required')
    if history_identity(link) != history_identity(source):
        if (not allowed(link,('linkedin.com','joinhandshake.com','handshake.com'))
                or normalized_url(proof.get('employer_link_on_listing')) != source
                or str(proof.get('employer') or '').strip().casefold() != job['company'].strip().casefold()
                or str(requisition_id(source) or '').casefold() != req
                or req not in proof['job_identity_quote'].casefold() or link not in [normalized_url(ref) for ref in proof['refs']]):
            raise Denied('platform listing must link to this employer and exact original requisition')
        old=store.con.execute('SELECT original FROM jr_opening_alias WHERE link=?',(opening_identity(link),)).fetchone()
        if old and history_identity(old[0]) != history_identity(source):
            raise Denied('platform listing already points to a different original opening')
        store.con.execute('INSERT OR IGNORE INTO jr_opening_alias VALUES(?,?)',(opening_identity(link),source))
    remember_employer_date(store,job['job_key'],dict(posted_at_source='employer.JobPosting.datePosted',
        posted_at=publication_value(proof['employer_posted_at'],observed),posted_at_url=source,link=job['link']),verified_original=True)
    store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                      (store.now(),'original-publication-verified',job['job_key'],json.dumps(proof)))


def verify_publication(store,q):
    with store.tx():
        store.check(q['owner'],q['token'])
        link_publication(store,store.job(q['job_key']),q['evidence'])
    return {'job_key':q['job_key'],'first_posted':store.employer_date(q['job_key'])}


def verify_recovery(store,q):
    """Clear a recorded discovery uncertainty using the observed original employer posting."""
    job = store.job(q['job_key'])
    identity = opening_identity(job['link'])
    proof = q.get('evidence') or {}
    source = normalized_url(proof.get('employer_url'))
    observed = timestamp(proof.get('observed_at'))
    date = timestamp(publication_value(proof.get('employer_posted_at'),observed))
    with store.tx():
        store.check(q['owner'],q['token'])
        recorded = recovery_holds(store).get(identity)
        if not recorded or recorded.get('excluded'):
            raise Denied('a pending verification item is required')
        if (not source or not source.startswith('https://') or not observed or not date
                or not 0 <= (store.clock()-observed).total_seconds() <= 300
                or date > observed or normalized_url(proof.get('source_posting')) != normalized_url(job['link'])
                or str(proof.get('opening_id')) != recorded['opening_id'] or not proof.get('employer_requisition')
                or not proof.get('employer') or not proof.get('condition_quote') or not proof.get('job_identity_quote')
                or not proof.get('date_quote') or not proof.get('refs') or source not in [normalized_url(ref) for ref in proof['refs']]
                or proof.get('condition_resolved') != recorded['reason']):
            raise Denied('verify the original employer opening, its posting date and the recorded eligibility question')
        if store.codex_production():
            link_publication(store,job,proof)
        else:
            from .adapters import remember_employer_date
            remember_employer_date(store,job['job_key'],{'posted_at_source':'employer.JobPosting.datePosted',
                'posted_at':proof['employer_posted_at'],'posted_at_url':source,'link':job['link']},verified_original=True)
        store.con.execute("UPDATE jr_backlog SET state='resolved',payload=? WHERE provider='recovery_verification' AND item_key=?",
                          (json.dumps(dict(recorded,resolution=proof)),identity))
        return {'resolved':True,'job_key':job['job_key']}


def needs_check(store,job,run_id=None):
    if job['status'] in ('Awaiting submit','Walled','Ready to fill'): return True
    if store.con.execute('SELECT 1 FROM jr_meta WHERE key=?',('history-required:'+job['job_key'],)).fetchone():
        return True
    return bool(store.con.execute("SELECT 1 FROM jr_attempts WHERE job_key=? AND (state<>'prepared' OR run_id<>?)",(job['job_key'],run_id or '')).fetchone()
                or re.search(r'\b(?:stalled|partly filled|previous click|submitted\?|awaiting submit)\b',job.get('notes') or '',re.I))


def check(store,q):
    from .outcomes import reconcile_portal
    store.check(q['owner'],q['token'])
    job=store.job(q['job_key'])
    a=store.con.execute("SELECT id,state FROM jr_attempts WHERE job_key=? AND state IN ('prepared','submit_started')",(job['job_key'],)).fetchone()
    if not a:
        identity = history_identity(job['link'])
        a = next((row for row in store.con.execute("SELECT id,state,identity FROM jr_attempts WHERE state IN ('prepared','submit_started')")
                  if history_identity(row['identity'])==identity),None)
    e=q.get('evidence') or {}
    if e.get('proof')=='portal_application_submitted':
        if a: return {'action':'reconcile_attempt','attempt_id':a['id'],'state':a['state']}
        return {'action':'skip_confirmed',**reconcile_portal(store,job['job_key'],e,q['owner'],q['token'])}
    if a: return {'action':'reconcile_attempt','attempt_id':a['id'],'state':a['state']}
    try: applied=store.already_applied(job)
    except Denied: return {'action':'pending_evidence'}
    if applied: return {'action':'skip_confirmed'}
    if not needs_check(store,job): return {'action':'eligible_for_review'}
    if e:
        posting=normalized_url(job['link'])
        source=normalized_url(e.get('history_url')); observed=timestamp(e.get('observed_at'))
        if (not source or not trusted_origin(posting,source) or not observed or not 0<=(store.clock()-observed).total_seconds()<=300
                or normalized_url(e.get('posting_url'))!=posting or not e.get('refs')
                or e.get('status') not in ('not_applied','draft','not_started')
                or not re.fullmatch(r'not applied|not yet applied|draft|application not started|you have not applied(?: to this (?:job|position))?',str(e.get('quote','')).strip(),re.I)
                or not requisition_id(posting) or str(e.get('opening_id',''))!=requisition_id(posting)):
            return {'action':'pending_evidence'}
        key='history-clear:'+q['run_id']+':'+job['job_key']
        with store.tx():
            store.check(q['owner'],q['token'])
            store.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',(key,json.dumps(e)))
        return {'action':'eligible_for_review'}
    return {'action':'inspect_exact_history'}


def guard(store,job,run_id):
    hold=recovery_holds(store).get(opening_identity(job['link']))
    if hold:
        raise Denied('recovery eligibility still needs verification: '+hold['reason'])
    if needs_check(store,job,run_id) and not store.con.execute('SELECT 1 FROM jr_meta WHERE key=?',('history-clear:'+run_id+':'+job['job_key'],)).fetchone():
        raise Denied('check exact opening history before retrying stalled application')
