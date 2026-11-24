import datetime as dt
import json
import unittest

from job_runtime import account_flow, accounts, portal_bridge
from job_runtime.policy import UTC
from test_runtime import RuntimeTests


class PortalBridgeTests(RuntimeTests):
    def setUp(self):
        super().setUp()
        self.at=dt.datetime.now(UTC)
        self.s.con.execute("UPDATE jr_leases SET expires=? WHERE name='worker'",(self.at.timestamp()+600,))
        self.origin='https://acme.wd5.myworkdayjobs.com'
        self.page=self.origin+'/en-US/one/register'
        key=self.job(link=self.origin+'/en-US/one/job/12000')
        (self.path.parent/accounts.FILE).write_text(json.dumps({'default':{'email':'fixture@example.invalid','password':'synthetic-only'}}))
        self.q=dict(owner=self.owner,token=self.token,run_id=self.run['run_id'],job_key=key,
                    page_origin=self.origin,page_sha256=account_flow.digest(self.page),mode='register')
        self.folder=self.path.parent/'portal'

    def arm(self):
        result=account_flow.begin(self.s,self.q)
        self.assertNotIn('password',result)
        grant=account_flow.value(self.s,account_flow.KEY)
        proc=portal_bridge.start(self.s,grant,self.folder)
        def close():
            if proc.poll() is None: proc.terminate()
            proc.wait(timeout=5)
        self.addCleanup(close)
        return proc

    def test_only_current_grant_on_exact_original_tenant_is_handed_to_native_client_once(self):
        self.assertEqual(portal_bridge.request(self.page,self.folder),{'error':'no_active_worker'})
        process=self.arm()
        self.assertEqual((self.folder/'active').stat().st_mode&0o777,0o600)
        self.assertEqual(self.folder.stat().st_mode&0o777,0o700)
        self.assertIn('error',portal_bridge.request(self.page.replace('/one/','/two/'),self.folder))
        result=portal_bridge.request(self.page,self.folder)
        self.assertEqual(result,{'email':'fixture@example.invalid','password':'synthetic-only','mode':'register'})
        process.wait(timeout=5)
        self.assertIn('error',portal_bridge.request(self.page,self.folder))
        self.assertFalse(account_flow.value(self.s,account_flow.KEY))

    def test_released_lease_cannot_return_credentials(self):
        self.arm()
        self.s.release(self.owner,self.token)
        self.assertIn('error',portal_bridge.request(self.page,self.folder))
        self.assertFalse(account_flow.value(self.s,'portal_last_autofill'))


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(PortalBridgeTests(name) for name in PortalBridgeTests.__dict__ if name.startswith('test_'))
