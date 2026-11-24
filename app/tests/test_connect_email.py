import unittest,json,tempfile
from pathlib import Path
from types import SimpleNamespace
from packet import canonical
import test_service as fixtures
try:
    from connect_email import connect
except ImportError:connect=None

class ConnectEmailTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.ServiceTests();f.setUp();self.addCleanup(f.doCleanups);self.root=f.root;self.config=f.config();self.client=self.root/'client.json'
        self.client.write_bytes(canonical({'installed':{'client_id':'test-client','client_secret':'test-secret','auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token'}}));self.client.chmod(0o600)
    def flow(self,client,scopes,**kwargs):
        self.scopes=scopes;self.pkce=kwargs.get('autogenerate_code_verifier')
        class Flow:
            def run_local_server(self,**kwargs):return SimpleNamespace(client_id='test-client',client_secret='test-secret',refresh_token='test-refresh',token='test-access')
        return Flow()
    def test_connection_writes_private_credentials_and_sender_without_activating_mail(self):
        self.assertIsNotNone(connect);dest=self.root/'oauth/gmail.json';r=connect(self.config,self.client,dest,flow_factory=self.flow,whoami=lambda token:{'email':'sender@example.com','email_verified':True})
        self.assertEqual(dest.stat().st_mode&0o777,0o600);self.assertNotIn('test-refresh',json.dumps(r));c=json.loads(self.config.read_text());self.assertFalse(c['sending_enabled']);self.assertEqual(c['sender'],'sender@example.com');self.assertEqual(c['credentials_path'],str(dest));self.assertTrue(self.pkce);self.assertIn('https://www.googleapis.com/auth/gmail.send',self.scopes);self.assertFalse(any('readonly' in s or 'modify' in s for s in self.scopes))
    def test_unverified_identity_or_untrusted_oauth_endpoint_creates_no_credentials(self):
        self.assertIsNotNone(connect);dest=self.root/'oauth/gmail.json'
        with self.assertRaises(ValueError):connect(self.config,self.client,dest,flow_factory=self.flow,whoami=lambda token:{'email':'sender@example.com','email_verified':False})
        self.assertFalse(dest.exists());v=json.loads(self.client.read_text());v['installed']['token_uri']='https://attacker.example/token';self.client.write_bytes(canonical(v))
        with self.assertRaises(ValueError):connect(self.config,self.client,dest,flow_factory=self.flow,whoami=lambda token:{})
    def test_existing_credential_file_is_not_overwritten(self):
        self.assertIsNotNone(connect);dest=self.root/'gmail.json';dest.write_text('existing');dest.chmod(0o600)
        with self.assertRaises(FileExistsError):connect(self.config,self.client,dest,flow_factory=self.flow,whoami=lambda token:{})
        self.assertEqual(dest.read_text(),'existing')
    def test_google_canonical_identity_scope_survives_real_token_parser(self):
        try:
            from oauthlib.oauth2 import WebApplicationClient
        except ImportError:
            self.skipTest('optional Google OAuth dependencies not installed')
        from connect_email import SCOPES
        client=WebApplicationClient('test-client',scope=SCOPES)
        response={'access_token':'synthetic-access','token_type':'Bearer','scope':'openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.send'}
        client.parse_request_body_response(json.dumps(response))
        self.assertIn('https://www.googleapis.com/auth/userinfo.email',SCOPES)
