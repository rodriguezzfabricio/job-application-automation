"""Browser discovery as a code-owned work queue (LinkedIn, Handshake).

Why (2026-11-13): every run observed the same 25 cards, read ONE posting, wrote "deferred: remaining
cards require detail reads" and stopped after 8 of its 20 minutes. The queue lived only in the model's
notes, so the next run started again from page 1. Claude's version read up to 40 cards a run and made
22 Easy Apply submissions a day out of them; that channel is more than half of its output.

Now code keeps every candidate card as a `jr_backlog` item (provider `<source>_cards`), hands the model a
batch to read, and turns each page the model reads into a job row with its full description saved, so
match -> one-click can follow in the same run. The model only does what needs a browser: list the
visible cards once, open each card of the batch, report what the page says.

Ops (see __main__): cards_ingest, cards_next, card_commit.
"""
import datetime as dt
import hashlib
import json
import re
from .adapters import legacy, defer, import_record
from .policy import ET, canonical_url, timestamp
from .store import Denied

SOURCES={
    'linkedin': {'url':'https://www.linkedin.com/jobs/view/{id}/','window_h':72,'easy':'easy apply only'},
    'handshake':{'url':'https://app.joinhandshake.com/jobs/{id}','window_h':72,'easy':'quick apply only'},
}
PROVIDER='{}_cards'

# Exact read-only page readers (op `cards_js`), tested on his Brave 2026-11-13 18:10. LinkedIn's results page
# (/jobs/search-results/, the AI-search layout every old /jobs/search/ link redirects to) has NO job ids in links
# or data-job-id: the 17:40 run saw 25 cards, could not find an id, and queued none, so LinkedIn Easy Apply (24 of
# Claude's 44 applications on 11-12) produced nothing. The id is in an ancestor attribute:
# componentkey="job-card-component-ref-<id>". Run each with tab.playwright.locator(<selector>).evaluateAll(<js>).
READERS={
    'linkedin':{
        'list_selector':'[componentkey^="job-card-component-ref-"]',
        'list_js':("es=>{const clean=s=>s.replace(/^Selected,\\s*/i,'').replace(/\\s*\\((Verified job|with verification)\\)\\s*$/i,'').trim();"
                   "return es.map(e=>{let L=e.innerText.split('\\n').map(x=>x.trim()).filter(x=>x&&!/^[·•]$/.test(x));"
                   "if(L.length>1&&clean(L[0])===L[1])L=L.slice(1);const t=e.innerText;const ago=L.find(x=>/\\bago$/i.test(x))||'';"
                   "return {id:e.getAttribute('componentkey').slice(23),title:clean(L[0]||''),company:L[1]||'',location:L[2]||'',"
                   "age:ago.replace(/^Posted\\s+/i,''),apply:/easy apply/i.test(t)?'easy':'unknown',reposted:/\\bReposted\\b/i.test(t)}})}"),
        'page_selector':'body',
        'page_js':("([b])=>{const L=b.innerText.split('\\n').map(x=>x.trim()).filter(Boolean);const txt=L.join('\\n');"
                   "const closed=/No longer accepting applications/i.test(txt);const applied=/\\bApplied\\s+\\d+\\s+\\w+\\s+ago\\b|Application submitted/i.test(txt);"
                   "const easy=[...document.querySelectorAll('button,a')].some(x=>/^Easy Apply$/i.test((x.innerText||'').trim())||/^Easy Apply to /i.test(x.getAttribute('aria-label')||''));"
                   "let employer='';const a=[...document.querySelectorAll('a')].find(x=>/^\\s*Apply\\b/i.test(x.innerText)&&!/easy/i.test(x.innerText));"
                   "if(a){try{const u=new URL(a.getAttribute('href'),location.href);const inner=u.searchParams.get('url')||(u.host.includes('linkedin.com')?'':u.href);"
                   "if(inner){const y=new URL(inner);const keep=[];for(const k of ['gh_jid','jk','jobId','jobid','id','job','token','for','jobReqId','req'])"
                   "if(y.searchParams.get(k))keep.push(k+'='+encodeURIComponent(y.searchParams.get(k)));employer=y.protocol+'//'+y.host+y.pathname+(keep.length?'?'+keep.join('&'):'')}}catch(e){}}"
                   "const m=txt.match(/(Reposted\\s+)?\\d+\\s+(minute|hour|day|week|month)s?\\s+ago/i);const i=L.findIndex(l=>/^About the job/i.test(l));"
                   "const end=L.findIndex((l,k)=>k>i&&/^(Set alert for similar jobs|About the company|Looking for talent\\?|More jobs)/i.test(l));"
                   "return {status:closed?'closed':applied?'applied':(m&&m[1])?'reposted':'ok',apply:easy?'easy':(a?'external':'none'),employer_link:employer,"
                   "age:m?m[0]:'',description:i<0?'':L.slice(i+1,end>i?end:i+200).join('\\n')}}"),
        'search_url':('https://www.linkedin.com/jobs/search/?keywords={query}&f_TPR=r259200&f_E=1%2C2&geoId=103644278&sortBy=DD&start={start}'),
    },
    'handshake':{
        'list_selector':'[data-hook^="job-result-card |"]',
        'list_js':("es=>es.map(e=>{const t=e.innerText.split('\\n').map(x=>x.trim()).filter(Boolean);"
                   "return {id:e.getAttribute('data-hook').split('|')[1].trim(),company:t[0]||'',title:t[1]||'',location:t[3]||'',"
                   "age:(t.find(x=>/\\bago$|just now|today|yesterday/i.test(x))||''),apply:/quick apply/i.test(e.innerText)?'easy':'unknown',"
                   "reposted:/\\brepost/i.test(e.innerText)}})"),
    },
}


def readers(source):
    _source(source)
    return dict(READERS.get(source, {}))

MIN_TEXT=400
MAX_READ_ATTEMPTS=2
UNIT_H={'minute':1/60,'min':1/60,'hour':1,'hr':1,'day':24,'week':168,'month':720}
AGE=re.compile(r'(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s+ago',re.I)


def age_hours(text):
    """Hours since posting from a card label ("3 hours ago", "1 day ago", "Just now"); None if unknown.
    Day and longer labels count whole units minus an hour, matching the owner's "3 days ago still passes"."""
    t=str(text or '').strip().lower()
    if not t:return None
    if re.search(r'just now|moments? ago|today|\bnow\b',t):return 0.0
    if 'yesterday' in t:return 23.0
    m=AGE.search(t)
    if not m:return None
    n=int(m.group(1));h=n*UNIT_H[m.group(2).lower()]
    return max(0.0,h-1) if m.group(2).lower() in ('day','week','month') else h


def link_of(source,card_id):
    return SOURCES[source]['url'].format(id=card_id)


def _source(source):
    if source not in SOURCES:raise Denied('unknown card source: '+str(source))
    return SOURCES[source]


def _known(store,link):
    """True when this posting already has a job row or a recorded application (never re-read it)."""
    identity=canonical_url(link)
    for r in store.con.execute('SELECT link FROM jobs UNION SELECT link FROM applications'):
        try:
            if r['link'] and canonical_url(r['link'])==identity:return True
        except ValueError:continue
    return False


def read_today(store,source):
    """Cards this source's pages were opened for today (Eastern); the daily cap protects the LinkedIn/Handshake sessions."""
    today=store.clock().astimezone(ET).date();n=0
    for r in store.con.execute("SELECT payload FROM jr_backlog WHERE provider=? AND state='resolved'",(PROVIDER.format(source),)):
        try:when=timestamp(json.loads(r['payload']).get('resolved_at'))
        except ValueError:continue
        if when and when.astimezone(ET).date()==today:n+=1
    return n


def budget_left(store,source):
    pol=store.policy_snapshot()
    return max(0,int(pol.get('cards_per_day',150))-read_today(store,source))


def next_batch(store,source,limit):
    _source(source)
    limit=min(int(limit),int(store.policy_snapshot().get('cards_batch',12)),budget_left(store,source))
    out=[]
    for r in store.con.execute("SELECT item_key,payload,first_seen FROM jr_backlog WHERE provider=? AND state='pending' ORDER BY first_seen",(PROVIDER.format(source),)):
        try:c=json.loads(r['payload'])
        except ValueError:continue
        out.append({'id':r['item_key'],'url':link_of(source,r['item_key']),'title':c.get('title',''),'company':c.get('company',''),
                    'age_hours':c.get('age_hours'),'apply':c.get('apply','unknown')})
    out.sort(key=lambda c:(c['age_hours'] if c['age_hours'] is not None else 999,c['id']))
    return out[:max(0,limit)]


def queue_size(store,source):
    return store.con.execute("SELECT count(*) FROM jr_backlog WHERE provider=? AND state='pending'",(PROVIDER.format(source),)).fetchone()[0]


def ingest(store,source,cards,owner,token,limit=12):
    """Take the cards read off a results page; keep the candidates, return the batch to read now.

    cards: [{id, title, company, location, age (label text), apply: easy|external|unknown, reposted}]
    """
    cfg=_source(source);decide=legacy('title_filter').decide;provider=PROVIDER.format(source)
    skipped={};new=0
    for c in cards:
        cid=str(c.get('id','')).strip()
        if not re.fullmatch(r'\d{6,}',cid):skipped['bad id']=skipped.get('bad id',0)+1;continue
        title=str(c.get('title','')).strip();company=str(c.get('company','')).strip()
        if not title or not company:skipped['missing title or company']=skipped.get('missing title or company',0)+1;continue
        why=None
        if c.get('reposted') or re.search(r'repost',str(c.get('age','')),re.I):why='reposted'
        elif not decide(title,str(c.get('location','')))['keep']:why='title is not a target role'
        else:
            hours=age_hours(c.get('age'))
            if hours is not None and hours>cfg['window_h']:why='older than %dh'%cfg['window_h']
        if not why and _known(store,link_of(source,cid)):why='already a job or application'
        if not why:
            old=store.con.execute('SELECT state FROM jr_backlog WHERE provider=? AND item_key=?',(provider,cid)).fetchone()
            if old:why='already queued' if old['state']=='pending' else 'already read'
        if why:skipped[why]=skipped.get(why,0)+1;continue
        defer(store,provider,cid,{'title':title,'company':company,'location':str(c.get('location','')),'age':str(c.get('age','')),
                                  'age_hours':age_hours(c.get('age')),'apply':c.get('apply') if c.get('apply') in ('easy','external') else 'unknown'},owner,token)
        new+=1
    return {'source':source,'seen':len(cards),'new':new,'skipped':skipped,'queue':queue_size(store,source),'read_today':read_today(store,source),
            'daily_cap_left':budget_left(store,source),'batch':next_batch(store,source,limit)}


def _resolve(store,provider,cid,outcome,detail='',keep_pending=False):
    row=store.con.execute('SELECT payload,attempts FROM jr_backlog WHERE provider=? AND item_key=?',(provider,cid)).fetchone()
    if not row:raise Denied('card is not in the queue: '+cid)
    payload=json.loads(row['payload']);payload.update(outcome=outcome,detail=str(detail)[:200],resolved_at=store.now())
    attempts=row['attempts']+1
    state='pending' if keep_pending and attempts<MAX_READ_ATTEMPTS else 'resolved'
    store.con.execute('UPDATE jr_backlog SET payload=?,attempts=?,state=? WHERE provider=? AND item_key=?',(json.dumps(payload),attempts,state,provider,cid))
    return state


def commit(store,source,card_id,read,owner,token):
    """Record what the opened page said.

    read: {status: ok|closed|applied|reposted|unreadable, apply: easy|external|none, employer_link, age (label),
           description (the full posting text), company, title, location}
    """
    cfg=_source(source);provider=PROVIDER.format(source);cid=str(card_id)
    row=store.con.execute('SELECT payload FROM jr_backlog WHERE provider=? AND item_key=?',(provider,cid)).fetchone()
    if not row:raise Denied('card is not in the queue: '+cid)
    card=json.loads(row['payload']);status=str(read.get('status','ok'))
    with store.tx():
        store.check(owner,token)
        if status in ('closed','applied','reposted'):
            _resolve(store,provider,cid,status);return {'id':cid,'outcome':status,'row':None}
        text=str(read.get('description') or '').strip()
        if status=='unreadable' or len(text)<MIN_TEXT:
            state=_resolve(store,provider,cid,'unreadable','page text too short' if status!='unreadable' else 'unreadable',keep_pending=True)
            return {'id':cid,'outcome':'unreadable','retry':state=='pending','row':None,
                    'hint':'description text was %d chars (need %d+). Read the posting with the cards_js page_js: '
                           'await tab.playwright.locator(page_selector).evaluateAll(eval(page_js)) and pass its `description` verbatim; '
                           'a domSnapshot or the page title is not the description. On LinkedIn click "… more" first if the text ends with it.'%(len(text),MIN_TEXT)}
        from .match_batch import hard_gate
        gate,quote=hard_gate(text)
        if gate:
            _resolve(store,provider,cid,'hard gate',gate+': '+quote[:120]);return {'id':cid,'outcome':'hard gate','gate':gate,'row':None}
        hours=age_hours(read.get('age') or card.get('age'))
        if hours is not None and hours>cfg['window_h']:
            _resolve(store,provider,cid,'old','%.0fh'%hours);return {'id':cid,'outcome':'old','row':None}
        apply=read.get('apply') if read.get('apply') in ('easy','external','none') else card.get('apply','unknown')
        ext=str(read.get('employer_link') or '').strip()
        link=ext if apply=='external' and re.match(r'https?://',ext) else link_of(source,cid)
        if apply=='none':
            _resolve(store,provider,cid,'no apply button');return {'id':cid,'outcome':'no apply button','row':None}
        posted=(store.clock()-dt.timedelta(hours=hours)).isoformat() if hours is not None else ''
        company=str(read.get('company') or card.get('company'))
        title=str(read.get('title') or card.get('title'))
        root=store.path.parent/'codex-runtime'/'postings';root.mkdir(parents=True,exist_ok=True)
        name=hashlib.sha256(link_of(source,cid).encode()).hexdigest()
        (root/(name+'.txt')).write_text(text)
        from .scoring import score
        record={'company':company,'title':title,'location':str(read.get('location') or card.get('location','')),'link':link,
                'posted_at':posted,'source':source,'description':text,'description_complete':True,
                'score':score(title,[],hours,'one_click' if apply=='easy' else 'employer',True)['score']}
    ack=import_record(store,source,cid,record,owner,token)
    key=ack['job_key']
    with store.tx():
        store.check(owner,token)
        marker=cfg['easy'] if apply=='easy' else ''
        j=store.job(key)
        if marker and marker not in (j['notes'] or ''):
            store.con.execute('UPDATE jobs SET notes=?,updated_at=? WHERE job_key=?',((marker+'; '+(j['notes'] or ''))[:1900],store.now(),key))
        _resolve(store,provider,cid,'row',key)
    defer(store,'requirements',key,{'description_file':str(root/(name+'.txt')),'description_complete':True,
                                    'source_ref':'cards:'+source+':'+cid},owner,token)
    return {'id':cid,'outcome':'row','row':key,'apply':apply}


CLOSED=re.compile(r'no longer accepting|not accepting applications|position (has been )?(filled|closed)|job (is )?(closed|expired)|'
                  r'posting (has )?(closed|expired)|no longer available',re.I)


def job_closed(store,job_key,evidence,owner,token):
    """The page says the posting stopped taking applications (2026-11-13: a LinkedIn job closed an hour after
    it was posted; the run pressed Submit three times and got "We couldn't submit your application" each time,
    then queued it again). Ends the job like Claude's fill.md section 1: Not eligible, "posting closed"."""
    quote=str((evidence or {}).get('quote',''))
    if not (evidence or {}).get('refs') or not CLOSED.search(quote):
        raise Denied('closed needs refs and the page quote that says so (e.g. "No longer accepting applications")')
    with store.tx():
        store.check(owner,token)
        j=store.job(job_key)
        if j['status'] in ('Applied','Rejected by me','Not eligible','Stale'):return {'job_key':job_key,'status':j['status'],'changed':False}
        day=store.clock().astimezone(ET).strftime('%Y-%m-%d %H:%M')
        store.con.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE job_key=?',
                          ('Not eligible','posting closed %s: %s'%(day,quote[:80]),store.now(),job_key))
        store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                          (store.now(),'job-set',job_key,json.dumps({'status':'Not eligible','by':'job_closed','quote':quote[:200]})))
    return {'job_key':job_key,'status':'Not eligible','changed':True}
