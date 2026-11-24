import datetime as dt
import json
import unittest

from job_runtime import canary, pace
from job_runtime.adapters import remember_employer_date
from job_runtime.planner import plan
from job_runtime.store import Denied
from test_runtime import RuntimeTests


class SameDayWorker(RuntimeTests):
    def setUp(self):
        super().setUp()
        folder=self.s.path.parent/'codex-runtime'
        folder.mkdir(exist_ok=True)
        (folder/'rollout-readiness.json').write_text(json.dumps(dict(form_parity=True,exact_history=True,registration_replay=True)))
        self.s.con.execute("UPDATE jr_meta SET value='codex' WHERE key='production_owner'")

    def original(self, value):
        job = self.s.job(self.key)
        remember_employer_date(self.s, self.key, dict(posted_at=value, posted_at_url=job['link'],
                              posted_at_source='employer.JobPosting.datePosted', link=job['link']),verified_original=True)

    def test_verified_calendar_date_before_midnight_and_missing_or_older_proof(self):
        self.s.con.execute("UPDATE jobs SET your_call='fill now' WHERE job_key=?", (self.key,))
        with self.assertRaisesRegex(Denied, 'employer posting date'):
            self.prepare()
        self.original('2026-11-12')
        self.assertIn(self.key, plan(self.s, self.policy, 'FULL')['to_fill'])
        self.original('2026-11-11T23:59:00-05:00')
        with self.assertRaisesRegex(Denied, 'Eastern day'):
            self.prepare()

    def test_ready_batch_can_run_ahead_of_pace_but_empty_queue_cannot(self):
        now = dt.datetime(2026, 11, 24, 9, 4, tzinfo=pace.ET)
        m = {'week': {'used': 21, 'resets_at': dt.datetime(2026,11,30,20,12,tzinfo=pace.ET).timestamp()},
             'day_start': {'used': 4, 'at': '2026-11-24T02:07:00-05:00'}}
        self.assertEqual(pace.decide(m, now, ready=1)['mode'], 'lean')
        m['week']['used'] = 4
        self.assertEqual(pace.decide(m, now, ready=0)['mode'], 'skip')
        m['week']['used'] = 97
        self.assertEqual(pace.decide(m, now, ready=1)['mode'], 'skip')

    def test_expired_unattempted_pilot_preserves_draft_and_pending_registration(self):
        self.original('2026-11-11')
        other=self.job(company='Other',suffix='R200')
        current={'phase':'active','jobs':{self.key:{'run_id':self.run['run_id']},other:{'run_id':self.run['run_id']}}}
        canary.save(self.s,current)
        self.s.con.execute("INSERT INTO jr_meta VALUES(?,?)",('account-access:fixture',
            json.dumps({'state':'registration_pending','job_key':other})))
        released=canary.release_expired(self.s,self.owner,self.token)
        self.assertEqual(released,[self.key])
        self.assertEqual(self.s.job(self.key)['status'],'Matched')
        self.assertIn(other,canary.state(self.s)['jobs'])


    def test_shortlist_merges_requisitions_and_retains_ambiguous_dates(self):
        from job_runtime import shortlist, jr
        base='https://example.wd5.myworkdayjobs.com'
        path='/job/Portland/Data-Engineer_JR11111'
        row=dict(company='Example',title='Data Engineer',location='Portland, OR',link=base+'/en-US/Careers'+path,
            posted_at='2026-11-12',posted_at_source='employer.Workday.startDate',
            posted_at_url=base+'/wday/cxs/example/Careers'+path,description_complete=True,
            description='Required qualifications\nExperience with Python.\nPreferred qualifications\nSQL.')
        alias=dict(row,link=row['link'].replace('/en-US/','/'))
        other=dict(row,link=row['link'].replace('JR11111','JR22222'))
        mirror=dict(row,link='https://aplyr.ai/jobs/333',posted_at_source='employer.JobPosting.datePosted',
                    posted_at_url='https://aplyr.ai/jobs/333')
        snapshot={'ran_at':self.s.now(),'postings':[row,alias,other,mirror]}
        shortlist.ingest(self.s,snapshot)
        shortlist.ingest(self.s,snapshot)
        stages=dict(self.s.con.execute('SELECT stage,count(*) FROM jr_shortlist GROUP BY stage'))
        self.assertEqual(stages,{'review':1,'verify_date':2})
        self.assertEqual(jr.readiness(self.s),(0,1))

    def test_platform_and_original_share_duplicate_receipt_only_after_verified_link(self):
        from job_runtime.opening_history import verify_publication
        job=self.job(company='Example',suffix='platform',title='Data Engineer I',link='https://www.linkedin.com/jobs/view/123456')
        base='https://jobs.lever.co/example/00000000-0000-4000-8000-00086a7c1a15'
        from job_runtime.adapters import import_record
        orig=import_record(self.s,'official','lever-1',dict(company='Example',title='Data Engineer I',link=base),self.owner,self.token)['job_key']
        link=self.s.job(job)['link']
        req=base.rsplit('/',1)[1]
        evidence=dict(source_posting=link,employer_url=base,employer='Example',employer_requisition=req,
            job_identity_quote='Requisition '+req,employer_posted_at='2026-11-12',date_quote='November 12',
            observed_at=self.s.now(),refs=[link,base])
        q=dict(owner=self.owner,token=self.token,job_key=job,evidence=evidence)
        with self.assertRaisesRegex(Denied,'platform listing'):
            verify_publication(self.s,q)
        evidence['employer_link_on_listing']=base
        verify_publication(self.s,q)
        self.s.con.execute("UPDATE jobs SET status='Applied' WHERE job_key=?",(orig,))
        self.assertTrue(self.s.already_applied(self.s.job(job)))

    def test_numbered_workday_site_keeps_legacy_dates_and_counts_one_requisition(self):
        from job_runtime import shortlist
        from job_runtime.store import Store
        base = 'https://example.wd1.myworkdayjobs.com'
        suffix = '/job/City/Software-Engineer-I----13851_r012696857-1'
        link = base+'/en-US/Careers_1'+suffix
        alias = base+'/careers_1'+suffix
        self.assertEqual(self.s.exact_identity(link),self.s.exact_identity(alias))
        self.assertNotEqual(self.s.exact_identity(link),self.s.exact_identity(alias.replace('careers_1','careers_2')))
        self.assertNotEqual(self.s.exact_identity(link),self.s.exact_identity(alias.replace('r012696857','R202684593')))
        legacy = json.dumps(['url',link],separators=(',',':'))
        proof = dict(value='2026-11-11',field='employer.Workday.startDate',url=base+'/wday/cxs/example/Careers_1'+suffix)
        self.s.con.execute('INSERT INTO jr_publications VALUES(?,?)',(legacy,json.dumps(proof)))
        self.s.con.execute("DELETE FROM jr_meta WHERE key='workday-site-date-index-v1'")
        other = Store(self.s.path, self.s.clock)
        try:
            row = dict(company='Example',title='Software Engineer I',link=alias,location='Portland, OR',
                       posted_at='2026-11-12',posted_at_source=proof['field'],posted_at_url=proof['url'])
            shortlist.ingest(other,dict(ran_at=other.now(),postings=[row,dict(row,link=link)]))
            report = shortlist.report_day(other)
            openings = [r for r in report['openings'] if r['company']=='Example']
            self.assertEqual([(r['publication'],r['stage']) for r in openings],[('2026-11-11','older')])
        finally:
            other.con.close()

    def test_empty_preview_and_new_dashboard_skip_have_no_reservation(self):
        from job_runtime import jr
        from job_runtime.quota import Quota
        from unittest.mock import patch
        self.original('2026-11-12')
        self.s.release(self.owner,self.token)
        when=self.at.timestamp()
        meter={'sampled_at':when,'account':'fixture','week':dict(used=10,resets_at=when+6*86400,window_min=10080)}
        ledger=Quota(self.path.parent,meter=lambda:meter,clock=lambda:when)
        with patch.object(jr,'worker_quota',return_value=ledger),patch.object(jr,'read_meters',return_value=meter):
            preview,_,reservation=jr.start_quota(self.s,'preview',reserve=False)
            self.assertNotEqual(preview['mode'],'skip')
            self.assertIsNone(reservation)
            self.packet['commands']['Jobs'].append(['skip',self.key])
            actual=jr.fresh_readiness(self.s,'preview',self.packet)
            self.assertEqual(actual,(0,0))
            skipped,_,reservation=jr.start_quota(self.s,'preview',queue=actual)
            self.assertEqual(skipped['mode'],'skip')
            self.assertIsNone(reservation)
            with ledger._transaction() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM reservations').fetchone()[0],0)
        self.assertNotEqual(self.s.job(self.key)['your_call'],'skip')

    def test_local_rules_leave_unrecognized_mandatory_conditions_for_review(self):
        from job_runtime.local_requirements import extract
        j=dict(title='Data Engineer',location='Portland, OR')
        text='Required qualifications\nExperience with Python.\nPreferred qualifications\nSQL.'
        self.assertIsNotNone(extract(j,text))
        self.assertIsNone(extract(j,text.replace('Python.','Python and a mandatory license.')))
        self.assertIsNone(extract(dict(j,location='2 Locations'),text))
        self.assertIsNone(extract(j,'Must have AWS experience.\n'+text))


    def test_paused_qualified_opening_stays_in_daily_denominator(self):
        from job_runtime.shortlist import report_day
        self.original('2026-11-12')
        self.s.con.execute('INSERT INTO jr_cache(key,value) VALUES(?,?)',('review:'+self.key,json.dumps(self.review)))
        job = self.s.job(self.key)
        self.s.con.execute('INSERT INTO jr_shortlist VALUES(?,?,?,?,?,?,?,?,?,?)',('2026-11-12','old-url',None,
                           job['company'],job['title'],job['link'],None,'verify_date','older feed needs a date',self.s.now()))
        self.assertEqual(report_day(self.s)['qualified'],1)
        from unittest.mock import patch
        with patch.object(self.s,'policy_snapshot',return_value=dict(self.policy,paused='yes')):
            report = report_day(self.s)
        self.assertEqual(report['qualified'],1)
        self.assertEqual(report['openings'][0]['reason'],'paused')

    def test_missing_rollout_readiness_cannot_fund_a_candidate(self):
        from job_runtime import jr
        self.original('2026-11-12')
        (self.s.path.parent/'codex-runtime/rollout-readiness.json').unlink()
        self.assertEqual(jr.readiness(self.s),(0,0))

    def test_unrelated_retry_tag_cannot_fund_an_unanswered_form(self):
        from job_runtime import jr
        from job_runtime.form_step import park_question, technical_hold
        self.original('2026-11-12')
        self.assertTrue(technical_hold(['Selected résumé readback missing'],'attachment_evidence'))
        self.assertFalse(technical_hold(['Selected résumé readback missing','What is your GPA?'],'attachment_evidence'))
        q=dict(owner=self.owner,token=self.token,run_id=self.run['run_id'],job_key=self.key,
               question='Unresolved custom selectors include education, GPA and employment history.',
               retry_on='attachment_evidence',refs=['fixture:required-fields'])
        park_question(self.s,q)
        record=self.s.con.execute("SELECT payload FROM jr_backlog WHERE provider='application_handoff' AND item_key=?",(self.key,)).fetchone()
        hold=json.loads(record[0])
        self.assertNotIn('retry_on',hold)
        hold.update(retry_on='attachment_evidence',blocked_run='older-run')
        self.s.con.execute("UPDATE jr_backlog SET payload=? WHERE provider='application_handoff' AND item_key=?",(json.dumps(hold),self.key))
        self.assertEqual(jr.readiness(self.s),(0,0))


    def test_older_employer_date_survives_rediscovery_on_a_later_day(self):
        from job_runtime import shortlist
        link='https://jobs.lever.co/example/00000000-0000-4000-8000-00086a7c1a15'
        row=dict(company='Example',title='Data Engineer',location='Portland, OR',link=link,posted_at='2026-11-11',
            posted_at_source='employer.Lever.createdAt',posted_at_url='https://api.lever.co/v0/postings/example?mode=json')
        shortlist.ingest(self.s,dict(ran_at=self.s.now(),postings=[row]))
        self.at+=dt.timedelta(days=1)
        shortlist.ingest(self.s,dict(ran_at=self.s.now(),postings=[dict(row,posted_at='2026-11-13')]))
        observed=self.s.con.execute("SELECT publication,stage FROM jr_shortlist WHERE day='2026-11-13'").fetchone()
        self.assertEqual(tuple(observed),('2026-11-11','older'))


    def test_literal_employer_today_keeps_its_observation_day(self):
        self.original('Posted Today')
        self.assertEqual(self.s.employer_date(self.key),'2026-11-12')
        self.at+=dt.timedelta(days=1)
        self.assertEqual(self.s.employer_date(self.key),'2026-11-12')


    def test_recoverable_expired_lease_does_not_block_fresh_preview(self):
        from job_runtime import jr
        self.original('2026-11-12')
        self.s.con.execute("UPDATE jr_leases SET expires=0 WHERE name='worker'")
        self.assertEqual(jr.fresh_readiness(self.s,'next-worker',self.packet),(1,0))
        self.assertEqual(self.s.con.execute("SELECT owner FROM jr_leases WHERE name='worker'").fetchone()[0],self.owner)


    def test_conflicting_discovery_does_not_abort_the_remaining_batch(self):
        from job_runtime.operations import import_feed_batch
        first=dict(company='Source',title='Software Engineer',link='https://example.test/jobs/A',source='agency')
        next_job=dict(first,link='https://example.test/jobs/B')
        import_feed_batch(self.s,dict(postings=[first]),self.owner,self.token,'fixture:public')
        self.s.con.execute("UPDATE jr_imports SET job_key=? WHERE provider='agency' AND record_id=?",(self.key,first['link']))
        result=import_feed_batch(self.s,dict(postings=[first,next_job]),self.owner,self.token,'fixture:public')
        self.assertEqual((result['committed'],len(result['held'])),(1,1))
        self.assertEqual(result['held'][0]['link'],first['link'])
        self.assertTrue(self.s.con.execute("SELECT 1 FROM jr_backlog WHERE provider='public_identity' AND state='pending'").fetchone())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SameDayWorker(name) for name in SameDayWorker.__dict__ if name.startswith('test_'))
