"""Short-lived local planning surface for the hosted browser. One opening and one fenced run."""
import hashlib
import html
import json
import os
import secrets
import select
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .store import Denied, Store


def start(store, q):
    store.check(q['owner'], q['token'])
    from . import canary
    if (store.path.parent/'codex-migration/worker-location.json').exists():
        canary.assert_owner(store)
        from .opening_history import guard
        guard(store,store.job(q['job_key']),q['run_id'])
        state=canary.state(store)
        if state['phase']!='expanded' and (state['phase']!='active' or q['job_key'] not in state['jobs']):
            raise Denied('enroll this opening before opening the form planner')
    process=subprocess.Popen([sys.executable,'-m','job_runtime.form_session'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,start_new_session=True,text=True)
    request={k:q[k] for k in ('owner','token','run_id','job_key')}
    process.stdin.write(json.dumps({**request,'database':str(store.path)})+'\n')
    process.stdin.close()
    if not select.select([process.stdout],[],[],6)[0]:
        process.terminate()
        raise Denied('local form planner did not start')
    url=process.stdout.readline().strip()
    process.stdout.close()
    if not url.startswith('http://127.0.0.1:'): raise Denied('local form planner unavailable')
    return {'url':url,'job_key':q['job_key'],'expires_in_seconds':600}


def serve(request):
    from .__main__ import dispatch
    from .form_step import normalized_url,requisition_id
    from .receipts import trusted_origin
    from .resumes import verify_attachment
    store=Store(request.pop('database'))
    url_path='/'+secrets.token_urlsafe(32)
    safe=lambda s:html.escape(s,quote=True)
    job=store.job(request['job_key'])
    datafolder=store.path.parent/'codex-runtime/runs'/request['run_id']/'forms'
    datafolder.mkdir(parents=True,exist_ok=True,mode=0o700)
    proof_path=datafolder/(hashlib.sha256(job['job_key'].encode()).hexdigest()+'.json')
    base_ref=None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def valid(self):
            return self.path==url_path and self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
        def respond(self, packet):
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','same-origin')
            self.send_header('Content-Security-Policy',"default-src 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            body=('<title>Local application planner</title><h1>Local application planner</h1>'
                  '<form method="post"><label>Form observation<textarea name="observation"></textarea></label>'
                  '<button>Process form observation</button></form><pre id="result">'+safe(json.dumps(packet))+'</pre>')
            self.wfile.write(body.encode())
        def do_GET(self):
            if not self.valid(): self.send_error(404); return
            self.respond({'ready':True})
        def do_POST(self):
            nonlocal base_ref
            origin=f'http://127.0.0.1:{self.server.server_port}'
            if not self.valid() or self.headers.get('Origin')!=origin: self.send_error(403);return
            try:
                store.check(request['owner'],request['token'])
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<400000: raise Denied('unsupported observation size')
                data=json.loads(parse_qs(self.rfile.read(length).decode())['observation'][0])
                page=data['page']
                posting=normalized_url(job['link'])
                route=normalized_url(page.get('url') or page.get('observed_url'))
                expected=requisition_id(posting)
                if (not route or not posting or (route!=posting and (not expected or expected!=requisition_id(route)
                    or not trusted_origin(posting,route))) or page.get('account') or page.get('captcha')
                    or page.get('identity_ambiguous') or page.get('incomplete')):
                    raise Denied('browser observation must identify the original opening')
                ref=hashlib.sha256(json.dumps([route,[(f['key'],f['label']) for f in page['fields']]]).encode()).hexdigest()
                args={**request,'page_ref':ref,'fields':page['fields'],'controls':page['controls'],'refs':[route]}
                if data['phase']=='plan':
                    packet=dispatch(store,{**args,'op':'form_packet'})
                    base_ref=ref
                elif data['phase']=='verify' and ref==base_ref:
                    packet=dispatch(store,{**args,'op':'form_step'})
                    questions=list(dict.fromkeys(b['label'] or b['reason'] for b in [*page.get('questions',[]),*packet['blocked']]))
                    if questions: dispatch(store,{**request,'op':'form_question','questions':questions,'refs':[route]})
                    attachment=page.get('attachment')
                    if attachment:
                        policy=store.policy_snapshot()
                        name=policy.get('resume_only') or job['resume']
                        from .backup import file_hash
                        sha=file_hash(store.path.parent/'my-resumes'/name)
                        store.verify_pdf(job,sha,required=True,policy=policy)
                        verify_attachment(store.path.parent,job,name,sha,attachment)
                        from .learning import verified
                        with store.tx():
                            store.check(request['owner'],request['token'])
                            verified(store,'attachment_evidence',job['job_key'],'Selected PDF and manifest verified on the original form.')
                    from . import learning
                    with store.tx():
                        store.check(request['owner'],request['token'])
                        for ans in packet['answers']:
                            if 'observed_value' in ans and ans.get('source') and ans.get('field'):
                                issue=learning.classify('question',learning.public_text(ans['field']))[0]
                                learning.verified(store,issue,job['job_key'],'Saved answer verified in the live employer field.')
                    pages=json.loads(proof_path.read_text()) if proof_path.exists() else {}
                    pages[ref]={'answers':packet['answers'],'required':packet.get('required_fields',[]),'attachment':attachment,'questions':questions,'refs':[route]}
                    temp=proof_path.with_suffix('.new')
                    temp.write_text(json.dumps(pages));temp.chmod(0o600);temp.replace(proof_path)
                    packet={'verified':not (questions or packet['ops'] or page.get('errors')),'questions':questions,'answer_count':sum('observed_value' in a for a in packet['answers']),'saved':str(proof_path)}
                else: raise Denied('plan this exact form page before verifying it')
                self.respond(packet)
            except (ValueError, KeyError,TypeError,Denied,OSError) as error:
                self.respond({'error':str(error) if isinstance(error,Denied) else 'invalid form observation'})
    with HTTPServer(('127.0.0.1',0),Handler) as server:
        server.timeout=5
        print(f'http://127.0.0.1:{server.server_port}'+url_path,flush=True)
        sys.stdout.close()
        deadline=time.monotonic()+600
        while time.monotonic()<deadline: server.handle_request()
    store.con.close()


if __name__=='__main__':
    serve(json.loads(sys.stdin.readline()))
