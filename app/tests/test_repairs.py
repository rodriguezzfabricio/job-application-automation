"""Regressions from the independent review; no live I/O."""
import copy,io,unittest
from email import policy as ep
from email.parser import BytesParser
from pypdf import PdfReader,PdfWriter
from pypdf.generic import DecodedStreamObject,NameObject
from policy import fit,location_matches,novelty
from packet import build_packet,check_packet,digest,canonical
from test_packet import PROFILE,JOB
NOW='2026-10-15T12:00:00Z'
class RepairPolicyTests(unittest.TestCase):
 def setUp(self):
  self.p=copy.deepcopy(PROFILE);self.p.update(needs_sponsorship=False,locations=['California','DMV','NYC'],graduation_month='2026-12',available_from_month='2026-12')
  self.j=dict(JOB,government_contracting=False,required_skills=['Python'],min_years=0,graduation_years=[])
 def test_start_before_completion_rejects(self):self.assertEqual(fit(self.p,dict(self.j,start_date='2026-11-30'))['status'],'reject')
 def test_start_during_unknown_day_reviews(self):
  for day in ['2026-12-01','2026-12-31']:self.assertEqual(fit(self.p,dict(self.j,start_date=day))['status'],'review')
 def test_start_after_month_accepts(self):self.assertEqual(fit(self.p,dict(self.j,start_date='2027-01-01'))['status'],'eligible')
 def test_required_gap_reviews(self):self.assertEqual(fit(self.p,dict(self.j,required_skills=['Python','Rust']))['status'],'review')
 def test_alternative_satisfied(self):self.assertEqual(fit(self.p,dict(self.j,required_skill_groups=[['Python','Rust']]))['status'],'eligible')
 def test_preferred_gap_is_allowed(self):self.assertEqual(fit(self.p,dict(self.j,preferred_skills=['Rust']))['status'],'eligible')
 def test_contractor_label_cannot_override(self):self.assertEqual(fit(self.p,dict(self.j,employer='Lockheed Martin',description='Department of Defense contractor. Public Trust required.'))['status'],'reject')
 def test_cohort_month_boundaries(self):self.assertEqual(fit(self.p,dict(self.j,graduation_window={'min':'2026-01','max':'2026-06'}))['status'],'reject')
 def test_requested_cities(self):
  for loc,reg in [('Palo Alto, California, United States','California'),('Sacramento, CA, United States','California'),('Bethesda, MD, United States','DMV'),('Bellevue, WA, United States','Seattle')]:self.assertTrue(location_matches(loc,[reg]),loc)
 def test_future_nonbootstrap_not_fresh(self):self.assertFalse(novelty([],dict(self.j,published_at='2027-01-01T00:00:00Z'),False,NOW))
class RepairPacketTests(unittest.TestCase):
 def setUp(self):self.packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid',NOW)
 def test_extra_recipients_rejected(self):
  for h in ['Cc','Bcc','Resent-To']:
   msg=BytesParser(policy=ep.SMTP).parsebytes(self.packet['email']);msg[h]='unexpected@example.invalid';self.assertFalse(check_packet(dict(self.packet,email=msg.as_bytes()),PROFILE,JOB))
 def test_prefix_url_is_not_exact(self):
  msg=BytesParser(policy=ep.SMTP).parsebytes(self.packet['email']);msg.get_body(preferencelist=('plain',)).set_content('Apply '+JOB['url']+'-wrong\n');self.assertFalse(check_packet(dict(self.packet,email=msg.as_bytes()),PROFILE,JOB))
 def test_white_cover_rejected_with_rehash(self):self._mutate_pdf(True,False)
 def test_active_pdf_rejected_with_rehash(self):self._mutate_pdf(False,True)
 def _mutate_pdf(self,cover,js):
  writer=PdfWriter();writer.clone_document_from_reader(PdfReader(io.BytesIO(self.packet['pdf'])))
  if cover:
   page=writer.pages[0];c=DecodedStreamObject();c.set_data(page.get_contents().get_data()+b'\nq 1 1 1 rg 0 0 612 792 re f Q\n');page[NameObject('/Contents')]=writer._add_object(c)
  if js:writer.add_js("app.alert('test');")
  out=io.BytesIO();writer.write(out);pdf=out.getvalue();m=dict(self.packet['manifest'],pdf_sha256=digest(pdf));msg=BytesParser(policy=ep.SMTP).parsebytes(self.packet['email']);att=list(msg.iter_attachments())[0];att.set_content(pdf,maintype='application',subtype='pdf',filename='resume.pdf')
  msg.replace_header('Message-ID','<'+digest(canonical(m))[:32]+'@example.invalid>')
  self.assertFalse(check_packet({'manifest':m,'pdf':pdf,'email':msg.as_bytes()},PROFILE,JOB))

import test_runner
from unittest.mock import patch
from runner import Runner
from state import Store
class RecoveryRepairTests(unittest.TestCase):
 setUp=test_runner.RunnerTests.setUp
 tearDown=test_runner.RunnerTests.tearDown
 def test_same_url_different_profile_and_source_once(self):
  first=self.runner.process(self.profile,self.job,NOW)
  alias=dict(self.job,id='other-source-id',source_id='alias')
  from packet import job_fingerprint
  self.store.snapshot('alias',[alias['id']],NOW,hashes={alias['id']:job_fingerprint(alias)})
  second=self.runner.process(self.profile,alias,NOW)
  third=self.runner.process(dict(self.profile,version='sample-v2'),self.job,NOW)
  self.assertEqual(first['status'],'capture_accepted');self.assertEqual(second['status'],'duplicate');self.assertEqual(third['status'],'duplicate')
  self.assertEqual(len(list(self.out.glob('*/message.eml'))),1)
 def test_prepared_orphan_reuses_frozen_bytes(self):
  with patch.object(self.store,'enqueue',side_effect=RuntimeError('crash after durable prepare')):
   with self.assertRaises(RuntimeError):self.runner.process(self.profile,self.job,NOW)
  prepared=next(self.out.glob('*/prepared.eml')).read_bytes()
  report=Runner('archive',self.io,Store(self.store.path)).process(self.profile,dict(self.job,source_observed_at='2026-10-15T12:00:01Z'),'2026-10-15T12:00:01Z')
  self.assertEqual(report['status'],'capture_accepted');self.assertEqual(next(self.out.glob('*/message.eml')).read_bytes(),prepared)
 def test_startup_recovers_stale_sending(self):
  original=self.store.claim
  def crash(*args,**kwargs):
   original(*args,**kwargs);raise RuntimeError('crash after claim')
  with patch.object(self.store,'claim',side_effect=crash):
   with self.assertRaises(RuntimeError):self.runner.process(self.profile,self.job,NOW)
  result=Runner('archive',self.io,Store(self.store.path)).process(self.profile,dict(self.job,source_observed_at='2026-10-15T12:10:00Z'),'2026-10-15T12:10:00Z')
  self.assertEqual(result.get('existing_state'),'delivery_unknown');self.assertEqual(len(list(self.out.glob('*/message.eml'))),0)
 def test_persisted_inactive_state_overrides_caller(self):
  self.store.snapshot('fixture',[self.job['id']],NOW);self.store.snapshot('fixture',[],'2026-10-15T12:01:00Z');self.store.snapshot('fixture',[],'2026-10-15T12:02:00Z')
  result=self.runner.process(self.profile,dict(self.job,source_kind='fixture',source_observed_at='2026-10-15T12:02:00Z'),'2026-10-15T12:02:00Z');self.assertEqual(result['status'],'review')

class ConcurrencyBoundaryTests(unittest.TestCase):
 setUp=test_runner.RunnerTests.setUp
 tearDown=test_runner.RunnerTests.tearDown
 def test_live_run_lock_prevents_recovery_steal(self):
  self.store.enqueue('p','job',b'frozen');self.store.claim('p','job',NOW)
  with self.store.runner_lock() as acquired:
   self.assertTrue(acquired)
   other=Store(self.store.path);self.assertEqual(other.recover('2026-10-15T12:10:00Z'),0)
   outcome=Runner('archive',self.io,other).process(self.profile,self.job,NOW);self.assertEqual(outcome['status'],'busy')
  self.assertEqual(self.store.intent('p','job')['status'],'sending')
  self.assertEqual(Store(self.store.path).recover('2026-10-15T12:10:00Z'),1)
 def test_partial_artifact_routes_review(self):
  destination=self.io.packet_directory(self.profile['version'],self.job['id']);destination.mkdir(parents=True)
  result=self.runner.process(self.profile,self.job,NOW);self.assertEqual(result['status'],'review');self.assertEqual(self.store.status(),{})
 def test_tracking_query_does_not_create_new_identity(self):
  from policy import merge_key
  a=merge_key({'url':'https://example.invalid/jobs/123?utm_source=one&req=123'})
  b=merge_key({'url':'https://example.invalid/jobs/123?req=123&utm_source=two#top'})
  self.assertEqual(a,b)
 def test_reasons_retain_each_constraint_state(self):
  from policy import fit
  result=fit(self.profile,dict(self.job,title='Senior Software Engineer',government_contracting=None))
  self.assertTrue(result['reject_reasons']);self.assertTrue(result['review_reasons'])

class QualificationBoundaryTests(unittest.TestCase):
 setUp=RepairPolicyTests.setUp
 def test_explicit_intern_type_rejected(self):self.assertEqual(fit(self.p,dict(self.j,employment_type='Intern'))['status'],'reject')
 def test_explicit_closed_rejected(self):self.assertEqual(fit(self.p,dict(self.j,active=False))['status'],'reject')
 def test_unresolved_required_degree_reviews(self):self.assertEqual(fit(self.p,dict(self.j,degree_requirement='PhD in Computer Science required',description='PhD in Computer Science required.'))['status'],'review')
 def test_brooklyn_is_in_nyc(self):self.assertTrue(location_matches('Brooklyn, NY, United States',['NYC']))
 def test_negated_contract_work_is_not_positive_evidence(self):
  j=dict(self.j,description='We hold no government contracts, perform no federal contract work, and have no defense customers.')
  self.assertEqual(fit(self.p,j)['status'],'eligible')
 def test_real_contract_work_after_negative_still_rejects(self):
  self.assertEqual(fit(self.p,dict(self.j,description='We hold no government contracts at parent level. This role delivers federal contract work.'))['status'],'reject')

class ExternalRecipientAnchorTests(unittest.TestCase):
 def test_coordinated_manifest_and_mime_recipient_change_refused(self):
  packet=build_packet(PROFILE,JOB,['api-tests'],'demo@example.invalid',NOW)
  changed=dict(packet['manifest'],recipient='other@example.invalid');msg=BytesParser(policy=ep.SMTP).parsebytes(packet['email']);msg.replace_header('To','other@example.invalid')
  self.assertFalse(check_packet(dict(packet,manifest=changed,email=msg.as_bytes()),PROFILE,JOB))

class ForeignRoutingTests(unittest.TestCase):
 setUp=RepairPolicyTests.setUp
 def test_explicit_foreign_country_rejects(self):
  for location in ['Toronto, Canada','Toronto, ON, CA']:
   self.assertEqual(fit(self.p,dict(self.j,location=location))['status'],'reject')
 def test_mixed_country_is_review_not_implicit_selection(self):
  self.assertEqual(fit(self.p,dict(self.j,location='New York, USA / Toronto, Canada'))['status'],'review')

class PersistedNoveltyTests(unittest.TestCase):
 setUp=test_runner.RunnerTests.setUp
 tearDown=test_runner.RunnerTests.tearDown
 def test_caller_cannot_disable_initial_baseline(self):
  from packet import job_fingerprint
  job=dict(self.job,bootstrap=False,published_at='2026-09-01T00:00:00Z')
  self.store.snapshot('archive-fixture',[job['id']],NOW,hashes={job['id']:job_fingerprint(job)})
  self.assertEqual(self.runner.process(self.profile,job,NOW)['status'],'baseline_only');self.assertEqual(self.store.status(),{})
 def test_later_new_old_posting_can_be_newly_detected(self):
  from packet import job_fingerprint
  job=dict(self.job,id='discovered-later',url='https://example.invalid/jobs/discovered-later',bootstrap=True,published_at='2026-09-01T00:00:00Z')
  self.store.snapshot('archive-fixture',[job['id']],NOW,hashes={job['id']:job_fingerprint(job)})
  self.assertEqual(self.runner.process(self.profile,job,NOW)['status'],'capture_accepted')

class AliasRegressionTests(unittest.TestCase):
 setUp=RepairPolicyTests.setUp
 def test_canadian_province_country_cannot_be_california_city(self):self.assertFalse(location_matches('Toronto, ON, CA',['California','NYC']))
 def test_explicit_internship_synonym_rejected(self):self.assertEqual(fit(self.p,dict(self.j,employment_type='Internship'))['status'],'reject')

class TechnologyIdentityTests(unittest.TestCase):
 setUp=RepairPolicyTests.setUp
 def test_c_sharp_does_not_satisfy_cpp_or_c(self):
  p=dict(self.p,skills=['C#'])
  for required in ['C++','C']:
   result=fit(p,dict(self.j,required_skills=[required]));self.assertEqual(result['status'],'review');self.assertTrue(result['skill_gaps'])
 def test_punctuation_preserved_in_skill_alternatives(self):
  p=dict(self.p,skills=['C#'])
  self.assertEqual(fit(p,dict(self.j,required_skills=[],required_skill_groups=[['C++','Rust']]))['status'],'review')
