"""Offline delivery state machine; all provider acceptance is simulated."""
import json,tempfile,unittest
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import patch
from packet import digest
try:
    from delivery import DeliveryQueue,DeliveryWorker
except ImportError:DeliveryQueue=DeliveryWorker=None

class Transport:
    def __init__(self,result=None):self.calls=[];self.result=result or {'state':'api_accepted','provider_message_id':'provider-1'}
    def send(self,raw):self.calls.append(raw);return self.result

class DeliveryTests(unittest.TestCase):
    now='2026-10-16T10:00:00+00:00'
    def setUp(self):
        self.assertIsNotNone(DeliveryQueue)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.q=DeliveryQueue(self.root/'outbox.sqlite');self.transport=Transport()
    def raw(self,subject='Job'):
        if subject=='Job' and hasattr(self,'_raw'):return self._raw
        m=EmailMessage();m['From']='sender@example.com';m['To']='recipient@example.com';m['Subject']=subject;m['Message-ID']='<job@example.com>';m.set_content('https://example.com/jobs/1');m.add_attachment(b'%PDF-1.4\nOFFLINE TEST',maintype='application',subtype='pdf',filename='resume.pdf');raw=m.as_bytes()
        if subject=='Job':self._raw=raw
        return raw
    def stage(self,canonical='employer:req1',profile='a'*64,raw=None):
        return self.q.stage(canonical,profile,'b'*64,raw or self.raw(),recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'synthetic_test':True})
    def run_worker(self,now=None,ready=True,**kwargs):
        return DeliveryWorker(self.q,self.transport,readiness=lambda item:ready,enabled=True,**kwargs).run(now or self.now)
    def test_review_is_durable_and_not_sent_by_default(self):
        x=self.stage();q=DeliveryQueue(self.root/'outbox.sqlite');self.assertEqual(q.get(x['key'])['status'],'needs_review')
        result=DeliveryWorker(q,self.transport).run(self.now);self.assertEqual(result['status'],'disabled');self.assertEqual(self.transport.calls,[])
    def test_refresh_only_replaces_same_unattempted_qualified_opening(self):
        raw=self.raw();changed=self.raw('Refreshed')
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','packet_key':'a'*32,'board':'example'})
        x=self.q.stage('employer:refresh','a'*64,'b'*64,raw,**args)
        args['metadata']['packet_key']='b'*32
        result=self.q.stage('employer:refresh','a'*64,'b'*64,changed,refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)
        self.assertEqual(result['status'],'refreshed');self.assertEqual(self.q.get(x['key'])['payload'],changed)
        self.assertEqual(self.q.get(x['key'])['attempts'],0)
        self.run_worker()
        self.assertEqual(self.q.stage('employer:refresh','a'*64,'b'*64,self.raw('Third'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)['status'],'conflict')
    def test_refresh_cannot_change_profile_job_sender_or_review_kind(self):
        for field in ['profile','job','kind']:
            canonical='employer:'+field;args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','packet_key':'a'*32,'board':'example'})
            self.q.stage(canonical,'a'*64,'b'*64,self.raw(),**args)
            if field=='kind':args['metadata']['packet_kind']='review'
            result=self.q.stage(canonical,('c' if field=='profile' else 'a')*64,('c' if field=='job' else 'b')*64,self.raw('Changed'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)
            self.assertEqual(result['status'],'conflict')
    def test_refresh_stale_worker_claim_cannot_send_old_bytes(self):
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','packet_key':'a'*32,'board':'example'})
        staged=self.q.stage('employer:race','a'*64,'b'*64,self.raw(),**args);stale=self.q.get(staged['key'])
        fresh_raw=self.raw('New');self.q.stage('employer:race','a'*64,'b'*64,fresh_raw,refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)
        self.assertIsNone(self.q.claim(stale,self.now,20,600))
        self.assertEqual(self.q.get(staged['key'])['attempts'],0)
        self.run_worker();self.assertEqual(self.transport.calls,[fresh_raw])
    def test_refresh_accounts_for_utf8_metadata_bytes(self):
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','packet_key':'a'*32,'board':'example','note':'😺'*5000})
        self.q.stage('employer:bytecap','a'*64,'b'*64,self.raw(),**args)
        args['metadata']['note']='😺'*20000;raw=self.raw('New')
        from packet import canonical
        with patch('delivery.MAX_TOTAL',len(raw)+len(canonical(args['metadata']))-1):
            with self.assertRaisesRegex(ValueError,'storage cap'):self.q.stage('employer:bytecap','a'*64,'b'*64,raw,refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)
    def test_review_cannot_be_refreshed_into_qualified_and_check_time_is_preserved(self):
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'review','board':'example'})
        x=self.q.stage('employer:review','a'*64,'b'*64,self.raw(),**args);args['metadata']['packet_kind']='qualified'
        self.assertEqual(self.q.stage('employer:review','a'*64,'b'*64,self.raw('New'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)['status'],'conflict')
        x=self.q.stage('employer:checked','a'*64,'b'*64,self.raw(),**args);self.q.hold(x['key'],'needs_review',now=self.now)
        self.q.stage('employer:checked','a'*64,'b'*64,self.raw('New'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)
        self.assertEqual(self.q.get(x['key'])['last_checked_at'],'2026-10-16T10:00:00+00:00')
    def test_simultaneous_refresh_has_one_winner_and_cannot_overwrite_claimed_send(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','packet_key':'a'*32,'board':'example'})
        staged=self.q.stage('employer:parallel','a'*64,'b'*64,self.raw(),**args);fresh=self.raw('Refreshed');barrier=threading.Barrier(2)
        def refresh(_):
            barrier.wait(timeout=5)
            return self.q.stage('employer:parallel','a'*64,'b'*64,fresh,refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)['status']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(refresh,range(2)))
        self.assertCountEqual(results,['refreshed','duplicate'])
        current=self.q.get(staged['key']);attempt=self.q.claim(current,self.now,20,600);self.assertIsNotNone(attempt)
        self.assertEqual(self.q.stage('employer:parallel','a'*64,'b'*64,self.raw('Third'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)['status'],'conflict')
        self.assertEqual(self.q.get(staged['key'])['payload'],fresh);self.assertEqual(self.q.get(staged['key'])['attempts'],1)
        with self.q.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],1)
    def test_previously_attempted_review_state_is_never_refreshed(self):
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','board':'example'})
        x=self.q.stage('employer:attempted','a'*64,'b'*64,self.raw(),**args);item=self.q.get(x['key']);attempt=self.q.claim(item,self.now,20,600)
        self.q.finish(x['key'],attempt,{'state':'not_attempted'},self.now);self.q.hold(x['key'],'needs_review',now=self.now)
        self.assertEqual(self.q.stage('employer:attempted','a'*64,'b'*64,self.raw('New'),refresh_unattempted=True,expected_payload_hash=digest(self.raw()),**args)['status'],'conflict')
        self.assertEqual(self.q.get(x['key'])['attempts'],1)
    def test_different_concurrent_refresh_payloads_cannot_both_replace_same_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','board':'example'})
        original=self.raw();x=self.q.stage('employer:competing','a'*64,'b'*64,original,**args);payloads=[self.raw('A'),self.raw('B')];barrier=threading.Barrier(2)
        def refresh(raw):
            barrier.wait(timeout=5)
            return self.q.stage('employer:competing','a'*64,'b'*64,raw,refresh_unattempted=True,expected_payload_hash=digest(original),**args)['status']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(refresh,payloads))
        self.assertCountEqual(results,['refreshed','conflict']);self.assertIn(self.q.get(x['key'])['payload'],payloads)
    def test_refresh_cannot_redirect_sender_or_recipient_even_with_consistent_new_mime(self):
        from email.parser import BytesParser
        for field,header in [('recipient','To'),('sender','From')]:
            args=dict(recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={'packet_kind':'qualified','board':'example'})
            raw=self.raw();x=self.q.stage('employer:redirect-'+field,'a'*64,'b'*64,raw,**args);before=self.q.get(x['key']);message=BytesParser().parsebytes(raw);message.replace_header(header,'different@example.com');args[field]='different@example.com'
            result=self.q.stage('employer:redirect-'+field,'a'*64,'b'*64,message.as_bytes(),refresh_unattempted=True,expected_payload_hash=digest(raw),**args)
            self.assertEqual(result['status'],'conflict');after=self.q.get(x['key'])
            self.assertEqual((after['sender'],after['recipient'],after['payload_hash']),(before['sender'],before['recipient'],before['payload_hash']))
    def test_current_readiness_must_explicitly_allow_release(self):
        x=self.stage();self.run_worker(ready=1);self.assertEqual(self.transport.calls,[]);self.assertEqual(self.q.get(x['key'])['status'],'needs_review')
    def test_accepted_is_persisted_and_never_repeated(self):
        x=self.stage();r=self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'api_accepted');self.assertFalse(r['delivery_verified'])
        self.run_worker();self.assertEqual(self.transport.calls,[self.raw()])
    def test_repeat_and_profile_change_never_overwrite_frozen_message(self):
        x=self.stage();self.assertEqual(self.stage()['status'],'duplicate')
        self.assertEqual(self.stage(profile='c'*64,raw=self.raw('Changed'))['status'],'conflict')
        self.assertEqual(self.q.get(x['key'])['payload'],self.raw())
    def test_corrupt_payload_is_held_before_transport(self):
        x=self.stage()
        with self.q.store.connection() as db:db.execute('UPDATE delivery_items SET payload=? WHERE key=?',(self.raw('Tampered'),x['key']))
        self.run_worker();self.assertEqual(self.transport.calls,[]);self.assertEqual(self.q.get(x['key'])['status'],'held')
    def test_wrong_recipient_or_pdf_hash_refused_at_stage(self):
        with self.assertRaises(ValueError):self.q.stage('x','a'*64,'b'*64,self.raw(),recipient='other@example.com',sender='sender@example.com',pdf_sha256='0'*64,metadata={})
        self.assertEqual(self.q.status(),{})
    def test_unknown_result_never_retries(self):
        x=self.stage();self.transport.result={'state':'unknown'};self.run_worker();self.run_worker('2026-10-17T10:00:00Z')
        self.assertEqual(len(self.transport.calls),1);self.assertEqual(self.q.get(x['key'])['status'],'delivery_unknown')
    def test_transport_exception_after_claim_is_unknown(self):
        x=self.stage()
        def send(raw):self.transport.calls.append(raw);raise TimeoutError('private provider text')
        self.transport.send=send;self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'delivery_unknown')
        self.assertNotIn('private provider text',json.dumps(self.q.status()))
    def test_crash_after_external_acceptance_does_not_send_again(self):
        x=self.stage()
        with patch.object(self.q,'finish',side_effect=SystemExit):
            with self.assertRaises(SystemExit):self.run_worker()
        self.assertEqual(self.q.get(x['key'])['status'],'sending')
        restarted=DeliveryQueue(self.root/'outbox.sqlite');DeliveryWorker(restarted,self.transport,readiness=lambda item:True,enabled=True).run(self.now)
        self.assertEqual(len(self.transport.calls),1);self.assertEqual(restarted.get(x['key'])['status'],'delivery_unknown')
    def test_definite_no_send_retries_with_backoff_and_limit(self):
        x=self.stage();self.transport.result={'state':'not_attempted'}
        self.run_worker();self.run_worker('2026-10-16T10:00:59Z');self.assertEqual(len(self.transport.calls),1)
        self.run_worker('2026-10-16T10:01:00Z');self.run_worker('2026-10-16T10:03:00Z');self.run_worker('2026-10-16T10:07:00Z');self.run_worker('2026-10-17T00:00:00Z')
        self.assertEqual(len(self.transport.calls),4);self.assertEqual(self.q.get(x['key'])['status'],'held')
    def test_rate_limit_honors_retry_after(self):
        x=self.stage();self.transport.result={'state':'rejected','http_status':429,'retry_after':'180'}
        self.run_worker();self.run_worker('2026-10-16T10:02:59Z');self.assertEqual(len(self.transport.calls),1)
        self.run_worker('2026-10-16T10:03:00Z');self.assertEqual(len(self.transport.calls),2)
    def test_http_date_retry_after_and_invalid_header(self):
        x=self.stage();self.transport.result={'state':'rejected','http_status':429,'retry_after':'Fri, 16 Oct 2026 10:05:00 GMT'};self.run_worker();self.run_worker('2026-10-16T10:04:59Z');self.assertEqual(len(self.transport.calls),1)
        self.transport.result={'state':'rejected','http_status':429,'retry_after':'invalid'};self.run_worker('2026-10-16T10:05:00Z');self.assertEqual(self.q.get(x['key'])['status'],'held')
    def test_daily_cap_and_worker_lock_prevent_extra_calls(self):
        self.stage();self.stage('employer:req2');r=self.run_worker(daily_cap=1);self.assertEqual(len(self.transport.calls),1)
        self.assertTrue(any(o['status']=='deferred' for o in r['outcomes']))
        self.q=DeliveryQueue(self.root/'outbox.sqlite')
        r=self.run_worker(daily_cap=1);self.assertEqual(len(self.transport.calls),1)
        self.assertTrue(any(o['status']=='deferred' for o in r['outcomes']))
        with self.q.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],1)
        self.run_worker('2026-10-17T10:00:00Z',daily_cap=1);self.assertEqual(len(self.transport.calls),2)
        with self.q.store.runner_lock() as acquired:
            self.assertTrue(acquired);r=self.run_worker()
        self.assertEqual(r['status'],'busy');self.assertEqual(len(self.transport.calls),2)
    def test_queue_storage_and_symlink_boundaries(self):
        with self.assertRaises(ValueError):DeliveryQueue(self.root/'small.sqlite',max_payload_bytes=10).stage('x','a'*64,'b'*64,self.raw(),recipient='recipient@example.com',sender='sender@example.com',pdf_sha256=digest(b'%PDF-1.4\nOFFLINE TEST'),metadata={})
        target=self.root/'other.sqlite';target.symlink_to(self.root/'outbox.sqlite')
        with self.assertRaises(ValueError):DeliveryQueue(target)

    def test_review_scans_do_not_starve_later_items(self):
        a=self.stage();b=self.stage('employer:req2');seen=[]
        worker=DeliveryWorker(self.q,self.transport,readiness=lambda item:seen.append(item['key']) or False,enabled=True)
        worker.run(self.now,limit=1);worker.run('2026-10-16T10:01:00Z',limit=1)
        self.assertEqual(set(seen),{a['key'],b['key']});self.assertEqual(self.transport.calls,[])
    def test_worker_composes_actual_gmail_adapter_with_simulated_https(self):
        from mail_transport import GmailTransport
        import base64
        creds=self.root/'credentials.json';creds.write_text(json.dumps({'client_id':'test','client_secret':'test-secret','refresh_token':'test-refresh'}));creds.chmod(0o600);calls=[]
        def http(method,url,headers,body,timeout):
            calls.append((url,body))
            return (200,b'{"access_token":"test-access"}') if 'oauth2' in url else (200,b'{"id":"accepted-1"}')
        x=self.stage();transport=GmailTransport(creds,recipient='recipient@example.com',sender='sender@example.com',http=http)
        result=DeliveryWorker(self.q,transport,readiness=lambda item:True,enabled=True).run(self.now)
        self.assertEqual(self.q.get(x['key'])['provider_id'],'accepted-1');self.assertFalse(result['delivery_verified']);self.assertEqual(len(calls),2)
        self.assertEqual(base64.urlsafe_b64decode(json.loads(calls[1][1])['raw']),self.raw())

    def test_malformed_metadata_does_not_abort_remaining_queue(self):
        a=self.stage();b=self.stage('employer:req2')
        with self.q.store.connection() as db:db.execute('UPDATE delivery_items SET metadata=? WHERE key=?',('{broken',a['key']))
        result=self.run_worker()
        self.assertEqual(result['status'],'completed');self.assertEqual(self.q.get(b['key'])['status'],'api_accepted')
        with self.q.store.connection() as db:self.assertEqual(db.execute('SELECT status FROM delivery_items WHERE key=?',(a['key'],)).fetchone()[0],'held')
    def test_hash_consistent_payload_with_wrong_binding_is_held(self):
        for field,value in [('pdf_hash','0'*64),('recipient','other@example.com')]:
            with self.subTest(field=field):
                x=self.stage('employer:'+field)
                with self.q.store.connection() as db:db.execute('UPDATE delivery_items SET '+field+'=? WHERE key=?',(value,x['key']))
                self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'held')
        self.assertEqual(self.transport.calls,[])
    def test_retry_header_bounds_and_absolute_deadline(self):
        for i,header in enumerate(['999999999','-1',str(7*86400+1),180,b'180',[]]):
            x=self.stage('employer:invalid'+str(i));self.transport.result={'state':'rejected','http_status':429,'retry_after':header};self.run_worker()
            self.assertEqual(self.q.get(x['key'])['status'],'held')
        x=self.stage('employer:deadline');self.transport.result={'state':'rejected','http_status':429,'retry_after':str(7*86400)};self.run_worker()
        self.run_worker('2026-10-23T10:00:00Z');self.assertEqual(self.q.get(x['key'])['status'],'held')
        self.assertEqual(self.q.get(x['key'])['attempts'],1)
    def test_provider_header_ignored_when_no_send_was_attempted(self):
        x=self.stage();self.transport.result={'state':'not_attempted','retry_after':str(7*86400)};self.run_worker()
        self.assertEqual(self.q.get(x['key'])['next_attempt_at'],'2026-10-16T10:01:00+00:00')
    def test_utc_offsets_share_attempt_cap_bucket(self):
        self.stage();self.stage('employer:req2');self.run_worker(now='2026-10-15T23:30:00-04:00',daily_cap=1)
        self.run_worker(now='2026-10-16T04:00:00Z',daily_cap=1)
        self.assertEqual(len(self.transport.calls),1)

    def test_sigkill_after_handoff_is_recovered_without_resending(self):
        import subprocess,sys,os,signal
        x=self.stage();marker=self.root/'provider-handoff'
        script='''
import os,signal,sys
from pathlib import Path
from delivery import DeliveryQueue,DeliveryWorker
class Handoff:
 def send(self,raw):
  with open(sys.argv[2],'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.kill(os.getpid(),signal.SIGKILL)
DeliveryWorker(DeliveryQueue(sys.argv[1]),Handoff(),readiness=lambda item:True,enabled=True).run(sys.argv[3])
'''
        child=subprocess.run([sys.executable,'-c',script,str(self.q.path),str(marker),self.now],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,-signal.SIGKILL);self.assertEqual(marker.read_bytes(),self.raw());self.assertEqual(self.q.get(x['key'])['status'],'sending')
        self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'delivery_unknown');self.assertEqual(self.transport.calls,[])
    def test_second_process_cannot_claim_while_worker_lock_is_held(self):
        import subprocess,sys
        self.stage()
        script='''
import sys
from delivery import DeliveryQueue,DeliveryWorker
class Fail:
 def send(self,raw):raise AssertionError('lock allowed transport')
r=DeliveryWorker(DeliveryQueue(sys.argv[1]),Fail(),readiness=lambda item:True,enabled=True).run(sys.argv[2])
print(r['status'])
'''
        with self.q.store.runner_lock() as acquired:
            self.assertTrue(acquired)
            child=subprocess.run([sys.executable,'-c',script,str(self.q.path),self.now],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(child.stdout.strip(),'busy');self.assertEqual(self.q.status(),{'needs_review':1})

    def test_review_kind_cannot_be_promoted_by_callback(self):
        x=self.stage()
        with self.q.store.connection() as db:db.execute('UPDATE delivery_items SET metadata=? WHERE key=?',(json.dumps({'packet_kind':'review'}),x['key']))
        self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'needs_review');self.assertEqual(self.transport.calls,[])
    def test_changed_canonical_key_is_held(self):
        x=self.stage()
        with self.q.store.connection() as db:db.execute('UPDATE delivery_items SET canonical_job=? WHERE key=?',('other:req',x['key']))
        self.run_worker();self.assertEqual(self.q.get(x['key'])['status'],'held');self.assertEqual(self.transport.calls,[])
    def test_nonretryable_rejections_and_malformed_acceptance_are_terminal(self):
        for i,result in enumerate([{'state':'rejected','http_status':403},{'state':'rejected','http_status':400},{'state':'rejected','http_status':500},{'state':'api_accepted'},{'state':'api_accepted','provider_message_id':'bad id'}]):
            x=self.stage('employer:result'+str(i));self.transport.result=result;self.run_worker()
            self.assertEqual(self.q.get(x['key'])['status'],'rejected' if i<2 else 'delivery_unknown')
        self.run_worker('2026-10-17T10:00:00Z');self.assertEqual(len(self.transport.calls),5)
    def test_monthly_cap_and_recovery_attempt_status_require_lock(self):
        x=self.stage();self.run_worker(monthly_cap=1);self.stage('employer:nextday');self.run_worker('2026-10-17T10:00:00Z',monthly_cap=1)
        self.assertEqual(len(self.transport.calls),1)
        with self.assertRaises(ValueError):self.q.recover()
        with self.q.store.connection() as db:db.execute("UPDATE delivery_items SET status='sending' WHERE key=?",(x['key'],));db.execute("UPDATE delivery_attempts SET status='sending'")
        with self.q.store.runner_lock() as acquired:self.assertTrue(acquired);self.q.recover()
        with self.q.store.connection() as db:self.assertEqual(db.execute('SELECT status FROM delivery_attempts').fetchone()[0],'delivery_unknown')

    def test_gmail_configuration_failure_is_held_before_attempt(self):
        from mail_transport import GmailTransport
        x=self.stage();calls=[]
        t=GmailTransport(self.root/'missing.json',recipient='recipient@example.com',sender='sender@example.com',http=lambda *args:calls.append(args))
        DeliveryWorker(self.q,t,readiness=lambda item:True,enabled=True).run(self.now)
        item=self.q.get(x['key']);self.assertEqual(item['status'],'held');self.assertEqual(item['attempts'],0);self.assertEqual(calls,[])

    def test_attempt_and_provider_completion_use_measured_clock(self):
        x=self.stage();times=iter(['2026-10-16T10:00:01Z','2026-10-16T10:00:07Z'])
        DeliveryWorker(self.q,self.transport,readiness=lambda item:True,enabled=True,clock=lambda:next(times)).run(self.now)
        with self.q.store.connection() as db:
            start=db.execute('SELECT started_at FROM delivery_attempts').fetchone()[0];finish=db.execute('SELECT finished_at FROM delivery_receipts').fetchone()[0]
        self.assertEqual(start,'2026-10-16T10:00:01+00:00');self.assertEqual(finish,'2026-10-16T10:00:07+00:00');self.assertEqual(self.q.get(x['key'])['status'],'api_accepted')
