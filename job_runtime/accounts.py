"""Private credentials for normal employer sign-in and registration. Worker recipes contain no secrets."""
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import legacy

FILE = 'portal-accounts.json'
STATES = ('new', 'again', 'ready')
# Sites where applying needs an account first (same list as the legacy plan.py). Owned here
# because "what needs an account" and "have we got one" are the same question.
ACCOUNT_SITES = ('myworkdayjobs.com', 'myworkdaysite.com', 'icims.com', 'taleo.net', 'successfactors.com', 'successfactors.eu',
                 'oraclecloud.com', 'brassring.com', 'ultipro.com', 'paylocity.com', 'amazon.jobs',
                 'careers.microsoft.com', 'avature.net')


# Workday lets a candidate fill the whole application as a guest; the account / sign-in
# is only demanded at the final Review/Submit step. Verified live 2026-11-13 against
# exampleg.wd5.myworkdayjobs.com (Apply -> Apply Manually -> 5-step guest form).
GUEST_FORM_SITES = ('myworkdayjobs.com', 'myworkdaysite.com')


def hostname(value):
    raw = str(value or '')
    try:
        site = urlsplit(raw if '://' in raw else 'https://' + raw)
        if site.scheme == 'https' and site.hostname and not site.username and not site.password and site.port in (None, 443):
            return site.hostname.lower()
    except ValueError:
        pass
    return ''


def allowed(value, domains):
    host = hostname(value)
    return bool(host and any(host == d or host.endswith('.' + d) for d in domains))


def account_site(link):
    return allowed(link, ACCOUNT_SITES)


def guest_form(link):
    return allowed(link, GUEST_FORM_SITES)


def private_of(store):
    return str(store.path.parent)


def load(private):
    try:
        data = json.loads((Path(private) / FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry(private, portal):
    """(email, password, source). A blank field falls back to the shared default."""
    data = load(private)
    default = data.get('default') if isinstance(data.get('default'), dict) else {}
    own = (data.get('portals') or {}).get(portal) if isinstance(data.get('portals'), dict) else {}
    own = own if isinstance(own, dict) else {}
    email = own.get('email') or default.get('email') or ''
    password = own.get('password') or default.get('password') or ''
    source = 'portal' if (own.get('email') or own.get('password')) else ('default' if (email or password) else '')
    return str(email).strip(), str(password), source


def credentials(private, portal):
    if not account_site(portal):
        return None
    email, password, source = _entry(private, portal)
    return {'email': email, 'password': password, 'source': source} if email and password else None


def has_credentials(private, portal):
    return credentials(private, portal) is not None


def assignment(private, portal):
    """Who signs in here: 'agent' when we hold credentials, else the owner, as before."""
    return 'agent' if has_credentials(private, portal) else 'human'


def recipe(private, portal, state='new'):
    state = state if state in STATES else 'new'
    portal = hostname(portal)
    steps = []
    if account_site(portal) and has_credentials(private, portal) and state != 'ready':
        if guest_form(portal):
            steps.append('Fill the application as a guest and upload the selected resume.')
            steps.append('Only at the final Review/Submit step use the displayed account flow.')
        if state == 'new':
            steps.append('Use account_decide. Choose Create account / Register on a supported wall with no known employer account.')
        else:
            steps.append('Sign in on the employer portal.')
        steps.append('Use the browser password manager or the armed private autofill on this exact opening.')
        if state == 'new':
            steps.append('Use the matching verification mail for this request if the portal requires verification.')
        steps.append('After observing an authenticated control and the original application form, record account_mark and continue that opening.')
    return {'portal': portal, 'state': state, 'available': bool(account_site(portal) and has_credentials(private, portal)), 'steps': steps}


def mark(store, portal_or_link, what='signed-up', detail='', *, owner=None, token=None):
    portal = hostname(portal_or_link)
    if not account_site(portal) or what not in ('signed-up', 'ok', 'wall', 'verified'):
        raise ValueError('unsupported portal or account state')
    with store.tx():
        if owner is not None:
            store.check(owner, token)
        return legacy('tracker').set_portal(store.con, portal, what, 'normal portal flow: ' + what)


def ready_portals(store):
    """Sites the owner signed up on, or the agent got into: their jobs are fillable again."""
    portals = legacy('portals')
    naive = dt.datetime.now()
    return {r['portal'] for r in store.con.execute('SELECT * FROM portals')
            if portals.state(dict(r), False, naive) == 'ready'}


def open_portals(store):
    """Account sites that currently have work waiting, so a caller can report who signs in."""
    portals = legacy('portals')
    rows = store.con.execute("SELECT link FROM jobs WHERE status IN ('New','Matched','Ready to fill','Walled','Awaiting submit')")
    return {portals.portal_of(r['link']) for r in rows if account_site(r['link'])} - {''}
