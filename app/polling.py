"""One private public-source polling run. Review packets only; no sending.

This connects existing collection, profile validation, matching, SQLite and PDF
helpers. It neither admits an employer source for unattended use nor verifies
all semantic requirements. Unclear jobs cannot become application-ready alerts.
"""
from datetime import datetime,timezone
from email import policy as email_policy
from email.message import EmailMessage
from email.parser import BytesParser
import json,os,re,stat,tempfile
from pathlib import Path
from packet import canonical,digest,job_fingerprint
from policy import fit,role_gate,merge_key,instant
from profile_store import load_profile
from matching import candidate_context,assess
from requirement_review import inspect_fit
from eligibility import VERSION as ELIGIBILITY_VERSION
from semantic_segments import VERSION as SEMANTIC_VERSION
from personal import select_variants,render_resume
from source_access import PublicHTTP,collect_ashby,verify_application
from mail_transport import _validate as validate_mail_structure
from state import Store

MAX_ARTIFACT_BYTES=500*1024*1024
SENDER='review@example.invalid'
PACKET_VERSION='private-review-1'


def _packet_key(approval,job):
    return digest(canonical({'profile':approval,'job':job_fingerprint(job),'packet_version':PACKET_VERSION}))[:24]


def _private_directory(path):
    path=Path(path);path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('owned private output directory required')
    return path


def _artifact_size(root):
    total=0
    for path in root.rglob('*'):
        if path.is_symlink():raise ValueError('symlink in private artifact tree')
        if not (stat.S_ISDIR(path.lstat().st_mode) or stat.S_ISREG(path.lstat().st_mode)):raise ValueError('special file in private artifact tree')
        if path.is_file() and path.suffix!='.sqlite' and not path.name.endswith('.runner.lock'):
            total+=path.stat().st_size
    return total


def _write(root,path,data,cap):
    if _artifact_size(root)+len(data)-(path.stat().st_size if path.exists() else 0)>cap:
        raise ValueError('private artifact cap exceeded; hold work for retention review')
    fd,name=tempfile.mkstemp(prefix='.writing-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _body(job,decision,first_seen):
    evidence=decision.get('semantic_inspection',{}).get('evidence_rows',[])
    evidence_text=('\n\nRequirement evidence:\n'+'\n\n'.join(row['status'].upper()+' ('+row['importance']+'): '+row['job_requirement']+'\n'+row['explanation'] for row in evidence)) if evidence else ''
    return ('REVIEW DRAFT ONLY. Not an application-ready recommendation. No email was sent.\n\n'
            +'Employer board: '+job['employer']+'\nRole: '+job['title']+'\nLocation: '+job['location']
            +'\nApplication URL: '+job['url']+'\nFirst detected UTC: '+first_seen
            +'\nProvider timestamp: '+str(job.get('published_at'))+' ('+job.get('publication_semantics','unknown')+')'
            +'\nOriginal publication may be unknown; no one-hour claim.\nDecision: '+decision['status'].upper()
            +'\nReasons: '+'; '.join(decision['reasons'])
            +'\nEmployer ownership and complete qualification are not certified by URL reachability.'
            +'\nThe PDF selects unchanged original-source facts. No application was submitted.\n'+evidence_text+('\n' if evidence_text else ''))


def _valid_cached(directory,profile,approval,job,decision,first_seen):
    try:
        for name in ['manifest.json','resume.pdf','message.eml']:
            info=(directory/name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size>4*1024*1024:return False
        manifest=json.loads((directory/'manifest.json').read_text())
        verification=manifest['verification']
        if type(manifest['external_messages_sent']) is not int or manifest['external_messages_sent']!=0:return False
        if not isinstance(verification,dict) or verification.get('status')!='application_url_reachable' or verification.get('url')!=job['url'] or verification.get('canonical_identity')!=merge_key(job):return False
        if not re.fullmatch('[0-9a-f]{64}',str(verification.get('application_page_sha256',''))):return False
        age=verification.get('source_observation_age_seconds')
        if type(age) not in (int,float) or not 0<=age<=900:return False
        pdf=(directory/'resume.pdf').read_bytes();raw=(directory/'message.eml').read_bytes()
        if manifest['packet_version']!=PACKET_VERSION or manifest['application_ready'] is not False or manifest['job_hash']!=job_fingerprint(job) or manifest['profile_hash']!=approval:return False
        if manifest['job_id']!=job['id'] or manifest['canonical_job']!=merge_key(job) or manifest['source_pdf_sha256']!=profile['source']['sha256'] or manifest['decision']!=decision or manifest['first_seen_at']!=first_seen:return False
        if manifest['pdf_sha256']!=digest(pdf) or manifest['email_sha256']!=digest(raw):return False
        if pdf!=render_resume(profile,job,manifest['selected_ids'],approval_sha256=approval):return False
        validate_mail_structure(raw,profile['recipient_email'],SENDER)
        msg=BytesParser(policy=email_policy.default).parsebytes(raw);parts=list(msg.iter_attachments())
        key=_packet_key(approval,job)
        return (str(msg['Subject'])=='[REVIEW DRAFT - NOT READY] '+job['title']+' - '+job['employer']
                and str(msg['Message-ID'])=='<'+key+'@review.example.invalid>'
                and len(parts)==1 and parts[0].get_payload(decode=True)==pdf
                and msg.get_body(preferencelist=('plain',)).get_content().replace('\r\n','\n')==_body(job,decision,first_seen))
    except (ValueError,KeyError,TypeError,OSError):return False


def _review_packet(root,profile,approval,job,decision,verification,first_seen,cap):
    key=_packet_key(approval,job)
    directory=root/key
    if directory.exists():
        valid=_valid_cached(directory,profile,approval,job,decision,first_seen)
        if valid:
            manifest=json.loads((directory/'manifest.json').read_text())
            manifest['verification']=verification
            _write(root,directory/'manifest.json',canonical(manifest),cap)
        else:
            _write(root,directory/'HELD.json',canonical({'status':'held_invalid_cache','application_ready':False,'external_messages_sent':0}),cap)
        return {'directory':str(directory),'status':'cached_review' if valid else 'held_invalid_cache','first_seen_at':first_seen,'current_verification':verification}
    selected=select_variants(profile,job,8,approval_sha256=approval)
    while selected:
        try:pdf=render_resume(profile,job,selected,approval_sha256=approval);break
        except ValueError as error:
            if not str(error).startswith('one-page draft overflow'):raise
            selected.pop()
    if not selected:raise ValueError('no readable original-source draft fits')
    msg=EmailMessage(policy=email_policy.SMTP);msg['From']=SENDER;msg['To']=profile['recipient_email']
    msg['Subject']='[REVIEW DRAFT - NOT READY] '+job['title']+' - '+job['employer']
    msg['Message-ID']='<'+key+'@review.example.invalid>';msg.set_content(_body(job,decision,first_seen))
    msg.add_attachment(pdf,maintype='application',subtype='pdf',filename='resume-review.pdf')
    raw=msg.as_bytes();validate_mail_structure(raw,profile['recipient_email'],SENDER)
    manifest={'packet_version':PACKET_VERSION,'application_ready':False,'job_id':job['id'],'canonical_job':merge_key(job),'job_hash':job_fingerprint(job),'profile_hash':approval,'source_pdf_sha256':profile['source']['sha256'],'selected_ids':selected,'pdf_sha256':digest(pdf),'email_sha256':digest(raw),'decision':decision,'verification':verification,'first_seen_at':first_seen,'external_messages_sent':0}
    with tempfile.TemporaryDirectory(prefix='.preparing-',dir=root) as temporary:
        staging=Path(temporary)
        for name,data in [('resume.pdf',pdf),('message.eml',raw),('manifest.json',canonical(manifest))]:_write(root,staging/name,data,cap)
        if not _valid_cached(staging,profile,approval,job,decision,first_seen):raise ValueError('prepared review packet validation failed')
        staging.rename(directory)
        descriptor=os.open(root,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    return {'directory':str(directory),'status':'prepared_review','first_seen_at':first_seen}


def poll_once(board,profile_path,approval_path,output,*,get=None,now=None,packet_limit=3,artifact_cap_bytes=MAX_ARTIFACT_BYTES,semantic_extractor=None):
    if not isinstance(board,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',board):raise ValueError('invalid board name')
    board=board.lower()
    if semantic_extractor is not None and getattr(semantic_extractor,'mode',None)!='segments':raise ValueError('explicit segment extractor required')
    if type(packet_limit) is not int or not 0<=packet_limit<=20:raise ValueError('packet limit must be0..20')
    if type(artifact_cap_bytes) is not int or not 4096<=artifact_cap_bytes<=MAX_ARTIFACT_BYTES:raise ValueError('invalid artifact cap')
    root=_private_directory(output)
    if _artifact_size(root)>artifact_cap_bytes:raise ValueError('private artifacts already exceed cap')
    profile,approval=load_profile(profile_path,approval_path)
    context=candidate_context(profile,approval_sha256=approval)
    at=now or datetime.now(timezone.utc).isoformat();instant(at)
    http=get or PublicHTTP();store=Store(root/'state.sqlite');source='ashby:'+board
    result={'mode':'private_review_poll','source_id':source,'automatic_alerts_enabled':False,'external_messages_sent':0,'source_jobs':0,'review':0,'reject':0,'eligible':0,'held':0,'packets':[]}
    with store.runner_lock() as acquired:
        if not acquired:return dict(result,status='busy')
        try:jobs=collect_ashby(board,http,at)
        except Exception as error:
            store.snapshot(source,None,at,error=type(error).__name__)
            result.update(status='source_failed',error_class=type(error).__name__,source_checked_at=at)
            source_root=_private_directory(_private_directory(root/'sources')/board)
            _write(root,source_root/'run-report.json',canonical(result),artifact_cap_bytes)
            _write(root,root/'run-report.json',canonical(result),artifact_cap_bytes)
            return result
        if semantic_extractor is not None:
            jobs=[dict(j,inspection_policy=SEMANTIC_VERSION+'-'+ELIGIBILITY_VERSION+'-comparison-5-email-2') for j in jobs]
        store.snapshot(source,[j['id'] for j in jobs],at,hashes={j['id']:job_fingerprint(j) for j in jobs},active_ids=[j['id'] for j in jobs if j['active']])
        result['source_jobs']=len(jobs);records=[]
        # Explicit early-career titles get bounded draft priority, without rejecting ambiguous titles.
        jobs.sort(key=lambda job:('early-career level not established' in role_gate(job,at),(root/_packet_key(approval,job)).exists(),job['id']))
        for job in jobs:
            first_seen=store.job(source,job['id'])['first_seen'];basic=fit(context,job)
            try:decision=assess(profile,job,at,approval_sha256=approval) if basic['status']!='reject' else basic
            except (ValueError,TypeError,KeyError):decision={'status':'review','reasons':['profile or requirement assessment unavailable']}
            result[decision['status']]+=1
            record={'job':job,'first_seen_at':first_seen,'decision':decision}
            records.append(record)
            gate=role_gate(job,at)
            if decision['status']=='reject' or gate or not job.get('description') or len(result['packets'])>=packet_limit:continue
            try:
                if semantic_extractor is not None:
                    extraction=semantic_extractor.extract(job['description'],now=at)
                    inspection=inspect_fit(profile,job,extraction,approval_sha256=approval)
                    decision['semantic_inspection']=inspection
                    decision['reasons']=list(dict.fromkeys(decision['reasons']+inspection['reasons']))
                    if inspection['status'] in {'review','reject'} and decision['status']!=inspection['status']:
                        result[decision['status']]-=1;result[inspection['status']]+=1;decision['status']=inspection['status']
                    if decision['status']=='reject':continue
                checked_at=now or datetime.now(timezone.utc).isoformat()
                verification=verify_application(job,http,now=checked_at)
                packet=_review_packet(root,profile,approval,job,decision,verification,first_seen,artifact_cap_bytes)
                if packet['status'] in {'prepared_review','cached_review'}:
                    from delivery import DeliveryQueue
                    directory=Path(packet['directory']);manifest=json.loads((directory/'manifest.json').read_text())
                    if not _valid_cached(directory,profile,approval,job,decision,first_seen):raise ValueError('packet not valid for durable staging')
                    staged=DeliveryQueue(root/'delivery.sqlite').stage(merge_key(job),approval,job_fingerprint(job),(directory/'message.eml').read_bytes(),recipient=profile['recipient_email'],sender=SENDER,pdf_sha256=manifest['pdf_sha256'],metadata={'job':job,'decision':decision,'packet_key':directory.name,'first_seen_at':first_seen,'packet_kind':'review'})
                    packet['outbox_status']=staged['status'];packet['outbox_key']=staged['key']
                    if staged['status']=='conflict':result['held']+=1
                result['packets'].append(dict(packet,job_id=job['id']))
                if packet['status']=='held_invalid_cache':result['held']+=1
            except (ValueError,OSError):
                record['packet_status']='held_validation_or_capacity';result['held']+=1
        source_root=_private_directory(_private_directory(root/'sources')/board)
        _write(root,source_root/'jobs-and-decisions.json',canonical(records),artifact_cap_bytes)
        result.update(status='completed_with_holds' if result['held'] else 'completed',source_checked_at=at,profile_hash=approval)
        _write(root,source_root/'run-report.json',canonical(result),artifact_cap_bytes)
        _write(root,root/'run-report.json',canonical(result),artifact_cap_bytes)
    return result
