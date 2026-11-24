"""Validate and commit a match from a full posting; no model or browser dependency."""
import hashlib
import json
from pathlib import Path
from .adapters import legacy,cache_key,cache_put
from .policy import gate,timestamp
from .scoring import score
from .store import Denied


def assess_match(store,job_key,posting,requirements,evidence,policy,*,qualification_only=False):
    """Rank the original résumés and check saved facts without changing a job or using a lease."""
    m=legacy('match_resume');library=store.path.parent/'my-resumes'
    resumes,tracks,warnings=m.load_library(str(library))
    errors=m.validate(requirements,posting,tracks)
    if errors:raise Denied('; '.join(errors))
    if requirements['track']=='data' and tracks.get('data'):
        resumes=[r for r in resumes if r['file']==tracks['data'][0]] or resumes
    table=m.rank(resumes,requirements['requirements'],tracks[requirements['track']]);best=table[0]
    chosen=next(r for r in resumes if r['file']==best['file'])
    if requirements.get('additional_conditions'):
        # The ranking rubric omits generic soft skills. Verify these separately against
        # the selected résumé and saved facts; a real required condition must not disappear from review.
        import re
        evidence_words = {'communication':r'\b(?:communicat\w*|presented|presentation\w*|documented|documentation)\b',
                          'collaboration':r'\b(?:collaborat\w*|partnered|cross.functional|worked with|teamed)\b'}
        facts = store.path.parent/'facts.md'
        personal = facts.read_text() if facts.is_file() else ''
        evidence_text = '\n'.join((chosen['work'],chosen['skills'],personal))
        for condition in requirements['additional_conditions']:
            if (condition['quote'] not in posting or any(not re.search(evidence_words.get(skill,r'(?!)'),
                    evidence_text,re.I) for skill in condition['skills'])):
                raise Denied('additional required qualification needs evidence from saved facts')
    j=store.job(job_key);channel='one_click' if evidence.get('channel')=='one_click' else 'form'
    must=[r for r in requirements['requirements'] if r['kind']=='must'];when=timestamp(j.get('posted_at'))
    scored=score(j['title'],[bool(best['marks'][r['id']]) for r in must],(store.clock()-when).total_seconds()/3600 if when else None,
                 'one_click' if channel=='one_click' else 'employer',bool(evidence.get('company_known')))
    review={'refs':evidence['refs'],'requirements_verified':True,'requirements':requirements['gates'],
            'resume_hash':chosen['sha256'],'resume':chosen['file'],'score':scored,'posting_sha256':hashlib.sha256(posting.encode()).hexdigest()}
    j.update(score=scored['score'],requirements=requirements['gates']);why=store.application_gate(j,policy,channel,qualification_only=qualification_only)
    return review,table,warnings,why


def commit_match(store,job_key,posting,requirements,evidence,owner,token):
    if not evidence.get('refs') or evidence.get('description_complete') is not True:
        raise Denied('complete observed posting evidence required')
    if not isinstance(requirements.get('gates'),dict):raise Denied('explicit gate assessment required')
    store.check(owner,token)
    if store.job(job_key)['status'] in ('Applied','Rejected by me','Not eligible','Stale'):
        raise Denied('terminal job must not be rematched')
    review,table,warnings,why=assess_match(store,job_key,posting,requirements,evidence,store.policy_snapshot())
    j=store.job(job_key)
    library=store.path.parent/'my-resumes'
    scored=review['score']
    # A command has higher priority than a match. Never reopen terminal or user-skipped rows.
    terminal=j['status'] in ('Applied','Rejected by me','Not eligible','Stale')
    status=j['status'] if terminal or j['status'] in ('Ready to fill','Walled','Awaiting submit') else 'Matched'
    if why and why not in ('posting outside freshness window','first posted time missing','not first posted on this Eastern day',
                           'original employer posting date needs verification','invalid employer posting date',
                           'employer posting time zone needs verification','first posted time is in the future',
                           'outside fill window','paused','score under 35','PM track only; fill now required','company requires manual application','company on hold') and not terminal:status='Not eligible'
    artifact=store.path.parent/'codex-runtime'/'matches'/hashlib.sha256(job_key.encode()).hexdigest();artifact.parent.mkdir(parents=True,exist_ok=True)
    artifact.with_suffix('.posting.txt').write_text(posting)
    artifact.with_suffix('.json').write_text(json.dumps({'requirements':requirements,'review':review,'ranking':table,'warnings':warnings},indent=2))
    manifest=json.loads((library/'manifest.json').read_text())
    with store.tx():
        store.check(owner,token)
        store.con.execute('UPDATE jobs SET resume=?,score=?,status=?,reason=?,updated_at=? WHERE job_key=?',
                          (review['resume'],scored['score'],status,why,store.now(),job_key))
        store.con.execute('INSERT OR REPLACE INTO jr_cache(key,value) VALUES(?,?)',('review:'+job_key,json.dumps(review)))
        store.con.execute("UPDATE jr_backlog SET state='resolved' WHERE provider='requirements' AND item_key=?",(job_key,))
        store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',(store.now(),'codex-resume-match',job_key,json.dumps(review)))
    cache_put(store,cache_key(posting,manifest),requirements,owner,token)
    return {'job_key':job_key,'resume':review['resume'],'sha256':review['resume_hash'],'score':scored,'status':status,'gate':why,'review':review,'artifact':str(artifact.with_suffix('.json'))}
