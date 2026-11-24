"""Durable personal outbox. Readiness comes from trusted code, never a job flag.

Provider acceptance is not inbox delivery. Unknown sends are never retried.
Current real review packets remain needs_review; this worker adds no qualifier.
"""
from datetime import timedelta,datetime,timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import json,math,os,re,stat
from pathlib import Path
from mail_transport import _validate
from packet import canonical,digest
from policy import instant
from state import Store

HASH=re.compile(r'[a-f0-9]{64}')
MAX_TOTAL=100*1024*1024

def _protected(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1:raise ValueError('protected outbox file required')


def _payload(raw,recipient,sender,pdf_sha):
    _validate(raw,recipient,sender)
    parts=list(BytesParser(policy=policy.default).parsebytes(raw).iter_attachments())
    if len(parts)!=1 or digest(parts[0].get_payload(decode=True))!=pdf_sha:raise ValueError('outbox PDF binding mismatch')


class DeliveryQueue:
    def __init__(self,path,*,max_payload_bytes=3*1024*1024):
        if type(max_payload_bytes) is not int or not 0<max_payload_bytes<=3*1024*1024:raise ValueError('invalid payload cap')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);parent=self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid() or parent.st_mode&0o077:raise ValueError('owned private outbox directory required')
        for file in [self.path,Path(str(self.path)+'.runner.lock')]:
            if file.exists() or file.is_symlink():_protected(file)
        self.cap=max_payload_bytes;self.store=Store(self.path)
        with self.store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS delivery_items(key TEXT PRIMARY KEY,canonical_job TEXT UNIQUE NOT NULL,profile_hash TEXT NOT NULL,job_hash TEXT NOT NULL,payload BLOB NOT NULL,payload_hash TEXT NOT NULL,pdf_hash TEXT NOT NULL,recipient TEXT NOT NULL,sender TEXT NOT NULL,metadata TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT,attempt_id INTEGER,provider_id TEXT,last_checked_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS delivery_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL,started_at TEXT NOT NULL,status TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS delivery_receipts(attempt_id INTEGER PRIMARY KEY,finished_at TEXT NOT NULL,status TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS delivery_refreshes(key TEXT NOT NULL,previous_payload_hash TEXT NOT NULL,new_payload_hash TEXT NOT NULL,previous_packet_key TEXT,new_packet_key TEXT,recorded_at TEXT NOT NULL,UNIQUE(key,new_payload_hash))')
    def stage(self,canonical_job,profile_hash,job_hash,raw,*,recipient,sender,pdf_sha256,metadata,refresh_unattempted=False,expected_payload_hash=None):
        if type(refresh_unattempted) is not bool:raise ValueError('explicit refresh policy required')
        if refresh_unattempted and (not isinstance(expected_payload_hash,str) or not HASH.fullmatch(expected_payload_hash)):raise ValueError('expected prior payload hash required for refresh')
        if not isinstance(canonical_job,str) or not canonical_job or len(canonical_job)>1000 or any(not isinstance(x,str) or not HASH.fullmatch(x) for x in [profile_hash,job_hash,pdf_sha256]):raise ValueError('outbox identity required')
        if not isinstance(raw,bytes) or not raw or len(raw)>self.cap or not isinstance(metadata,dict):raise ValueError('bounded outbox payload required')
        meta=canonical(metadata)
        if len(meta)>100000:raise ValueError('outbox metadata exceeds cap')
        _payload(raw,recipient,sender,pdf_sha256);key=digest(canonical_job.encode());sha=digest(raw)
        with self.store.connection() as db:
            old=db.execute('SELECT * FROM delivery_items WHERE key=?',(key,)).fetchone()
            if old:
                identical=old['payload_hash']==sha and old['profile_hash']==profile_hash and old['job_hash']==job_hash
                oldmeta=json.loads(old['metadata'])
                refresh=(refresh_unattempted and not identical and old['payload_hash']==expected_payload_hash and old['attempts']==0 and old['status']=='needs_review' and old['profile_hash']==profile_hash and old['job_hash']==job_hash and old['recipient']==recipient and old['sender']==sender and oldmeta.get('packet_kind')==metadata.get('packet_kind')=='qualified' and oldmeta.get('board')==metadata.get('board'))
                if not refresh:return {'key':key,'status':'duplicate' if identical else 'conflict','existing_state':old['status']}
                used=db.execute('SELECT COALESCE(SUM(length(payload)+length(CAST(metadata AS BLOB))),0) FROM delivery_items').fetchone()[0]
                if used-len(old['payload'])-len(old['metadata'].encode())+len(raw)+len(meta)>MAX_TOTAL or self.path.stat().st_size>MAX_TOTAL:raise ValueError('outbox storage cap reached; retention review required')
                changed=db.execute("UPDATE delivery_items SET payload=?,payload_hash=?,pdf_hash=?,metadata=? WHERE key=? AND payload_hash=? AND attempts=0 AND status='needs_review'",(raw,sha,pdf_sha256,meta.decode(),key,expected_payload_hash)).rowcount
                if changed!=1:return {'key':key,'status':'conflict','existing_state':old['status']}
                db.execute('INSERT INTO delivery_refreshes VALUES(?,?,?,?,?,?)',(key,old['payload_hash'],sha,oldmeta.get('packet_key'),metadata.get('packet_key'),datetime.now(timezone.utc).isoformat()))
                return {'key':key,'status':'refreshed','existing_state':old['status']}
            used=db.execute('SELECT COALESCE(SUM(length(payload)+length(CAST(metadata AS BLOB))),0) FROM delivery_items').fetchone()[0]
            if used+len(raw)+len(meta)>MAX_TOTAL or self.path.stat().st_size>MAX_TOTAL:raise ValueError('outbox storage cap reached; retention review required')
            db.execute('INSERT INTO delivery_items(key,canonical_job,profile_hash,job_hash,payload,payload_hash,pdf_hash,recipient,sender,metadata,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,canonical_job,profile_hash,job_hash,raw,sha,pdf_sha256,recipient,sender,meta.decode(),'needs_review'))
        return {'key':key,'status':'staged_review'}
    def get(self,key):
        with self.store.connection() as db:row=db.execute('SELECT * FROM delivery_items WHERE key=?',(key,)).fetchone()
        if row is None:return None
        value=dict(row);value['metadata']=json.loads(value['metadata']);return value
    def status(self):
        with self.store.connection() as db:return {r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM delivery_items GROUP BY status')}
    def candidates(self,now,limit):
        at=instant(now).isoformat()
        with self.store.connection() as db:return [row[0] for row in db.execute("SELECT key FROM delivery_items WHERE status IN ('needs_review','pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY CASE WHEN status='needs_review' THEN 1 ELSE 0 END,COALESCE(last_checked_at,''),rowid LIMIT ?",(at,limit))]
    def hold(self,key,status='held',now=None):
        if status not in {'held','needs_review'}:raise ValueError('invalid hold')
        with self.store.connection() as db:db.execute("UPDATE delivery_items SET status=?,last_checked_at=COALESCE(?,last_checked_at) WHERE key=? AND status IN ('needs_review','pending','retry_wait')",(status,instant(now).isoformat() if now else None,key))
    def claim(self,item,now,daily_cap,monthly_cap):
        at=instant(now).isoformat()
        with self.store.connection() as db:
            if db.execute('SELECT COUNT(*) FROM delivery_attempts WHERE substr(started_at,1,10)=?',(at[:10],)).fetchone()[0]>=daily_cap:return None
            if db.execute('SELECT COUNT(*) FROM delivery_attempts WHERE substr(started_at,1,7)=?',(at[:7],)).fetchone()[0]>=monthly_cap:return None
            changed=db.execute("UPDATE delivery_items SET status='sending',attempts=attempts+1 WHERE key=? AND payload_hash=? AND profile_hash=? AND attempts<4 AND status IN ('needs_review','pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=?)",(item['key'],item['payload_hash'],item['profile_hash'],at)).rowcount
            if changed!=1:return None
            attempt=db.execute("INSERT INTO delivery_attempts(key,started_at,status) VALUES(?,?,'sending')",(item['key'],at)).lastrowid
            db.execute('UPDATE delivery_items SET attempt_id=? WHERE key=?',(attempt,item['key']));return attempt
    def recover(self):
        if getattr(self.store,'_runner_lock_owner',None)!=(os.getpid(),__import__('threading').get_ident()):raise ValueError('worker lock required for recovery')
        with self.store.connection() as db:
            db.execute("UPDATE delivery_attempts SET status='delivery_unknown' WHERE id IN (SELECT attempt_id FROM delivery_items WHERE status='sending')")
            return db.execute("UPDATE delivery_items SET status='delivery_unknown' WHERE status='sending'").rowcount
    def finish(self,key,attempt,result,now):
        state=result.get('state') if isinstance(result,dict) else None;provider=None;next_at=None
        if state=='api_accepted' and isinstance(result.get('provider_message_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}',result['provider_message_id']):status='api_accepted';provider=result['provider_message_id']
        elif state=='not_attempted' or (state=='rejected' and type(result.get('http_status')) is int and result['http_status'] in {401,429}):status='retry_wait'
        elif state=='rejected' and type(result.get('http_status')) is int and 400<=result['http_status']<500:status='rejected'
        else:status='delivery_unknown'
        with self.store.connection() as db:
            item=db.execute("SELECT * FROM delivery_items WHERE key=? AND status='sending' AND attempt_id=?",(key,attempt)).fetchone()
            if item is None:raise ValueError('no current delivery attempt')
            if status=='retry_wait':
                try:
                    at=instant(now);delay=60*2**(item['attempts']-1);header=result.get('retry_after') if state=='rejected' and result.get('http_status')==429 else None
                    if header is not None:
                        if not isinstance(header,str) or not header or len(header)>128 or any(ord(c)<32 for c in header):raise ValueError('invalid retry header')
                        if header.isascii() and header.isdigit():seconds=int(header)
                        else:
                            parsed=parsedate_to_datetime(header)
                            if parsed.tzinfo is None:raise ValueError('aware retry date required')
                            seconds=max(0,math.ceil((parsed-at).total_seconds()))
                        if not 0<=seconds<=7*86400:raise ValueError('retry interval needs review')
                        delay=max(delay,seconds)
                    if item['attempts']>=4:status='held'
                    else:next_at=(at+timedelta(seconds=delay)).isoformat()
                except (ValueError,TypeError,OverflowError):status='held'
            db.execute('UPDATE delivery_items SET status=?,next_attempt_at=?,provider_id=? WHERE key=? AND attempt_id=?',(status,next_at,provider,key,attempt))
            db.execute('UPDATE delivery_attempts SET status=? WHERE id=?',(status,attempt))
            db.execute('INSERT INTO delivery_receipts VALUES(?,?,?)',(attempt,instant(now).isoformat(),status))
        return status


class DeliveryWorker:
    def __init__(self,queue,transport=None,*,readiness=None,enabled=False,daily_cap=20,monthly_cap=600,clock=None):
        if type(enabled) is not bool or any(type(x) is not int or x<1 for x in [daily_cap,monthly_cap]):raise ValueError('explicit worker configuration required')
        self.queue=queue;self.transport=transport;self.readiness=readiness;self.enabled=enabled;self.daily=daily_cap;self.monthly=monthly_cap
        self.clock=clock
    def run(self,now,limit=20):
        instant(now)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded worker pass required')
        report={'status':'disabled','delivery_verified':False,'outcomes':[],'transport_calls':0}
        if not self.enabled:return report
        if self.transport is None or self.readiness is None:return dict(report,status='configuration_missing')
        with self.queue.store.runner_lock() as acquired:
            if not acquired:return dict(report,status='busy')
            report.update(status='completed',recovered_unknown=self.queue.recover())
            for key in self.queue.candidates(now,limit):
                try:
                    item=self.queue.get(key)
                    if digest(item['canonical_job'].encode())!=key:raise ValueError('canonical queue key differs')
                    if digest(item['payload'])!=item['payload_hash']:raise ValueError('payload hash differs')
                    _payload(item['payload'],item['recipient'],item['sender'],item['pdf_hash'])
                    preflight=getattr(self.transport,'preflight',None)
                    if preflight is not None and preflight(item['payload']) is not True:raise ValueError('transport configuration not ready')
                    with self.queue.store.connection() as db:
                        first=db.execute('SELECT MIN(started_at) FROM delivery_attempts WHERE key=?',(key,)).fetchone()[0]
                    if first and instant(now)>=instant(first)+timedelta(days=7):raise ValueError('delivery retry deadline reached')
                    ready=False if item['metadata'].get('packet_kind')=='review' else self.readiness(item)
                except Exception:
                    self.queue.hold(key);report['outcomes'].append({'key':key,'status':'held'});continue
                if ready is not True:
                    self.queue.hold(key,'needs_review',now=now);report['outcomes'].append({'key':key,'status':'needs_review'});continue
                attempt=self.queue.claim(item,self.clock() if self.clock else now,self.daily,self.monthly)
                if attempt is None:report['outcomes'].append({'key':key,'status':'deferred'});continue
                try:result=self.transport.send(item['payload'])
                except Exception:result={'state':'unknown'}
                # Retain conservative attempt reservations even when a trusted
                # transport declines before handoff; never erase audit history.
                if not isinstance(result,dict) or result.get('state')!='not_attempted':report['transport_calls']+=1
                status=self.queue.finish(key,attempt,result,self.clock() if self.clock else now);report['outcomes'].append({'key':key,'status':status})
        report['queue']=self.queue.status();return report


def personal_readiness(profile_path,approval_path,source_root,now):
    """Trusted current pipeline recheck; stored ready flags cannot release mail.

    Current assess() is review-only. A qualified-packet producer and its full
    source/file validation remain an explicit prerequisite for live releases.
    """
    def check(item):
        from profile_store import load_profile
        from matching import assess
        from packet import job_fingerprint
        from policy import merge_key
        profile,approval=load_profile(profile_path,approval_path)
        job=item['metadata'].get('job')
        if not isinstance(job,dict) or approval!=item['profile_hash'] or job_fingerprint(job)!=item['job_hash'] or merge_key(job)!=item['canonical_job']:return False
        if item['recipient']!=profile['recipient_email']:return False
        source=Path(source_root)/'state.sqlite'
        if not source.exists() or not Store(source).current(job['source_id'],job['id'],job_fingerprint(job),now):return False
        decision=assess(profile,job,now,approval_sha256=approval)
        # This is intentionally computed, never copied from metadata or a job.
        if decision.get('status')!='eligible' or decision.get('requirements_verified') is not True:return False
        # Review-format packets cannot be used as qualified alerts, even after a
        # future qualifier changes its verdict. A ready issuer must be integrated.
        return False
    return check
