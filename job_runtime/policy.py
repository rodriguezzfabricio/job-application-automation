"""Shared effective policy and lossless requisition URL identity."""
import datetime as dt
import hashlib
import json
import re
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
DEFAULTS = dict(paused='no', fill_mode='hourly', fill_windows='08,12,17',
                max_fills_per_day=40, one_click_per_day=40, one_click_per_run=8,
                fills_per_run=3, fast_fills_per_run=2, max_posting_age_hours=24,
                one_click_max_age_hours=72, linkedin_loads_per_day=60, cards_batch=12, cards_per_day=150,
                recruiter_companies_per_run=2, phone_updates='every run', push_hour=18,
                resume_only='', captcha_policy='detect', captcha_max_attempts=2,
                captcha_budget_seconds=90, captcha_max_per_run=6, captcha_solver='off',
                # one email/password. A sign-in is slow (a verification mail), so it is capped.
                signins_per_run=2)
TRACKING = re.compile(r'^(utm_.+|source|src|ref|referrer|trackingId|trk|p_sid|p_uid|soure|ss)$', re.I)

# A scheduled run has nobody to answer the host's action-time confirmation, so the
CAPTCHA_POLICIES = ('off', 'detect', 'checkbox', 'full')
CAPTCHA_SOLVERS = ('off', 'capsolver', 'twocaptcha')
CAPTCHA_ALIASES = {'off': 'off', 'none': 'off', 'no': 'off', 'detect': 'detect', 'only': 'detect',
                   'rec': 'detect', 'checkbox': 'checkbox', 'box': 'checkbox', 'check': 'checkbox',
                   'tick': 'checkbox', 'full': 'full', 'yes': 'full', 'on': 'full', 'auto': 'full', 's': 'full'}
CAPTCHA_BOUNDS = {'captcha_max_attempts': (1, 5), 'captcha_budget_seconds': (10, 300), 'captcha_max_per_run': (0, 50)}


def alias(value, allowed, aliases, setting):
    """Read one user-editable cell tolerantly: a typo must not kill a whole 20-minute cycle."""
    text = str(value if value is not None else '').strip().lower()
    if not text: return allowed[-1]
    if text in allowed: return text
    if text in aliases: return aliases[text]
    for name in allowed:
        if name.startswith(text): return name
    raise ValueError('unknown %s: %s' % (setting, value))


def captcha_policy(value):
    return alias(value, CAPTCHA_POLICIES, CAPTCHA_ALIASES, 'captcha_policy')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def canonical_url(url):
    """Preserve unknown query parameters, their case, tenants and SPA fragments."""
    p = urlsplit(url.strip())
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('HTTP(S) job URL required')
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), urlencode(q), p.fragment if p.fragment.startswith('/') else ''))

def opening_identity(url):
    """Lever's posting and the /apply form identify the same tenant and requisition."""
    normalized = canonical_url(url)
    parts = urlsplit(normalized)
    if parts.hostname == 'jobs.lever.co' and re.fullmatch(r'/[^/]+/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/apply', parts.path):
        return urlunsplit(parts._replace(path=parts.path.removesuffix('/apply')))
    return normalized

@lru_cache(maxsize=4096)
def history_identity(url):
    """Compare original Workday tenant and requisition across locale and location URLs."""
    canonical = opening_identity(url)
    parsed = urlsplit(canonical)
    if (parsed.hostname or '').endswith(('.myworkdayjobs.com','.myworkdaysite.com')) and '/job/' in parsed.path.casefold():
        from .account_flow import tenant
        from .form_step import requisition_id
        bare = urlunsplit(parsed._replace(query='',fragment=''))
        identifier = requisition_id(bare)
        scope = tenant(bare)
        if scope and identifier and identifier==requisition_id(canonical):
            return ('workday',scope,identifier.casefold())
    return ('url',canonical)

def timestamp(value):
    if not value: return None
    try:
        out = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            out = dt.datetime.strptime(str(value), '%Y-%m-%d %H:%M')
        except ValueError:
            return None  # Approximate labels are not canonical first-posted evidence.
    return out.replace(tzinfo=ET) if out.tzinfo is None else out

def posted_time(job):
    """When the posting went up, the way the Claude worker read it (legacy plan.py posted_time):
    a date with no clock time counts from the END of that day (23:59 ET), and a row with no posting
    date at all counts from when it was found. Codex read "2026-11-12" as midnight, which made the same
    posting 24 hours older than Claude thought, and refused rows with no date outright."""
    raw = str(job.get('posted_at') or '').strip()
    t = timestamp(raw) or timestamp(job.get('found_at'))
    if t and re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        t = t.astimezone(ET).replace(hour=23, minute=59, second=0, microsecond=0)
    return t


def publication_value(value,observed):
    """Anchor an original employer's literal Today label once, when it was observed."""
    if observed and str(value or '').strip().casefold() in ('today','posted today','first posted today'):
        return observed.astimezone(ET).date().isoformat()
    return value


def publication_hold(value, now):
    """Compare a publisher's date with the Eastern application day at its actual precision."""
    value = str(value or '').strip()
    if not value:
        return 'original employer posting date needs verification'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            day = dt.date.fromisoformat(value)
        except ValueError:
            return 'invalid employer posting date'
    else:
        try:
            moment = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return 'invalid employer posting date'
        if moment.tzinfo is None:
            return 'employer posting time zone needs verification'
        if moment > now:
            return 'first posted time is in the future'
        day = moment.astimezone(ET).date()
    return '' if day == now.astimezone(ET).date() else 'not first posted on this Eastern day'


def effective(settings, overrides_hash=''):
    p = dict(DEFAULTS, **settings)
    for k, v in DEFAULTS.items():
        if isinstance(v, int):
            p[k] = int(p[k])
            if p[k] < 0: raise ValueError('negative setting: ' + k)
    if str(p['paused']).lower() not in ('yes', 'no'): raise ValueError('paused must be yes or no')
    p['captcha_policy'] = captcha_policy(p['captcha_policy'])
    p['captcha_solver'] = alias(p['captcha_solver'], CAPTCHA_SOLVERS, {'none': 'off', 'no': 'off', '2c': 'twocaptcha', 'caps': 'capsolver'}, 'captcha_solver')
    for key, (low, high) in CAPTCHA_BOUNDS.items():
        p[key] = min(high, max(low, p[key]))
    p.update(version=1, score_floor=35, required_years_max=3,
             overrides_hash=overrides_hash)
    p['hash'] = digest(p)
    return p

def company_group(company):
    return re.sub(r'[^a-z0-9]', '', company.casefold())


def configured_companies(settings, name):
    configured = settings.get(name, [])
    if isinstance(configured, str):
        configured = configured.split(',')
    return {company_group(item) for item in configured if isinstance(item, str) and item.strip()}


OWNER_GO = ('fill now', 'fill', 'apply', 'refill')
GATE_ALIASES = {'government_required': 'government_role_required', 'required_years': 'professional_years_required',
                'years_required': 'professional_years_required'}


def owner_go(job):
    """The owner's own go-ahead: 'fill now' in the Jobs 'Your call' or Today 'Mark' column.

    Status 'Ready to fill' alone is not enough: the tab sweep also sets it when a
    filled form tab disappears, and those rows must keep the normal age/score gates.
    """
    return str(job.get('your_call') or '').strip().lower() in OWNER_GO


def gate(job, policy, now, channel='form', manual=False, qualification_only=False):
    if not qualification_only and str(policy['paused']).lower() == 'yes': return 'paused'
    group = company_group(job.get('company', ''))
    if group in configured_companies(policy, 'held_companies'): return 'company on hold'
    if group in configured_companies(policy, 'manual_companies'): return 'company requires manual application'
    if (not qualification_only or job.get('status')=='Not eligible') and job.get('status') in ('Applied', 'Rejected by me', 'Not eligible', 'Stale'): return 'job is not actionable'
    if not qualification_only and str(job.get('your_call', '')).lower() in ('skip', 'no', 'reject'): return 'user skipped'
    # Cheap title/location gates are deterministic even if an extractor omits a flag.
    import sys
    from pathlib import Path
    folder=str(Path(__file__).resolve().parents[1]/'stopgap-tools')
    if folder not in sys.path: sys.path.insert(0,folder)
    from title_filter import decide
    title_decision=decide(job.get('title',''),job.get('location',''))
    if not title_decision['keep']: return title_decision['reason']
    evidence = dict(job.get('requirements') or {})
    # Matches written before 2026-11-13 used these names; the gate silently ignored them.
    for old, new in GATE_ALIASES.items():
        if new not in evidence and old in evidence: evidence[new] = evidence[old]
    for name in ('outside_us', 'citizenship_required', 'clearance_required', 'government_role_required', 'closed', 'reposted', 'senior', 'internship'):
        if evidence.get(name) is True: return name
    # Batch matching labels the posting's role; "other" (e.g. mechanical, CAD-only) is never a target role.
    if str(evidence.get('role') or '').lower() == 'other': return 'not a target role (posting)'
    try: years = float(evidence.get('professional_years_required', 0) or 0)
    except (TypeError, ValueError): years = 0
    if years > 3: return 'more than 3 required years'
    text = str(job.get('notes', ''))
    if not qualification_only and re.search(r'submit result unknown|submitting \d{4}-|one-click sending \d{4}-', text, re.I): return 'legacy attempt needs reconciliation'
    if re.search(r'\brepost(?:ed)?\b|no longer accepting|posting closed', text, re.I): return 'closed or reposted'
    ready = owner_go(job) or manual
    if not ready and float(job.get('score') or 0) < policy['score_floor']: return 'score under 35'
    if not ready and (evidence.get('role') == 'pm' or title_decision['role']=='pm'): return 'PM track only; fill now required'
    posted = posted_time(job)
    if posted is None and not ready: return 'first posted time missing'
    hours = policy['one_click_max_age_hours'] if channel == 'one_click' else policy['max_posting_age_hours']
    if posted and not ready and (now - posted).total_seconds() > hours * 3600: return 'posting outside freshness window'
    if posted and (posted - now).total_seconds() > 300:
        date_only_today = str(job.get('posted_at', '')).strip() == now.astimezone(ET).date().isoformat()
        if not date_only_today: return 'first posted time is in the future'
    if not qualification_only and not ready and policy['fill_mode'] == 'windows' and now.astimezone(ET).hour not in [int(x) for x in str(policy['fill_windows']).split(',')]: return 'outside fill window'
    return ''
