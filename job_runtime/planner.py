"""Read-only bounded queues. Final submission always repeats policy and budget checks."""
import json
import re
import sqlite3
from . import accounts
from .adapters import legacy
from .policy import ET, gate, timestamp, company_group, owner_go, posted_time
from .store import Denied

# Sites where applying needs an account; owned by accounts.py so "needs an account" and
# "we hold one" cannot drift apart.
ACCOUNT_SITES=accounts.ACCOUNT_SITES
account_site=accounts.account_site
NOT_PORTAL_GO=re.compile(r'captcha|filled \d{4}-\d{2}-\d{2}',re.I)


def open_portal_tabs(store, now):
    """Sites with an application page open in his Brave (sweep's open-tabs.json, under 2 h old)."""
    try:
        with open(store.path.parent/'scratch'/'open-tabs.json',encoding='utf-8') as f: snap=json.load(f)
        at=timestamp(snap.get('at'))
        if at and 0<=(now-at).total_seconds()<=7200: return set(snap.get('portal_tabs') or [])
    except (OSError,ValueError,AttributeError): pass
    return set()


def ready_sites(store, now):
    """Sites (portals.py) where he marked 'signed up' on Today, the agent got in since the last wall, or
    (Claude parity) one of his application pages on that site is open in Brave."""
    P=legacy('portals')
    try: rows=[dict(r) for r in store.con.execute('SELECT * FROM portals')]
    except sqlite3.OperationalError: return set()
    naive=now.astimezone(ET).replace(tzinfo=None);tabs=open_portal_tabs(store,now)
    return {r['portal'] for r in rows if P.state(r,r['portal'] in tabs,naive)=='ready'}


def apply_walls(store, keys, owner, token):
    """Move matched jobs on account sites to Walled, so Today lists them under 'Apply yourself'
    instead of the fill stage spending a slot on a sign-in page. Call right after plan()."""
    P=legacy('portals');done=[]
    with store.tx():
        store.check(owner,token)
        for key in keys:
            try: j=store.job(key)
            except Denied: continue
            if j['status'] not in ('New','Matched'): continue
            site=P.label(P.portal_of(j['link']),j['company'])
            note=f"account site {store.now()[:10]}: sign in or create the account on {site}, then set Mark to 'signed up'; "
            store.con.execute("UPDATE jobs SET status='Walled', notes=?, updated_at=? WHERE job_key=?",((note+(j['notes'] or ''))[:1900],store.now(),key))
            store.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',(store.now(),'codex-account-site',key,json.dumps({'portal':P.portal_of(j['link'])})))
            done.append(key)
    return done

def mark_opened(store, keys, owner, token):
    """Record that a walled job's page was opened in his Brave, so it is never opened twice."""
    done=[]
    with store.tx():
        store.check(owner,token)
        for key in keys:
            try: j=store.job(key)
            except Denied: continue
            if 'opened for you' in (j['notes'] or ''): continue
            store.con.execute('UPDATE jobs SET notes=?,updated_at=? WHERE job_key=?',
                              (('opened for you '+store.now()[:16]+'; '+(j['notes'] or ''))[:1900],store.now(),key))
            done.append(key)
    return done


BLOCK_HOURS=24
TRANSIENT_MINUTES=30
# Browser glitches (upload chooser timeouts, crashes) are retried soon; walls and missing proof are not.
TRANSIENT=re.compile(r'time[ds]? ?out|timed out|chooser|temporar|network|crash|retry',re.I)


def fill_blockers(store, now):
    """Fill-stage blockers (sign-in wall, missing proof, outside 24h...) recorded with adapters.defer.

    Keyed by job_key or company name (the stage writes both). A blocker holds for its
    retry_at, else BLOCK_HOURS from first_seen (TRANSIENT_MINUTES for browser glitches),
    so a known-blocked job does not take a fill slot in every run while fresh jobs wait.
    """
    out={}
    for b in store.con.execute("SELECT item_key,payload,first_seen,retry_at FROM jr_backlog WHERE provider='fill' AND state='pending'"):
        try: reason=str(json.loads(b['payload']).get('reason') or 'see fill backlog')
        except (ValueError,TypeError,AttributeError): reason='see fill backlog'
        retry=timestamp(b['retry_at']);seen=timestamp(b['first_seen'])
        hold=TRANSIENT_MINUTES*60 if TRANSIENT.search(reason) else BLOCK_HOURS*3600
        active=retry>now if retry else bool(seen and (now-seen).total_seconds()<hold)
        if not active: continue
        out[str(b['item_key']).strip().lower()]=reason[:120]
    return out


def plan(store, policy, mode, observed_auth=None):
    now=store.clock(); queues={'to_match':[],'to_one_click':[],'to_fill':[],'to_wall':[],'to_signin':[],'signin':{},'walled_to_open':[],'reconcile':[],'handoffs':[],'skipped':{}}
    observed_auth=observed_auth or {}
    pending={r['job_key']:dict(r) for r in store.con.execute("SELECT * FROM jr_attempts WHERE state IN ('prepared','submit_started')")}
    blocked=fill_blockers(store,now)
    ready=ready_sites(store,now)
    P=legacy('portals');portal_of=P.portal_of
    private=accounts.private_of(store)
    naive=now.astimezone(ET).replace(tzinfo=None)
    prows={r['portal']:dict(r) for r in store.con.execute("SELECT * FROM portals")}
    creds_cache={}
    def agent_login_for(portal):
        # One credential read per site, not one per row.
        if portal not in creds_cache: creds_cache[portal]=accounts.has_credentials(private,portal)
        return creds_cache[portal]
    rows=[dict(r) for r in store.con.execute("SELECT * FROM jobs WHERE status IN ('New','Matched','Ready to fill','Walled','Awaiting submit')")]
    # Owner go-ahead first, then newest posting, then score. A system-set 'Ready to fill'
    # (tab sweep) gets no priority, or stale rows hold every fill slot forever.
    from . import canary
    from .opening_history import recovery_holds
    from .policy import opening_identity
    recovery = recovery_holds(store)
    rollout = canary.state(store)
    enrolled = rollout['jobs'] if rollout['phase'] == 'active' else {}
    rows.sort(key=lambda j:(j['job_key'] not in enrolled,not owner_go(j),-(posted_time(j) or now).timestamp(),-(j['score'] or 0)))
    from .form_step import answer_revision, technical_hold
    waiting={}
    for record in store.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='application_handoff' AND state<>'resolved'"):
        payload=json.loads(record['payload'])
        if payload.get('answer_revision'):
            waiting[record['item_key']]=payload
    revision=answer_revision(store.path.parent) if waiting else None
    active = store.con.execute("SELECT id FROM jr_runs WHERE status='running' ORDER BY started DESC LIMIT 1").fetchone()
    for j in rows:
        key=j['job_key']; channel='one_click' if re.search('easy apply only|quick apply only',j['notes'] or '',re.I) else 'form'
        if key in pending:
            queues['reconcile' if pending[key]['state']=='submit_started' else 'handoffs'].append({'job_key':key,'attempt_id':pending[key]['id']});continue
        hold = waiting.get(key) or {}
        technical_retry = (technical_hold(hold.get('questions') or [hold.get('question','')], hold.get('retry_on'))
                           and (not active or active[0] != hold.get('blocked_run')))
        if hold.get('answer_revision') and hold['answer_revision']==revision and not technical_retry:
            queues['handoffs'].append({'job_key':key,'reason':waiting[key]['question'],
                                      'questions':waiting[key].get('questions') or [waiting[key]['question']]})
            continue
        why=store.application_gate(j,policy,channel)
        if why: queues['skipped'][key]=why;continue
        try:
            if store.already_applied(j):queues['skipped'][key]='already applied';continue
        except Denied as e:
            queues['reconcile'].append({'job_key':key,'reason':str(e)});continue
        review = recovery.get(opening_identity(j['link']))
        if review:
            queues['reconcile'].append({'job_key':key,'reason':review['reason'],'opening_id':review['opening_id']})
            continue
        if len(enrolled) >= canary.LIMIT and key not in enrolled:
            queues['skipped'][key]='first five openings still in rollout';continue
        why_blocked = blocked.get(key.strip().lower()) or blocked.get(str(j['company'] or '').strip().lower())
        if not j['resume']:
            if why_blocked and not technical_retry:
                queues['skipped'][key]='fill blocked: '+why_blocked
            else:
                queues['to_match'].append(key)
            continue
        auth=observed_auth.get(key,{})
        observed=timestamp(auth.get('observed_at'))
        signed_in=bool(auth.get('refs') and auth.get('signed_in') and observed and 0<=(now-observed).total_seconds()<=300)
        # His 'signed up' mark is remembered (portals table), not only a sign-in seen in the last 5 minutes.
        site_ready=signed_in or (portal_of(j['link']) in ready and not NOT_PORTAL_GO.search(j['notes'] or ''))
        # Credentials on file turn a sign-in into the agent's own step instead of a wait for him.
        agent_login=agent_login_for(portal_of(j['link']))
        account_blocker = re.search(r'sign[ -]?in|log[ -]?in|create (?:an? )?account|private autofill',why_blocked or '',re.I)
        if (why_blocked and not technical_retry and not owner_go(j)
                and not (account_blocker and (site_ready or agent_login or account_site(j['link'])))):
            queues['skipped'][key]='fill blocked: '+why_blocked
            continue
        if j['status']=='Awaiting submit' and not signed_in and agent_login:
            queues['to_signin'].append(key)
            continue
        if j['status']=='Awaiting submit' and not signed_in or j['status']=='Walled' and not site_ready and not agent_login:
            # Claude parity: a walled job is opened in his Brave once ("leave open the ones you can't do and
            if j['status']=='Walled' and 'opened for you' not in (j['notes'] or '') and not NOT_PORTAL_GO.search(j['notes'] or ''):
                queues['walled_to_open'].append({'job_key':key,'company':j['company'],'title':j['title'],'link':j['link']})
            queues['handoffs'].append({'job_key':key,'reason':'inspect existing tab; sign-in required'});continue
        # A sign-in wall belongs on a list (his, or the agent's), never in a silent skip.
        if channel=='form' and not site_ready and not owner_go(j) and (account_site(j['link']) or j['status']=='Walled'):
            (queues['to_signin'] if agent_login else queues['to_wall']).append(key)
            continue
        if why_blocked and not site_ready and not technical_retry and not owner_go(j):
            queues['skipped'][key]='fill blocked: '+why_blocked
            continue
        queues['to_one_click' if channel=='one_click' else 'to_fill'].append(key)
    # Budget checks happen again before reserving each attempt; queues themselves do not spend slots.
    # Signing in is the slowest step (a verification mail), so it is capped and carries its recipe.
    queues['to_signin']=queues['to_signin'][:policy['signins_per_run']]
    for key in queues['to_signin']:
        portal=portal_of(store.job(key)['link'])
        queues['signin'][portal]=accounts.recipe(private,portal,P.state(prows.get(portal),False,naive))
    queues['walled_to_open']=queues['walled_to_open'][:4]
    if store.codex_production():
        locally_verified = {r['item_key'] for r in store.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='requirements' AND state='pending'")
                            if json.loads(r['payload'] or '{}').get('verified_review')}
        queues['to_match'].sort(key=lambda key:key not in locally_verified)
    queues['to_match']=queues['to_match'][:25]
    queues['to_one_click']=queues['to_one_click'][:policy['one_click_per_run']]
    queues['to_fill']=queues['to_fill'][:policy['fast_fills_per_run' if mode=='FAST' else 'fills_per_run']]
    queues['policy_hash']=policy['hash']; queues['mode']=mode
    return queues
