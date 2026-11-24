"""Fence the first five live openings and audit their durable receipts before expansion."""
from contextlib import contextmanager
import json
import sqlite3

from .policy import timestamp
from .store import Denied


ROLLOUT_STATE_NAME = 'submission_canary_v1'
LIMIT = 5


class UnsafeAttempt(Denied):
    """An unsafe attempt stops admissions without rolling back the tracker."""


def state(store):
    row = store.con.execute('SELECT value FROM jr_meta WHERE key=?', (ROLLOUT_STATE_NAME,)).fetchone()
    return json.loads(row[0]) if row else {'phase': 'waiting', 'jobs': {}}


def save(store, current):
    store.con.execute('INSERT OR REPLACE INTO jr_meta VALUES (?,?)', (ROLLOUT_STATE_NAME, json.dumps(current)))


@contextmanager
def transaction(store):
    problem = None
    with store.tx():
        store.con.execute('SAVEPOINT submission_write')
        try:
            yield
        except UnsafeAttempt as error:
            store.con.execute('ROLLBACK TO submission_write')
            current = state(store)
            if current['phase'] in ('active', 'expanded'):
                current.update(phase='halted', why=str(error), stopped_at=store.now())
                save(store, current)
            problem = error
        finally:
            store.con.execute('RELEASE submission_write')
    if problem:
        raise problem


def rollout_ready(store):
    path=store.path.parent/'codex-runtime/rollout-readiness.json'
    if not path.exists(): return False
    try: evidence=json.loads(path.read_text())
    except (OSError,ValueError): return False
    return all(evidence.get(k) is True for k in ('form_parity','exact_history','registration_replay'))


def assert_owner(store):
    owner = store.con.execute("SELECT value FROM jr_meta WHERE key='production_owner'").fetchone()
    if not owner or owner[0] != 'codex' or (store.path.parent / 'scratch/run.lock').exists():
        raise Denied('production cutover requires one exclusive Codex worker')
    path = store.path.parent / 'codex-migration/worker-location.json'
    if path.exists() and json.loads(path.read_text()).get('production_owner') != 'codex':
        raise Denied('worker-location still assigns production to another worker')


def _admit(store, key, run_id):
    assert_owner(store)
    current = state(store)
    from .opening_history import guard
    guard(store,store.job(key),run_id)
    phase = current['phase']
    if phase == 'waiting':
        if not rollout_ready(store):
            raise Denied('production cutover awaits grouped form, exact-history and portal tests')
        current.update(phase='active', started_at=store.now())
    if phase == 'halted':
        raise Denied('submissions halted: ' + current.get('why', 'canary audit failed'))
    if store.con.execute("SELECT 1 FROM jr_attempts WHERE state='submit_started' LIMIT 1").fetchone():
        raise Denied('reconcile the preceding application click before any new submission')
    if phase == 'expanded':
        return current
    jobs = current['jobs']
    if any(item.get('attempt') and not item.get('confirmed_at') for item in jobs.values()):
        raise Denied('reconcile the preceding canary click before admitting another opening')
    if key not in jobs:
        identity=store.exact_identity(store.job(key)['link'])
        if any(store.exact_identity(store.job(other)['link'])==identity for other in jobs):
            raise Denied('use the enrolled tracker record for this exact opening')
        if len(jobs) >= LIMIT:
            raise Denied('five-opening canary full; audit before expansion')
        jobs[key] = {'enrolled_at':store.now(), 'run_id':run_id}
        save(store,current)
    return current


def release_expired(store, owner=None, token=None):
    """Retire unused prior-day places without deleting drafts or touching uncertain actions."""
    from .policy import ET, timestamp
    if not store.codex_production():
        return []
    with store.tx():
        if owner is not None:
            store.check(owner, token)
        elif store.con.execute("SELECT 1 FROM jr_leases WHERE name='worker' AND owner<>''").fetchone() or store.con.execute(
                "SELECT 1 FROM jr_runs WHERE status='running'").fetchone():
            return []
        cohort = state(store)
        if cohort['phase'] != 'active':
            return []
        held = {store.exact_identity(row[0]) for row in store.con.execute('SELECT identity FROM jr_attempts')}
        for row in store.con.execute("SELECT key,value FROM jr_meta WHERE key IN ('portal_autofill','portal_last_autofill','portal_verification') OR "
                                     "key LIKE 'account-access:%' OR key LIKE 'combined-registration:%'"):
            item = json.loads(row['value'])
            if item.get('state') == 'registration_pending' or (
                    row['key'] in ('portal_autofill','portal_last_autofill','portal_verification') and item.get('expires',0) > store.clock().timestamp()):
                if item.get('job_key'):
                    held.add(store.exact_identity(store.job(item['job_key'])['link']))
        released = []
        today = store.clock().astimezone(ET).date()
        for key, item in list(cohort['jobs'].items()):
            job = store.job(key)
            first = timestamp(store.employer_date(key))
            if not first or first.astimezone(ET).date() >= today or item.get('attempt') or store.exact_identity(job['link']) in held:
                continue
            try:
                if store.already_applied(job):
                    continue
            except Denied:
                continue
            cohort.setdefault('retired', {})[key] = dict(item, released_at=store.now(), reason='posting predates this Eastern day')
            del cohort['jobs'][key]
            released.append(key)
        if released:
            save(store, cohort)
        return released


def enroll(store, q):
    with store.tx():
        store.check(q['owner'],q['token'])
        key=q['job_key']
        row=store.con.execute('SELECT owner,token FROM jr_runs WHERE id=? AND status=?',(q['run_id'],'running')).fetchone()
        if not row or tuple(row)!=(q['owner'],q['token']):
            raise Denied('active run required for enrollment')
        job=store.job(key)
        reason=store.application_gate(job,store.policy_snapshot(),q.get('channel','form'))
        if reason or not job.get('resume') or store.already_applied(job):
            raise Denied('opening is ineligible for the five-job canary')
        current=_admit(store,key,q['run_id'])
    return {'phase':current['phase'],'enrolled':len(current['jobs']),'job_key':key}


def register(store, attempt):
    """Called in the click transaction. The existing worker lease serializes writers."""
    current = _admit(store, attempt['job_key'], attempt['run_id'])
    if current['phase'] == 'expanded':
        return
    item = current['jobs'][attempt['job_key']]
    if item.get('attempt'):
        raise UnsafeAttempt('duplicate submission attempt during the five-opening canary')
    item.update(attempt=attempt['id'],run_id=attempt['run_id'],marker_at=store.now())
    save(store,current)


def record(store, attempt, outcome, evidence):
    current = state(store)
    item = current['jobs'].get(attempt['job_key'])
    if not item or item.get('attempt') != attempt['id']:
        if current['phase']=='expanded' and outcome=='unknown':
            current.update(phase='halted',why='application click has no verified receipt',stopped_at=store.now())
            save(store,current)
        return
    clicked, marked = timestamp(evidence.get('clicked_at')), timestamp(item.get('marker_at'))
    if clicked and marked and marked <= clicked <= store.clock():
        item['clicked_at']=clicked.isoformat()
    if outcome != 'confirmed':
        if current['phase'] == 'active':
            current.update(phase='halted', why='canary click has no verified receipt', stopped_at=store.now())
    elif not item.get('confirmed_at'):
        item['confirmed_at'] = store.now()
    save(store, current)


def audit(store):
    from .quota import Quota
    from .receipts import validate
    current = state(store)
    jobs = current['jobs']
    rows, problems, reservations = [], [], set()
    if len(jobs) != LIMIT:
        problems.append(f'{len(jobs)} of {LIMIT} openings entered')
    for key, item in jobs.items():
        row = store.con.execute('SELECT state,evidence FROM jr_attempts WHERE id=?', (item.get('attempt'),)).fetchone()
        try:
            if not row or row['state'] != 'confirmed' or not item.get('confirmed_at'):
                raise ValueError()
            validate(store.job(key), json.loads(row['evidence']))
            apps = store.con.execute('SELECT submitted_by FROM applications WHERE job_key=? AND source_id=?',
                                     (key, item['attempt'])).fetchall()
            if len(apps) != 1 or apps[0][0] != 'task':
                raise ValueError()
        except (TypeError, ValueError):
            problems.append('unverified receipt for ' + key)
        # A canary cannot expand with an unexplained handoff on an enrolled opening.
        handoff = store.con.execute("SELECT 1 FROM jr_backlog WHERE provider='application_handoff' AND item_key=? AND state<>'resolved'", (key,)).fetchone()
        if handoff:
            problems.append('unreviewed handoff for ' + key)
        start = timestamp(item['enrolled_at'])
        confirmation=timestamp(item.get('confirmed_at'))
        click=timestamp(item.get('clicked_at'))
        if not start or not click or click < start:
            problems.append('missing time to submission for ' + key)
        rows.append({'job_key': key, 'seconds_to_submit':(click-start).total_seconds() if click and start else None,
                     'seconds_to_receipt':(confirmation-start).total_seconds() if confirmation and start else None,
                     'receipt_verified': not any(key in p for p in problems)})
        reservation = store.con.execute('SELECT value FROM jr_meta WHERE key=?', ('quota:' + item['run_id'],)).fetchone()
        if reservation:
            reservations.add(reservation[0])
        else:
            problems.append('missing quota reservation for ' + key)
    pending_accounts = []
    for row in store.con.execute("SELECT value FROM jr_meta WHERE key LIKE 'account-access:%' OR key LIKE 'combined-registration:%'"):
        account = json.loads(row[0])
        if account.get('state')=='registration_pending':
            pending_accounts.append({'job_key':account['job_key'],'portal':account['portal']})
            if account['job_key'] in jobs:
                problems.append('pending account registration for ' + account['job_key'])
    charged = observed = 0
    try:
        if reservations:
            path = Quota(store.path.parent).path
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as usage:
                for rid in reservations:
                    result = usage.execute("SELECT charged,observed FROM reservations WHERE id=? AND status='settled' AND consumer='application'", (rid,)).fetchone()
                    if not result or result[1] is None:
                        raise ValueError()
                    charged += result[0]
                    observed += result[1]
    except (sqlite3.Error, ValueError):
        problems.append('worker quota settlement incomplete')
    return {'phase': current['phase'], 'jobs': rows, 'pending_accounts':pending_accounts, 'charged_points': charged, 'observed_points': observed,
            'problems': problems, 'can_expand': current['phase'] == 'active' and not problems}


def expand(store, owner, token):
    with store.tx():
        store.check(owner, token)
        assert_owner(store)
        report = audit(store)
        if not report['can_expand'] or not rollout_ready(store):
            raise Denied('five-job receipt, handoff, timing, quota and routing checks must pass before expansion')
        current = state(store)
        current.update(phase='expanded', audit=report, expanded_at=store.now())
        save(store, current)
    return report


def expand_settled(store):
    """The fifth receipt settles after finish releases the lease. Audit under an idle write lock."""
    if not store.codex_production():
        return False
    with store.tx():
        current = state(store)
        if (current['phase'] != 'active' or len(current['jobs']) != LIMIT
                or any(not row.get('confirmed_at') for row in current['jobs'].values())
                or store.con.execute("SELECT 1 FROM jr_leases WHERE name='worker' AND owner<>''").fetchone()
                or store.con.execute("SELECT 1 FROM jr_runs WHERE status='running'").fetchone()
                or not rollout_ready(store)):
            return False
        assert_owner(store)
        report = audit(store)
        if report['can_expand']:
            current.update(phase='expanded',audit=report,expanded_at=store.now())
            save(store,current)
            return True
        return False
