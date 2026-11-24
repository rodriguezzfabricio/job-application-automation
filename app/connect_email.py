"""One-time Google browser consent. No email is sent and activation stays off."""
from pathlib import Path
import argparse,json,os,tempfile,urllib.request
from profile_store import _private_json
from service import configuration
from polling import _private_directory
from packet import canonical
from mail_transport import _mailbox,_NoRedirect

# Google returns the canonical userinfo scope in token responses. Using its
# short 'email' alias makes OAuthlib reject an otherwise equivalent grant.
SCOPES=['https://www.googleapis.com/auth/gmail.send','openid','https://www.googleapis.com/auth/userinfo.email']

def _identity(access_token):
    opener=urllib.request.build_opener(_NoRedirect())
    req=urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':'Bearer '+access_token})
    with opener.open(req,timeout=15) as response:
        if response.status!=200:raise ValueError('sender identity verification failed')
        raw=response.read(65537)
    if len(raw)>65536:raise ValueError('identity response too large')
    return json.loads(raw)

def connect(config_path,client_path,credential_path,*,flow_factory=None,whoami=None):
    config_path=Path(config_path);credential_path=Path(credential_path)
    if not credential_path.is_absolute():raise ValueError('absolute private credential path required')
    config=configuration(config_path);client=_private_json(client_path);installed=client.get('installed')
    if not isinstance(installed,dict) or installed.get('auth_uri') not in {'https://accounts.google.com/o/oauth2/auth','https://accounts.google.com/o/oauth2/v2/auth'} or installed.get('token_uri')!='https://oauth2.googleapis.com/token':raise ValueError('Google Desktop OAuth client required; arbitrary endpoints refused')
    if credential_path.exists() or credential_path.is_symlink():raise FileExistsError('existing sender credentials preserved')
    _private_directory(credential_path.parent)
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow_factory=InstalledAppFlow.from_client_config
    flow=flow_factory(client,SCOPES,autogenerate_code_verifier=True)
    credentials=flow.run_local_server(host='127.0.0.1',port=0,timeout_seconds=180,open_browser=True,prompt='consent',access_type='offline',authorization_prompt_message='Authorize the sender account in your browser.',success_message='Sender connected. You may close this window. No email has been sent.')
    if any(not isinstance(getattr(credentials,k,None),str) or not getattr(credentials,k) for k in ['client_id','client_secret','refresh_token','token']):raise ValueError('offline sender authorization was not granted')
    identity=(whoami or _identity)(credentials.token)
    if not isinstance(identity,dict) or identity.get('email_verified') is not True:raise ValueError('verified Google sender address required')
    sender=_mailbox(identity.get('email'));secret={k:getattr(credentials,k) for k in ['client_id','client_secret','refresh_token']}
    fd=os.open(credential_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(canonical(secret));out.flush();os.fsync(out.fileno())
    config.update(sender=sender,credentials_path=str(credential_path),sending_enabled=False)
    fd,name=tempfile.mkstemp(dir=config_path.parent,prefix='.sender-config-')
    try:
        with os.fdopen(fd,'wb') as out:out.write(canonical(config));out.flush();os.fsync(out.fileno())
        os.replace(name,config_path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return {'status':'sender_connected','sender':sender,'credentials_stored_privately':True,'sending_enabled':False,'emails_sent':0,'limits':'Account authorization only. Final release checks and a provider/inbox test remain separate.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);parser.add_argument('--client',type=Path,required=True);parser.add_argument('--credentials',type=Path,default=Path.home()/'.config/job-assistant/gmail.json');args=parser.parse_args()
    try:print(json.dumps(connect(args.config,args.client,args.credentials),indent=2))
    except Exception as exc:print(json.dumps({'status':'not_connected','error_class':type(exc).__name__,'message':'Connection failed; no provider response or token is printed.'}));raise SystemExit(1)
