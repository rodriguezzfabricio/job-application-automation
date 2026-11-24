"""Fenced portal access. Credentials travel only through the private native host."""
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from . import accounts, canary
from .form_step import normalized_url, requisition_id
from .policy import timestamp
from .store import Denied

TTL = 600
KEY = 'portal_autofill'
VERIFICATION = 'portal_verification'


def value(store, key):
    row = store.con.execute('SELECT value FROM jr_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def write(store, key, data):
    store.con.execute('INSERT OR REPLACE INTO jr_meta VALUES (?,?)', (key, json.dumps(data)))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def tenant(url):
    host = accounts.hostname(url)
    parts = [p.casefold() for p in urlsplit(url).path.split('/') if p]
    if parts and re.fullmatch(r'[a-z]{2}-[a-z]{2}', parts[0]):
        parts.pop(0)
    if accounts.allowed(host, ('myworkdaysite.com',)) and parts[:1]==['recruiting']:
        return host+'/recruiting/'+'/'.join(parts[1:3]) if len(parts)>=3 else ''
    if accounts.allowed(host, ('myworkdayjobs.com','myworkdaysite.com')):
        return host + '/' + (parts[0] if parts and parts[0] not in ('job','jobs','login','register','apply') else '')
    if accounts.allowed(host, ('successfactors.com','successfactors.eu','brassring.com')):
        query = {k.lower(): v for k,v in parse_qs(urlsplit(url).query).items()}
        key = 'company' if 'successfactors' in host else 'partnerid'
        marker = query.get(key, [])
        return host + '/' + marker[0].casefold() if len(marker)==1 else ''
    if accounts.allowed(host, ('ultipro.com','taleo.net','oraclecloud.com')):
        if 'taleo.net' in host and 'careersection' in parts: parts = parts[parts.index('careersection')+1:]
        if 'oraclecloud.com' in host and 'sites' in parts: parts = parts[parts.index('sites')+1:]
        return host + '/' + parts[0] if parts else ''
    return host


def target(store, job_key, url):
    job = store.job(job_key)
    site = accounts.hostname(job['link'])
    if (not accounts.account_site(job['link']) or not accounts.has_credentials(store.path.parent, site)
            or not str(url).startswith('https://') or accounts.hostname(url) != site):
        raise Denied('normal account page on the exact credentialed employer host required')
    return site


def access(store, q):
    job = store.job(q['job_key'])
    site = accounts.hostname(job['link'])
    scope = tenant(job['link'])
    if not scope or not accounts.account_site(site):
        raise Denied('identify the employer tenant on the supported portal')
    key = 'account-access:' + digest(scope)
    return job, site, scope, key, value(store, key)


def combined_key(job):
    scope = tenant(job['link'])
    if accounts.account_site(job['link']):
        return 'account-access:' + digest(scope)
    return 'combined-registration:' + digest(scope + '|' + job['company_key'])


def combined_registration_started(store, attempt, job, answers):
    """A verified checkbox can make the final application click a registration too."""
    if not any(row.get('rule')=='recruiting_consent' and
               re.search(r'\bcreating an account\b',str(row.get('field','')),re.I)
               for row in answers or []):
        return
    key = combined_key(job)
    prior = value(store,key)
    if prior.get('state')=='registration_pending':
        raise Denied('reconcile the pending account registration before another combined application click')
    if prior.get('state') in ('created','signed_in') and prior.get('last_opening')==job['job_key']:
        return
    if prior.get('state') in ('created','signed_in','exists'):
        raise Denied('return to this opening signed in before continuing the combined application')
    now = store.clock().timestamp()
    portal = accounts.hostname(job['link'])
    request = dict(state='registration_pending',mode='combined_application',portal=portal,tenant=tenant(job['link']),
                   job_key=job['job_key'],attempt_id=attempt['id'],run_id=attempt['run_id'],submitted_at=now,expires=now+TTL)
    write(store,key,request)
    store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                      (store.now(),'portal-registration-started',job['job_key'],json.dumps(request)))


def decision(store,q):
    store.check(q['owner'],q['token'])
    job,site,scope,_,prior=access(store,q)
    creds=accounts.credentials(store.path.parent,site)
    portal=store.con.execute('SELECT signed_up_at,ok_at FROM portals WHERE portal=?',(site,)).fetchone()
    host_specific = scope==site or accounts.allowed(site,('myworkdayjobs.com','myworkdaysite.com','icims.com'))
    known=prior.get('state') in ('exists','created','signed_in') or (
        host_specific and (bool(portal and any(portal)) or bool(creds and creds['source']=='portal')))
    if prior.get('state')=='registration_pending':
        return {'mode':'reconcile_registration','job_key':prior['job_key'],'portal':site}
    mode='sign_in' if known else 'register'
    return {'mode':mode if creds else 'needs_private_autofill','job_key':job['job_key'],'portal':site}


def owner_check(store,q):
    store.check(q['owner'],q['token'])
    if (store.path.parent/'codex-migration/worker-location.json').exists():
        canary.assert_owner(store)
        state=canary.state(store)
        if state['phase']=='halted' or (state['phase']!='expanded' and q['job_key'] not in state['jobs']):
            raise Denied('enroll eligible opening before account access')


def check_current_opening(store, key):
    if store.codex_production():
        from .policy import publication_hold
        hold = publication_hold(store.employer_date(key), store.clock())
        if hold:
            raise Denied(hold)


def begin(store, q):
    origin, mode = q['page_origin'], q['mode']
    page_digest = q.get('page_sha256', '')
    if not re.fullmatch('[a-f0-9]{64}', page_digest) or origin != 'https://' + accounts.hostname(origin):
        raise Denied('secure page origin and SHA-256 of the exact visible URL required')
    with store.tx():
        owner_check(store,q)
        check_current_opening(store, q['job_key'])
        job,portal,scope,_,_=access(store,q)
        if (store.path.parent/'codex-migration/worker-location.json').exists():
            from .opening_history import guard
            guard(store,job,q['run_id'])
        target(store,job['job_key'],origin)
        route=decision(store,q)['mode']
        saved=q.get('browser_saved_login') or {}
        if route=='register' and saved.get('observed') is True and saved.get('origin')==origin and saved.get('tenant')==scope: route='sign_in'
        if route!=mode or mode not in ('sign_in','register'):
            raise Denied('account route requires '+route)
        now=store.clock().timestamp()
        grant=dict(grant_id=secrets.token_hex(16),job_key=q['job_key'],owner=q['owner'],token=q['token'],portal=portal,tenant=scope,
                   mode=mode,page_digest=page_digest,requested_at=now,expires=now+TTL)
        write(store,KEY,grant)
    if (store.path.parent/'codex-migration/worker-location.json').is_file():
        from . import portal_bridge
        portal_bridge.start(store,grant)
    return {'portal':portal,'mode':mode,'job_key':q['job_key'],'ready':True,'expires':grant['expires']}


def consume(store, actual_url, expected_grant=None):
    """Native message only. No worker operation exposes this response."""
    with store.tx():
        grant=value(store,KEY)
        if (not grant or grant['expires']<=store.clock().timestamp() or digest(actual_url)!=grant['page_digest']
                or (expected_grant is not None and expected_grant!=grant.get('grant_id'))):
            raise Denied('no live grant for this exact account page')
        owner_check(store,grant)
        check_current_opening(store, grant['job_key'])
        portal=target(store,grant['job_key'],actual_url)
        if tenant(actual_url)!=grant['tenant']:
            raise Denied('account form belongs to another employer tenant')
        c=accounts.credentials(store.path.parent,portal)
        store.con.execute('DELETE FROM jr_meta WHERE key=?',(KEY,))
        write(store,'portal_last_autofill',dict(grant,fill_source='native_extension'))
    return {'email':c['email'],'password':c['password'],'mode':grant['mode']}


def saved_autofill_used(store,q):
    """Accept a native password-manager fill without reading any credential values."""
    with store.tx():
        owner_check(store,q)
        _,site,scope,_,_=access(store,q)
        grant=value(store,KEY)
        proof=q.get('autofill') or {}
        if (grant.get('mode')!='register' or grant.get('job_key')!=q['job_key'] or grant.get('owner')!=q['owner']
                or grant.get('token')!=q['token'] or grant.get('page_digest')!=q.get('page_sha256')
                or grant.get('expires',0)<store.clock().timestamp() or proof.get('origin')!='https://'+site
                or proof.get('tenant')!=scope or proof.get('provider')!='browser_saved_login'
                or proof.get('saved_entry_selected') is not True or proof.get('email_populated') is not True
                or proof.get('all_password_fields_populated') is not True or proof.get('password_field_count') not in (1,2)):
            raise Denied('observed private fill by the browser password manager on the armed registration required')
        store.con.execute('DELETE FROM jr_meta WHERE key=?',(KEY,))
        write(store,'portal_last_autofill',dict(grant,fill_source='browser_saved_login'))
    return {'private_fill_observed':True,'portal':site}


def filled_observed(store,q):
    """Record boolean field readback after the private helper on this exact armed page."""
    with store.tx():
        owner_check(store,q)
        check_current_opening(store,q['job_key'])
        grant=value(store,'portal_last_autofill')
        obs=q.get('autofill') or {}
        if (grant.get('fill_source')!='native_extension' or grant.get('job_key')!=q['job_key']
                or grant.get('owner')!=q['owner'] or grant.get('token')!=q['token']
                or grant.get('expires',0)<store.clock().timestamp() or q.get('page_sha256')!=grant.get('page_digest')
                or obs.get('origin')!='https://'+grant['portal'] or obs.get('tenant')!=grant['tenant']
                or obs.get('email_populated') is not True or obs.get('all_password_fields_populated') is not True):
            raise Denied('observed populated controls on the exact funded account page required')
        grant['fields_observed_at']=store.now()
        write(store,'portal_last_autofill',grant)
    return {'job_key':q['job_key'],'private_fill_observed':True,'portal':grant['portal']}


def register_started(store,q):
    """Commit before the single normal registration click. Holds survive a new lease."""
    with store.tx():
        owner_check(store,q)
        check_current_opening(store, q['job_key'])
        _,site,scope,key,prior=access(store,q)
        grant=value(store,'portal_last_autofill')
        if prior.get('state') in ('registration_pending','exists','signed_in','created'):
            raise Denied('reconcile the existing registration before clicking')
        if (grant.get('mode')!='register' or grant.get('job_key')!=q['job_key'] or grant.get('owner')!=q['owner']
                or grant.get('token')!=q['token'] or grant.get('page_digest')!=q.get('page_sha256')
                or grant.get('expires',0)<store.clock().timestamp()):
            raise Denied('registration requires private autofill on the current form')
        if (store.codex_production() and accounts.guest_form(site) and grant.get('fill_source')=='native_extension'
                and not grant.get('fields_observed_at')):
            raise Denied('observe populated Workday account controls before registering')
        now=store.clock().timestamp()
        pending=dict(grant,state='registration_pending',submitted_at=now,expires=now+TTL)
        write(store,key,pending)
        store.con.execute('DELETE FROM jr_meta WHERE key=?',('portal_last_autofill',))
        return {'click_once':True,'portal':site,'job_key':q['job_key']}


def account_exists(store,q):
    with store.tx():
        owner_check(store,q)
        _,site,scope,key,prior=access(store,q)
        if (prior.get('state')!='registration_pending' or prior['job_key']!=q['job_key']
                or q.get('origin')!='https://'+site or not prior.get('page_digest') or q.get('page_sha256')!=prior['page_digest']
                or not re.search(r'\b(?:account (?:already )?exists|already (?:registered|in use)|email (?:address )?is already registered)\b',q.get('notice',''),re.I)):
            raise Denied('observed existing-account response on this registration required')
        write(store,key,dict(prior,state='exists'))
    return {'mode':'sign_in','portal':site}


def code_challenge(store,q):
    """Bind a visible registration code input to this pending employer tenant."""
    with store.tx():
        owner_check(store,q)
        _,site,scope,key,prior=access(store,q)
        if (prior.get('state')!='registration_pending' or prior.get('job_key')!=q['job_key']
                or prior.get('expires',0)<store.clock().timestamp() or q.get('origin')!='https://'+site
                or q.get('tenant')!=scope or not re.fullmatch('[0-9a-f]{64}',str(q.get('page_sha256','')))
                or q.get('input_count')!=1 or not re.search(r'verif(?:y|ication)|activation',str(q.get('label','')),re.I)):
            raise Denied('one observed account-verification code input on this employer tenant required')
        write(store,key,dict(prior,code_challenge_sha256=q['page_sha256']))
    return {'challenge_bound':True,'portal':site}


def verification_filter(store,q):
    store.check(q['owner'],q['token'])
    _,portal,scope,_,request=access(store,q)
    if (request.get('state')!='registration_pending' or request['job_key']!=q['job_key']
            or request['expires']<store.clock().timestamp()):
        raise Denied('no current submitted registration request for this opening')
    creds=accounts.credentials(store.path.parent,portal)
    return {'recipient_sha256':digest(creds['email'].strip().casefold()),'portal':portal,'tenant':scope,
            'after_epoch':request['submitted_at'],'before_epoch':request['expires'],
            'code_challenge_sha256':request.get('code_challenge_sha256')}


def verification_match(store,q):
    expected=verification_filter(store,q)
    mail=q.get('mail') or {}
    received=timestamp(mail.get('received_at'))
    portal=expected['portal']
    signer=str(mail.get('authenticated_sender_domain') or '').lower()
    exact_sender=bool(signer and (portal==signer or (portal.endswith('.'+signer) and accounts.account_site(signer))))
    if accounts.allowed(portal,('myworkdayjobs.com','myworkdaysite.com')) and signer in ('myworkday.com','workday.com'):
        exact_sender=True
    valid=bool(received and expected['after_epoch']<=received.timestamp()<=min(store.clock().timestamp(),expected['before_epoch'])
               and mail.get('recipient_sha256')==expected['recipient_sha256'] and mail.get('authenticated') is True
               and mail.get('purpose')=='account_verification' and mail.get('message_id') and exact_sender
               and mail.get('destination_origin')=='https://'+portal and mail.get('destination_tenant')==expected['tenant']
               and re.fullmatch('[a-f0-9]{64}',str(mail.get('action_sha256',''))))
    if mail.get('method','link')=='code':
        challenge=expected.get('code_challenge_sha256')
        valid=bool(valid and challenge and mail.get('action_sha256')==digest('code|'+str(mail.get('message_id'))+'|'+challenge))
    elif mail.get('method','link')!='link':
        valid=False
    if valid:
        with store.tx():
            verification_filter(store,q)
            _,_,_,key,prior=access(store,q)
            fingerprint=mail['action_sha256']
            if prior.get('matched_digest') not in (None,fingerprint):
                return {'matched':False,'portal':portal,'job_key':q['job_key']}
            write(store,key,dict(prior,matched_digest=fingerprint,matched_message=mail['message_id']))
    return {'matched':valid,'portal':portal,'job_key':q['job_key']}


def verification_used(store,q):
    """Record the observed successful activation, distinct from merely matching a message."""
    with store.tx():
        owner_check(store,q)
        _,portal,scope,key,prior=access(store,q)
        verification_filter(store,q)
        if (not prior.get('matched_digest') or prior['matched_digest']!=q.get('action_sha256')
                or prior.get('matched_message')!=q.get('message_id') or q.get('origin')!='https://'+portal
                or q.get('tenant')!=scope or not re.search(r'(?:account|e-?mail)(?: address)? (?:has been |is |was )?(?:successfully )?(?:activated|verified|confirmed)',
                    str(q.get('notice','')),re.I)):
            raise Denied('observed successful activation on this request and employer tenant required')
        if not prior.get('activated_at'):
            store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                (store.now(),'portal-account-activated',q['job_key'],json.dumps({'portal':portal,'original_form':'pending'})))
        write(store,key,dict(prior,activated_at=store.now()))
    return {'activation_observed':True,'portal':portal}


def creation_basis(store, prior, observation):
    if prior.get('activated_at'):
        return 'verified_activation'
    notice=str(observation.get('creation_notice','')).strip()
    if (observation.get('creation_confirmed') is True and
            re.search(r'\baccount (?:has been |was |is )?(?:successfully )?(?:created|registered)\b',notice,re.I)
            and not re.search(r'\b(?:previously|already|not|if|could|will|unable|failed|before|once)\b',notice,re.I)):
        return 'portal_creation_confirmation'
    step=observation.get('registration_transition') or {}
    clicked,returned=timestamp(step.get('clicked_at')),timestamp(step.get('returned_at'))
    if (prior.get('mode')=='register' and step.get('before_page_sha256')==prior.get('page_digest')
            and step.get('before_tab')==observation.get('tab_id')==step.get('after_tab')
            and step.get('before_signed_in') is False and step.get('intervening_auth') is False
            and str(step.get('button','')).strip().casefold() in ('create account','create an account','register')
            and clicked and returned and prior['submitted_at']<=clicked.timestamp()<=returned.timestamp()<=prior['expires']
            and 0<=(store.clock()-returned).total_seconds()<=300):
        return 'direct_registration_to_original_form'
    return None


def mark_observed(store,q):
    what=q.get('what','ok')
    site=accounts.hostname(q['portal'])
    if what=='wall':
        return accounts.mark(store,site,what,owner=q['owner'],token=q['token'])
    with store.tx():
        owner_check(store,q)
        job = store.job(q['job_key'])
        if accounts.account_site(job['link']):
            job,portal,scope,key,prior=access(store,q)
        else:
            key=combined_key(job)
            prior=value(store,key)
            if prior.get('mode')!='combined_application':
                raise Denied('an observed combined registration request on this opening is required')
            portal,scope=accounts.hostname(job['link']),tenant(job['link'])
        obs=q.get('auth') or {}
        form=obs.get('form') or {}
        form_url=form.get('url','')
        tracked=normalized_url(job['link'])
        expected_id=str(job.get('requisition_id') or requisition_id(tracked) or '')
        actual_id=requisition_id(form_url)
        field_kinds={str(f.get('type') or f.get('kind')) for f in form.get('fields',[]) if f.get('label')}
        route=normalized_url(form_url)
        application_surface=bool(re.search(r'/(?:apply|application|jobapplication)(?:/|$)',urlsplit(form_url).path,re.I)
            or any(re.search(r'resum[eé]|curriculum vitae|^cv$',str(f.get('label','')),re.I) for f in form.get('fields',[]))
            or any(re.search(r'application|my (?:experience|information)',str(h),re.I) for h in form.get('headings',[])))
        if (site!=portal or obs.get('signed_in') is not True or obs.get('origin')!='https://'+portal
                or obs.get('control') not in ('sign_out','authenticated_candidate_home')
                or not application_surface or not route or accounts.hostname(form_url)!=portal or tenant(form_url)!=scope
                or form.get('account') is not False or form.get('incomplete') or form.get('captcha') or form.get('identity_ambiguous')
                or not obs.get('tab_id') or obs['tab_id']!=form.get('tab_id') or not field_kinds.intersection({'file','text','email','select','textarea','tel'})
                or not expected_id or str(form.get('requisition_id') or actual_id or '')!=expected_id
                or (actual_id and actual_id!=expected_id) or (not actual_id and normalized_url(form.get('posting_link'))!=tracked)):
            raise Denied('signed-in control and application form for the original opening required')
        if prior.get('state')=='registration_pending' and prior['job_key']!=job['job_key']:
            raise Denied('resolve original registration opening first')
        basis=creation_basis(store,prior,obs) if prior.get('state')=='registration_pending' else None
        created=bool(basis)
        if prior.get('state')=='registration_pending' and not created:
            raise Denied('registration outcome still needs explicit account creation or verification evidence')
        outcome='created' if created else 'signed_in'
        run=str(q.get('run_id') or q['owner'])
        if prior.get('state')=='created' and prior.get('last_run')==run and prior.get('last_opening')==job['job_key']:
            return {'portal':site,'outcome':'created','form_restored':True,'job_key':job['job_key']}
        if prior.get('last_run')!=run or prior.get('last_opening')!=job['job_key'] or prior.get('state') not in ('created','signed_in'):
            proof={'portal':site,'run_id':run,'tenant':scope,'original_requisition':expected_id,
                   'authenticated_control':obs['control'],'tab_id':obs['tab_id'],'form_url_sha256':digest(form_url),
                   'application_fields':len(form['fields']),'basis':basis or 'authenticated_original_form'}
            grant = value(store,'portal_last_autofill')
            for fill in (prior, grant):
                if (fill.get('job_key') == job['job_key'] and fill.get('tenant') == scope
                        and (fill.get('owner') == q['owner'] and fill.get('token') == q['token']
                             or fill is prior and prior.get('state')=='registration_pending')
                        and (fill.get('fill_source')=='browser_saved_login' or
                             fill.get('fill_source')=='native_extension' and fill.get('fields_observed_at'))):
                    proof['fill_source'] = fill['fill_source']
                    break
            store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                              (store.now(),'portal-account-'+outcome,job['job_key'],json.dumps(proof)))
        result_state=dict(state=outcome,last_run=run,last_opening=job['job_key'],created=created or prior.get('created',False))
        if prior.get('mode')=='combined_application':
            result_state.update(mode='combined_application',job_key=job['job_key'])
        write(store,key,result_state)
        fill=prior if created else value(store,'portal_last_autofill')
        current_login=(fill.get('owner')==q['owner'] and fill.get('token')==q['token']
                       and fill.get('mode')=='sign_in' and fill.get('expires',0)>=store.clock().timestamp())
        if (fill.get('fill_source')=='native_extension' and fill.get('job_key')==job['job_key']
                and (created or current_login)):
            from .learning import verified
            verified(store,'private_autofill',job['job_key'],'Private autofill was used and the signed-in application reopened for requisition '+expected_id+'.')
        from .adapters import legacy
        result=legacy('tracker').set_portal(store.con,site,'signed-up' if created else 'ok','original application form verified')
    return {'portal':site,'outcome':outcome,'form_restored':True,'job_key':job['job_key'],'record':result}
