"""Explicit bounded DeepSeek extraction, independently disabled from alert polling.

Source extraction sends no candidate data. Explicit shadow evidence assessment sends
only original professional bullet facts, without contact fields. No tool execution,
implicit retries, qualification authority or mail. Every attempted
call reserves worst-context cost. Unknown outcomes retain the reservation.
"""
import json,os,stat,tempfile,urllib.request,urllib.error
from pathlib import Path
from deepseek_matching import build_request,parse_response,cost_microusd
from semantic_segments import build_segment_request,parse_segment_response
from packet import canonical,digest
from policy import instant
from state import Store

ENDPOINT='https://api.deepseek.com/chat/completions'
# Entire documented 1M context at peak uncached input price plus 2K output.
# More conservative than estimating the small actual prompt before tokenization.
RESERVATION_MICROUSD=302400
MONTHLY_CAP_MICROUSD=1500000
MAX_RESPONSE_BYTES=256000

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('provider redirect refused')


def _http(method,url,headers,body,timeout):
    if method!='POST' or url!=ENDPOINT:raise ValueError('provider endpoint refused')
    opener=urllib.request.build_opener(_NoRedirect())
    request=urllib.request.Request(url,data=body,headers=headers,method=method)
    try:
        with opener.open(request,timeout=timeout) as response:
            data=response.read(MAX_RESPONSE_BYTES+1);status=response.status
    except urllib.error.HTTPError as error:
        return error.code,b''  # Never echo provider error bodies or request secrets.
    if len(data)>MAX_RESPONSE_BYTES:raise ValueError('provider response too large')
    return status,data


def _private(path,directory=False):
    info=path.lstat()
    expected=stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('owned private regular file/directory required')
    if not directory and info.st_size>MAX_RESPONSE_BYTES:raise ValueError('private file too large')


class DeepSeekExtractor:
    def __init__(self,key_path,state_path,cache_dir,*,http=None,monthly_cap_microusd=MONTHLY_CAP_MICROUSD,mode='quoted'):
        if mode not in {'quoted','segments'}:raise ValueError('invalid extraction mode')
        self.mode=mode
        if type(monthly_cap_microusd) is not int or not 0<monthly_cap_microusd<=MONTHLY_CAP_MICROUSD:raise ValueError('invalid model cap')
        self.key_path=Path(key_path);self.state_path=Path(state_path);self.cache=Path(cache_dir)
        self.http=http or _http;self.cap=monthly_cap_microusd

    def extract(self,description,*,now):
        builder,parser=(build_segment_request,parse_segment_response) if self.mode=='segments' else (build_request,parse_response)
        request=builder(description)
        return self._execute(request,lambda response:parser(response,description),now)

    def assess_evidence(self,profile,job,extraction,*,approval_sha256,fact_export_path,fact_export_sha256,now):
        from semantic_evidence import build_evidence_request,parse_evidence_response
        from profile_store import _private_json
        request,context=build_evidence_request(profile,job,extraction,approval_sha256=approval_sha256,fact_export_approval=_private_json(fact_export_path),fact_export_sha256=fact_export_sha256)
        return self._execute(request,lambda response:parse_evidence_response(response,context),now)

    def _execute(self,request,parse_result,now):
        at=instant(now);period='deepseek:'+at.strftime('%Y-%m')
        _private(self.key_path);_private(self.key_path.parent,directory=True)
        self.cache.mkdir(parents=True,exist_ok=True,mode=0o700);_private(self.cache,directory=True)
        self.state_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);_private(self.state_path.parent,directory=True)
        if self.state_path.exists():_private(self.state_path)
        store=Store(self.state_path);identity=digest(canonical(request));path=self.cache/(identity+'.json')
        with store.runner_lock() as acquired:
            if not acquired:raise ValueError('model worker busy')
            if path.exists():
                _private(path);saved=json.loads(path.read_bytes())
                if saved.get('request_sha256')!=identity:raise ValueError('cache identity mismatch')
                result=parse_result(saved['response'])
                return dict(result,cache_hit=True,request_sha256=identity,incremental_cost_estimate_microusd=0)
            with store.connection() as db:
                prior=db.execute('SELECT 1 FROM reservations WHERE id=?',(identity,)).fetchone()
            if prior:raise ValueError('prior model call exists without reusable cache; review required')
            if not store.reserve(period,identity,RESERVATION_MICROUSD,self.cap):raise ValueError('model budget unavailable or prior call outcome unresolved')
            key=self.key_path.read_text().strip()
            if not key or any(c.isspace() for c in key):raise ValueError('invalid provider key')
            status=None;phase='http_request'
            try:
                status,raw=self.http('POST',ENDPOINT,{'Authorization':'Bearer '+key,'Content-Type':'application/json'},canonical(request),45)
                phase='http_status'
                if status!=200:raise ValueError('provider request rejected')
                phase='body_validation'
                if not isinstance(raw,bytes) or len(raw)>MAX_RESPONSE_BYTES:raise ValueError('invalid provider body')
                response=json.loads(raw);phase='usage_validation';usage=response['usage']
                actual=cost_microusd(usage['prompt_tokens'],usage['completion_tokens'])
                phase='budget_settlement';store.settle(period,identity,actual)
            except Exception as exc:
                # Retain only machine diagnostics: never provider bodies, source
                # text, exception messages, request headers or credentials.
                diagnostic={'request_sha256':identity,'observed_at':at.isoformat(),'phase':phase,'http_status':status if type(status) is int and 100<=status<=599 else None,'error_class':type(exc).__name__ if type(exc).__name__ in {'TimeoutError','HTTPError','ValueError','OSError','URLError','JSONDecodeError','KeyError','TypeError'} else 'Exception','automatic_retry':False,'reservation_retained':True}
                try:
                    fd=os.open(self.cache/(identity+'.failure.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as stream:stream.write(canonical(diagnostic));stream.flush();os.fsync(stream.fileno())
                except OSError:pass
                raise ValueError('model request failed or output held; automatic retry disabled') from None
            saved=canonical({'request_sha256':identity,'response':response,'observed_at':at.isoformat()})
            fd,name=tempfile.mkstemp(prefix='.writing-',dir=self.cache)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(saved);stream.flush();os.fsync(stream.fileno())
                os.replace(name,path)
            finally:
                if os.path.exists(name):os.unlink(name)
            # Preserve billed responses even when grounding/schema validation fails.
            # Future reads revalidate; an invalid cached response is never approved.
            try:result=parse_result(response)
            except ValueError:raise ValueError('model output held; private response preserved; no automatic retry') from None
            return dict(result,cache_hit=False,request_sha256=identity,incremental_cost_estimate_microusd=actual)
