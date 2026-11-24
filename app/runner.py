"""Offline vertical slice. The single I/O seam cannot send real email."""
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import patch
from packet import build_packet, check_packet, canonical, digest, job_fingerprint
from policy import fit, role_gate, instant, norm, novelty, merge_key, skill_key
from policy import select_required_variants

class NoExternalNetwork(contextlib.AbstractContextManager):
    """Process-level Python guard, not an operating-system security sandbox."""
    @staticmethod
    def allowed(host):
        if host=='localhost':return True
        try:return ipaddress.ip_address(host).is_loopback
        except ValueError:return False
    def __enter__(self):
        self.stack=contextlib.ExitStack();connect=socket.socket.connect;connect_ex=socket.socket.connect_ex;resolve=socket.getaddrinfo;create=socket.create_connection;sendto=socket.socket.sendto
        def check(host):
            if not self.allowed(host):raise PermissionError('external network disabled in archive prototype')
        def guarded_connect(sock,address):
            if isinstance(address,tuple):check(address[0])
            else:raise PermissionError('non-IP sockets disabled')
            return connect(sock,address)
        def guarded_connect_ex(sock,address):
            if isinstance(address,tuple):check(address[0])
            else:raise PermissionError('non-IP sockets disabled')
            return connect_ex(sock,address)
        def guarded_resolve(host,*args,**kwargs):check(host);return resolve(host,*args,**kwargs)
        def guarded_create(address,*args,**kwargs):check(address[0]);return create(address,*args,**kwargs)
        def guarded_sendto(sock,data,*args):
            address=args[-1]
            if not isinstance(address,tuple):raise PermissionError('invalid datagram target')
            check(address[0]);return sendto(sock,data,*args)
        for target,value in [('socket.socket.connect',guarded_connect),('socket.socket.connect_ex',guarded_connect_ex),('socket.getaddrinfo',guarded_resolve),('socket.create_connection',guarded_create),('socket.socket.sendto',guarded_sendto)]:self.stack.enter_context(patch(target,value))
        return self
    def __exit__(self,*args):return self.stack.__exit__(*args)

class ArchiveIO:
    mode='archive'
    def __init__(self,archive_root,output_root,allowed_names):
        self.archive_root=Path(archive_root).resolve();self.output_root=Path(output_root).resolve();self.allowed_names=frozenset(allowed_names)
    def disabled(self):return (self.output_root/'DISABLED').exists()
    def read(self,name,max_bytes=10*1024*1024):
        if self.disabled():raise ValueError('disabled')
        if name not in self.allowed_names:raise ValueError('unregistered archive; no network fallback')
        path=(self.archive_root/name).resolve()
        if not path.is_relative_to(self.archive_root):raise ValueError('archive path escaped root')
        with path.open('rb') as file:data=file.read(max_bytes+1)
        if len(data)>max_bytes:raise ValueError('archive size cap exceeded')
        parsed=json.loads(data)
        if not isinstance(parsed,(dict,list)):raise ValueError('invalid archive schema')
        return parsed
    def packet_directory(self,profile_version,job_id):
        return self.output_root/digest((profile_version+'\0'+job_id).encode())[:20]
    def prepare(self,packet):
        if self.disabled():raise ValueError('disabled')
        m=packet['manifest']
        if m.get('synthetic') is not True or m.get('delivery_mode')!='local_capture':raise ValueError('archive capture requires synthetic packet')
        key=digest((m['profile_version']+'\0'+m['job_id']).encode())[:20]
        self.output_root.mkdir(parents=True,exist_ok=True,mode=0o700);destination=self.output_root/key
        if destination.exists():
            if (destination/'prepared.eml').read_bytes()!=packet['email']:raise ValueError('existing packet differs; reconcile before replacement')
            return destination
        temporary=Path(tempfile.mkdtemp(prefix='.preparing-',dir=self.output_root))
        try:
            for name,data in [('resume.pdf',packet['pdf']),('manifest.json',canonical(m)),('prepared.eml',packet['email'])]:
                fd=os.open(temporary/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as file:file.write(data);file.flush();os.fsync(file.fileno())
            temporary.rename(destination)
            directory_fd=os.open(self.output_root,os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        except BaseException:shutil.rmtree(temporary,ignore_errors=True);raise
        return destination
    def capture(self,destination,payload):
        if self.disabled():raise ValueError('disabled before capture')
        target=destination/'message.eml'
        if target.exists():
            if target.read_bytes()!=payload:raise ValueError('conflicting captured bytes')
            return target
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as file:file.write(payload);file.flush();os.fsync(file.fileno())
        return target

class Runner:
    def __init__(self,mode,io_port,store):
        if mode!='archive' or getattr(io_port,'mode',None)!='archive':raise ValueError('only explicit archive mode is implemented; live activation unavailable')
        self.io=io_port;self.store=store
    def process(self,profile,job,now):
        with self.store.runner_lock() as acquired:
            if not acquired:return {'job_id':job.get('id'),'status':'busy','reason':'another process owns the run lock'}
            with NoExternalNetwork():return self._process(profile,job,now)
    def _process(self,profile,job,now):
        if self.io.disabled():return {'job_id':job.get('id'),'status':'disabled'}
        if profile.get('synthetic') is not True or job.get('synthetic') is not True:raise ValueError('real candidate/job processing is disabled')
        if profile.get('approved') is not True:raise ValueError('profile approval required')
        self.store.recover(now)
        version=profile['version'];id=job['id'];existing=self.store.intent(version,id)
        if existing and existing['status']!='pending':return {'job_id':id,'status':'duplicate','existing_state':existing['status']}
        decision=fit(profile,job);reasons=role_gate(job,now)
        try:
            age=(instant(now)-instant(job.get('source_observed_at'))).total_seconds()
            if not 0<=age<=900:reasons.append('source snapshot not current for fixture clock')
        except (ValueError,TypeError):reasons.append('source observation timestamp missing')
        if job.get('active') is not True:reasons.append('listing activity unconfirmed')
        source=job.get('source_id',job.get('source_kind','archive-fixture'))
        if not self.store.current(source,id,job_fingerprint(job),now):reasons.append('persisted source state not current/active/bound')
        if job.get('published_at') is not None:
            try:
                if instant(job['published_at'])>instant(now):reasons.append('future publication invalid')
            except (ValueError,TypeError):reasons.append('publication invalid')
        if decision['status']!='eligible' or reasons:
            return {'job_id':id,'status':'reject' if decision['status']=='reject' else 'review','reasons':decision['reasons']+reasons,'fit':decision}
        observed=self.store.job(source,id)
        if not existing and not novelty([],job,observed['first_generation']==1,now):return {'job_id':id,'status':'baseline_only','reasons':['existing/baseline listing is not a fresh alert']}
        identity=self.store.delivery_identity(profile.get('candidate_id') or profile['email'],merge_key(job),version,id)
        if identity['profile']!=version or identity['job']!=id:return {'job_id':id,'status':'duplicate','existing_state':(self.store.intent(identity['profile'],identity['job']) or {}).get('status','prepared_or_pending')}
        selected=select_required_variants(profile,job)
        if not selected:return {'job_id':id,'status':'review','reasons':['no approved evidence matches extracted skill tags']}
        destination=self.io.packet_directory(version,id)
        if existing or destination.exists():
            try:
                packet={'manifest':json.loads((destination/'manifest.json').read_text()),'pdf':(destination/'resume.pdf').read_bytes(),'email':existing['payload'] if existing else (destination/'prepared.eml').read_bytes()}
            except (OSError,ValueError,KeyError):
                return {'job_id':id,'status':'review','reasons':['incomplete or corrupt prepared artifact; quarantined from delivery; reconcile before reuse']}
            if (instant(now)-instant(packet['manifest']['created_at'])).total_seconds()>86400:
                return {'job_id':id,'status':'expired','reasons':['prepared alert older than24hours; no resend']}
        else:
            packet=build_packet(profile,job,selected,'demo@example.invalid',now)
            destination=self.io.prepare(packet)
        if not check_packet(packet,profile,job):return {'job_id':id,'status':'review','reasons':['prepared packet invalidated; operator reconciliation required']}
        if self.io.disabled():return {'job_id':id,'status':'disabled'}
        self.store.enqueue(version,id,packet['email'])
        # Immediate pre-claim verification of the immutable replay context and bytes.
        if not check_packet(packet,profile,job):raise ValueError('pre-send packet binding failed')
        if not self.store.claim(version,id,now):return {'job_id':id,'status':'deferred','reason':'delivery quota or existing claim'}
        try:
            frozen=self.store.intent(version,id)['payload']
            if frozen!=packet['email']:raise ValueError('outbox payload mismatch')
            path=self.io.capture(destination,frozen)
        except BaseException:
            self.store.finish(version,id,'delivery_unknown');raise
        self.store.finish(version,id,'capture_accepted')
        return {'job_id':id,'status':'capture_accepted','selected_ids':selected,'pdf':str(destination/'resume.pdf'),'manifest':str(destination/'manifest.json'),'email':str(path),'fit':decision,'note':'Local sample capture only; no inbox delivery or application.'}
