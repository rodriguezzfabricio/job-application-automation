"""No paid requests: transport, budget, replay, and secret boundaries."""
import json,tempfile,unittest
from pathlib import Path
from state import Store
try:
    from deepseek_transport import DeepSeekExtractor
except ImportError:DeepSeekExtractor=None

class DeepSeekTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.key=self.root/'provider.key';self.key.write_text('synthetic-test-key');self.key.chmod(0o600);self.calls=[]
    def http(self,method,url,headers,body,timeout):
        self.calls.append((method,url,headers,body,timeout))
        return 200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'requirements':[{'quote':'Python required.','importance':'mandatory','category':'skill','alternatives':['Python']}]})}}],'usage':{'prompt_tokens':100,'completion_tokens':50}}).encode()
    def extractor(self,**kw):
        self.assertIsNotNone(DeepSeekExtractor)
        return DeepSeekExtractor(self.key,self.root/'state.sqlite',self.root/'cache',http=self.http,**kw)
    def test_exact_call_settles_cost_and_repeat_uses_cache(self):
        extractor=self.extractor();one=extractor.extract('Python required.',now='2026-10-16T01:00:00Z');two=extractor.extract('Python required.',now='2026-10-16T01:01:00Z')
        self.assertEqual(len(self.calls),1);self.assertEqual(one['requirements'],two['requirements'])
        self.assertFalse(one['requirements_verified']);self.assertTrue(two['cache_hit'])
        self.assertEqual(one['incremental_cost_estimate_microusd'],90);self.assertEqual(two['incremental_cost_estimate_microusd'],0)
        method,url,headers,body,timeout=self.calls[0]
        self.assertEqual((method,url),('POST','https://api.deepseek.com/chat/completions'));self.assertEqual(timeout,45)
        self.assertEqual(headers['Authorization'],'Bearer synthetic-test-key');self.assertNotIn('synthetic-test-key',body.decode())
        budget=Store(self.root/'state.sqlite').budget('deepseek:2026-10')
        self.assertEqual(budget['spent'],90);self.assertEqual(budget['reserved'],0)
    def test_failure_audit_keeps_status_and_phase_without_provider_text_or_key(self):
        def rejected(*args):return 429,b'private-provider-body-with-secret'
        extractor=self.extractor();extractor.http=rejected
        with self.assertRaises(ValueError):extractor.extract('Python required.',now='2026-10-16T01:00:00Z')
        records=list((self.root/'cache').glob('*.failure.json'));self.assertEqual(len(records),1)
        value=json.loads(records[0].read_text());self.assertEqual(value['http_status'],429);self.assertEqual(value['phase'],'http_status')
        self.assertEqual(records[0].stat().st_mode&0o777,0o600);self.assertNotIn('private-provider',records[0].read_text());self.assertNotIn('synthetic-test-key',records[0].read_text())
    def test_budget_refuses_before_request(self):
        with self.assertRaises(ValueError):self.extractor(monthly_cap_microusd=100).extract('Python required.',now='2026-10-16T01:00:00Z')
        self.assertEqual(self.calls,[])
    def test_uncertain_request_is_not_automatically_retried(self):
        def fail(*args):self.calls.append(args);raise TimeoutError('secret response details')
        extractor=self.extractor();extractor.http=fail
        for _ in range(2):
            with self.assertRaises(ValueError) as error:extractor.extract('Python required.',now='2026-10-16T01:00:00Z')
            self.assertNotIn('secret response',str(error.exception))
        self.assertEqual(len(self.calls),1)
        self.assertGreater(Store(self.root/'state.sqlite').budget('deepseek:2026-10')['reserved'],0)
    def test_public_or_symlink_key_refused_before_request(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):self.extractor().extract('Python required.',now='2026-10-16T01:00:00Z')
        self.key.chmod(0o600);link=self.root/'link';link.symlink_to(self.key);self.key=link
        with self.assertRaises(ValueError):self.extractor().extract('Python required.',now='2026-10-16T01:00:00Z')
        self.assertEqual(self.calls,[])
    def test_budget_settlement_cannot_exceed_reservation_or_repeat(self):
        store=Store(self.root/'state.sqlite');self.assertTrue(store.reserve('month','call',100,1000))
        self.assertTrue(hasattr(store,'settle'),'cost settlement missing')
        with self.assertRaises(ValueError):store.settle('month','call',101)
        store.settle('month','call',60)
        self.assertEqual(store.budget('month')['spent'],60);self.assertEqual(store.budget('month')['reserved'],0)
        with self.assertRaises(ValueError):store.settle('month','call',60)
        self.assertFalse(store.reserve('month','call',100,1000))

    def test_unknown_call_does_not_retry_when_month_changes(self):
        def fail(*args):self.calls.append(args);raise TimeoutError()
        extractor=self.extractor();extractor.http=fail
        for now in ['2026-10-16T01:00:00Z','2026-11-16T01:00:00Z']:
            with self.assertRaises(ValueError):extractor.extract('Python required.',now=now)
        self.assertEqual(len(self.calls),1)

    def test_failed_output_preserved_privately_without_repeat_call(self):
        def unsupported(*args):
            status,raw=self.http(*args);response=json.loads(raw)
            response['choices'][0]['message']['content']=json.dumps({'requirements':[{'quote':'Invented requirement.','importance':'mandatory','category':'skill','alternatives':[]}]})
            return status,json.dumps(response).encode()
        extractor=self.extractor();extractor.http=unsupported
        for _ in range(2):
            with self.assertRaises(ValueError):extractor.extract('Python required.',now='2026-10-16T01:00:00Z')
        self.assertEqual(len(self.calls),1)
        audits=list((self.root/'cache').glob('*.json'));self.assertEqual(len(audits),1)
        self.assertEqual(audits[0].stat().st_mode&0o077,0)
        self.assertIn('Invented requirement.',audits[0].read_text())

    def test_explicit_segment_mode_uses_same_budget_and_cache_boundary(self):
        extractor=self.extractor(mode='segments')
        def http(method,url,headers,body,timeout):
            self.calls.append((method,url,headers,body,timeout))
            return 200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','skill','mandatory',['Python']]]})}}],'usage':{'prompt_tokens':100,'completion_tokens':50}}).encode()
        extractor.http=http
        result=extractor.extract('Python required.',now='2026-10-16T01:00:00Z')
        self.assertTrue(result['segment_coverage_complete']);self.assertFalse(result['requirements_verified'])
        again=extractor.extract('Python required.',now='2026-10-16T01:00:00Z')
        self.assertTrue(again['cache_hit']);self.assertEqual(len(self.calls),1)
