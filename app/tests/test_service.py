"""One-shot scheduled service integration; no external sends or real candidate."""
import unittest,json,copy
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
import test_alerts as fixtures
from packet import canonical
try:
    from service import run_service,service_health
except ImportError:run_service=service_health=None

class ServiceTests(unittest.TestCase):
    now=fixtures.AlertTests.now
    identifier=fixtures.AlertTests.identifier
    setUp=fixtures.AlertTests.setUp
    extract=fixtures.AlertTests.extract
    get=fixtures.AlertTests.get
    def config(self):
        p=self.root/'service.json';p.write_bytes(canonical({'version':1,'profile_path':str(self.profile),'approval_path':str(self.approval),'state_dir':str(self.root/'service'),'sources':[self.source],'sender':'sender@example.com','sending_enabled':False,'semantic_enabled':True,'shortlist_limit':3,'notify_existing':True,'daily_email_cap':3,'monthly_email_cap':60,'pilot_ends_at':'2026-10-22T00:00:00Z'}));p.chmod(0o600);return p
    def extractor(self):
        outer=self
        class Extract:
            mode='segments'
            def extract(self,text,now):return outer.extraction
        return Extract()
    def test_enabled_service_requires_explicit_pilot_limits(self):
        from service import configuration
        from datetime import timedelta
        cfg=self.config();base=json.loads(cfg.read_text());base.update(sending_enabled=True,daily_email_cap=3,monthly_email_cap=60,pilot_ends_at='2026-10-22T00:00:00Z')
        base['pilot_ends_at']=(datetime.now(timezone.utc)+timedelta(days=6)).isoformat()
        cfg.write_bytes(canonical(base));self.assertEqual(configuration(cfg)['daily_email_cap'],3)
        for field in ['daily_email_cap','monthly_email_cap','pilot_ends_at']:
            c=dict(base);c.pop(field);cfg.write_bytes(canonical(c))
            with self.subTest(missing=field),self.assertRaises(ValueError):configuration(cfg)
        for field,value in [('daily_email_cap',4),('monthly_email_cap',61),('pilot_ends_at','2099-01-01T00:00:00Z')]:
            c=dict(base);c[field]=value;cfg.write_bytes(canonical(c))
            with self.subTest(field=field),self.assertRaises(ValueError):configuration(cfg)
    def test_wrapped_transport_does_not_bypass_real_clock_guard(self):
        from datetime import timedelta
        from service import _PilotTransport
        cfg=self.config();c=json.loads(cfg.read_text());c['sending_enabled']=True;cfg.write_bytes(canonical(c))
        class Transport:
            def send(self,raw):raise AssertionError('must not reach transport')
        t=_PilotTransport(Transport(),c['pilot_ends_at'],lambda:self.now)
        with self.assertRaisesRegex(ValueError,'current system clock'):
            run_service(cfg,now=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),get=self.get,extractor=self.extractor(),transport=t)
    def test_official_job_template_is_checked_when_loading_configuration(self):
        from service import configuration
        cfg=self.config();c=json.loads(cfg.read_text());c['sources'][0]['official_job_page_template']='https://evil.example/jobs/{id}';cfg.write_bytes(canonical(c))
        with self.assertRaises(ValueError):configuration(cfg)
        c['sources'][0]['official_job_page_template']='https://employer.example/jobs/{id}';cfg.write_bytes(canonical(c));self.assertEqual(configuration(cfg)['sources'][0]['board'],'example')
    def test_qualified_source_to_queue_and_repeat_is_not_processed_again(self):
        self.assertIsNotNone(run_service);cfg=self.config();r=run_service(cfg,now=self.now,get=self.get,extractor=self.extractor())
        self.assertEqual(r['sources'][0]['qualified'],1);self.assertEqual(r['queue'],{'needs_review':1});self.assertEqual(r['delivery']['transport_calls'],0)
        repeat=run_service(cfg,now='2026-10-16T10:01:00Z',get=self.get,extractor=self.extractor());self.assertEqual(repeat['sources'][0]['status'],'not_due')
        self.assertEqual(service_health(cfg,'2026-10-16T10:01:00Z')['status'],'healthy')
    def test_expired_unattempted_alert_is_refreshed_and_then_sent_once(self):
        from delivery import DeliveryQueue
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor())
        q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0];original=q.get(key)
        class Transport:
            def __init__(self):self.calls=[]
            def send(self,raw):self.calls.append(raw);return {'state':'api_accepted','provider_message_id':'synthetic-acceptance'}
        v=json.loads(cfg.read_text());v['sending_enabled']=True;cfg.write_bytes(canonical(v));t=Transport()
        r=run_service(cfg,now='2026-10-17T10:01:00Z',get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(len(t.calls),1);self.assertEqual(r['refreshed_alerts'],1)
        refreshed=q.get(key);self.assertNotEqual(original['payload_hash'],refreshed['payload_hash']);self.assertEqual(refreshed['status'],'api_accepted')
        run_service(cfg,now='2026-10-18T10:01:00Z',get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(len(t.calls),1)
    def test_transient_refresh_failure_retries_without_losing_unattempted_alert(self):
        from delivery import DeliveryQueue
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0]
        def flaky(url,hosts):
            if '/application' in url:raise TimeoutError('transient fetch')
            return self.get(url,hosts)
        run_service(cfg,now='2026-10-17T10:01:00Z',get=flaky,extractor=self.extractor())
        self.assertEqual(q.get(key)['status'],'needs_review');self.assertEqual(q.get(key)['attempts'],0)
        r=run_service(cfg,now='2026-10-17T10:06:00Z',get=self.get,extractor=self.extractor());self.assertEqual(r['refreshed_alerts'],1)
    def test_refresh_appends_decision_evidence_and_retains_original_packet(self):
        from delivery import DeliveryQueue
        import sqlite3
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0];old=q.get(key);directory=self.root/'service/alerts'/old['metadata']['packet_key'];original=(directory/'message.eml').read_bytes()
        run_service(cfg,now='2026-10-17T10:01:00Z',get=self.get,extractor=self.extractor())
        self.assertEqual((directory/'message.eml').read_bytes(),original);self.assertEqual(q.get(key)['attempts'],0)
        with sqlite3.connect(self.root/'service/service.sqlite') as db:records=[json.loads(row[0]) for row in db.execute('SELECT record FROM decision_records')]
        self.assertTrue(any(row.get('kind')=='packet_refresh' and row['evaluated_at']=='2026-10-17T10:01:00Z' for row in records))
    def test_unattempted_refresh_generation_is_bounded(self):
        from delivery import DeliveryQueue
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0]
        for day in [17,18,19]:
            r=run_service(cfg,now=f'2026-10-{day}T10:01:{day}Z',get=self.get,extractor=self.extractor());self.assertEqual(r['refreshed_alerts'],1)
        r=run_service(cfg,now='2026-10-20T10:02:00Z',get=self.get,extractor=self.extractor());self.assertEqual(r['refreshed_alerts'],0);self.assertEqual(q.get(key)['status'],'held')
    def test_before_expiry_packet_does_not_refresh(self):
        from delivery import DeliveryQueue
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0];old=q.get(key)['payload_hash']
        r=run_service(cfg,now='2026-10-17T09:59:00Z',get=self.get,extractor=self.extractor());self.assertEqual(r['refreshed_alerts'],0);self.assertEqual(q.get(key)['payload_hash'],old)
    def test_attempted_expired_packet_does_not_bypass_readiness(self):
        from delivery import DeliveryQueue,DeliveryWorker
        class Transport:
            def __init__(self):self.calls=0
            def send(self,raw):self.calls+=1;return {'state':'not_attempted'}
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');t=Transport()
        DeliveryWorker(q,t,enabled=True,readiness=lambda item:True).run(self.now);self.assertEqual(t.calls,1)
        v=json.loads(cfg.read_text());v['sending_enabled']=True;cfg.write_bytes(canonical(v))
        run_service(cfg,now='2026-10-17T10:01:00Z',get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(t.calls,1);self.assertEqual(q.status(),{'needs_review':1})
    def test_audit_capacity_does_not_strand_or_replace_waiting_packet(self):
        from delivery import DeliveryQueue
        import sqlite3
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0];old=q.get(key)['payload_hash']
        with sqlite3.connect(self.root/'service/service.sqlite') as db:db.executemany('INSERT INTO decision_records VALUES(?,?)',[(f'cap-{i}','{}') for i in range(49998)])
        r=run_service(cfg,now='2026-10-17T10:01:00Z',get=self.get,extractor=self.extractor())
        self.assertEqual(q.get(key)['status'],'needs_review');self.assertEqual(q.get(key)['payload_hash'],old);self.assertEqual(r['refresh_audit_capacity_holds'],1)
    def test_explicit_pilot_caps_limit_emails_and_expiry_stops_transport(self):
        cfg=self.config();v=json.loads(cfg.read_text());v.update(sending_enabled=True,daily_email_cap=1,monthly_email_cap=20,pilot_ends_at='2026-10-17T10:00:00Z');cfg.write_bytes(canonical(v))
        other=dict(self.row,applyUrl=self.row['applyUrl'].replace(self.identifier,'22222222-2222-4222-8222-222222222222'))
        def get(url,hosts):
            if 'api.ashbyhq.com' in url:return canonical({'jobs':[self.row,other]})
            return self.get(url.replace('22222222-2222-4222-8222-222222222222',self.identifier),hosts)
        class Transport:
            def __init__(self):self.calls=[]
            def send(self,raw):self.calls.append(raw);return {'state':'api_accepted','provider_message_id':'synthetic-acceptance'}
        t=Transport();r=run_service(cfg,now=self.now,get=get,extractor=self.extractor(),transport=t,_allow_test_clock=True);self.assertEqual(len(t.calls),1);self.assertEqual(r['queue'],{'api_accepted':1,'needs_review':1});self.assertTrue(any(o['status']=='deferred' for o in r['delivery']['outcomes']))
        repeat=run_service(cfg,now='2026-10-16T10:05:00Z',get=get,extractor=self.extractor(),transport=t,_allow_test_clock=True);self.assertEqual(len(t.calls),1);self.assertTrue(any(o['status']=='deferred' for o in repeat['delivery']['outcomes']))
        r=run_service(cfg,now='2026-10-17T10:00:00Z',get=get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(len(t.calls),1);self.assertEqual(r['delivery']['status'],'pilot_expired');self.assertFalse(service_health(cfg,'2026-10-17T10:00:00Z')['sending_enabled'])
    def test_pilot_caps_are_bounded_and_expiry_requires_aware_timestamp(self):
        from service import configuration
        cfg=self.config()
        for field,value in [('daily_email_cap',0),('daily_email_cap',True),('monthly_email_cap',601),('pilot_ends_at','2026-10-17')]:
            c=json.loads(cfg.read_text());c[field]=value;cfg.write_bytes(canonical(c))
            with self.subTest(field=field,value=value):
                with self.assertRaises(ValueError):configuration(cfg)
            c.pop(field);cfg.write_bytes(canonical(c))
    def test_pilot_expiring_after_claim_still_cannot_reach_provider(self):
        from delivery import DeliveryQueue
        from datetime import datetime,timezone
        cfg=self.config();v=json.loads(cfg.read_text());v.update(sending_enabled=True,pilot_ends_at='2026-10-16T10:00:05Z');cfg.write_bytes(canonical(v))
        class Transport:
            def __init__(self):self.calls=[]
            def send(self,raw):self.calls.append(raw);return {'state':'api_accepted','provider_message_id':'unexpected'}
        t=Transport();original=DeliveryQueue.claim
        with patch('service.datetime') as clock:
            clock.now.return_value=datetime(2026,10,16,10,0,0,tzinfo=timezone.utc)
            def claim(queue,*args,**kwargs):
                result=original(queue,*args,**kwargs);clock.now.return_value=datetime(2026,10,16,10,0,5,tzinfo=timezone.utc);return result
            with patch.object(DeliveryQueue,'claim',claim):r=run_service(cfg,get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(t.calls,[]);self.assertEqual(r['queue'],{'retry_wait':1});self.assertEqual(r['delivery']['transport_calls'],0)
    def test_post_refresh_audit_failure_does_not_undo_or_hold_refreshed_item(self):
        from delivery import DeliveryQueue
        from state import Store
        from contextlib import contextmanager
        import sqlite3
        cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());q=DeliveryQueue(self.root/'service/delivery.sqlite');key=q.candidates(self.now,1)[0];original=Store.connection
        @contextmanager
        def fail_queued_event(store):
            with original(store) as db:
                class Proxy:
                    def execute(self,sql,args=()):
                        if 'INSERT OR IGNORE INTO decision_records' in sql and args and '"outcome":"queued"' in str(args[-1]):raise sqlite3.OperationalError('simulated audit outage')
                        return db.execute(sql,args)
                yield Proxy()
        with patch.object(Store,'connection',fail_queued_event):r=run_service(cfg,now='2026-10-17T10:01:00Z',get=self.get,extractor=self.extractor())
        self.assertEqual(q.get(key)['status'],'needs_review');self.assertEqual(r['refreshed_alerts'],1);self.assertGreaterEqual(r['refresh_audit_pending'],1)
        with q.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_refreshes').fetchone()[0],1)
    def test_source_failure_preserves_previous_good_state_and_reports_unhealthy(self):
        self.assertIsNotNone(run_service);cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor())
        def fail(url,hosts):raise TimeoutError('secret provider message')
        r=run_service(cfg,now='2026-10-16T10:31:00Z',get=fail,extractor=self.extractor());self.assertEqual(r['sources'][0]['status'],'source_failed')
        self.assertNotIn('secret provider',json.dumps(r));self.assertEqual(service_health(cfg,'2026-10-16T10:32:00Z')['status'],'degraded')
    def test_explicit_sending_uses_current_readiness_and_frozen_payload(self):
        self.assertIsNotNone(run_service);cfg=self.config();v=json.loads(cfg.read_text());v['sending_enabled']=True;cfg.write_bytes(canonical(v))
        class Transport:
            def __init__(self):self.calls=[]
            def send(self,raw):self.calls.append(raw);return {'state':'api_accepted','provider_message_id':'synthetic-acceptance'}
        t=Transport();r=run_service(cfg,now=self.now,get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertEqual(len(t.calls),1);self.assertEqual(r['queue'],{'api_accepted':1});self.assertFalse(r['delivery']['delivery_verified'])
        run_service(cfg,now='2026-10-16T10:31:00Z',get=self.get,extractor=self.extractor(),transport=t,_allow_test_clock=True);self.assertEqual(len(t.calls),1)
    def test_stopped_runner_is_detectable_and_disabled_semantics_costs_nothing(self):
        self.assertIsNotNone(run_service);cfg=self.config();v=json.loads(cfg.read_text());v['semantic_enabled']=False;cfg.write_bytes(canonical(v));r=run_service(cfg,now=self.now,get=self.get)
        self.assertEqual(r['sources'][0]['qualified'],0);self.assertEqual(service_health(cfg,'2026-10-16T12:00:00Z')['status'],'stale')
    def test_cli_health_loads_private_config_without_network(self):
        import subprocess,sys
        cfg=self.config();res=subprocess.run([sys.executable,'main.py','health','--config',str(cfg)],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(res.returncode,0,res.stderr);self.assertEqual(json.loads(res.stdout)['status'],'stale')
    def test_decision_evidence_is_persisted_for_each_processed_job(self):
        import sqlite3
        self.assertIsNotNone(run_service);cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor())
        with sqlite3.connect(self.root/'service/service.sqlite') as db:
            rows=db.execute('SELECT record FROM decision_records').fetchall()
        self.assertEqual(len(rows),1);e=json.loads(rows[0][0]);self.assertEqual(e['decision']['status'],'eligible');self.assertEqual(e['decision']['evidence_rows'][0]['status'],'met')
    def test_bootstrap_baseline_does_not_flood_inbox_but_new_opening_is_processed(self):
        self.assertIsNotNone(run_service);cfg=self.config();v=json.loads(cfg.read_text());v['notify_existing']=False;cfg.write_bytes(canonical(v))
        r=run_service(cfg,now=self.now,get=self.get,extractor=self.extractor());self.assertEqual(r['queue'],{});self.assertEqual(r['sources'][0]['baseline'],1)
        second=dict(self.row,applyUrl=self.row['applyUrl'].replace(self.identifier,'22222222-2222-4222-8222-222222222222'))
        def get(url,hosts):return canonical({'jobs':[self.row,second]}) if 'api.ashbyhq.com' in url else self.get(url,hosts)
        r=run_service(cfg,now='2026-10-16T10:31:00Z',get=get,extractor=self.extractor());self.assertEqual(r['sources'][0]['qualified'],1);self.assertEqual(r['queue'],{'needs_review':1})
    def test_closure_after_evaluation_is_refetched_before_send(self):
        self.assertIsNotNone(run_service);cfg=self.config();v=json.loads(cfg.read_text());v['sending_enabled']=True;cfg.write_bytes(canonical(v));feed_calls=[]
        def get(url,hosts):
            if 'api.ashbyhq.com' in url:
                feed_calls.append(url);return canonical({'jobs':[dict(self.row,isListed=len(feed_calls)==1)]})
            return self.get(url,hosts)
        class Transport:
            def __init__(self):self.calls=[]
            def send(self,raw):self.calls.append(raw);return {'state':'api_accepted','provider_message_id':'accepted'}
        t=Transport();r=run_service(cfg,now=self.now,get=get,extractor=self.extractor(),transport=t,_allow_test_clock=True)
        self.assertGreaterEqual(len(feed_calls),2);self.assertEqual(t.calls,[])
    def test_health_handles_retired_board_rows(self):
        import sqlite3
        self.assertIsNotNone(run_service);cfg=self.config();run_service(cfg,now=self.now,get=self.get,extractor=self.extractor())
        with sqlite3.connect(self.root/'service/service.sqlite') as db:db.execute("INSERT INTO service_sources(board,last_success) VALUES('retired','2026-10-16T10:00:00Z')")
        self.assertEqual(service_health(cfg,self.now)['status'],'healthy')
    def test_runtime_records_distinct_detection_and_completion_times(self):
        import sqlite3
        from datetime import datetime,timezone,timedelta
        self.assertIsNotNone(run_service);cfg=self.config();base=datetime(2026,10,16,10,tzinfo=timezone.utc);counter=iter(range(100))
        with patch('service.datetime') as clock:
            clock.now.side_effect=lambda *args:base+timedelta(seconds=next(counter))
            r=run_service(cfg,get=self.get,extractor=self.extractor())
        self.assertLess(r['started_at'],r['completed_at'])
        with sqlite3.connect(self.root/'service/service.sqlite') as db:seen=db.execute('SELECT first_seen FROM jobs').fetchone()[0]
        self.assertGreater(seen,r['started_at']);self.assertLess(seen,r['completed_at'])
    def test_real_delivery_refuses_a_stale_injected_clock(self):
        self.assertIsNotNone(run_service);cfg=self.config();v=json.loads(cfg.read_text());v['sending_enabled']=True;cfg.write_bytes(canonical(v))
        with self.assertRaisesRegex(ValueError,'clock'):run_service(cfg,now='2020-01-01T00:00:00Z',get=self.get,extractor=self.extractor())
