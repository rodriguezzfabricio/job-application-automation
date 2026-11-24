"""Qualified alert issuer and current-source readiness; no implicit mail sending."""
from pathlib import Path
from datetime import datetime,timezone
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import urlsplit
import json,re,tempfile
from packet import canonical,digest,job_fingerprint
from policy import instant,merge_key,ashby_identity
from profile_store import load_profile
from source_access import PublicHTTP,collect_ashby,verify_application,validate_public_url
from personal import select_variants,render_resume
from qualification import qualify,VERSION
from polling import _private_directory,_write,MAX_ARTIFACT_BYTES
from mail_transport import _validate

PACKET_VERSION='qualified-alert-2'
class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.text=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if tag=='a':self.links += [v for k,v in attrs if k=='href' and isinstance(v,str)]
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.text.append(data)

def source_registration_url(source,identifier):
    """Resolve a trusted official job-page pattern, never a job-supplied URL."""
    if type(source) is not dict:raise ValueError('JSON source registry object required')
    host=source.get('official_host');base=source.get('careers_url');validate_public_url(base,{host})
    template=source.get('official_job_page_template')
    if template is None:return base
    if not isinstance(template,str) or not re.fullmatch(r'[^{}]+/\{id\}',template):raise ValueError('bounded official job-page template required')
    _,canonical_id=ashby_identity('https://jobs.ashbyhq.com/'+source['board']+'/'+identifier,source['board'])
    if identifier!=canonical_id:raise ValueError('canonical job ID only; suffixes and queries refused')
    url=template.replace('{id}',identifier);parsed=validate_public_url(url,{host})
    if parsed.query:raise ValueError('official job-page template query refused')
    return url

def current_opening(source,identifier,get,now):
    if type(source) is not dict:raise ValueError('JSON source registry object required')
    source=dict(source)
    board=source.get('board');host=source.get('official_host')
    if not isinstance(board,str) or not re.fullmatch('[a-z0-9_-]{1,100}',board) or not isinstance(host,str):raise ValueError('trusted source registration required')
    url=source_registration_url(source,identifier);page=get(url,{host})
    if not isinstance(page,bytes) or len(page)>20*1024*1024 or b'<html' not in page.lower():raise ValueError('official careers page unavailable')
    parser=Links();parser.feed(page.decode('utf-8',errors='replace'));owned=False
    if source.get('official_job_page_template') is not None:
        visible=' '.join(parser.text).casefold()
        if re.search(r'\b(?:page|job|opening|position) (?:was )?not found\b|no longer accepting applications|position has been filled',visible):raise ValueError('official job page unavailable or closed')
    for link in parser.links:
        try:
            p=urlsplit(link)
            if p.scheme=='https' and p.hostname=='jobs.ashbyhq.com' and p.path.strip('/').split('/')[0]==board and not p.username and not p.password:
                if source.get('official_job_page_template') is None or ashby_identity(link,board)[1]==identifier:owned=True
        except ValueError:continue
    if not owned:raise ValueError('official employer does not link registered job board')
    jobs=collect_ashby(board,get,now);matches=[j for j in jobs if j['id']=='ashby:'+board+':'+identifier and j['active']]
    if len(matches)!=1:raise ValueError('opening no longer listed')
    job=matches[0]
    verification=verify_application(job,get,now=now)
    verification.update(employer_board_verified=True,careers_url=source['careers_url'],registered_careers_page_fetched=url==source['careers_url'],careers_page_sha256=digest(page) if url==source['careers_url'] else None,ownership_url=url,ownership_page_sha256=digest(page),verified_at=now)
    return job,verification

def _body(job,decision,first_seen,prepared):
    lines=['READY TO REVIEW — likely fit; you decide whether to apply.', 'Employer: '+job['employer'],'Role: '+job['title'],'Location: '+job['location'],'Work arrangement: '+job.get('workplace','unknown'),'Apply: '+job['url'],
           'Provider publication timestamp: '+str(job.get('published_at'))+' ('+job.get('publication_semantics','unknown')+')','Original publication time: unknown unless explicitly recorded by employer.','First detected UTC: '+first_seen,'Prepared UTC: '+prepared,
           'First detected local: '+instant(first_seen).astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).isoformat(),'No application has been submitted.','Fit evidence:']
    for row in decision['evidence_rows']:lines += [row['importance'].upper()+' / '+row['status'].upper()+': '+row['job_requirement'],row['explanation']]
    lines += ['Screening is not guaranteed. All résumé bullet text is selected unchanged from the approved original source.','Salary and deadline: not verified in this packet; check the application page.']
    return '\n'.join(lines)+'\n'

def _render(profile,approval,job):
    selected=select_variants(profile,job,8,approval_sha256=approval)
    while selected:
        try:return selected,render_resume(profile,job,selected,approval_sha256=approval,job_binding=job_fingerprint(job))
        except ValueError as exc:
            if not str(exc).startswith('one-page draft overflow'):raise
            selected.pop()
    raise ValueError('no readable source-bound resume fits')


def build_alert_message(job,decision,first_seen,prepared,key,pdf,profile,sender):
    """Deterministic qualified-alert MIME (E9-S-002): identical inputs always
    produce byte-identical output, including the multipart boundary."""
    msg=EmailMessage(policy=policy.SMTP);msg['From']=sender;msg['To']=profile['recipient_email'];msg['Subject']='[JOB — REVIEW & APPLY] '+job['title']+' — '+job['employer'];msg['Message-ID']='<'+key+'@job-assistant.local>'
    msg.set_content(_body(job,decision,first_seen,prepared));msg.add_attachment(pdf,maintype='application',subtype='pdf',filename='resume-'+key+'.pdf')
    msg.set_boundary('jobassistant-'+key)
    raw=msg.as_bytes();_validate(raw,profile['recipient_email'],sender)
    return raw

def prepare_alert(profile_path,approval_path,source,identifier,extraction,output,*,sender,now,first_seen,get=None):
    instant(now)
    if not 0<=(instant(now)-instant(first_seen)).total_seconds():raise ValueError('future first-seen timestamp')
    http=get or PublicHTTP();profile,approval=load_profile(profile_path,approval_path)
    if not profile.get('recipient_email'):raise ValueError('approved alert destination required')
    job,verification=current_opening(source,identifier,http,now);decision=qualify(profile,job,extraction,now,approval_sha256=approval)
    if decision['status']!='eligible':return {'status':decision['status'],'decision':decision}
    root=_private_directory(output);key=digest(canonical({'job':job_fingerprint(job),'profile':approval,'sender':sender,'packet_version':PACKET_VERSION,'qualification_policy':VERSION,'prepared_day_utc':instant(now).date().isoformat()}))[:32];directory=root/key
    if directory.exists():
        if not validate_alert(directory,profile_path,approval_path,source,now,get=http):raise ValueError('existing alert failed validation')
        return {'status':'cached','directory':str(directory),'canonical_job':merge_key(job)}
    selected,pdf=_render(profile,approval,job)
    raw=build_alert_message(job,decision,first_seen,now,key,pdf,profile,sender)
    manifest={'packet_version':PACKET_VERSION,'qualification_policy':VERSION,'application_ready':True,'job':job,'job_hash':job_fingerprint(job),'canonical_job':merge_key(job),'profile_hash':approval,'source_pdf_sha256':profile['source']['sha256'],'selected_ids':selected,'pdf_sha256':digest(pdf),'email_sha256':digest(raw),'recipient':profile['recipient_email'],'sender':sender,'source_registration':source,'extraction':extraction,'decision':decision,'verification':verification,'first_seen_at':first_seen,'prepared_at':now,'qualified_at':now,'qualified_at_epoch':int(instant(now).timestamp()),'original_publication_at':job.get('published_at') if job.get('publication_semantics')=='original' else None,'publication_semantics':job.get('publication_semantics'),'external_messages_sent':0}
    with tempfile.TemporaryDirectory(prefix='.issuing-',dir=root) as name:
        staging=Path(name)
        for filename,data in [('manifest.json',canonical(manifest)),('resume.pdf',pdf),('message.eml',raw)]:_write(root,staging/filename,data,MAX_ARTIFACT_BYTES)
        if not validate_alert(staging,profile_path,approval_path,source,now,get=http):raise ValueError('new qualified alert validation failed')
        staging.rename(directory)
    return {'status':'prepared','directory':str(directory),'canonical_job':merge_key(job)}

def validate_alert(directory,profile_path,approval_path,source,now,*,get=None):
    try:
        d=Path(directory);profile,approval=load_profile(profile_path,approval_path)
        for name in ['manifest.json','resume.pdf','message.eml']:
            f=d/name
            if f.is_symlink() or not f.is_file() or f.stat().st_size>4*1024*1024:return False
        m=json.loads((d/'manifest.json').read_text());pdf=(d/'resume.pdf').read_bytes();raw=(d/'message.eml').read_bytes()
        job,verification=current_opening(source,m['job']['id'].rsplit(':',1)[1],get or PublicHTTP(),now)
        if m['packet_version']!=PACKET_VERSION or m['qualification_policy']!=VERSION or m['application_ready'] is not True or m['source_registration']!=source:return False
        if m.get('qualified_at')!=m['prepared_at'] or not isinstance(m.get('qualified_at_epoch'),int) or int(instant(m['prepared_at']).timestamp())!=m['qualified_at_epoch']:return False
        key=digest(canonical({'job':m['job_hash'],'profile':m['profile_hash'],'sender':m['sender'],'packet_version':PACKET_VERSION,'qualification_policy':VERSION,'prepared_day_utc':instant(m['prepared_at']).date().isoformat()}))[:32]
        if job_fingerprint(job)!=m['job_hash'] or job_fingerprint(m['job'])!=m['job_hash'] or m['canonical_job']!=merge_key(job):return False
        if m['profile_hash']!=approval or m['source_pdf_sha256']!=profile['source']['sha256'] or m['recipient']!=profile['recipient_email']:return False
        if not 0<=(instant(now)-instant(m['prepared_at'])).total_seconds()<=86400 or instant(m['first_seen_at'])>instant(m['prepared_at']):return False
        decision=qualify(profile,job,m['extraction'],now,approval_sha256=approval)
        if decision!=m['decision'] or decision['status']!='eligible':return False
        if digest(pdf)!=m['pdf_sha256'] or digest(raw)!=m['email_sha256'] or render_resume(profile,job,m['selected_ids'],approval_sha256=approval,job_binding=job_fingerprint(job))!=pdf:return False
        # E9-S-002: the message must be deterministically regenerable byte-for-byte.
        if build_alert_message(job,decision,m['first_seen_at'],m['prepared_at'],key,pdf,profile,m['sender'])!=raw:return False
        _validate(raw,m['recipient'],m['sender']);msg=BytesParser(policy=policy.default).parsebytes(raw);parts=list(msg.iter_attachments())
        return len(parts)==1 and parts[0].get_payload(decode=True)==pdf and str(msg['Subject'])=='[JOB — REVIEW & APPLY] '+job['title']+' — '+job['employer'] and msg.get_body(preferencelist=('plain',)).get_content().replace('\r\n','\n')==_body(job,decision,m['first_seen_at'],m['prepared_at'])
    except (ValueError,KeyError,TypeError,OSError):return False

def alert_readiness(profile_path,approval_path,source,packet_root,now,*,get=None):
    root=Path(packet_root).resolve()
    def check(item):
        metadata=item.get('metadata',{});key=metadata.get('packet_key')
        if metadata.get('packet_kind')!='qualified' or not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):return False
        directory=root/key
        if directory.is_symlink() or not validate_alert(directory,profile_path,approval_path,source,now,get=get):return False
        m=json.loads((directory/'manifest.json').read_text())
        return (item.get('canonical_job')==m['canonical_job'] and item.get('profile_hash')==m['profile_hash'] and item.get('job_hash')==m['job_hash'] and item.get('pdf_hash')==m['pdf_sha256'] and item.get('recipient')==m['recipient'] and item.get('sender')==m['sender'] and digest(item['payload'])==m['email_sha256'])
    return check
