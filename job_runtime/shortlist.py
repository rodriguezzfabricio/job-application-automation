"""Public, model-free discovery. The scanner cannot acquire a worker lease or submit."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from . import adapters
from .match_batch import hard_gate, SOFT, NEGATED
from .policy import ET, publication_hold, publication_value, timestamp
from .quota import PRIVATE_ROOT
from .scoring import score
from .store import Store


def write_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as output:
        temp = Path(output.name)
        try:
            output.write(text)
            output.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def explicit_hold(row):
    if row.get('reposted'):
        return 'reposted opening'
    decision = adapters.legacy('title_filter').decide(row['title'], row.get('location') or '')
    if not decision['keep']:
        return decision['reason']
    text = row.get('description') or ''
    gate, _ = hard_gate(text)
    if gate:
        return gate
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        if SOFT.search(sentence) or NEGATED.search(sentence) or re.search(r'\bor\b', sentence, re.I):
            continue
        required = re.search(r'\b(?:at least|minimum(?: of)?|must have|requires?)\s+(\d{1,2})\+?\s+years?\b', sentence, re.I)
        if required and int(required[1]) > 3 and re.search(r'experience|professional|working',sentence,re.I):
            return 'more than 3 required years'
    return ''


def ingest(store, report):
    """Keep each opening once per ET day. Only public listing and requirement evidence is written."""
    now = store.clock()
    day = now.astimezone(ET).date().isoformat()
    observed = timestamp(report.get('ran_at'))
    if not observed or observed.astimezone(ET).date().isoformat() != day:
        raise ValueError('a same-day public feed snapshot is required')
    known = {}
    for job in store.con.execute('SELECT job_key,link FROM jobs WHERE link<>\'\''):
        try:
            known.setdefault(store.exact_identity(job['link']), job['job_key'])
        except ValueError:
            continue
    lines = list(report.get('postings', []))
    lines.extend(r for r in report.get('excluded', []) if r.get('needs_retry') or r.get('reposted'))
    counts = Counter()
    for row in lines:
        if not row.get('company') or not row.get('title') or not row.get('link'):
            continue
        try:
            exact = store.exact_identity(row['link'])
        except ValueError:
            continue
        identity = json.dumps(exact, ensure_ascii=False, separators=(',', ':'))
        key = known.get(exact)
        field = row.get('posted_at_source')
        url = row.get('posted_at_url') or row['link']
        original = publication_value(row.get('posted_at'),observed) if adapters.original_date_source(store,row,field,url) else None
        with store.tx():
            proof = dict(value=str(original),field=field,url=url,observed_at=store.now()) if original else None
            first = adapters.first_publication(store,identity,proof)
            if first:
                original = first['value']
            if key and proof:
                adapters.remember_employer_date(store,key,dict(row,observed_at=observed.isoformat()))
            if key:
                original = store.employer_date(key) or original
            date_reason = publication_hold(original, now)
            if date_reason:
                stage = 'older' if date_reason == 'not first posted on this Eastern day' else 'verify_date'
                why = date_reason
            else:
                why = explicit_hold(row)
                stage = 'excluded' if why else 'review'
            if stage == 'review':
                # Do not import public fields as editable owner commands or claims of qualification.
                allowed = ('company','title','location','link','posted_at','posted_at_source','posted_at_url','source','description')
                record = {name:row[name] for name in allowed if name in row}
                record['observed_at'] = observed.isoformat()
                age = (now-timestamp(original)).total_seconds()/3600
                record['score'] = score(row['title'],[],age,'employer',True)['score']
                try:
                    ack = adapters.import_record_locked(store,'local_shortlist',identity,record)
                except adapters.Denied as problem:
                    # Preserve ambiguous tracker aliases without choosing or rewriting either draft.
                    store.con.execute('INSERT OR REPLACE INTO jr_shortlist VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (day,identity,None,row['company'],row['title'],row['link'],original,'verify_identity',str(problem),store.now()))
                    counts['verify_identity'] += 1
                    continue
                key = known[exact] = ack['job_key']
                job = store.job(key)
                if row.get('location') and re.fullmatch(r'(?:\d+ locations)?',(job.get('location') or '').strip(),re.I):
                    store.con.execute('UPDATE jobs SET location=? WHERE job_key=?',(row['location'],key))
                if row.get('description_complete') and row.get('description') and not job.get('resume'):
                    content = row['description']
                    folder = store.path.parent/'codex-runtime'/'shortlist'/'descriptions'
                    path = folder/(hashlib.sha256(content.encode()).hexdigest()+'.txt')
                    if not path.exists():
                        write_file(path,content)
                    from .local_requirements import extract
                    payload = dict(description_file=str(path),description_complete=True,source_ref=url)
                    parsed = extract(row, content, store.path.parent)
                    if parsed:
                        payload['local_requirements'] = parsed
                        if (store.path.parent/'my-resumes/manifest.json').is_file():
                            from .matching import assess_match
                            from .store import Denied
                            try:
                                review,_,_,hold = assess_match(store,key,content,parsed,
                                    dict(refs=[url],company_known=True),store.policy_snapshot(max_age=10**9),
                                    qualification_only=True)
                            except (Denied,OSError,ValueError) as error:
                                payload['review_reason'] = str(error)
                            else:
                                if not hold:
                                    payload['verified_review'] = review
                                else:
                                    payload['review_reason'] = hold
                    store.con.execute("INSERT INTO jr_backlog(provider,item_key,payload,first_seen,state) VALUES('requirements',?,?,?,'pending') "
                                      "ON CONFLICT(provider,item_key) DO UPDATE SET payload=excluded.payload",
                                      (key,json.dumps(payload),store.now()))
                why = 'requirements need verification' if row.get('description_complete') else 'full original posting needs verification'
            # A partial source must not erase a verified date or a known exclusion on a repeat scan.
            prior = store.con.execute('SELECT * FROM jr_shortlist WHERE day=? AND identity=?',(day,identity)).fetchone()
            if prior and not original and prior['publication']:
                continue
            if prior and prior['stage']=='excluded' and stage=='review' and not row.get('description_complete'):
                continue
            store.con.execute('INSERT OR REPLACE INTO jr_shortlist VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (day,identity,key,row['company'],row['title'],row['link'],original,stage,why,store.now()))
            counts[stage]+=1
    return dict(counts)


def report_day(store):
    from . import canary
    from .store import Denied
    day = store.clock().astimezone(ET).date().isoformat()
    # Receipt time may be later than the click. The attempt day is the application day.
    confirmed = {}
    for row in store.con.execute("""SELECT t.identity,t.job_key,j.company,j.title,j.link FROM jr_attempts t
            JOIN applications a ON a.source_id=t.id AND a.job_key=t.job_key JOIN jobs j ON j.job_key=t.job_key
            WHERE a.applied_at LIKE ? AND t.state='confirmed' AND a.submitted_by='task'""",(day+'%',)):
        if not publication_hold(store.employer_date(row['job_key']),store.clock()):
            confirmed[store.exact_identity(row['identity'])] = dict(row)
    candidates = {}
    policy = store.policy_snapshot(max_age=10**9)
    cohort = canary.state(store)
    pilot_full = cohort['phase']=='active' and len(cohort['jobs'])>=canary.LIMIT
    for item in store.con.execute('SELECT * FROM jr_shortlist WHERE day=? ORDER BY observed,identity',(day,)):
        r=dict(item)
        candidates[store.exact_identity(r['link']),r['job_key']]=r
    # Include original postings discovered through browser channels as well as public feeds.
    for item in store.con.execute("SELECT j.job_key,j.company,j.title,j.link,c.value AS date_proof FROM jobs j JOIN jr_cache c ON c.key='first-employer-date:'||j.job_key"):
        entry = dict(item)
        old_proof = json.loads(entry.pop('date_proof'))
        trusted = store.employer_date(item['job_key'])
        exact = store.exact_identity(item['link'])
        row_id = (exact,item['job_key'])
        if not publication_hold(trusted,store.clock()):
            if row_id not in candidates or candidates[row_id]['stage']=='verify_date':
                candidates[row_id]=dict(entry,publication=trusted,stage='review',reason='requirements need verification')
        elif not trusted and not publication_hold(old_proof.get('value'),store.clock()):
            candidates.setdefault(row_id,dict(entry,stage='verify_date',reason='original employer publication needs verification'))
    rows={}
    # An undated mirror cannot erase an employer-verified qualification for the same requisition.
    rank={'verify_date':0,'review':1,'held':2,'verify_identity':3,'qualified':4,'excluded':5,'older':6,'confirmed':7}
    def priority(row):
        return rank[row['stage']], row.get('job_key') in cohort['jobs']
    for (exact,key),r in candidates.items():
        if exact in confirmed:
            r.update(stage='confirmed',reason='exact opening receipt verified')
        elif key and r['stage']=='review':
            job=store.job(key)
            cached=store.con.execute('SELECT value FROM jr_cache WHERE key=?',('review:'+key,)).fetchone()
            review=json.loads(cached[0]) if cached else {}
            pending=store.con.execute("SELECT payload FROM jr_backlog WHERE provider='requirements' AND item_key=? AND state='pending'",(key,)).fetchone()
            assessment=json.loads(pending[0]) if pending else {}
            if not review and assessment.get('verified_review'):
                review=assessment['verified_review']
                job.update(resume=review['resume'],score=review['score']['score'])
            elif not review and assessment.get('review_reason'):
                r['reason']=assessment['review_reason']
            job['requirements']=review.get('requirements') or {}
            reason=store.application_gate(job,policy,qualification_only=True)
            if reason:
                r.update(stage='held',reason=reason)
            elif review.get('requirements_verified') and job.get('resume'):
                handoff=store.con.execute("SELECT payload FROM jr_backlog WHERE provider='application_handoff' AND item_key=? AND state<>'resolved'",(key,)).fetchone()
                blocked = store.con.execute("SELECT payload FROM jr_backlog WHERE provider='fill' AND item_key IN (?,?) AND state='pending' ORDER BY first_seen DESC LIMIT 1",(key,job['company'])).fetchone()
                questions = json.loads(handoff[0]) if handoff else {}
                why = ' / '.join(questions.get('questions') or []) or questions.get('question')
                if not why and blocked:
                    why = json.loads(blocked[0]).get('reason')
                channel = 'one_click' if re.search('easy apply only|quick apply only',job.get('notes') or '',re.I) else 'form'
                why = why or store.application_gate(job,policy,channel)
                if not why:
                    try:
                        store._budget(job,policy,day,channel,None)
                    except Denied as exc:
                        why = str(exc)
                if not why and cohort['phase']=='halted':
                    why=cohort.get('why') or 'new submissions paused'
                if not why and pilot_full and key not in cohort['jobs']:
                    why='all five pilot places are occupied'
                r.update(stage='qualified',reason=why or 'awaiting confirmed submission')
        if exact not in rows or priority(r)>priority(rows[exact]):
            rows[exact]=r
    charges=holds=0
    quota_path=store.path.parent/'codex-runtime'/'quota.sqlite'
    if quota_path.exists():
        with closing(sqlite3.connect(quota_path.resolve().as_uri()+'?mode=ro',uri=True)) as ledger:
            lookup={r[0]:r[1] for r in store.con.execute('SELECT owner,started FROM jr_runs')}
            has_times=ledger.execute("SELECT 1 FROM sqlite_master WHERE name='reservation_times'").fetchone()
            query='SELECT r.owner,r.charged,r.status,'+('t.opened_at FROM reservations r LEFT JOIN reservation_times t ON r.id=t.id' if has_times else 'NULL FROM reservations r')+" WHERE r.consumer='application'"
            for owner,amount,status,opened in ledger.execute(query):
                instant=dt.datetime.fromtimestamp(opened,ET) if opened else timestamp(lookup.get(owner))
                if instant and instant.astimezone(ET).date().isoformat()==day:
                    if status=='settled':
                        charges+=amount
                    else:
                        holds+=amount
    for exact,item in confirmed.items():
        if exact not in rows:
            rows[exact]=dict(item,stage='confirmed',reason='exact opening receipt verified')
    counts=dict(Counter(r['stage'] for r in rows.values()))
    return dict(day=day,confirmed=len(confirmed),qualified=counts.get('qualified',0)+counts.get('confirmed',0),
                job_points=charges,job_points_held=holds,
                stages=counts,openings=list(rows.values()))


def save_daily(store):
    report = report_day(store)
    path = store.path.parent/'codex-runtime'/'shortlist'/(report['day']+'-report.json')
    write_file(path,json.dumps(report,ensure_ascii=False,indent=2))
    return dict(summary=summary(report),file=str(path))


def summary(report):
    stages=report['stages']
    hold = f" {report['job_points_held']} application points reserved." if report.get('job_points_held') else ''
    return (f"{report['confirmed']}/{report['qualified']} verified qualifying same-day openings confirmed. "
            f"{stages.get('verify_date',0)} dates and {stages.get('review',0)} qualification reviews pending. "
            f"{report['job_points']} application points charged.{hold}")


def scan(root, input_path=None):
    now=dt.datetime.now(ET)
    if not 6<=now.hour<24:
        return {'state':'outside operating hours'}
    folder=root/'codex-runtime'/'shortlist'
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state':'scan in progress'}
        if input_path is None:
            import contextlib, io
            with tempfile.TemporaryDirectory(dir=folder) as temp:
                output=Path(temp)/'feed.json'
                boards=root/'codex-runtime'/'runs'/'boards-current.csv'
                with contextlib.redirect_stdout(io.StringIO()):
                    adapters.legacy('feeds').run(SimpleNamespace(hours=26,out=str(output),sources='',boards_csv=str(boards),
                                                                max_descriptions=150,no_seen=True,exact_only=True))
                raw=json.loads(output.read_text())
        else:
            raw=json.loads(input_path.read_text())
        day=now.date().isoformat()
        target=folder/(day+'-feed.json')
        store=Store(root/'tracker.sqlite')
        try:
            result=ingest(store,raw)
            write_file(target,json.dumps(raw,ensure_ascii=False))
            return {'state':'collected','day':day,'observed':result,**save_daily(store)}
        finally:
            store.con.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,help='replay a saved public snapshot for the current Eastern day')
    parser.add_argument('--scheduled',action='store_true')
    args=parser.parse_args()
    root=Path(os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or PRIVATE_ROOT)
    print(json.dumps(scan(root,args.input),ensure_ascii=False))


if __name__=='__main__':
    main()
