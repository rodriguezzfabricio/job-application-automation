"""Single-host scheduled assistant. Private configuration, no implicit activation."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from email.utils import parsedate_to_datetime
import json,re,time,sqlite3
from profile_store import _private_json,load_profile
from packet import canonical,digest,job_fingerprint
from policy import instant,fit,role_gate,merge_key
from matching import candidate_context
from qualification import qualify,VERSION
from semantic_segments import VERSION as EXTRACTION_VERSION
from polling import _private_directory,_write,MAX_ARTIFACT_BYTES
from source_access import PublicHTTP,collect_ashby,validate_public_url
from alerts import current_opening,prepare_alert,alert_readiness,source_registration_url
from delivery import DeliveryQueue,DeliveryWorker
from state import Store

class RunWindowExpired(TimeoutError):pass

class _PilotTransport:
    """Check the stop time again immediately before any provider handoff."""
    def __init__(self,transport,ends,clock):self.transport=transport;self.ends=instant(ends);self.clock=clock
    def preflight(self,raw):
        check=getattr(self.transport,'preflight',None)
        return check(raw) if check else True
    def send(self,raw):
        if instant(self.clock())>=self.ends:return {'state':'not_attempted','reason':'pilot_expired'}
        return self.transport.send(raw)

def configuration(path):
    c=_private_json(path)
    if c.get('version')!=1 or type(c.get('sending_enabled')) is not bool or type(c.get('semantic_enabled')) is not bool:raise ValueError('explicit service flags required')
    if type(c.get('notify_existing',False)) is not bool:raise ValueError('explicit bootstrap policy required')
    if not isinstance(c.get('sources'),list) or not 1<=len(c['sources'])<=10:raise ValueError('one to ten registered sources required')
    if type(c.get('shortlist_limit',3)) is not int or not 1<=c.get('shortlist_limit',3)<=20:raise ValueError('bounded shortlist required')
    for field,default,ceiling in [('daily_email_cap',20,20),('monthly_email_cap',600,600)]:
        value=c.get(field,default)
        if type(value) is not int or not 1<=value<=ceiling:raise ValueError('bounded email caps required')
    if c.get('pilot_ends_at') is not None:instant(c['pilot_ends_at'])
    if c['sending_enabled']:
        if not c.get('pilot_ends_at') or 'daily_email_cap' not in c or 'monthly_email_cap' not in c:raise ValueError('enabled sending requires explicit pilot deadline and caps')
        if c['daily_email_cap']>3 or c['monthly_email_cap']>60:raise ValueError('pilot ceilings are three daily and sixty monthly attempts')
        if instant(c['pilot_ends_at'])>datetime.now(timezone.utc)+timedelta(days=7):raise ValueError('pilot horizon cannot exceed seven days')
    boards=set()
    for s in c['sources']:
        board=s.get('board');seconds=s.get('interval_seconds')
        if not isinstance(board,str) or not re.fullmatch('[a-z0-9_-]{1,100}',board) or board in boards:raise ValueError('unique source board required')
        boards.add(board)
        if type(seconds) is not int or not 1800<=seconds<=86400:raise ValueError('source interval must be1800..86400seconds')
        validate_public_url(s.get('careers_url'),{s.get('official_host')})
        if s.get('official_job_page_template') is not None:source_registration_url(s,'00000000-0000-4000-8000-000000000000')
    for field in ['profile_path','approval_path','state_dir']:
        if not isinstance(c.get(field),str) or not Path(c[field]).is_absolute():raise ValueError('absolute private service paths required')
    return c

def _store(root):
    store=Store(root/'service.sqlite')
    with store.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS service_sources(board TEXT PRIMARY KEY,last_attempt TEXT,last_success TEXT,next_due TEXT,failures INTEGER NOT NULL DEFAULT 0,error_class TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS processed(key TEXT PRIMARY KEY,board TEXT,status TEXT,at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS decision_records(key TEXT PRIMARY KEY,record TEXT NOT NULL)')
    return store

def service_health(config_path,now):
    c=configuration(config_path);root=_private_directory(c['state_dir']);store=_store(root)
    with store.connection() as db:rows=[dict(r) for r in db.execute('SELECT * FROM service_sources')]
    intervals={s['board']:s['interval_seconds'] for s in c['sources']};retired=[r['board'] for r in rows if r['board'] not in intervals];rows=[r for r in rows if r['board'] in intervals]
    bad=not rows or len(rows)!=len(c['sources']) or any(r['error_class'] for r in rows)
    stale=any(not r['last_success'] or not 0<=(instant(now)-instant(r['last_success'])).total_seconds()<=intervals[r['board']]+900 for r in rows)
    expired=bool(c.get('pilot_ends_at') and instant(now)>=instant(c['pilot_ends_at']))
    return {'status':'stale' if stale or not rows else 'degraded' if bad else 'healthy','sources':rows,'sending_enabled':c['sending_enabled'] and not expired,'email_status':'pilot_expired' if expired and c['sending_enabled'] else 'qualified_only_pilot' if c['sending_enabled'] else 'disabled','pilot_ends_at':c.get('pilot_ends_at'),'inbox_delivery_verified':False}

def run_service(config_path,*,now=None,get=None,extractor=None,transport=None,_allow_test_clock=False):
    c=configuration(config_path);fixed_now=now;clock=lambda:fixed_now if fixed_now is not None else datetime.now(timezone.utc).isoformat();now=clock();at=instant(now);root=_private_directory(c['state_dir']);store=_store(root)
    if type(_allow_test_clock) is not bool:raise ValueError('explicit test clock switch required')
    if _allow_test_clock and transport is None:raise ValueError('test clock requires an explicitly injected test transport')
    if c['sending_enabled'] and fixed_now is not None and not _allow_test_clock:
        if abs((at-datetime.now(timezone.utc)).total_seconds())>60:raise ValueError('real delivery requires a current system clock')
    profile,approval=load_profile(c['profile_path'],c['approval_path']);context=candidate_context(profile,approval_sha256=approval)
    queue=DeliveryQueue(root/'delivery.sqlite');http=get or PublicHTTP();cache={};started=time.monotonic()
    def cached(url,hosts):
        if time.monotonic()-started>300:raise RunWindowExpired('service verification window expired')
        validate_public_url(url,hosts);key=(url,tuple(sorted(hosts)))
        if key not in cache:cache[key]=http(url,hosts)
        return cache[key]
    if c['semantic_enabled'] and extractor is None:
        from deepseek_transport import DeepSeekExtractor
        runtime=Path.home()/'.config/job-assistant/deepseek-runtime'
        extractor=DeepSeekExtractor(Path.home()/'.config/job-assistant/deepseek.key',runtime/'budget.sqlite',runtime/'responses',mode='segments')
    report={'status':'completed','started_at':now,'sources':[],'delivery':{'status':'disabled','transport_calls':0,'delivery_verified':False},'production_ready':False}
    with store.runner_lock() as acquired:
        if not acquired:return dict(report,status='busy')
        for source in c['sources']:
            board=source['board'];row={'board':board,'qualified':0,'review':0,'reject':0,'held':0,'baseline':0,'status':'completed'};report['sources'].append(row)
            if time.monotonic()-started>120:row['status']='time_budget_reached';continue
            with store.connection() as db:previous=db.execute('SELECT * FROM service_sources WHERE board=?',(board,)).fetchone()
            if previous and previous['next_due'] and instant(previous['next_due'])>at:row['status']='not_due';continue
            try:
                # Employer ownership is checked before any AI spend, not inferred from feed reachability.
                jobs=collect_ashby(board,cached,now)
                observed=clock()
                for job in jobs:job['source_observed_at']=observed
                sourceid='ashby:'+board;store.snapshot(sourceid,[j['id'] for j in jobs],observed,hashes={j['id']:job_fingerprint(j) for j in jobs},active_ids=[j['id'] for j in jobs if j['active']]);row['discovered']=len(jobs)
                candidates=sorted(jobs,key=lambda j:('early-career level not established' in role_gate(j,now),j['id']));used=0
                for job in candidates:
                    if time.monotonic()-started>120:row['status']='time_budget_reached';break
                    if not c.get('notify_existing',False) and store.job(sourceid,job['id'])['first_generation']==1:
                        row['baseline']+=1;continue
                    rawsha=job_fingerprint(job);key=digest(canonical({'profile':approval,'job':rawsha,'policy':VERSION,'extractor':EXTRACTION_VERSION}));basic=fit(context,job)
                    with store.connection() as db:done=db.execute('SELECT status FROM processed WHERE key=?',(key,)).fetchone()
                    if done:continue
                    decision=basic
                    if basic['status']=='reject':row['reject']+=1;status='reject'
                    elif role_gate(job,now) or not c['semantic_enabled']:row['review']+=1;continue
                    elif used>=c.get('shortlist_limit',3):continue
                    else:
                        used+=1
                        try:
                            current,verification=current_opening(source,job['id'].rsplit(':',1)[1],cached,clock())
                            extraction=extractor.extract(current['description'],now=now);decision=qualify(profile,current,extraction,clock(),approval_sha256=approval);status=decision['status'];row['qualified' if status=='eligible' else status]+=1
                            if status=='eligible':
                                if not c.get('sender'):row['held']+=1;continue
                                seen=store.job(sourceid,job['id'])['first_seen'];result=prepare_alert(c['profile_path'],c['approval_path'],source,job['id'].rsplit(':',1)[1],extraction,root/'alerts',sender=c['sender'],now=clock(),first_seen=seen,get=cached)
                                if result['status'] not in {'prepared','cached'}:row['held']+=1;continue
                                directory=Path(result['directory']);manifest=json.loads((directory/'manifest.json').read_text());staged=queue.stage(merge_key(current),approval,job_fingerprint(current),(directory/'message.eml').read_bytes(),recipient=profile['recipient_email'],sender=c['sender'],pdf_sha256=manifest['pdf_sha256'],metadata={'packet_kind':'qualified','packet_key':directory.name,'board':board})
                                if staged['status']=='conflict':row['held']+=1;continue
                        except RunWindowExpired:row['status']='time_budget_reached';break
                        except (ValueError,OSError,KeyError,TypeError):row['held']+=1;continue
                    with store.connection() as db:
                        if db.execute('SELECT COUNT(*) FROM processed').fetchone()[0]>=50000:raise ValueError('processed retention cap reached')
                        evidence=canonical({'job_id':job['id'],'job_hash':rawsha,'profile_hash':approval,'evaluated_at':now,'decision':decision})
                        if len(evidence)>100000:raise ValueError('decision record exceeds bound')
                        db.execute('INSERT OR IGNORE INTO decision_records VALUES(?,?)',(key,evidence.decode()))
                        db.execute('INSERT OR IGNORE INTO processed VALUES(?,?,?,?)',(key,board,status,now))
                with store.connection() as db:db.execute('INSERT INTO service_sources VALUES(?,?,?,?,0,NULL) ON CONFLICT(board) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=excluded.last_success,next_due=excluded.next_due,failures=0,error_class=NULL',(board,now,observed,(at+timedelta(seconds=source['interval_seconds'])).isoformat()))
            except RunWindowExpired:row['status']='time_budget_reached'
            except Exception as exc:
                failures=(previous['failures'] if previous else 0)+1;delay=min(86400,source['interval_seconds']*2**min(failures-1,5));retry=None
                try:
                    header=exc.headers.get('Retry-After') if getattr(exc,'code',None)==429 and getattr(exc,'headers',None) else None
                    if header:
                        retry=int(header) if header.isdigit() else max(0,int((parsedate_to_datetime(header)-at).total_seconds()))
                        if not 0<=retry<=7*86400:raise ValueError('source retry needs review')
                        delay=max(delay,retry)
                except (ValueError,TypeError,OverflowError):delay=7*86400
                with store.connection() as db:db.execute('INSERT INTO service_sources(board,last_attempt,next_due,failures,error_class) VALUES(?,?,?,?,?) ON CONFLICT(board) DO UPDATE SET last_attempt=excluded.last_attempt,next_due=excluded.next_due,failures=excluded.failures,error_class=excluded.error_class',(board,now,(at+timedelta(seconds=delay)).isoformat(),failures,type(exc).__name__))
                store.snapshot('ashby:'+board,None,now,error=type(exc).__name__);row.update(status='source_failed',error_class=type(exc).__name__)
        # Refresh only expired, never-attempted qualified packets. Preserve the
        # canonical queue identity; accepted/unknown/retry items are never reset.
        report['refreshed_alerts']=0;report['alert_refresh_holds']=0;report['refresh_audit_pending']=0;report['refresh_audit_capacity_holds']=0
        sources={s['board']:s for s in c['sources']};refresh_count=0
        def refresh_event(item,outcome,extra=None):
            try:
                event={'kind':'packet_refresh','outcome':outcome,'job_hash':item['job_hash'],'profile_hash':item['profile_hash'],'evaluated_at':clock(),'previous_packet_key':item['metadata'].get('packet_key'),'previous_payload_hash':item['payload_hash']}
                event.update(extra or {});raw=canonical(event)
                if len(raw)>100000:raise ValueError('refresh decision record exceeds bound')
                with store.connection() as db:
                    if db.execute('SELECT COUNT(*) FROM decision_records').fetchone()[0]>=50000:raise ValueError('decision retention cap reached')
                    db.execute('INSERT OR IGNORE INTO decision_records VALUES(?,?)',(digest(raw),raw.decode()))
                return True
            except Exception:
                # The applied replacement has its own atomic delivery.sqlite
                # history. An auxiliary audit outage must not strand it.
                report['refresh_audit_pending']+=1;return False
        for key in queue.candidates(clock(),20):
            if refresh_count>=3 or time.monotonic()-started>120:break
            item=queue.get(key)
            if item['attempts'] or item['status']!='needs_review' or item['metadata'].get('packet_kind')!='qualified':continue
            try:
                packet_key=item['metadata'].get('packet_key')
                if not isinstance(packet_key,str) or not re.fullmatch('[a-f0-9]{32}',packet_key):raise ValueError('invalid packet key')
                directory=root/'alerts'/packet_key
                if directory.is_symlink():raise ValueError('invalid packet directory')
                manifest_path=directory/'manifest.json'
                if manifest_path.is_symlink() or manifest_path.stat().st_size>4*1024*1024:raise ValueError('invalid manifest')
                manifest=json.loads(manifest_path.read_text())
                if (instant(clock())-instant(manifest['prepared_at'])).total_seconds()<=86400:continue
                with store.connection() as db:audit_count=db.execute('SELECT COUNT(*) FROM decision_records').fetchone()[0]
                if audit_count>49998:
                    report['refresh_audit_capacity_holds']+=1;queue.hold(key,'needs_review',now=clock());continue
                generation=item['metadata'].get('refresh_generation',0)
                if type(generation) is not int or not 0<=generation<3:raise ValueError('packet refresh generation cap reached')
                refresh_count+=1;source=sources.get(item['metadata'].get('board'))
                if not source or manifest['source_registration']!=source or manifest['profile_hash']!=approval or item['profile_hash']!=approval or manifest['job_hash']!=item['job_hash'] or digest(item['payload'])!=manifest['email_sha256']:raise ValueError('expired packet identity differs')
                current,_=current_opening(source,manifest['job']['id'].rsplit(':',1)[1],cached,clock())
                if job_fingerprint(current)!=item['job_hash'] or merge_key(current)!=item['canonical_job']:raise ValueError('opening changed since original queue entry')
                result=prepare_alert(c['profile_path'],c['approval_path'],source,manifest['job']['id'].rsplit(':',1)[1],manifest['extraction'],root/'alerts',sender=c['sender'],now=clock(),first_seen=manifest['first_seen_at'],get=cached)
                if result['status'] not in {'prepared','cached'}:raise ValueError('expired opening no longer qualifies')
                fresh=Path(result['directory']);m=json.loads((fresh/'manifest.json').read_text())
                if not refresh_event(item,'prepared_not_queued',{'job_id':m['job']['id'],'decision':m['decision'],'packet_key':fresh.name}):
                    report['alert_refresh_holds']+=1;continue
                refreshed=queue.stage(item['canonical_job'],approval,m['job_hash'],(fresh/'message.eml').read_bytes(),recipient=profile['recipient_email'],sender=c['sender'],pdf_sha256=m['pdf_sha256'],metadata={'packet_kind':'qualified','packet_key':fresh.name,'board':source['board'],'refresh_generation':generation+1},refresh_unattempted=True,expected_payload_hash=item['payload_hash'])
                if refreshed['status']!='refreshed':raise ValueError('expired queue conflict')
                report['refreshed_alerts']+=1
                refresh_event(item,'queued',{'packet_key':fresh.name,'refresh_generation':generation+1})
            except Exception as exc:
                retryable=isinstance(exc,(OSError,sqlite3.OperationalError)) or (isinstance(exc,ValueError) and str(exc).startswith(('outbox storage cap','private artifact cap','decision retention cap')))
                queue.hold(key,'needs_review' if retryable else 'held',now=clock());refresh_event(item,'retryable_fetch_or_storage_error' if retryable else 'held_for_validation_identity_or_generation');report['alert_refresh_holds']+=1
        if c['sending_enabled'] and c.get('pilot_ends_at') and instant(clock())>=instant(c['pilot_ends_at']):
            report['delivery'].update(status='pilot_expired')
        elif c['sending_enabled']:
            if transport is None and c.get('credentials_path') and c.get('sender'):
                from mail_transport import GmailTransport
                transport=GmailTransport(c['credentials_path'],recipient=profile['recipient_email'],sender=c['sender'],send_allowed=(lambda:instant(clock())<instant(c['pilot_ends_at'])) if c.get('pilot_ends_at') else None)
            sources={s['board']:s for s in c['sources']}
            def readiness(item):
                source=sources.get(item['metadata'].get('board'))
                # Delivery must re-fetch rather than replay evaluation-time bytes.
                if c.get('pilot_ends_at') and instant(clock())>=instant(c['pilot_ends_at']):return False
                ready=bool(source and alert_readiness(c['profile_path'],c['approval_path'],source,root/'alerts',clock(),get=http)(item))
                return ready and (not c.get('pilot_ends_at') or instant(clock())<instant(c['pilot_ends_at']))
            if transport is not None and c.get('pilot_ends_at'):transport=_PilotTransport(transport,c['pilot_ends_at'],clock)
            report['delivery']=DeliveryWorker(queue,transport,readiness=readiness,enabled=True,clock=clock,daily_cap=c.get('daily_email_cap',20),monthly_cap=c.get('monthly_email_cap',600)).run(clock())
        report['queue']=queue.status();report['completed_at']=clock()
        _write(root,root/'last-run.json',canonical(report),MAX_ARTIFACT_BYTES)
    return report
