"""Unknown legacy sends block retries without inventing an application receipt."""
import json
import sqlite3
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_runtime as fixtures
from job_runtime.store import Store, Denied


class LegacyOutcomeHoldTests(unittest.TestCase):
    job = fixtures.RuntimeTests.job
    prepare = fixtures.RuntimeTests.prepare
    tearDown = fixtures.RuntimeTests.tearDown

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        # Exercise a copied tracker, never the production database.
        copied = self.path.with_name('copied.sqlite')
        target = sqlite3.connect(copied)
        self.s.con.backup(target)
        target.close()
        self.s.con.close()
        self.path = copied
        self.s = Store(copied, lambda: self.at)

    def hold(self, item_key=None, payload=None, state='pending'):
        self.s.con.execute('INSERT INTO jr_backlog(provider,item_key,payload,first_seen,state) VALUES(?,?,?,?,?)',
                           ('legacy_unknown_submission',item_key or self.key,json.dumps(payload or {}),self.s.now(),state))

    def test_pending_job_key_prevents_prepare_without_receipt(self):
        self.hold()
        with self.assertRaisesRegex(Denied,'reconcile legacy outcome'):
            self.prepare()
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM jr_attempts').fetchone()[0],0)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM applications').fetchone()[0],0)

    def test_canonical_link_and_payload_key_block_retry(self):
        self.hold(self.s.job(self.key)['link']+'?utm_source=legacy')
        with self.assertRaisesRegex(Denied,'reconcile legacy outcome'):
            self.prepare()
        self.s.con.execute('DELETE FROM jr_backlog')
        self.hold('legacy-record',{'job_key':self.key},state='needs_review')
        with self.assertRaisesRegex(Denied,'reconcile legacy outcome'):
            self.prepare()

    def test_confirmed_application_remains_duplicate(self):
        j=self.s.job(self.key)
        self.s.con.execute('INSERT INTO applications(app_key,job_key,company,company_key,title,link,applied_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           ('confirmed',self.key,j['company'],j['company_key'],j['title'],j['link'],self.s.now(),self.s.now()))
        self.hold()
        self.assertTrue(self.s.already_applied(j))
        with self.assertRaisesRegex(Denied,'already applied'):
            self.prepare()

    def test_resolving_hold_allows_normal_prepare(self):
        self.hold()
        self.s.con.execute("UPDATE jr_backlog SET state='resolved'")
        attempt=self.prepare()
        self.assertEqual(self.s.con.execute('SELECT state FROM jr_attempts WHERE id=?',(attempt,)).fetchone()[0],'prepared')

    def budget(self, key, policy=None, channel='one_click'):
        return self.s._budget(self.s.job(key),policy or self.policy,self.at.date().isoformat(),channel,self.run['run_id'])

    def test_hold_reserves_daily_slot_and_company_slot(self):
        company=self.s.job(self.key)['company_key']
        self.hold(payload={'day':self.at.date().isoformat(),'channel':'one_click','company_key':company})
        other=self.job(company='Other',suffix='daily')
        with self.assertRaisesRegex(Denied,'daily channel limit'):
            self.budget(other,dict(self.policy,one_click_per_day=1))
        second=self.job(suffix='held2',title='Second Engineer')
        self.hold(second,{'day':self.at.date().isoformat(),'channel':'one_click','company_key':company})
        with self.assertRaisesRegex(Denied,'company daily limit'):
            self.budget(self.job(suffix='candidate',title='Third Engineer'))

    def test_other_day_and_resolved_holds_do_not_reserve(self):
        self.hold(payload={'day':'2026-11-11','channel':'one_click'})
        self.hold('resolved-record',{'day':self.at.date().isoformat(),'channel':'one_click'},state='resolved')
        self.budget(self.job(company='Other',suffix='new'),dict(self.policy,one_click_per_day=1))

    def test_audit_counts_unique_unknown_without_inventing_receipts(self):
        attempt=self.prepare()
        self.s.con.execute("UPDATE jr_attempts SET state='submit_started' WHERE id=?",(attempt,))
        self.hold()
        self.hold(self.s.job(self.key)['link']+'?utm_source=legacy')
        result=self.s.audit(self.run['run_id'])
        self.assertEqual(result['unknown_submissions'],1)
        self.assertEqual(result['legacy_unknown_submissions'],1)
        self.assertEqual(result['confirmed_submissions'],0)
        self.assertFalse(result['success'])

    def test_existing_reservation_not_counted_twice_and_unknown_channel_conservative(self):
        self.prepare()
        self.hold(payload={'channel':'one_click','day':self.at.date().isoformat()})
        other=self.job(company='Other',suffix='new')
        self.budget(other,dict(self.policy,max_fills_per_day=2),channel='form')
        self.s.con.execute('DELETE FROM jr_attempts')
        self.s.con.execute("UPDATE jr_backlog SET payload='{}'")
        for channel,cap in [('form','max_fills_per_day'),('one_click','one_click_per_day')]:
            with self.assertRaisesRegex(Denied,'daily channel limit'):
                self.budget(other,dict(self.policy,**{cap:1}),channel=channel)


if __name__=='__main__':unittest.main()
