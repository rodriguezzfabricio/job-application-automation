"""AI suggestions cite original facts; never certify eligibility or modify a resume."""
import unittest,json,copy
from pathlib import Path
import test_qualification as fixtures
from packet import canonical,digest
from semantic_segments import parse_segment_response
try:
 from semantic_evidence import build_evidence_request,parse_evidence_response
except ImportError:build_evidence_request=parse_evidence_response=None

class EvidenceTests(unittest.TestCase):
 def setUp(self):
  f=fixtures.QualificationTests();f.setUp();self.addCleanup(f.doCleanups);self.p=f.p;self.sha=f.sha;self.root=Path(f.tmp.name) if hasattr(f,'tmp') else Path(self.p['source']['path']).parent
  self.export={'authority':'synthetic explicit professional export approval','profile_sha256':self.sha,'fact_hashes':{v['id']:digest(v['text'].encode()) for v in self.p['variants']}};self.export_sha=digest(canonical(self.export));self.export_path=self.root/'fact-export.json';self.export_path.write_bytes(canonical(self.export));self.export_path.chmod(0o600)
  self.job=f.job('Requirements\nPython required.');self.extraction=parse_segment_response({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Python']]]})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}},self.job['description'])
 def bundle(self):
  self.assertIsNotNone(build_evidence_request)
  return build_evidence_request(self.p,self.job,self.extraction,approval_sha256=self.sha,fact_export_approval=self.export,fact_export_sha256=self.export_sha)
 def response(self,rows):return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'evidence':rows})}}],'usage':{'prompt_tokens':100,'completion_tokens':100}}
 def test_outbound_uses_only_original_bullets_and_no_contact_or_source_path(self):
  request,context=self.bundle();body=canonical(request).decode();data=json.loads(request['messages'][1]['content'])
  self.assertNotIn(self.p['email'],body);self.assertNotIn(self.p.get('name','NEVER_THIS'),body);self.assertNotIn(self.p['source']['path'],body)
  self.assertEqual(set(data),{'untrusted_job_requirements','approved_original_facts','bindings'})
  # Required by DeepSeek's JSON-mode API contract, not only a parsing preference.
  self.assertRegex(request['messages'][0]['content'],r'(?i)\bjson\b')
  self.assertEqual({v['text'] for v in data['approved_original_facts']},{v['text'] for v in self.p['variants']})
 def test_suggestion_is_unverified_and_does_not_certify_or_rewrite(self):
  request,context=self.bundle();fact=next(iter(context['facts']));r=parse_evidence_response(self.response([['s1','possible_support',[fact]]]),context)
  self.assertFalse(r['requirements_verified']);self.assertFalse(r['application_ready']);self.assertFalse(r['qualification_changed']);self.assertEqual(r['evidence'][0]['supporting_original_facts'][0]['text'],context['facts'][fact])
 def test_missing_or_invented_rows_facts_and_freeform_fields_are_refused(self):
  _,context=self.bundle();fact=next(iter(context['facts']))
  for rows in [[],[['s9','possible_support',[fact]]],[['s1','possible_support',['invented']]],[['s1','possible_support',[]]],[['s1','met',[fact]]],[['s1','unclear',[]],['s1','unclear',[]]]]:
   with self.subTest(rows=rows):
    with self.assertRaises(ValueError):parse_evidence_response(self.response(rows),context)
  response=self.response([['s1','unclear',[]]]);response['choices'][0]['message']['content']=json.dumps({'evidence':[['s1','unclear',[]]],'new_resume':'invented'})
  with self.assertRaises(ValueError):parse_evidence_response(response,context)
 def test_wrong_profile_or_source_binding_is_refused_before_request(self):
  bad=copy.deepcopy(self.p);bad['email']='changed@example.com'
  with self.assertRaises(ValueError):build_evidence_request(bad,self.job,self.extraction,approval_sha256=self.sha,fact_export_approval=self.export,fact_export_sha256=self.export_sha)
  job=dict(self.job,description='Rust required.')
  with self.assertRaises(ValueError):build_evidence_request(self.p,job,self.extraction,approval_sha256=self.sha,fact_export_approval=self.export,fact_export_sha256=self.export_sha)
 def test_truncated_tool_call_and_invalid_usage_are_held(self):
  _,context=self.bundle()
  for field,value in [('finish_reason','length'),('tool_calls',[{'function':'email'}]),('usage',{'prompt_tokens':True,'completion_tokens':1})]:
   response=self.response([['s1','unclear',[]]])
   if field=='finish_reason':response['choices'][0][field]=value
   elif field=='tool_calls':response['choices'][0]['message'][field]=value
   else:response[field]=value
   with self.assertRaises(ValueError):parse_evidence_response(response,context)
 def test_hard_eligibility_cannot_be_proved_by_project_fact_suggestions(self):
  text='Must be a US citizen.';job=dict(self.job,description=text);extraction=parse_segment_response({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','authorization','mandatory',[]]]})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}},text)
  _,context=build_evidence_request(self.p,job,extraction,approval_sha256=self.sha,fact_export_approval=self.export,fact_export_sha256=self.export_sha);fact=next(iter(context['facts']))
  result=parse_evidence_response(self.response([['s0','possible_support',[fact]]]),context);self.assertEqual(result['evidence'][0]['suggestion'],'unclear');self.assertEqual(result['evidence'][0]['guard'],'hard_eligibility_requires_separate_check');self.assertFalse(result['requirements_verified'])
 def test_transport_shares_budget_and_cache_and_excludes_contacts(self):
  from deepseek_transport import DeepSeekExtractor
  self.root.chmod(0o700);key=self.root/'semantic-test.key';key.write_text('synthetic-key');key.chmod(0o600);calls=[];_,context=self.bundle();fact=next(iter(context['facts']))
  def http(method,url,headers,body,timeout):calls.append(body);return 200,canonical(self.response([['s1','possible_support',[fact]]]))
  model=DeepSeekExtractor(key,self.root/'semantic-budget.sqlite',self.root/'semantic-cache',http=http)
  first=model.assess_evidence(self.p,self.job,self.extraction,approval_sha256=self.sha,fact_export_path=self.export_path,fact_export_sha256=self.export_sha,now='2026-10-16T10:00:00Z');second=model.assess_evidence(self.p,self.job,self.extraction,approval_sha256=self.sha,fact_export_path=self.export_path,fact_export_sha256=self.export_sha,now='2026-10-16T10:01:00Z')
  self.assertEqual(len(calls),1);self.assertTrue(second['cache_hit']);self.assertFalse(first['application_ready']);self.assertNotIn(self.p['email'].encode(),calls[0])
  from state import Store
  self.assertEqual(Store(self.root/'semantic-budget.sqlite').budget('deepseek:2026-10')['spent'],150)
 def test_missing_technology_cannot_be_supported_with_unrelated_original_fact(self):
  text='Rust required.';job=dict(self.job,description=text);extraction=parse_segment_response({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','skill','mandatory',['Rust']]]})}}],'usage':{'prompt_tokens':1,'completion_tokens':1}},text)
  _,context=build_evidence_request(self.p,job,extraction,approval_sha256=self.sha,fact_export_approval=self.export,fact_export_sha256=self.export_sha)
  result=parse_evidence_response(self.response([['s0','possible_support',['api']]]),context);self.assertEqual(result['evidence'][0]['suggestion'],'unclear');self.assertEqual(result['evidence'][0]['guard'],'required_skill_absent_from_cited_facts');self.assertEqual(result['evidence'][0]['fact_ids'],[])
 def test_poisoned_original_home_path_does_not_reach_http(self):
  import io
  from reportlab.pdfgen.canvas import Canvas
  from deepseek_transport import DeepSeekExtractor
  poison='Built a Python API at /mnt/private/source.pdf. Call 5551230987.'
  self.p['facts']['f3']={'group':'work','claim_text':poison};self.p['variants'].append({'id':'poison','group':'work','facts':['f3'],'text':poison,'skills':['Python']})
  out=io.BytesIO();canvas=Canvas(out)
  for i,v in enumerate(self.p['variants']):canvas.drawString(30,700-i*25,v['text'])
  canvas.save();Path(self.p['source']['path']).write_bytes(out.getvalue());self.p['source']['sha256']=digest(out.getvalue());self.sha=digest(canonical(self.p));calls=[]
  # Reapprove only the two prior excerpts for the new profile, not the poison.
  self.export['profile_sha256']=self.sha;self.export_sha=digest(canonical(self.export));self.export_path.write_bytes(canonical(self.export))
  key=self.root/'poison-test.key';key.write_text('synthetic-key');key.chmod(0o600)
  def http(method,url,headers,body,timeout):calls.append(body);return 200,canonical(self.response([['s1','unclear',[]]]))
  model=DeepSeekExtractor(key,self.root/'poison-budget.sqlite',self.root/'poison-cache',http=http)
  model.assess_evidence(self.p,self.job,self.extraction,approval_sha256=self.sha,fact_export_path=self.export_path,fact_export_sha256=self.export_sha,now='2026-10-16T10:00:00Z')
  self.assertEqual(len(calls),1);self.assertNotIn(poison.encode(),calls[0]);self.assertNotIn(b'5551230987',calls[0]);self.assertNotIn(b'/mnt/private',calls[0])
 def test_evidence_cap_refuses_before_paid_call(self):
  from deepseek_transport import DeepSeekExtractor
  key=self.root/'budget-test.key';key.write_text('synthetic-key');key.chmod(0o600);calls=[]
  model=DeepSeekExtractor(key,self.root/'tiny-budget.sqlite',self.root/'tiny-cache',http=lambda *args:calls.append(args),monthly_cap_microusd=100)
  with self.assertRaises(ValueError):model.assess_evidence(self.p,self.job,self.extraction,approval_sha256=self.sha,fact_export_path=self.export_path,fact_export_sha256=self.export_sha,now='2026-10-16T10:00:00Z')
  self.assertEqual(calls,[])
 def test_retrieval_recall_metric_is_not_mistaken_for_email(self):
  import io
  from reportlab.pdfgen.canvas import Canvas
  text='Built a Python retrieval benchmark with 91.4% Recall@5.'
  self.p['facts']['f3']={'group':'work','claim_text':text};self.p['variants'].append({'id':'retrieval','group':'work','facts':['f3'],'text':text,'skills':['Python']})
  out=io.BytesIO();canvas=Canvas(out)
  for i,v in enumerate(self.p['variants']):canvas.drawString(30,700-i*25,v['text'])
  canvas.save();Path(self.p['source']['path']).write_bytes(out.getvalue());self.p['source']['sha256']=digest(out.getvalue());self.sha=digest(canonical(self.p))
  self.export['profile_sha256']=self.sha;self.export['fact_hashes']['retrieval']=digest(text.encode());self.export_sha=digest(canonical(self.export))
  request,context=self.bundle();self.assertIn('retrieval',context['facts']);self.assertIn('Recall@5',request['messages'][1]['content'])
