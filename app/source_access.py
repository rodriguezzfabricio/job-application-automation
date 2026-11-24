"""Bounded public Ashby reader. No login, scraping bypass, scheduler or email."""
import hashlib,ipaddress,json,re,socket,urllib.request,urllib.error
from urllib.parse import urlsplit
from datetime import datetime,timezone
from policy import source_record,instant,merge_key,ashby_identity,text_only

ENVELOPE_KINDS=('unsupported','partial','malformed','error')


class EnvelopeError(ValueError):
    """Typed source-envelope failure class (Q144).

    Only a complete VALID envelope may establish a successful snapshot or mutate
    activity. 'unsupported': structure this adapter cannot interpret (not an
    object, missing/oversized jobs body). 'partial': reserved for paginated
    adapters where only part of the listing was captured (never emitted by the
    full-listing Ashby adapter). 'malformed': JSON structure present but rows or
    identity invalid. 'error': transport/HTTP/protocol failures. Callers must
    preserve last-good state on any of these; none may qualify/enqueue/send.
    """
    def __init__(self,kind,message):
        if kind not in ENVELOPE_KINDS:raise ValueError('unknown envelope kind')
        super().__init__(kind+': '+message)
        self.kind=kind
        self.message=message


def validate_public_url(url,hosts):
    if not isinstance(url,str) or any(ord(c)<=32 or ord(c)==127 for c in url):raise ValueError('invalid raw public URL')
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in hosts or p.username is not None or p.password is not None or p.port not in (None,443) or p.fragment:raise ValueError('unapproved public URL')
    return p

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise EnvelopeError('error','redirect requires explicit source review')

class PublicHTTP:
    def __init__(self):self.opener=urllib.request.build_opener(NoRedirect())
    def __call__(self,url,hosts):
        p=validate_public_url(url,hosts)
        try:
            addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise EnvelopeError('error','dns resolution failed') from exc
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise EnvelopeError('error','nonpublic destination refused')
        request=urllib.request.Request(url,headers={'User-Agent':'PersonalJobResearch/0.1','Accept':'application/json,text/html'})
        # No automatic retries: caller records failure/Retry-After, never a successful empty feed.
        try:
            with self.opener.open(request,timeout=15) as response:
                if response.status!=200:raise EnvelopeError('error','unexpected HTTP status '+str(response.status))
                data=response.read(20*1024*1024+1)
        except urllib.error.HTTPError as exc:
            raise EnvelopeError('error','http '+str(exc.code)) from exc
        except (urllib.error.URLError,TimeoutError,socket.timeout,ConnectionError) as exc:
            raise EnvelopeError('error','transport failure') from exc
        if len(data)>20*1024*1024:raise EnvelopeError('unsupported','response exceeds20MiB limit')
        return data

def collect_ashby(board,get,observed_at):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',board):raise ValueError('invalid board name')
    board=board.lower()
    instant(observed_at);raw=get('https://api.ashbyhq.com/posting-api/job-board/'+board,{'api.ashbyhq.com'})
    try:body=json.loads(raw)
    except (ValueError,UnicodeDecodeError) as exc:raise EnvelopeError('malformed','invalid JSON body') from exc
    if not isinstance(body,dict) or not isinstance(body.get('jobs'),list):raise EnvelopeError('unsupported','incomplete or invalid jobs response')
    result=[];seen=set()
    for row in body['jobs']:
        if not isinstance(row,dict) or type(row.get('isListed')) is not bool:raise EnvelopeError('malformed','invalid listing flag')
        if row.get('location') is not None and not isinstance(row['location'],str):raise EnvelopeError('malformed','invalid source location')
        j=source_record('ashby',board,row)
        if j['id'] in seen:raise EnvelopeError('malformed','duplicate source identity')
        seen.add(j['id']);row_sha=hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest();j.update(source_hash=row_sha,source_observed_at=observed_at,source_id='ashby:'+board,active=row.get('isListed') is True)
        result.append(j)
    return result

def verify_application(job,get,*,now=None):
    if job.get('source_kind')!='ashby' or job.get('active') is not True or job.get('is_listed') is not True:raise ValueError('current listed provider record required')
    try:age=(instant(now or datetime.now(timezone.utc).isoformat())-instant(job.get('source_observed_at'))).total_seconds()
    except (ValueError,TypeError):raise ValueError('source observation timestamp missing/invalid') from None
    if not 0<=age<=900:raise ValueError('source observation stale or future')
    validate_public_url(job['url'],{'jobs.ashbyhq.com'})
    if not isinstance(job.get('employer'),str) or not job['employer']:raise ValueError('source employer required')
    board,identifier=ashby_identity(job['url'],job['employer'])
    if job['id']!='ashby:'+board+':'+identifier:raise ValueError('application identity differs from source')
    page=get(job['url'],{'jobs.ashbyhq.com'})
    if not page or b'<html' not in page.lower():raise ValueError('application page not readable HTML')
    visible=text_only(page.decode('utf-8',errors='replace')).casefold()
    if any(marker in visible for marker in ['no longer accepting applications','this job is no longer available','this position has been filled','job not found']):raise ValueError('application page explicitly closed')
    return {'status':'application_url_reachable','source_observation_age_seconds':age,'url':job['url'],'canonical_identity':merge_key(job),'application_page_sha256':hashlib.sha256(page).hexdigest(),'limits':'Given a caller-supplied listed source record observed within15minutes, HTTPS page is reachable. This helper does not independently re-fetch the feed or establish employer ownership, application completion, or rendered form availability.'}
