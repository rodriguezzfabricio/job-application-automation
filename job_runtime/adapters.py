"""Deterministic host boundary: commands, mirror patches, caches and outbox."""
import hashlib
import importlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from .policy import canonical_url, opening_identity, history_identity, digest, timestamp, publication_value, ET
from .store import Denied

class IdentityConflict(Denied):
    """A public identifier needs reconciliation before this single row can be imported."""


def legacy(name):
    folder=Path(__file__).resolve().parents[1]/'stopgap-tools'
    if str(folder) not in sys.path: sys.path.insert(0,str(folder))
    return importlib.import_module(name)

EDITABLE={'Today':('Mark done / skip','job_key'), 'Jobs':('Your call','job_key'),
          'Recruiters':('Status','person_key'), 'Companies':('Research status','Company')}

def check_command_packet(store, packet):
    observed=timestamp(packet['observed_at'])
    if observed is None or not 0 <= (store.clock()-observed).total_seconds() <= 60: raise Denied('fresh command packet required')
    if set(packet['commands'])!=set(EDITABLE): raise Denied('all editable tabs required')
    for tab,(command,keycol) in EDITABLE.items():
        rows=packet['commands'][tab]
        if not rows or command not in rows[0] or keycol not in rows[0]: raise Denied('missing command headers: '+tab)
    return observed

def import_commands(store, packet, owner, token):
    # Changes and settings commit together; a partial tab read never changes policy.
    observed=check_command_packet(store,packet)
    t=legacy('tracker'); changes={}
    with store.tx():
        store.check(owner,token)
        for tab,(command,keycol) in EDITABLE.items():
            rows=packet['commands'][tab]
            ci,ki=rows[0].index(command),rows[0].index(keycol); changed=[rows[0]]
            for row in rows[1:]:
                if len(row)<=ki or not row[ki]: continue
                key=str(row[ki]); cmd=str(row[ci] if len(row)>ci else '').strip().lower()
                prior=store.con.execute('SELECT command FROM jr_commands WHERE tab=? AND record_key=?',(tab,key)).fetchone()
                if not prior or prior['command']!=cmd:
                    if cmd: changed.append(row)
                    store.con.execute('INSERT OR REPLACE INTO jr_commands VALUES(?,?,?,?)',(tab,key,cmd,store.now()))
            changes[tab]=t.import_edits(store.con,tab,changed)
        from .policy import effective
        p=effective(packet['settings'],packet.get('overrides_hash',''))
        store.con.execute('INSERT OR REPLACE INTO jr_settings VALUES(1,?,?,?)',(json.dumps(packet),observed.isoformat(),p['hash']))
    return {'policy':p,'changes':changes}

DATE_HEADERS={'Date','Found (ET)','Posted','Posted (ET)','Last researched','Verified on'}

def display_value(header,value):
    if header in DATE_HEADERS:
        when=timestamp(value)
        if when:
            when=when.astimezone(ET)
            return when.date().isoformat() if len(str(value))==10 or header=='Date' else when.strftime('%Y-%m-%d %H:%M')
    return value

def sheet_value(header,value):
    if value is None:return ''
    if header in DATE_HEADERS:
        when=timestamp(value)
        if when:return when.astimezone(ET).replace(second=0,microsecond=0).isoformat()
    return str(value)

def mirror_patch(tab,current,exported):
    """Stable rows; never move/delete rows or write existing editable cells.

    Returned cells are targeted updates. Re-read all headers/keys before execution;
    command cells remain untouched even if a user edits them during the write.
    """
    if tab=='Settings': raise Denied('Settings is read-only to the mirror')
    if not current or not exported: raise Denied('header snapshots required')
    if current[0]!=exported[0]: raise Denied('header change needs reconciliation')
    header=current[0]; edit,keyname=EDITABLE.get(tab,(None,None))
    identity_cols=[keyname] if keyname else {'Applied':['job_key','Date'],'Rejected':['Company','Title','Link'],'History':['Date','Company','Role']}.get(tab)
    if not identity_cols or any(k not in header for k in identity_cols): raise Denied('stable-key export required: '+tab)
    indexes=[header.index(k) for k in identity_cols];ci=header.index(edit) if edit else -1
    def row_key(row):
        return tuple(sheet_value(header[i],row[i]) if len(row)>i else '' for i in indexes)
    unchanged_groups = set()
    if tab == 'History':
        def groups(rows):
            result = defaultdict(Counter)
            for row in rows[1:]:
                values = tuple(sheet_value(name, row[i] if i < len(row) else '') for i, name in enumerate(header))
                result[row_key(row)][values] += 1
            return result
        old, new = groups(current), groups(exported)
        # Separate receipts can share a date, company and title. They need no
        # positional matching when every visible row and its multiplicity agree.
        unchanged_groups = {key for key, rows in old.items() if sum(rows.values()) > 1 and rows == new.get(key)}
    keys={}
    for i,row in enumerate(current[1:],1):
        key=row_key(row)
        if any(key) and key not in unchanged_groups:
            if key in keys: raise Denied('duplicate dashboard key; reconcile ambiguous rows without overwriting')
            keys[key]=i
    patches=[]; nextrow=len(current)
    export_keys=set()
    for row in exported[1:]:
        key=row_key(row)
        if not any(key) or key in unchanged_groups: continue
        if key in export_keys: raise Denied('export identity collision; stable helper ID needed')
        export_keys.add(key)
        exists=key in keys; target=keys.get(key,nextrow)
        if not exists: nextrow+=1
        before=current[target] if exists else []
        for col,value in enumerate(row):
            if col==ci: continue
            value=display_value(header[col],value)
            prior=before[col] if col<len(before) else ''
            if sheet_value(header[col],prior)!=sheet_value(header[col],value): patches.append({'row':target,'col':col,'value':value})
    return {'tab':tab,'snapshot_hash':digest(current),'key_hash':digest([(i,row_key(r)) for i,r in enumerate(current)]),'cells':patches}

def append_only_patch(tab,current,exported):
    if tab not in ('History','Rejected'): raise Denied('append-only strategy only for immutable history/rejections')
    if not current or not exported or current[0]!=exported[0]: raise Denied('matching headers required')
    def key(row):return tuple(sheet_value(current[0][i],v) for i,v in enumerate(row))
    prior={key(row) for row in current[1:]}
    rows=[[display_value(current[0][i],v) for i,v in enumerate(row)] for row in exported[1:] if key(row) not in prior]
    return {'tab':tab,'start_row':len(current),'rows':rows,'snapshot_hash':digest(current)}

def defer(store,provider,item_key,payload,owner,token,retry_at=None):
    with store.tx():
        store.check(owner,token)
        store.con.execute("INSERT INTO jr_backlog(provider,item_key,payload,first_seen,retry_at,state) VALUES(?,?,?,?,?,'pending') ON CONFLICT(provider,item_key) DO UPDATE SET payload=excluded.payload,retry_at=excluded.retry_at",(provider,item_key,json.dumps(payload),store.now(),retry_at))

def backlog(store,provider):
    rows=[dict(r) for r in store.con.execute("SELECT * FROM jr_backlog WHERE provider=? AND state='pending' AND (retry_at IS NULL OR retry_at<=?) ORDER BY first_seen",(provider,store.now()))]
    return {'items':rows,'oldest_age_seconds':max([(store.clock()-timestamp(r['first_seen'])).total_seconds() for r in rows],default=0)}

def cache_key(description,manifest,version='requirements-v1'):
    return digest({'description':description,'manifest':manifest,'version':version})

def cache_get(store,key):
    with store.tx():
        row=store.con.execute('SELECT * FROM jr_cache WHERE key=?',(key,)).fetchone()
        if row:
            store.con.execute('UPDATE jr_cache SET hits=hits+1 WHERE key=?',(key,))
            return json.loads(row['value'])

def cache_put(store,key,value,owner,token):
    with store.tx():
        store.check(owner,token)
        store.con.execute('INSERT OR IGNORE INTO jr_cache(key,value) VALUES(?,?)',(key,json.dumps(value)))

def ingest_message(store,provider,message_id,evidence,update,owner,token):
    # A thread's later message has a different key. Updates and cursor commit once.
    with store.tx():
        store.check(owner,token)
        if store.con.execute('SELECT 1 FROM jr_messages WHERE provider=? AND message_id=?',(provider,message_id)).fetchone(): return False
        update(store.con)
        store.con.execute('INSERT INTO jr_messages VALUES(?,?,?)',(provider,message_id,json.dumps(evidence)))
    return True

EMPLOYER_DATE_FIELDS={'employer.Greenhouse.first_published','employer.Lever.createdAt',
    'employer.Ashby.publishedAt','employer.SmartRecruiters.releasedDate',
    'employer.Workday.startDate','employer.Workday.postedOn','employer.JobPosting.datePosted','employer.Amazon.posted_date'}

def original_date_source(store,job,field,url):
    """A listing site's JobPosting metadata is not an employer's original publication."""
    from urllib.parse import urlsplit
    from .accounts import allowed, hostname, ACCOUNT_SITES
    from .match_batch import ats_target

    link = job.get('link') or ''
    if field not in EMPLOYER_DATE_FIELDS or not link.startswith('https://') or not str(url).startswith('https://'):
        return False
    host = hostname(url)
    mirrors = ('aplyr.ai','applyguy.com','swejoblists.com','builtin.com','wellfound.com','jobgether.com','linkedin.com','indeed.com')
    if not host or allowed(url, mirrors) or allowed(link, mirrors):
        return False
    target = ats_target(link) or {}
    source = urlsplit(url)
    if target.get('lever',('',))[0].casefold() == 'jobgether':
        # Jobgether's board publishes partner-company listings under its own tenant.
        # Link a partner's original requisition before accepting its publication date.
        return False
    if field != 'employer.JobPosting.datePosted':
        if field in ('employer.Workday.startDate','employer.Workday.postedOn') and 'wd' in target:
            origin, tenant, site, path = target['wd']
            prefix = f'/wday/cxs/{tenant}/{site}/job/'
            if host != hostname(link):
                return False
            if source.path[:len(prefix)].casefold() == prefix.casefold():
                employer_link = f'{origin}/{site}/job/'+source.path[len(prefix):]
                return history_identity(link) == history_identity(employer_link) or source.path == f'/wday/cxs/{tenant}/{site}{path}'
            return history_identity(url) == history_identity(link)
        if field == 'employer.Greenhouse.first_published' and 'gh' in target:
            slug, req = target['gh']
            return history_identity(link) == history_identity(url) or (
                host == 'boards-api.greenhouse.io' and source.path == f'/v1/boards/{slug}/jobs/{req}')
        if field == 'employer.SmartRecruiters.releasedDate' and 'sr' in target:
            slug, req = target['sr']
            return host == 'api.smartrecruiters.com' and source.path == f'/v1/companies/{slug}/postings/{req}'
        if field == 'employer.Ashby.publishedAt' and 'ashby' in target:
            slug, _ = target['ashby']
            return host == 'api.ashbyhq.com' and source.path == f'/posting-api/job-board/{slug}'
        if field == 'employer.Lever.createdAt' and 'lever' in target:
            slug, req = target['lever']
            return host == 'api.lever.co' and source.path in (f'/v0/postings/{slug}', f'/v0/postings/{slug}/{req}')
        return (field == 'employer.Amazon.posted_date' and allowed(link, ('amazon.jobs',))
                and history_identity(link) == history_identity(url))
    if history_identity(link) != history_identity(url):
        return False
    if allowed(host, (*ACCOUNT_SITES,'greenhouse.io','lever.co','ashbyhq.com','smartrecruiters.com','icims.com','amazon.jobs')):
        return True
    row=store.con.execute('SELECT domain,careers_url FROM companies WHERE company_key=?',
                          (job.get('company_key') or legacy('tracker').company_key(job['company']),)).fetchone()
    if not row:
        return False
    domains=[urlsplit(v if '://' in v else 'https://'+v).hostname for v in row if v]
    return allowed(host,tuple(d for d in domains if d))


def earlier_publication(prior,current):
    if not prior:
        return current
    previous,when = timestamp(prior['value']),timestamp(current['value'])
    old_day,new_day = len(prior['value'])==10,len(current['value'])==10
    if old_day != new_day and when.astimezone(ET).date()==previous.astimezone(ET).date():
        return current if old_day else prior
    return prior if previous <= when else current


def first_publication(store, identity, proof=None):
    row=store.con.execute('SELECT proof FROM jr_publications WHERE identity=?',(identity,)).fetchone()
    prior=json.loads(row[0]) if row else None
    if proof is None:
        return prior
    from .policy import publication_hold
    if publication_hold(proof['value'],store.clock()) in ('invalid employer posting date','employer posting time zone needs verification'):
        return prior
    chosen=earlier_publication(prior,proof)
    if chosen != prior:
        store.con.execute('INSERT OR REPLACE INTO jr_publications VALUES(?,?)',(identity,json.dumps(chosen)))
    return chosen


def index_legacy_workday_dates(store):
    """Retain date and exclusion evidence saved before numbered Workday sites were recognized."""
    marker = 'workday-site-date-index-v1'
    if store.con.execute('SELECT 1 FROM jr_meta WHERE key=?', (marker,)).fetchone():
        return
    with store.tx():
        for row in store.con.execute("""SELECT identity,proof FROM jr_publications
                WHERE identity LIKE '["url",%'""").fetchall():
            _, link = json.loads(row['identity'])
            exact = history_identity(link)
            if exact[0] == 'workday':
                identity = json.dumps(exact, ensure_ascii=False, separators=(',', ':'))
                first_publication(store, identity, json.loads(row['proof']))
        for row in store.con.execute("""SELECT * FROM jr_shortlist WHERE stage='excluded'
                AND identity LIKE '["url",%' ORDER BY observed DESC""").fetchall():
            exact = history_identity(row['link'])
            if exact[0] == 'workday':
                item = dict(row, identity=json.dumps(exact, ensure_ascii=False, separators=(',', ':')))
                store.con.execute("""INSERT OR IGNORE INTO jr_shortlist VALUES
                    (:day,:identity,:job_key,:company,:title,:link,:publication,:stage,:reason,:observed)""", item)
        store.con.execute('INSERT OR IGNORE INTO jr_meta VALUES(?,?)',(marker,store.now()))


def remember_employer_date(store,key,record,*,verified_original=False):
    """Correct listing dates without moving a known employer publication forward."""
    field=record.get('posted_at_source');value=publication_value(record.get('posted_at'),timestamp(record.get('observed_at')) or store.clock())
    if field not in EMPLOYER_DATE_FIELDS:return
    when=timestamp(value)
    if when is None:raise Denied('canonical employer posting date required')
    source=canonical_url(record.get('posted_at_url') or record['link'])
    cache_key='first-employer-date:'+key
    cached=store.con.execute('SELECT value FROM jr_cache WHERE key=?',(cache_key,)).fetchone()
    prior=json.loads(cached[0]) if cached else None
    chosen={'value':str(value),'field':field,'url':source,'observed_at':store.now()}
    if verified_original:
        chosen['verified_original']=True
    job = store.job(key)
    new_trusted = verified_original or original_date_source(store, job, field, source)
    old_trusted = prior and (prior.get('verified_original') or original_date_source(store, job, prior['field'], prior['url']))
    if old_trusted and not new_trusted:
        return
    if prior and (old_trusted or not new_trusted):
        chosen=earlier_publication(prior,chosen)
        if verified_original and prior['value']==str(value) and prior['url']==source:
            chosen=dict(chosen,verified_original=True)
    if old_trusted or new_trusted:
        identity=json.dumps(store.exact_identity(job['link']),ensure_ascii=False,separators=(',',':'))
        chosen=first_publication(store,identity,chosen) or chosen
    before=store.job(key)['posted_at']
    store.con.execute('INSERT OR REPLACE INTO jr_cache(key,value) VALUES(?,?)',(cache_key,json.dumps(chosen)))
    if before!=chosen['value']:
        store.con.execute('UPDATE jobs SET posted_at=?,updated_at=? WHERE job_key=?',(chosen['value'],store.now(),key))
        store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',(store.now(),'employer-posting-date',key,json.dumps({'previous_listing_date':before,'employer_evidence':chosen})))

def import_record(store,provider,record_id,record,owner,token):
    with store.tx():
        store.check(owner,token)
        return import_record_locked(store,provider,record_id,record)


def import_record_locked(store,provider,record_id,record):
    """Upsert public listing fields inside the caller's transaction. No application operations."""
    if not store.con.in_transaction:
        raise Denied('public record import requires a transaction')
    record=dict(record)
    for field in ('company','title','link'):
        if not isinstance(record.get(field),str) or not record[field].strip():
            raise Denied('record requires nonempty '+field)
    source=record.get('source',provider)
    if isinstance(source,list):
        if not all(isinstance(value,str) for value in source):raise Denied('invalid record sources')
        source='; '.join(source)
    if not isinstance(source,str):raise Denied('invalid record source')
    record['source']=source
    if not record.get('description'):
        record['description']=record.get('description_excerpt') or ''
    if not isinstance(record['description'],str):raise Denied('invalid record description')
    old=store.con.execute('SELECT job_key FROM jr_imports WHERE provider=? AND record_id=?',(provider,record_id)).fetchone()
    if old:
        if store.exact_identity(store.job(old['job_key'])['link'])!=store.exact_identity(record['link']):raise IdentityConflict('record identity changed; reconcile before import')
        remember_employer_date(store,old['job_key'],record)
        return {'record_id':record_id,'job_key':old['job_key'],'committed':True}
    t=legacy('tracker'); identity=opening_identity(record['link'])
    # Preserve preexisting tracker identities; never title-merge distinct requisitions.
    exact=store.exact_identity(identity)
    same=[dict(r) for r in store.con.execute('SELECT * FROM jobs') if r['link'] and store.exact_identity(r['link'])==exact]
    if len(same)>1:
        canonical = [row for row in same if canonical_url(row['link']) == identity]
        if len(canonical)==1: same=canonical
    if len(same)>1:raise IdentityConflict('multiple existing job identities; reconcile without rekeying')
    if same: key=same[0]['job_key']
    else:
        key=identity; ck=t.ensure_company(store.con,record['company']); at=store.now()
        store.con.execute('INSERT INTO jobs(job_key,company,company_key,title,location,link,score,eligibility,status,resume,posted_at,found_at,source,notes,reason,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,record['company'],ck,record['title'],record.get('location',''),record['link'],record.get('score'),record.get('eligibility',''),'New','',record.get('posted_at',''),record.get('found_at',at),record.get('source',provider),record.get('description','')[:1500],record.get('reason',''),at))
    store.con.execute('INSERT INTO jr_imports VALUES(?,?,?)',(provider,record_id,key))
    remember_employer_date(store,key,record)
    return {'record_id':record_id,'job_key':key,'committed':True}

def queue_notification(store,dedup,payload,owner,token):
    with store.tx():
        store.check(owner,token)
        prior=store.con.execute("SELECT * FROM jr_outbox WHERE dedup=? ORDER BY created DESC LIMIT 1",(dedup,)).fetchone()
        if prior and (prior['state'] in ('queued','sending','failed') or (prior['delivered'] and (store.clock()-timestamp(prior['delivered'])).total_seconds()<43200)):
            return {'id':prior['id'],'state':prior['state']}
        eid=str(uuid.uuid4());store.con.execute('INSERT INTO jr_outbox VALUES(?,?,?,?,?,?,?)',(eid,dedup,json.dumps(payload),'queued',store.now(),None,None))
    return {'id':eid,'state':'queued'}

def notification_result(store,event_id,state,evidence,owner,token):
    if state not in ('delivered','failed','expired','sending'): raise ValueError('invalid delivery state')
    if not evidence.get('refs'): raise Denied('delivery receipt required')
    with store.tx():
        store.check(owner,token)
        store.con.execute('UPDATE jr_outbox SET state=?,delivered=?,error=? WHERE id=?',(state,store.now() if state=='delivered' else None,json.dumps(evidence),event_id))

def record_usage(store,run_id,stage,usage,owner,token):
    # Missing host meters are NULL, never fabricated zeros.
    with store.tx():
        store.check(owner,token)
        store.con.execute('INSERT INTO jr_usage VALUES(?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),run_id,stage,usage.get('model'),usage.get('effort'),usage.get('input_tokens'),usage.get('output_tokens'),usage.get('cached_tokens'),usage.get('duration_ms'),usage['source']))

def closeable_tabs(store):
    return [r['tab_id'] for r in store.con.execute("SELECT t.* FROM jr_tabs t JOIN jobs j ON j.job_key=t.job_key WHERE t.owned=1 AND t.persistent=0 AND j.status IN ('Applied','Rejected by me') AND NOT EXISTS (SELECT 1 FROM jr_attempts a WHERE a.job_key=t.job_key AND a.state IN ('prepared','submit_started'))")]
