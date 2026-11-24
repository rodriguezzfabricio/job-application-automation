"""Composed source/profile/decision/PDF/MIME flow with injected public HTTP."""
import copy,json,os,unittest
from pathlib import Path
from email import policy
from email.parser import BytesParser
from packet import canonical,digest
import test_personal as fixtures
try:
    from polling import poll_once
except ModuleNotFoundError:poll_once=None

NOW='2026-10-15T12:00:00Z'
class PollingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PersonalTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=Path(self.fixture.tmp.name);p=self.fixture.p
        p.update(locations=['NYC'],needs_sponsorship=False,recipient_email='recipient@example.invalid',earliest_start={'month':'2026-12','precision':'month','day':None})
        self.profile=self.root/'profile.json';self.profile.write_bytes(canonical(p));self.profile.chmod(0o600)
        self.approval=self.root/'approval.json';self.approval.write_bytes(canonical({'profile_sha256':digest(canonical(p)),'source_pdf_sha256':p['source']['sha256'],'authority':'synthetic fixture intake'}));self.approval.chmod(0o600)
        self.output=self.root/'private-output';self.calls=[]
        self.row={'title':'New Grad Software Engineer','jobUrl':'https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc','applyUrl':'https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc/application','location':'NYC','descriptionPlain':'Requirements\nPython experience required.\nPreferred Qualifications\nRust is preferred.','isListed':True,'employmentType':'FullTime'}
    def get(self,url,hosts):
        self.calls.append(url)
        return json.dumps({'jobs':[self.row]}).encode() if 'api.ashbyhq.com' in url else b'<html>Application</html>'
    def run_poll(self,**kw):
        self.assertIsNotNone(poll_once,'Live source/profile pipeline is missing')
        return poll_once('example',self.profile,self.approval,self.output,get=self.get,now=NOW,**kw)
    def test_real_shape_reaches_review_pdf_and_matching_email_without_sending(self):
        result=self.run_poll()
        self.assertEqual(result['source_jobs'],1);self.assertEqual(result['review'],1);self.assertEqual(result['external_messages_sent'],0)
        self.assertFalse(result['automatic_alerts_enabled'])
        packet=Path(result['packets'][0]['directory']);manifest=json.loads((packet/'manifest.json').read_text())
        self.assertFalse(manifest['application_ready']);self.assertEqual(manifest['decision']['status'],'review')
        self.assertEqual(manifest['decision']['profile_evidence']['availability_month'],'2026-12')
        msg=BytesParser(policy=policy.default).parsebytes((packet/'message.eml').read_bytes())
        self.assertEqual(str(msg['To']),'recipient@example.invalid');self.assertIn('REVIEW',str(msg['Subject']))
        self.assertEqual(next(msg.iter_attachments()).get_payload(decode=True),(packet/'resume.pdf').read_bytes())
        self.assertEqual(os.stat(self.output).st_mode&0o077,0)
    def test_repeat_preserves_first_seen_and_reuses_identical_packet(self):
        first=self.run_poll();directory=Path(first['packets'][0]['directory']);before=(directory/'resume.pdf').stat().st_mtime_ns
        second=self.run_poll()
        self.assertEqual(second['packets'][0]['status'],'cached_review');self.assertEqual((directory/'resume.pdf').stat().st_mtime_ns,before)
        self.assertEqual(first['packets'][0]['first_seen_at'],second['packets'][0]['first_seen_at'])
    def test_source_failure_is_recorded_without_closing_existing_jobs(self):
        self.run_poll()
        def fail(*unused):raise TimeoutError('sensitive internal details')
        result=poll_once('example',self.profile,self.approval,self.output,get=fail,now=NOW)
        self.assertEqual(result['status'],'source_failed');self.assertNotIn('sensitive',json.dumps(result))
        from state import Store
        store=Store(self.output/'state.sqlite');self.assertTrue(store.job('ashby:example','ashby:example:12345678-1234-1234-1234-123456789abc')['active'])
        self.assertFalse(store.current('ashby:example','ashby:example:12345678-1234-1234-1234-123456789abc','anything',NOW))
    def test_senior_unlisted_and_missing_description_never_make_review_pdf(self):
        original=copy.deepcopy(self.row)
        for change in [{'title':'Senior Software Engineer'},{'isListed':False},{'descriptionPlain':''}]:
            with self.subTest(change=change):
                self.row=dict(original,**change);result=self.run_poll()
                self.assertEqual(result['packets'],[])
    def test_public_output_directory_is_refused(self):
        self.output.mkdir();self.output.chmod(0o755)
        with self.assertRaises(ValueError):self.run_poll()
        self.assertEqual(self.calls,[])
    def test_packet_count_and_disk_caps_hold_work(self):
        result=self.run_poll(packet_limit=0);self.assertEqual(result['packets'],[])
        with self.assertRaises(ValueError):self.run_poll(packet_limit=-1)
        with self.assertRaises(ValueError):self.run_poll(artifact_cap_bytes=10)
    def test_edited_job_gets_new_draft_without_resetting_first_seen(self):
        first=self.run_poll();self.row['descriptionPlain']+='\nTypeScript is preferred.'
        second=self.run_poll();self.assertNotEqual(first['packets'][0]['directory'],second['packets'][0]['directory'])
        self.assertEqual(first['packets'][0]['first_seen_at'],second['packets'][0]['first_seen_at'])
        self.assertEqual(second['external_messages_sent'],0)
    def test_wrong_cached_attachment_is_held_or_safely_regenerated(self):
        first=self.run_poll();directory=Path(first['packets'][0]['directory']);(directory/'resume.pdf').write_bytes(b'wrong PDF')
        second=self.run_poll();self.assertNotEqual(second['packets'][0]['status'],'cached_review')
        self.assertEqual(second['external_messages_sent'],0)
        self.assertEqual(second['status'],'completed_with_holds');self.assertEqual(second['held'],1)

    def test_packet_cache_is_versioned_without_losing_previous_draft(self):
        self.assertIsNotNone(poll_once)
        import polling
        from unittest.mock import patch
        first=self.run_poll()
        with patch.object(polling,'PACKET_VERSION','test-next-version',create=True):second=self.run_poll()
        self.assertNotEqual(first['packets'][0]['directory'],second['packets'][0]['directory'])
        self.assertTrue(Path(first['packets'][0]['directory']).exists())

    def test_interrupted_preparation_does_not_publish_a_permanently_partial_packet(self):
        self.assertIsNotNone(poll_once)
        import polling
        from unittest.mock import patch
        write=polling._write
        def interrupt(root,path,data,cap):
            if path.name=='message.eml':raise OSError('simulated interruption')
            return write(root,path,data,cap)
        with patch.object(polling,'_write',side_effect=interrupt):self.assertEqual(self.run_poll()['packets'],[])
        result=self.run_poll();self.assertEqual(result['packets'][0]['status'],'prepared_review')

    def test_coordinated_cache_subject_or_decision_changes_are_held(self):
        first=self.run_poll();directory=Path(first['packets'][0]['directory']);original_manifest=(directory/'manifest.json').read_bytes();original_email=(directory/'message.eml').read_bytes()
        for target in ['subject','decision']:
            with self.subTest(target=target):
                manifest=json.loads(original_manifest);message=BytesParser(policy=policy.SMTP).parsebytes(original_email)
                if target=='subject':message.replace_header('Subject','A different job title')
                else:manifest['decision']['status']='eligible'
                raw=message.as_bytes();manifest['email_sha256']=digest(raw)
                (directory/'manifest.json').write_bytes(canonical(manifest));(directory/'message.eml').write_bytes(raw)
                self.assertEqual(self.run_poll()['packets'][0]['status'],'held_invalid_cache')
                (directory/'manifest.json').write_bytes(original_manifest);(directory/'message.eml').write_bytes(original_email)

    def test_symlinked_output_directory_is_refused(self):
        self.assertIsNotNone(poll_once)
        target=self.root/'other-private';target.mkdir(mode=0o700);self.output.symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):self.run_poll()
        self.assertEqual(self.calls,[])

    def test_present_but_unlisted_source_job_is_persisted_inactive(self):
        self.row['isListed']=False;self.run_poll()
        from state import Store
        row=Store(self.output/'state.sqlite').job('ashby:example','ashby:example:12345678-1234-1234-1234-123456789abc')
        self.assertFalse(row['active'])

    def test_cached_earlier_job_does_not_starve_new_draft_when_limit_is_one(self):
        first=self.run_poll(packet_limit=1)
        old=dict(self.row);new=dict(self.row,applyUrl=self.row['applyUrl'].replace('12345678','22345678'),jobUrl=self.row['jobUrl'].replace('12345678','22345678'))
        def get(url,hosts):return json.dumps({'jobs':[old,new]}).encode() if 'api.ashbyhq.com' in url else b'<html>Application</html>'
        second=poll_once('example',self.profile,self.approval,self.output,get=get,now=NOW,packet_limit=1)
        self.assertEqual(second['packets'][0]['status'],'prepared_review');self.assertNotEqual(first['packets'][0]['job_id'],second['packets'][0]['job_id'])

    def test_multiple_sources_preserve_separate_decision_records(self):
        self.run_poll();old=self.output/'sources/example/jobs-and-decisions.json';self.assertTrue(old.exists())
        row=dict(self.row,applyUrl=self.row['applyUrl'].replace('/example/','/other/'),jobUrl=self.row['jobUrl'].replace('/example/','/other/'))
        def get(url,hosts):return json.dumps({'jobs':[row]}).encode() if 'api.ashbyhq.com' in url else b'<html>Application</html>'
        poll_once('other',self.profile,self.approval,self.output,get=get,now=NOW)
        self.assertTrue(old.exists());self.assertTrue((self.output/'sources/other/jobs-and-decisions.json').exists())

    def test_failure_overwrites_persisted_success_report(self):
        self.run_poll()
        def fail(*unused):raise TimeoutError()
        result=poll_once('example',self.profile,self.approval,self.output,get=fail,now=NOW)
        for report in [self.output/'run-report.json',self.output/'sources/example/run-report.json']:
            self.assertEqual(json.loads(report.read_text())['status'],'source_failed')

    def test_malformed_source_does_not_commit_success_or_close_job(self):
        self.run_poll()
        from state import Store
        store=Store(self.output/'state.sqlite');before=store.job('ashby:example','ashby:example:12345678-1234-1234-1234-123456789abc')
        for change in [{'location':7},{'location':[]},{'isListed':'true'}]:
            original=copy.deepcopy(self.row);self.row.update(change)
            with self.subTest(change=change):self.assertEqual(self.run_poll()['status'],'source_failed')
            self.assertEqual(store.job('ashby:example',before['id']),before)
            self.row=original

    def test_uppercase_board_uses_same_canonical_identity(self):
        first=self.run_poll()
        second=poll_once('EXAMPLE',self.profile,self.approval,self.output,get=self.get,now=NOW)
        self.assertEqual(first['source_id'],second['source_id'])
        self.assertEqual(second['packets'][0]['status'],'cached_review')

    def test_cache_fabricated_send_count_or_verification_is_held(self):
        first=self.run_poll();directory=Path(first['packets'][0]['directory']);original=(directory/'manifest.json').read_bytes()
        for field,value in [('external_messages_sent',99),('verification',{'status':'verified'})]:
            manifest=json.loads(original);manifest[field]=value;(directory/'manifest.json').write_bytes(canonical(manifest))
            with self.subTest(field=field):self.assertEqual(self.run_poll()['packets'][0]['status'],'held_invalid_cache')
            (directory/'manifest.json').write_bytes(original)

    def test_special_file_in_cache_is_refused_without_opening(self):
        first=self.run_poll();path=Path(first['packets'][0]['directory'])/'resume.pdf';path.unlink();os.mkfifo(path)
        # Guard the read itself so a regression fails instead of blocking the test runner.
        from unittest.mock import patch
        original=Path.read_bytes
        def guarded(p):
            import stat
            if not stat.S_ISREG(p.lstat().st_mode):raise AssertionError('special file opened')
            return original(p)
        with patch.object(Path,'read_bytes',guarded):
            with self.assertRaises(ValueError):self.run_poll()

    def test_cache_refreshes_verification_without_regenerating_resume(self):
        first=self.run_poll();directory=Path(first['packets'][0]['directory']);before=(directory/'resume.pdf').read_bytes()
        def changed(url,hosts):
            return self.get(url,hosts) if 'api.ashbyhq.com' in url else b'<html>Updated application page</html>'
        second=poll_once('example',self.profile,self.approval,self.output,get=changed,now=NOW)
        manifest=json.loads((directory/'manifest.json').read_text())
        self.assertEqual(second['packets'][0]['status'],'cached_review')
        self.assertEqual(manifest['verification']['application_page_sha256'],digest(b'<html>Updated application page</html>'))
        self.assertEqual((directory/'resume.pdf').read_bytes(),before)

    def test_actual_poll_stages_bound_review_packet_in_durable_outbox(self):
        from delivery import DeliveryQueue
        first=self.run_poll();self.assertEqual(first['packets'][0]['outbox_status'],'staged_review')
        queue=DeliveryQueue(self.output/'delivery.sqlite');self.assertEqual(queue.status(),{'needs_review':1})
        second=self.run_poll();self.assertEqual(second['packets'][0]['outbox_status'],'duplicate');self.assertEqual(queue.status(),{'needs_review':1})
        self.assertEqual(second['external_messages_sent'],0)
