"""Collect observed failures locally. A retry, a passing test and live proof are distinct."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .store import Denied
from .policy import ET


RULES = (
    ('startup_snapshot','Technical','Dashboard snapshot expired before startup','Read all command ranges together and reuse the same funded reservation after a fresh read.',
     r'fresh command packet required|stale dashboard command packet|re-read current settings'),
    ('private_autofill','Technical','Private employer autofill','Retry the armed form and verify the original application opens signed in.',
     r'private autofill|autofill (?:shortcut|hotkey)|credential (?:inputs?|fields?) (?:empty|blank)'),
    ('attachment_evidence','Technical','Selected résumé could not be verified','Verify the chosen PDF against the active résumé manifest in the same form.',
     r'attachment.{0,45}(?:proof|conceal|readback|unverif)|(?:file|résumé|resume).{0,45}(?:bytes|conceal|readback|unverif|could not be verified)'),
    ('dashboard_rows','Technical','Dashboard row identity or readback failed','Compare the proposed rows and verify the changed cells in Drive.',
     r'duplicate.{0,40}(?:dashboard|applied|keys?|history)|(?:mirror|dashboard).{0,32}(?:row|readback).{0,25}(?:fail|reject|mismatch)'),
    ('exact_history','Opening evidence','Exact prior application status is unknown','Check the portal history or authenticated receipt for this one requisition.',
     r'prior.{0,45}receipt|exact (?:prior |opening |application )?(?:history|receipt).{0,35}(?:pending|missing|unknown|unverified)|uncertain.{0,32}click'),
    ('employer_source','Opening evidence','Original employer posting needs verification','Check the employer, requisition, publication date and the recorded eligibility question.',
     r'underlying (?:client|employer)|(?:missing|no verified|verify|confirm).{0,40}(?:employer date|original posting|employer posting)'),
    ('recruiting_consent','Your answer','Recruiting terms and contact consent','Reuse an owner-approved answer only for the same displayed scope.',
     r'(?:terms.{0,60}privacy|privacy.{0,60}terms).{0,75}(?:text|call|permission|consent|review)'),
    ('usage_pace','Usage','Subscription allowance or pacing','Resume funded work on the normal schedule when the existing gate admits it.',
     r'usage (?:limit|pace)|weekly allowance|quota (?:reservation|ledger)|five.hour meter|5.hour meter'),
)


def public_text(value, limit=260):
    value=' '.join(str(value or '').split())
    value=re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|/(?:Users|var|private|tmp)/\S+', '[private reference]', value, flags=re.I)
    value=re.sub(r'\b(password|passcode|token|secret|authorization code|verification code|one.time code)\s*[:=]\s*\S+',
                 r'\1 [redacted]',value,flags=re.I)
    value=re.sub(r'\b(?:otp|code|passcode|verification code)\s+(?:is\s+)?[A-Z0-9-]{4,12}\b', 'code [redacted]',value,flags=re.I)
    value=re.sub(r'\bBearer\s+\S+|\b(?:\d[ -]?){9,}\b|\b[A-Za-z0-9_-]{32,}\b','[redacted]',value,flags=re.I)
    value=re.sub(r'\(\d{3}\)\s*\d{3}[- .]?\d{4}\b','[redacted]',value)
    value=re.sub(r'(?<!\w)[A-Z][a-z]+(?: [A-Z][a-z]+){0,2} is saved\.', 'A saved value is on file.',value)
    return value[:limit]


def readable(value):
    text=public_text(value).strip().rstrip('.')
    text=re.sub(r'[;:]\s+', '. ',text).replace('jr_backlog','the saved queue')
    return re.sub(r'([\\`*_{}\[\]<>#|])',r'\\\1',text)


def section_name(subject):
    if subject.startswith('run/'):
        return 'Scheduled run completion'
    if not subject.startswith('stage/'):
        return public_text(subject)
    stage=subject.split('/',1)[1]
    return {'linkedin':'LinkedIn','handshake':'Handshake','notify':'Status reporting',
            'gate':'Subscription check','one_click':'One-click applications',
            'mirror':'Dashboard','startup':'Worker startup'}.get(stage,stage.replace('_',' ').capitalize())


def classify(context, detail, retry=None):
    if context.startswith('feed/'):
        issue=hashlib.sha256((context+' '+detail.casefold()).encode()).hexdigest()[:14]
        action='Verify the public board address when the source next runs.'
        if re.search(r'\b403\b',detail):
            action='Keep using accessible public postings while this source restricts access.'
        return 'feed-'+issue,'Discovery source',detail,action
    for issue,kind,title,action,pattern in RULES:
        if retry==issue or re.search(pattern,detail,re.I):
            return issue,kind,title,action
    if context=='recovery':
        return 'recovery-'+hashlib.sha256(detail.casefold().encode()).hexdigest()[:12],'Opening evidence',detail,'Recheck the recorded condition against the original employer opening.'
    if context=='question':
        # Keep separate unanswered fields separate, including on a single opening.
        norm=re.sub(r'\W+',' ',detail.casefold()).strip()
        key=hashlib.sha256(norm.encode()).hexdigest()[:16]
        return 'answer-'+key,'Your answer',detail,'Use a verified saved answer or the owner’s reply before retrying.'
    if context.startswith('operation/'):
        token=hashlib.sha256((context+re.sub(r'\d+','#',detail.casefold())).encode()).hexdigest()[:14]
        return 'operation-'+token,'Technical',section_name('stage/'+context.split('/',1)[1])+' failed','Reproduce the failed operation locally and check its next scheduled result.'
    return 'stage-'+context,'Workflow',context.replace('_',' ').title()+' needs follow-up','Resume this stage from its saved evidence and verify its result.'


def observe(store,run_id,subject,context,reason,retry=None,at=None):
    detail=public_text(reason)
    if not run_id or not subject or not detail:
        return
    issue,kind,title,action=classify(context,detail,retry)
    store.con.execute('''INSERT INTO jr_learning_observations VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,issue,subject) DO UPDATE SET detail=excluded.detail, at=excluded.at
        WHERE EXISTS(SELECT 1 FROM jr_learning_checks c WHERE c.issue=excluded.issue AND c.subject=excluded.subject
                     AND c.result='verified' AND c.at>=jr_learning_observations.at AND c.at<excluded.at)''',
                      (run_id,issue,subject,kind,title,detail,action,at or store.now()))


def stage(store,run_id,name,status,evidence,at=None):
    if status=='succeeded' and evidence.get('refs'):
        verified(store,'stage-'+name,'stage/'+name,section_name('stage/'+name)+' completed with a recorded stage receipt.',at)
    if status in ('deferred','failed','handoff'):
        why=evidence.get('reason') or evidence.get('blocker') or evidence.get('error') or evidence.get('blocked')
        if not isinstance(why,str):
            why='The '+name+' stage ended '+status+' without a more specific recorded cause.'
        observe(store,run_id,'stage/'+name,name,why,at=at)


def feed_errors(store,run_id,stats,at=None):
    if not run_id or not isinstance(stats,dict):
        return
    for provider,record in stats.items():
        if not isinstance(record,dict) or not isinstance(record.get('errors'),list):
            continue
        for message in record['errors']:
            if isinstance(message,str):
                observe(store,run_id,'stage/feeds','feed/'+public_text(provider,50),message,at=at)


def capture_unfinished(store):
    """At the next normal gate, preserve evidence of a funded run that lost its lease before finish."""
    with store.tx():
        rows=store.con.execute("""SELECT r.id FROM jr_runs r JOIN jr_meta q ON q.key='quota:'||r.id
            JOIN jr_leases l ON l.name='worker' WHERE r.status='running' AND r.ended IS NULL AND
            (l.token>r.token OR (l.token=r.token AND (l.owner IS NOT r.owner OR l.expires<=?)))
            AND NOT EXISTS(SELECT 1 FROM jr_learning_observations o WHERE o.run_id=r.id AND o.issue='stage-finish' AND o.subject='run/'||r.id)""",
            (store.clock().timestamp(),)).fetchall()
        for row in rows:
            observe(store,row['id'],'run/'+row['id'],'finish','The funded worker lost its lease before saving a finish receipt.')
    return bool(rows)


def operation_succeeded(store,state,command,subject,result):
    if not isinstance(result,dict) or result.get('error') or result.get('blocked') or result.get('ok') is False:
        return
    if not any(result.get(k) is True for k in ('saved','ok','verified','committed','recorded')) and result.get('status')!='succeeded':
        return
    subject=subject or 'stage/'+command
    problems=store.con.execute("SELECT DISTINCT issue,detail FROM jr_learning_observations WHERE subject=? AND issue LIKE 'operation-%'",(subject,)).fetchall()
    for problem in problems:
        if classify('operation/'+command,problem['detail'])[0]==problem['issue']:
            with store.tx():
                store.check(state['owner'],state['token'])
                verified(store,problem['issue'],subject,'The same operation succeeded and its result was recorded.')


def startup_path(store,owner):
    return store.path.parent/'codex-runtime'/'learning'/'startup'/(hashlib.sha256(owner.encode()).hexdigest()+'.json')


def operation_error(store,state,command,owner,why,subject=None):
    if not owner or command=='learning_fix' or command=='finish' and 'productive work' in why:
        return
    detail=public_text(why)
    if command=='begin' and not state:
        path=startup_path(store,owner)
        try: errors=json.loads(path.read_text())
        except FileNotFoundError: errors={}
        issue=classify('operation/begin',detail)[0]
        if issue not in errors:
            errors[issue]={'run_id':owner,'subject':'stage/startup','detail':detail,'at':store.now()}
            save_text(path,json.dumps(errors))
    elif state and state.get('owner')==owner:
        with store.tx():
            store.check(owner,state['token'])
            observe(store,state['run_id'],subject or 'stage/'+command,'operation/'+command,detail)


def startup_succeeded(store,owner,run_id):
    path=startup_path(store,owner)
    if path.is_file():
        for early in json.loads(path.read_text()).values():
            observe(store,run_id,'stage/startup','operation/begin',early['detail'],at=early['at'])
    verified(store,'startup_snapshot','stage/startup','Fresh command snapshot accepted and original reservation linked to the running job.')


def verified(store,issue,subject,proof,at=None):
    if not store.con.execute('SELECT 1 FROM jr_learning_observations WHERE issue=? AND subject=? LIMIT 1',(issue,subject)).fetchone():
        return
    when=at or store.now()
    last=store.con.execute('SELECT MAX(at) FROM jr_learning_observations WHERE issue=? AND subject=?',(issue,subject)).fetchone()[0]
    previous=store.con.execute("SELECT MAX(at) FROM jr_learning_checks WHERE issue=? AND subject=? AND result='verified'",(issue,subject)).fetchone()[0]
    if when>=last and (not previous or previous<last):
        store.con.execute("INSERT OR IGNORE INTO jr_learning_checks VALUES(?,?,?,'verified',?)",(issue,subject,when,public_text(proof)))


def review(store):
    grouped={}
    checks={(r['issue'],r['subject']):r for r in store.con.execute("SELECT * FROM jr_learning_checks WHERE result='verified' ORDER BY at")}
    trials={r['issue']:dict(r) for r in store.con.execute('SELECT * FROM jr_learning_trials ORDER BY at')}
    rows=list(store.con.execute('''SELECT o.*,j.company,j.title AS role FROM jr_learning_observations o
                                  LEFT JOIN jobs j ON j.job_key=o.subject ORDER BY o.at,o.run_id,o.subject'''))
    owners={r['owner']:r['id'] for r in store.con.execute('SELECT id,owner FROM jr_runs ORDER BY started')}
    for path in (store.path.parent/'codex-runtime'/'learning'/'startup').glob('*.json'):
        try: early=json.loads(path.read_text())
        except (OSError,ValueError): continue
        for entry in early.values():
            issue,kind,title,action=classify('operation/begin',entry['detail'])
            rows.append(dict(entry,issue=issue,kind=kind,title=title,next_check=action,company=None,role=None))
    for path in (store.path.parent/'codex-runtime'/'scheduled-skips').glob('*-usage-pace.json'):
        try:
            skip=json.loads(path.read_text())
        except (OSError,ValueError):
            continue
        if skip.get('learning_issue')!='usage_pace' or not skip.get('at'):
            continue
        detail=public_text(skip.get('blocker'))
        issue,kind,title,action=classify('gate',detail,'usage_pace')
        rows.append(dict(run_id=skip.get('owner') or path.stem,issue=issue,subject='stage/gate',kind=kind,title=title,
                         detail=detail,next_check=action,at=skip['at'],company=None,role=None))
    named_openings={(owners.get(r['run_id'],r['run_id']),r['issue']) for r in rows if r['company'] and r['role']}
    for r in sorted(rows,key=lambda r:r['at']):
        # The stage repeats the account or attachment failure already recorded on a concrete opening.
        # Keep the stage receipt in the run, without making it a third opening that can never pass a form check.
        if (r['subject']=='stage/fill' and r['kind']=='Technical'
                and (owners.get(r['run_id'],r['run_id']),r['issue']) in named_openings):
            continue
        item=grouped.setdefault(r['issue'],dict(issue=r['issue'],kind=r['kind'],title=r['title'],next_check=r['next_check'],
                runs=set(),subjects={},first_seen=r['at'],last_seen=r['at']))
        check=checks.get((r['issue'],r['subject']))
        passed=bool(check and check['at']>=r['at'])
        item['subjects'][r['subject']]=dict(opening=public_text(r['company']+' · '+r['role']) if r['company'] and r['role'] else section_name(r['subject']),
                detail=public_text(r['detail']),at=r['at'],verified=passed,proof=check['proof'] if passed else '')
        item['runs'].add(owners.get(r['run_id'],r['run_id']))
        item['last_seen']=max(r['at'],item['last_seen'])
    for item in grouped.values():
        item['run_count']=len(item.pop('runs'))
        item['open_count']=sum(not r['verified'] for r in item['subjects'].values())
        trial=trials.get(item['issue'])
        item['last_trial']=trial
        item['state']='Verified' if not item['open_count'] else 'Open'
        if item['open_count'] and trial and trial['result']=='passed' and trial['at']>=item['last_seen']:
            item['state']='Test passed · awaiting live proof'
    return sorted(grouped.values(),key=lambda i:(not bool(i['open_count']),-i['run_count'],-i['open_count'],i['kind'],i['title']))


def next_fix(items):
    return next((i for i in items if i['kind']=='Technical' and i['state']=='Open' and i['run_count']>=2),None)


def proposed_fix(store,run_id,items):
    from .coordinator import run_pace
    from .policy import timestamp
    if run_pace(store,run_id).get('mode')!='run':
        return None
    last=store.con.execute('SELECT at FROM jr_learning_trials ORDER BY at DESC LIMIT 1').fetchone()
    if last and timestamp(last[0]).astimezone(ET).date()==store.clock().astimezone(ET).date():
        return None
    issue=next_fix(items)
    return {k:issue[k] for k in ('issue','title','next_check','run_count')} if issue else None


def short_summary(store,items=None):
    if items is None: items=review(store)
    return {'open_issues':sum(i['open_count']>0 for i in items),'owner_questions':sum(i['kind']=='Your answer' and i['open_count']>0 for i in items),
            'repeat_fix':(next_fix(items) or {}).get('title')}


def save_text(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False,encoding='utf-8') as f:
        temp=f.name
        f.write(content)
    os.replace(temp,path)


def export(store,run_id=None):
    root=store.path.parent/'codex-runtime'/'learning'
    items=review(store)
    updated=store.clock().astimezone(ET).strftime('%b %d, %Y at %-I:%M %p %Z')
    out=['# Job worker learning log','','Updated '+updated+'. Code collects failures on every run. Repeat attempts in one run count once.',
         'The worker can test one recurring technical problem per day when subscription pacing allows. A live result closes it.','']
    for label,choices in [('Questions for you',[i for i in items if i['kind']=='Your answer' and i['open_count']]),
                          ('Technical fixes',[i for i in items if i['kind']=='Technical' and i['open_count']]),
                          ('Discovery sources',[i for i in items if i['kind']=='Discovery source' and i['open_count']]),
                          ('Other work to resume',sorted((i for i in items if i['kind'] not in ('Technical','Your answer','Discovery source') and i['open_count']),key=lambda i:i['kind']!='Opening evidence')),
                          ('Verified improvements',[i for i in items if not i['open_count']])]:
        out+=['## '+label,'']
        if not choices: out+=['None recorded.','']
        for i in choices[:5]:
            examples=sorted(i['subjects'].values(),key=lambda r:(r['verified'],r['opening']))
            e=examples[0]
            title=i['title']
            if i['kind']=='Your answer' and 'adverse-credit-history' in e['detail'].lower() and 'conditional' in e['detail'].lower():
                title='Review the background-check authorization and the adverse-credit-history question'
            if label=='Questions for you' and e['detail']==i['title'] and title!=i['title']:
                e={**e,'detail':'Review the authorization for conditional credit, criminal and drug checks. The form also asks about adverse credit history.'}
                from .terms import SCREENING_APPROVAL
                answers=store.path.parent/'form-answers.md'
                if answers.is_file() and SCREENING_APPROVAL in answers.read_text():
                    e={**e,'detail':'Your screening authorization is saved. The separate adverse-credit-history question still needs your answer.'}
            if i['issue'].startswith('operation-') and e['detail']=="'dedup'":
                title='Status report was missing a duplicate-prevention key'
            if i['kind']=='Your answer' and not e['verified']:
                title=e['opening']
            count=i['run_count']
            parts=[f"- **{readable(title)}** · {count} {'run' if count==1 else 'runs'}."]
            if title!=e['opening']: parts.append(readable(e['opening'])+'.')
            if e['verified']:
                parts.append(readable(e['proof']).rstrip('.')+'.')
            else:
                if e['detail']!=title and e['detail']!="'dedup'": parts.append(readable(e['detail']).rstrip('.')+'.')
                if i['last_trial']:
                    t=i['last_trial']
                    parts.append('Local test '+t['result']+' for '+readable(t['change_note']).rstrip('.')+'.')
                parts.append(readable(i['next_check']).rstrip('.')+'.')
            out+=[' '.join(parts),'']
    save_text(root/'review.md','\n'.join(out).rstrip()+'\n')
    buf=io.StringIO();writer=csv.writer(buf)
    writer.writerow(['Issue','Type','Opening or stage','Observed problem','Runs for issue','First seen','Last observed','Status','Next check','Verified outcome','Last change tested','Test result'])
    for i in items:
        for e in i['subjects'].values():
            attempt=i['last_trial'] or {}
            cols=[i['issue'],i['kind'],e['opening'],e['detail'],i['run_count'],i['first_seen'],e['at'],
                  'Verified' if e['verified'] else i['state'],i['next_check'],e['proof'],attempt.get('change_note',''),attempt.get('result','')]
            writer.writerow(["'"+c if isinstance(c,str) and c.startswith(('=','+','-','@')) else c for c in cols])
    save_text(root/'openings.csv',buf.getvalue())
    if run_id:
        current=[]
        for row in store.con.execute('''SELECT o.*,j.company,j.title AS role FROM jr_learning_observations o
                                     LEFT JOIN jobs j ON j.job_key=o.subject WHERE run_id=? ORDER BY at,issue''',(run_id,)):
            entry={k:public_text(row[k]) for k in ('issue','kind','title','detail','next_check','at')}
            entry['opening_or_stage']=public_text(row['company']+' · '+row['role']) if row['company'] and row['role'] else section_name(row['subject'])
            entry['latest_live_proof']=next((i['subjects'].get(row['subject'],{}).get('proof','') for i in items if i['issue']==row['issue']),'')
            current.append(entry)
        save_text(store.path.parent/'codex-runtime'/'runs'/run_id/'learning.json',json.dumps(current,indent=2,ensure_ascii=False)+'\n')
    summary=short_summary(store,items)
    if run_id:
        summary['proposed_fix']=proposed_fix(store,run_id,items)
    return summary


def trial(store,q):
    """Use one local regression check inside an admitted run. No model is called."""
    from .coordinator import run_pace
    from .policy import ET,timestamp
    run=q['run_id']; issue=q['issue']; name=q['test_file']; note=public_text(q.get('change'))
    repo=Path(__file__).resolve().parents[1]
    if not re.fullmatch(r'test_[a-z0-9_]+\.py',name) or not (repo/'tests'/'migration'/name).is_file() or not note:
        raise Denied('provide a changed behavior and an existing migration regression test filename')
    with store.tx():
        store.check(q['owner'],q['token'])
        live=store.con.execute("SELECT 1 FROM jr_runs WHERE id=? AND owner=? AND token=? AND status='running'",(run,q['owner'],q['token'])).fetchone()
        if not live or run_pace(store,run).get('mode')!='run':
            raise Denied('technical improvements wait for a funded normal run')
        old=store.con.execute('SELECT issue,result FROM jr_learning_trials WHERE run_id=?',(run,)).fetchone()
        if old:
            if old['issue']!=issue: raise Denied('the daily technical improvement check was already used')
            return {'issue':old['issue'],'result':old['result'],'already_recorded':True}
        recent=store.con.execute('SELECT at FROM jr_learning_trials ORDER BY at DESC LIMIT 1').fetchone()
        if recent and timestamp(recent[0]).astimezone(ET).date()==store.clock().astimezone(ET).date():
            raise Denied('the daily technical improvement check was already used')
        if not any(i['issue']==issue and i['kind']=='Technical' and i['run_count']>=2 and i['state']=='Open' for i in review(store)):
            raise Denied('select an open technical issue observed in two different runs')
        store.con.execute('INSERT INTO jr_learning_trials VALUES(?,?,?,?,?,?)',(run,issue,store.now(),name,note,'started'))
    try:
        tested=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests/migration','-p',name],cwd=repo,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
        outcome='passed' if tested.returncode==0 and re.search(rb'Ran [1-9][0-9]* tests? in ',tested.stdout) else 'failed'
    except (OSError,subprocess.TimeoutExpired):
        outcome='failed'
    with store.tx():
        store.check(q['owner'],q['token'])
        store.con.execute('UPDATE jr_learning_trials SET result=? WHERE run_id=?',(outcome,run))
    return {'issue':issue,'result':outcome,'live_evidence':'pending'}
