"""Additive SQLite state. Never rekeys legacy records or steals an expired lease."""
import contextlib
import datetime as dt
import json
import re
import sqlite3
import uuid
from pathlib import Path
from .policy import ET, UTC, opening_identity, history_identity, digest, effective, gate, owner_go, timestamp

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jr_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, token INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jr_runs (id TEXT PRIMARY KEY, slot TEXT UNIQUE NOT NULL, mode TEXT NOT NULL, owner TEXT NOT NULL, token INTEGER NOT NULL, policy TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL, expected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_actions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL, input TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL, result TEXT, completed TEXT);
CREATE TABLE IF NOT EXISTS jr_attempts (id TEXT PRIMARY KEY, job_key TEXT NOT NULL, identity TEXT NOT NULL, run_id TEXT NOT NULL, company_key TEXT NOT NULL, channel TEXT NOT NULL, day TEXT NOT NULL, resume_hash TEXT NOT NULL, policy_hash TEXT NOT NULL, state TEXT NOT NULL, review TEXT NOT NULL, evidence TEXT, updated TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS jr_attempt_one_job ON jr_attempts(job_key) WHERE state <> 'not_submitted';
CREATE UNIQUE INDEX IF NOT EXISTS jr_attempt_one_identity ON jr_attempts(identity) WHERE state <> 'not_submitted';
CREATE TABLE IF NOT EXISTS jr_cursors (provider TEXT PRIMARY KEY, value TEXT NOT NULL, succeeded TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jr_publications(identity TEXT PRIMARY KEY, proof TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_opening_alias(link TEXT PRIMARY KEY, original TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_shortlist (
    day TEXT NOT NULL, identity TEXT NOT NULL, job_key TEXT, company TEXT NOT NULL, title TEXT NOT NULL,
    link TEXT NOT NULL, publication TEXT, stage TEXT NOT NULL, reason TEXT NOT NULL, observed TEXT NOT NULL,
    PRIMARY KEY(day,identity));
CREATE TABLE IF NOT EXISTS jr_learning_observations (
    run_id TEXT NOT NULL, issue TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, next_check TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY(run_id,issue,subject));
CREATE INDEX IF NOT EXISTS jr_learning_issue_subject ON jr_learning_observations(issue,subject,at);
CREATE TABLE IF NOT EXISTS jr_learning_checks (
    issue TEXT NOT NULL, subject TEXT NOT NULL, at TEXT NOT NULL, result TEXT NOT NULL, proof TEXT NOT NULL,
    PRIMARY KEY(issue,subject,at));
CREATE TABLE IF NOT EXISTS jr_learning_trials (
    run_id TEXT PRIMARY KEY, issue TEXT NOT NULL, at TEXT NOT NULL, test_file TEXT NOT NULL,
    change_note TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_messages (provider TEXT NOT NULL, message_id TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(provider,message_id));
CREATE TABLE IF NOT EXISTS jr_imports (provider TEXT NOT NULL, record_id TEXT NOT NULL, job_key TEXT NOT NULL, PRIMARY KEY(provider,record_id));
CREATE TABLE IF NOT EXISTS jr_commands (tab TEXT NOT NULL, record_key TEXT NOT NULL, command TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(tab,record_key));
CREATE TABLE IF NOT EXISTS jr_settings (id INTEGER PRIMARY KEY CHECK(id=1), packet TEXT NOT NULL, observed TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_outbox (id TEXT PRIMARY KEY, dedup TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL, delivered TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS jr_usage (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, model TEXT, effort TEXT, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, duration_ms REAL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jr_tabs (tab_id TEXT PRIMARY KEY, job_key TEXT, owned INTEGER NOT NULL DEFAULT 0, persistent INTEGER NOT NULL DEFAULT 1, reason TEXT);
CREATE TABLE IF NOT EXISTS jr_backlog (provider TEXT NOT NULL, item_key TEXT NOT NULL, payload TEXT NOT NULL, first_seen TEXT NOT NULL, retry_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, PRIMARY KEY(provider,item_key));
'''

class Denied(RuntimeError): pass

class Store:
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.con = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA foreign_keys=ON')
        self.con.execute('PRAGMA busy_timeout=10000')
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone(): raise Denied('existing tracker database required')
        self.con.executescript(SCHEMA)
        self.con.execute("INSERT OR IGNORE INTO jr_meta VALUES('schema_version','1')")
        self.con.execute("INSERT OR IGNORE INTO jr_meta VALUES('production_owner','legacy')")
        from .adapters import index_legacy_workday_dates
        index_legacy_workday_dates(self)

    def now(self): return self.clock().isoformat()

    @contextlib.contextmanager
    def tx(self):
        self.con.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.con.execute('COMMIT')
        except BaseException:
            self.con.execute('ROLLBACK')
            raise

    def acquire(self, owner, seconds=1800):
        with self.tx():
            old = self.con.execute("SELECT * FROM jr_leases WHERE name='worker'").fetchone()
            if old and old['owner']: raise Denied('worker lease occupied; verify stopped worker before explicit recovery')
            token = old['token'] + 1 if old else 1
            self.con.execute("INSERT OR REPLACE INTO jr_leases VALUES('worker',?,?,?)", (owner,token,self.clock().timestamp()+seconds))
        return token

    def check(self, owner, token):
        lease = self.con.execute("SELECT * FROM jr_leases WHERE name='worker'").fetchone()
        if not lease or (lease['owner'],lease['token']) != (owner,token) or lease['expires'] < self.clock().timestamp(): raise Denied('missing, expired or fenced worker lease')

    def heartbeat(self, owner, token, seconds=1800):
        with self.tx():
            self.check(owner,token)
            self.con.execute("UPDATE jr_leases SET expires=? WHERE name='worker'", (self.clock().timestamp()+seconds,))

    def release(self, owner, token):
        with self.tx():
            self.check(owner,token)
            self.con.execute("UPDATE jr_leases SET owner='',expires=0 WHERE name='worker'")

    def recover_stopped(self, stopped_evidence):
        if not stopped_evidence: raise Denied('stopped-worker evidence required')
        with self.tx():
            self.con.execute("UPDATE jr_leases SET owner='',token=token+1,expires=0 WHERE name='worker'")
            self.con.execute("UPDATE jr_runs SET status='interrupted',ended=? WHERE status='running'",(self.now(),))
            self.con.execute('INSERT OR REPLACE INTO jr_meta VALUES(?,?)',('last_recovery',json.dumps(stopped_evidence)))

    def start(self, owner, token, policy, now=None):
        when = (now or self.clock()).astimezone(ET)
        if when.hour < 6: raise Denied('outside 06:00–23:59 ET')
        if self.con.execute("SELECT value FROM jr_meta WHERE key='production_owner'").fetchone()[0]=='codex':
            current=self.latest_policy()
            if current['hash']!=policy.get('hash'):raise Denied('start policy differs from fresh imported commands')
        boundary=when.replace(minute=when.minute//20*20,second=0,microsecond=0)
        upcoming=boundary+dt.timedelta(minutes=20)
        # The desktop scheduler applies an early/late offset to nominal times.
        # Map an observed start within three minutes early to that nominal slot.
        if 0<(upcoming-when).total_seconds()<=180 and upcoming.hour>=6:
            boundary=upcoming
        slot=boundary.isoformat()
        mode = 'FULL' if boundary.minute == 0 else 'FAST'
        # FULL = FAST + email intake + public feeds (the automation prompt's wording). Since 2026-11-13 the schedule is
        # one FULL run every 2 hours (one Plus account's weekly allowance holds about 5 productive runs a day), so
        # LinkedIn/Handshake discovery has to be part of FULL or it would never run (the local usage report).
        expected = ['commands','resume_sync','intake','feeds','handshake','linkedin','plan','match','one_click','fill','audit','mirror'] if mode == 'FULL' else ['commands','resume_sync','handshake','linkedin','plan','match','one_click','fill','audit','mirror']
        if mode == 'FULL' and boundary.hour == 21: expected.insert(-2,'recruiters')
        rid = str(uuid.uuid4())
        with self.tx():
            self.check(owner,token)
            self.con.execute('INSERT INTO jr_runs VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,slot,mode,owner,token,json.dumps(policy),self.now(),None,'running',json.dumps(expected)))
        from .operations import publish_started
        publish_started(self, rid)
        return dict(run_id=rid,slot=slot,mode=mode,expected=expected)

    def resume_empty_run(self, run_id, owner, token, policy, evidence):
        """Retry a stopped setup-only run without deleting its failed history."""
        with self.tx():
            self.check(owner,token)
            row=self.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
            if not row or row['status'] not in ('partial','interrupted') or not row['ended']:
                raise Denied('only a finished setup-only run can resume')
            if not evidence.get('refs') or evidence.get('stopped_owner')!=row['owner']:
                raise Denied('exact stopped-worker evidence required')
            if self.con.execute('SELECT 1 FROM jr_actions WHERE run_id=?',(run_id,)).fetchone() or self.con.execute('SELECT 1 FROM jr_attempts WHERE run_id=?',(run_id,)).fetchone():
                raise Denied('run has work; preserve its receipts and use the next slot')
            if self.latest_policy()['hash']!=policy.get('hash'):raise Denied('fresh resume policy required')
            self.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',
                (self.now(),'codex-empty-run-resumed',run_id,json.dumps({'previous':dict(row),'stopped_evidence':evidence})))
            self.con.execute("UPDATE jr_runs SET owner=?,token=?,policy=?,started=?,ended=NULL,status='running' WHERE id=?",
                (owner,token,json.dumps(policy),self.now(),run_id))
        from .operations import publish_started
        publish_started(self,run_id)
        return dict(run_id=run_id,slot=row['slot'],mode=row['mode'],expected=json.loads(row['expected']),resumed=True)

    def action(self, run, stage, kind, payload, key):
        if kind not in ('browser','drive','gmail','model','deterministic'): raise ValueError('unknown action kind')
        rid = run['run_id'] if isinstance(run,dict) else run
        r = self.con.execute('SELECT * FROM jr_runs WHERE id=?',(rid,)).fetchone()
        if not r: raise Denied('run missing')
        with self.tx():
            self.check(r['owner'],r['token'])
            old = self.con.execute('SELECT * FROM jr_actions WHERE idempotency_key=?',(key,)).fetchone()
            if old:
                if old['input'] != json.dumps(payload,sort_keys=True) or old['run_id'] != rid: raise Denied('idempotency key collision')
                return dict(old)
            aid = str(uuid.uuid4())
            self.con.execute('INSERT INTO jr_actions VALUES(?,?,?,?,?,?,?,?,?)',(aid,rid,stage,kind,json.dumps(payload,sort_keys=True),key,'pending',None,None))
        return dict(id=aid,run_id=rid,stage=stage,kind=kind,input=payload,idempotency_key=key)

    def receipt(self, owner, token, action_id, status, evidence, cursor=None):
        if status not in ('succeeded','failed','handoff','deferred'): raise ValueError('unknown receipt status')
        if not isinstance(evidence,dict) or not evidence.get('refs'): raise Denied('evidence references required')
        with self.tx():
            self.check(owner,token)
            a = self.con.execute('SELECT * FROM jr_actions WHERE id=?',(action_id,)).fetchone()
            if not a: raise Denied('unknown action')
            r = self.con.execute('SELECT * FROM jr_runs WHERE id=?',(a['run_id'],)).fetchone()
            if (r['owner'],r['token']) != (owner,token): raise Denied('receipt from wrong run owner')
            if a['status'] != 'pending':
                if a['status']==status and json.loads(a['result'])==evidence: return
                raise Denied('action already completed with a different receipt')
            self.con.execute('UPDATE jr_actions SET status=?,result=?,completed=? WHERE id=?',(status,json.dumps(evidence),self.now(),action_id))
            from . import learning
            learning.stage(self,a['run_id'],a['stage'],status,evidence)
            if cursor is not None and status == 'succeeded': self.con.execute('INSERT OR REPLACE INTO jr_cursors VALUES(?,?,?)',(a['stage'],json.dumps(cursor),self.now()))

    def audit(self, run_id):
        r = self.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
        rows = self.con.execute('SELECT * FROM jr_actions WHERE run_id=?',(run_id,)).fetchall()
        done = {a['stage'] for a in rows if a['status']=='succeeded'}
        missing = sorted(set(json.loads(r['expected'])) - done)
        errors = [a['id'] for a in rows if a['status'] not in ('succeeded','handoff')]
        unknown_attempts=self.con.execute("SELECT job_key,identity FROM jr_attempts WHERE state='submit_started'").fetchall()
        holds=self.legacy_unknown_holds()
        unknown=len({r['identity'] or r['job_key'] for r in unknown_attempts} |
                    {r['identity'] or r['job_key'] for r in holds})
        attempts=self.con.execute("SELECT * FROM jr_attempts WHERE run_id=? AND state='confirmed'",(run_id,)).fetchall()
        inconsistent=[];sent_by_worker=0;reconciled_prior=0
        for attempt in attempts:
            app=self.con.execute('SELECT * FROM applications WHERE job_key=? AND source_id=?',(attempt['job_key'],attempt['id'])).fetchone()
            if not app:inconsistent.append(attempt['id'])
            elif app['submitted_by']=='task':sent_by_worker+=1
            else:reconciled_prior+=1
        reconciled_prior += self.con.execute("""SELECT COUNT(DISTINCT e.key) FROM events e
            JOIN applications a ON a.job_key=e.key AND a.submitted_by='unattributed' AND a.source_id LIKE 'portal:%'
            WHERE e.kind IN ('portal-submission-reconciled','portal-reconciliation-linked')
            AND json_extract(e.detail,'$.run_id')=?""",(run_id,)).fetchone()[0]
        for action in rows:
            result=json.loads(action['result']) if action['result'] else {}
            claimed=result.get('confirmed_submissions')
            if claimed is not None and int(claimed)>sent_by_worker:inconsistent.append(action['id'])
        return dict(success=not (missing or errors or unknown or inconsistent),missing=missing,failed_or_pending=errors,
                    unknown_submissions=unknown,legacy_unknown_submissions=len(holds),confirmed_submissions=sent_by_worker,reconciled_prior_submissions=reconciled_prior,inconsistent_submission_receipts=inconsistent)

    def settings(self, packet, owner, token):
        observed = timestamp(packet['observed_at'])
        if observed is None: raise Denied('canonical observation time required')
        age=(self.clock()-observed).total_seconds()
        if age < -5 or age > 60: raise Denied('fresh connector settings/commands snapshot required (60 seconds)')
        if not packet.get('source_ref'): raise Denied('connector source reference required')
        if set(packet.get('commands',{})) != {'Today','Jobs','Recruiters','Companies'}: raise Denied('all editable tabs required, including FAST runs')
        p = effective(packet['settings'],packet.get('overrides_hash',''))
        with self.tx():
            self.check(owner,token)
            self.con.execute('INSERT OR REPLACE INTO jr_settings VALUES(1,?,?,?)',(json.dumps(packet),observed.isoformat(),p['hash']))
        return p

    def latest_policy(self):
        row = self.con.execute('SELECT * FROM jr_settings WHERE id=1').fetchone()
        if not row or not 0 <= (self.clock()-timestamp(row['observed'])).total_seconds() <= 60: raise Denied('re-read current Settings and editable tabs before submit')
        packet=json.loads(row['packet'])
        return effective(packet['settings'],packet.get('overrides_hash',''))

    def policy_snapshot(self, max_age=2700):
        """Policy from this run's command import, for planning, matching and discovery.

        Only a final submission needs the 60-second `latest_policy()`; submit_started still demands it, so
        a pause or setting change applies before any click. Planning and matching are not submissions:
        needing a fresh sheet read for each of them produced a "stale policy" error in nearly every run.
        """
        row = self.con.execute('SELECT * FROM jr_settings WHERE id=1').fetchone()
        if not row or not 0 <= (self.clock()-timestamp(row['observed'])).total_seconds() <= max_age: raise Denied('re-read current Settings and editable tabs before planning')
        packet=json.loads(row['packet'])
        return effective(packet['settings'],packet.get('overrides_hash',''))

    def job(self, key):
        r=self.con.execute('SELECT * FROM jobs WHERE job_key=?',(key,)).fetchone()
        if not r: raise Denied('job missing')
        return dict(r)

    def codex_production(self):
        row = self.con.execute("SELECT value FROM jr_meta WHERE key='production_owner'").fetchone()
        return bool(row and row[0] == 'codex')

    def exact_identity(self,link):
        row = self.con.execute('SELECT original FROM jr_opening_alias WHERE link=?',(opening_identity(link),)).fetchone()
        return history_identity(row[0] if row else link)

    def employer_date(self, key):
        from .adapters import original_date_source, earlier_publication
        job = self.job(key)
        identity = json.dumps(self.exact_identity(job['link']),ensure_ascii=False,separators=(',',':'))
        public = self.con.execute('SELECT proof FROM jr_publications WHERE identity=?',(identity,)).fetchone()
        first = json.loads(public[0]) if public else None
        if first and not first.get('verified_original'):
            alias = self.con.execute('SELECT original FROM jr_opening_alias WHERE link=?',(opening_identity(job['link']),)).fetchone()
            original_job = dict(job,link=alias[0]) if alias else job
            if not original_date_source(self,original_job,first['field'],first['url']):
                first=None
        row = self.con.execute('SELECT value FROM jr_cache WHERE key=?', ('first-employer-date:' + key,)).fetchone()
        if row:
            proof=json.loads(row[0])
            if proof.get('verified_original') or original_date_source(self,job,proof.get('field'),proof.get('url')):
                first = earlier_publication(first,proof)
        return first['value'] if first else None

    def application_gate(self, job, policy, channel='form', *, qualification_only=False):
        if self.codex_production():
            from .policy import publication_hold
            original = self.employer_date(job['job_key'])
            reason = publication_hold(original, self.clock())
            if reason:
                return reason
            identity = json.dumps(self.exact_identity(job['link']), ensure_ascii=False, separators=(',', ':'))
            hold = self.con.execute("SELECT reason FROM jr_shortlist WHERE day=? AND identity=? AND stage='excluded'",
                                    (self.clock().astimezone(ET).date().isoformat(), identity)).fetchone()
            if hold:
                return hold['reason']
            job = dict(job, posted_at=original)
        return gate(job, policy, self.clock(), channel,qualification_only=qualification_only)

    def legacy_unknown_holds(self):
        jobs=[dict(r) for r in self.con.execute('SELECT job_key,link,company_key FROM jobs')]
        holds={}
        for row in self.con.execute("SELECT * FROM jr_backlog WHERE provider='legacy_unknown_submission' AND state<>'resolved'"):
            try: data=json.loads(row['payload'])
            except (ValueError,TypeError): data={}
            if not isinstance(data,dict):data={}
            key=data.get('job_key') or row['item_key']; identity=None
            for link in (data.get('link'),data.get('identity'),row['item_key']):
                try:
                    if link:identity=opening_identity(link);break
                except ValueError:pass
            job=next((j for j in jobs if j['job_key']==key),None)
            if not job and identity:
                for candidate in jobs:
                    try:
                        if self.exact_identity(candidate['link'])==self.exact_identity(identity):job=candidate;break
                    except ValueError:pass
            if job:
                key=job['job_key'];identity=opening_identity(job['link'])
            when=timestamp(row['first_seen'])
            day=data.get('day') or (when.astimezone(ET).date().isoformat() if when else None)
            holds[identity or key]=dict(job_key=key,identity=identity,day=day,
                channel=data.get('channel'),company_key=data.get('company_key') or (job or {}).get('company_key'))
        return list(holds.values())

    def already_applied(self,job):
        if self.con.execute('SELECT 1 FROM applications WHERE job_key=?',(job['job_key'],)).fetchone():return True
        identity=opening_identity(job['link'])
        for row in self.con.execute("SELECT link FROM applications UNION SELECT link FROM jobs WHERE status='Applied'"):
            if not row['link']:continue
            try:
                if self.exact_identity(row['link'])==self.exact_identity(identity):return True
            except ValueError:continue
        # A stopped legacy worker may have submitted without leaving a receipt.
        # Keep that uncertainty distinct from a confirmed duplicate.
        for row in self.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='legacy_unknown_submission' AND state<>'resolved'"):
            if row['item_key']==job['job_key']:raise Denied('reconcile legacy outcome before retry')
            try:
                payload=json.loads(row['payload'])
            except (ValueError,TypeError):
                payload={}
            if not isinstance(payload,dict):payload={}
            if payload.get('job_key')==job['job_key']:raise Denied('reconcile legacy outcome before retry')
            for link in (row['item_key'],payload.get('link'),payload.get('identity')):
                if not link:continue
                try:
                    if self.exact_identity(link)==self.exact_identity(identity):raise Denied('reconcile legacy outcome before retry')
                except ValueError:continue
        # Unidentified confirmation for this company is ambiguous, never permission to submit.
        for row in self.con.execute("SELECT title,applied_at FROM applications WHERE company_key=?",(job['company_key'],)):
            if str(row['title'] or '').strip() in ('','?'):
                when=timestamp(row['applied_at'])
                if when and (self.clock()-when).total_seconds()<=72*3600:raise Denied('unidentified recent application needs reconciliation')
        return False

    def resolve_legacy_holds(self, job_key, evidence):
        """Called inside a fenced reconciliation transaction; never clear unrelated holds."""
        identity=opening_identity(self.job(job_key)['link'])
        for row in self.con.execute("SELECT item_key,payload FROM jr_backlog WHERE provider='legacy_unknown_submission' AND state<>'resolved'").fetchall():
            try: payload=json.loads(row['payload'])
            except (ValueError,TypeError): payload={}
            if not isinstance(payload,dict):payload={}
            match=row['item_key']==job_key or payload.get('job_key')==job_key
            for link in (row['item_key'],payload.get('link'),payload.get('identity')):
                try:match=match or bool(link and self.exact_identity(link)==self.exact_identity(identity))
                except ValueError:pass
            if match:
                payload['resolution']=evidence
                self.con.execute("UPDATE jr_backlog SET state='resolved',payload=? WHERE provider='legacy_unknown_submission' AND item_key=?",(json.dumps(payload),row['item_key']))

    def prepare(self, owner, token, run_id, key, channel, resume_hash, review):
        if channel not in ('form','one_click'): raise ValueError('unknown application channel')
        if not isinstance(resume_hash,str) or not re.fullmatch('[a-f0-9]{64}',resume_hash): raise Denied('reviewed PDF SHA-256 required')
        if not review.get('refs') or not review.get('requirements_verified'): raise Denied('review evidence required')
        from . import canary
        with canary.transaction(self):
            self.check(owner,token)
            r=self.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
            if not r or (r['owner'],r['token'])!=(owner,token): raise Denied('wrong run owner')
            rollout=canary.state(self)
            if rollout['phase']=='active' and rollout['jobs'].get(key,{}).get('attempt'):
                raise canary.UnsafeAttempt('duplicate preparation for a canary opening after its click marker')
            p=self.latest_policy(); j=self.job(key); j['requirements']=review.get('requirements',{})
            self.verify_pdf(j,resume_hash)
            why=self.application_gate(j,p,channel)
            if why: raise Denied(why)
            if self.already_applied(j):
                if rollout['phase'] in ('active','expanded'):
                    raise canary.UnsafeAttempt('duplicate preparation for an already applied opening')
                raise Denied('already applied')
            identity = opening_identity(j['link'])
            for other in self.con.execute("SELECT job_key,identity FROM jr_attempts WHERE job_key<>? AND state<>'not_submitted'", (key,)):
                if self.exact_identity(other['identity']) == self.exact_identity(identity):
                    raise canary.UnsafeAttempt('duplicate attempt on another URL for this exact opening')
            day=self.clock().astimezone(ET).date().isoformat(); self._budget(j,p,day,channel,run_id)
            aid=str(uuid.uuid4())
            self.con.execute('INSERT INTO jr_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,key,identity,run_id,j['company_key'],channel,day,resume_hash,p['hash'],'prepared',json.dumps(review),None,self.now()))
            return aid

    def _budget(self,j,p,day,channel,run_id,exclude=''):
        legacy=self.con.execute("SELECT * FROM applications WHERE submitted_by='task' AND applied_at LIKE ?",(day+'%',)).fetchall()
        one={a['job_key'] for a in legacy if any(s in (a['confirmed_via'] or '').lower() for s in ('easy apply','quick apply'))}
        forms={a['job_key'] for a in legacy if a['job_key'] not in one}
        for row in self.con.execute('SELECT job_key,notes FROM jobs'):
            if re.search(r'(filled|submitted) '+re.escape(day),row['notes'] or '') and row['job_key'] not in one: forms.add(row['job_key'])
        # Finishing an already-counted form consumes its existing slot, not a second one.
        one.discard(j['job_key']); forms.discard(j['job_key'])
        attempts=self.con.execute("SELECT * FROM jr_attempts WHERE day=? AND state<>'not_submitted' AND id<>?",(day,exclude)).fetchall()
        holds=[h for h in self.legacy_unknown_holds() if h['day'] in (None,day)]
        # Existing receipts and reservations already consume their slot.
        reserved={a['job_key'] for a in legacy}|{a['job_key'] for a in attempts}
        identities={a['identity'] for a in attempts}
        for a in legacy:
            try:identities.add(opening_identity(a['link']))
            except ValueError:pass
        holds=[h for h in holds if h['job_key'] not in reserved and h['identity'] not in identities]
        held={h['job_key'] for h in holds if h['channel'] not in ('form','one_click') or h['channel']==channel}
        used=(one if channel=='one_click' else forms)|{a['job_key'] for a in attempts if a['channel']==channel}|held
        if len(used)>=p['one_click_per_day' if channel=='one_click' else 'max_fills_per_day']: raise Denied('daily channel limit reached')
        mode=self.con.execute('SELECT mode FROM jr_runs WHERE id=?',(run_id,)).fetchone()['mode'] if run_id else 'FULL'
        cap=p['one_click_per_run'] if channel=='one_click' else p['fast_fills_per_run' if mode=='FAST' else 'fills_per_run']
        if sum(1 for a in attempts if a['run_id']==run_id and a['channel']==channel)>=cap: raise Denied('per-run channel limit reached')
        company={a['job_key'] for a in legacy if a['company_key']==j['company_key']}|{a['job_key'] for a in attempts if a['company_key']==j['company_key']}
        company|={row['job_key'] for row in self.con.execute('SELECT job_key FROM jobs WHERE company_key=?',(j['company_key'],)) if row['job_key'] in forms}
        company|={h['job_key'] for h in holds if h['company_key']==j['company_key']}
        company.discard(j['job_key'])
        if not owner_go(j) and len(company)>=2: raise Denied('company daily limit reached')

    def submit_started(self,owner,token,attempt_id,actual_hash,evidence,production=True):
        from . import canary
        with canary.transaction(self):
            self.check(owner,token)
            if production and (self.path.parent/'scratch'/'run.lock').exists():
                raise Denied('legacy worker lock present; verify stopped legacy worker before submission')
            a=self.con.execute('SELECT * FROM jr_attempts WHERE id=?',(attempt_id,)).fetchone()
            if not a: raise Denied('attempt missing')
            if a['state']!='prepared':
                raise canary.UnsafeAttempt('attempt needs reconciliation, not another click')
            if production and self.con.execute("SELECT value FROM jr_meta WHERE key='production_owner'").fetchone()[0]!='codex':
                grant=self.con.execute("SELECT value FROM jr_meta WHERE key='acceptance_grant'").fetchone()
                grant=json.loads(grant[0]) if grant else {}
                if grant.get('owner')!=owner or a['job_key'] not in grant.get('job_keys',[]) or grant.get('expires',0)<self.clock().timestamp():
                    raise Denied('production cutover not verified; legacy worker remains owner')
            r=self.con.execute('SELECT * FROM jr_runs WHERE id=?',(a['run_id'],)).fetchone()
            if (r['owner'],r['token'])!=(owner,token): raise Denied('wrong attempt owner')
            p=self.latest_policy(); j=self.job(a['job_key']); j['requirements']=json.loads(a['review']).get('requirements',{})
            self.verify_pdf(j,actual_hash,required=production)
            why=self.application_gate(j,p,a['channel'])
            if why: raise Denied(why)
            if actual_hash!=a['resume_hash']: raise Denied('attachment changed; review again')
            if not evidence.get('refs') or not evidence.get('attachment_verified') or not evidence.get('answers_verified'): raise Denied('attachment and answer evidence required')
            if production:
                from .answers import validate_answers, check_required_coverage
                from .form_step import inventory
                from .resumes import verify_attachment
                verify_attachment(self.path.parent,j,p.get('resume_only') or j['resume'],actual_hash,evidence.get('attachment'))
                pages=inventory(self,a['run_id'],j['job_key'])
                if not pages:
                    raise Denied('observe the application pages with form_step before final review')
                answer_rows=evidence.get('answer_evidence')
                check_required_coverage([field for page in pages.values() for field in page],answer_rows)
                if answer_rows:
                    validate_answers(self.path.parent,answer_rows,j)
            if evidence.get('required_unknown') or evidence.get('consent_pending') or evidence.get('auth_required') or evidence.get('captcha') or evidence.get('payment'): raise Denied('user handoff required')
            if self.already_applied(j): raise Denied('already applied')
            if self.clock().astimezone(ET).date().isoformat()!=a['day']: raise Denied('ET day changed; reconcile reservation before new attempt')
            self._budget(j,p,a['day'],a['channel'],a['run_id'],a['id'])
            now = self.clock().astimezone(ET)
            midnight = dt.datetime.combine(now.date()+dt.timedelta(days=1),dt.time(),ET)
            click_deadline = min(now+dt.timedelta(seconds=60),midnight)
            if production and (click_deadline-now).total_seconds()<5:
                raise Denied('insufficient time for a same-day final click')
            if production:
                from .account_flow import combined_registration_started
                canary.register(self,a)
                combined_registration_started(self,a,j,answer_rows)
            self.con.execute("UPDATE jr_attempts SET state='submit_started',policy_hash=?,evidence=?,updated=? WHERE id=?",(p['hash'],json.dumps(evidence),self.now(),attempt_id))
            # Keep the submitted answers: reconcile replaces `evidence`, and model evals audit what was answered and why.
            self.con.execute('INSERT INTO events(at,kind,key,detail) VALUES(?,?,?,?)',(self.now(),'submit-answers',attempt_id,json.dumps(
                [{k:(str(x.get(k))[:120] if k=='value' else x.get(k)) for k in ('field','kind','rule','meaning','value','source')}
                 for x in (evidence.get('answer_evidence') or []) if isinstance(x,dict)])))
        return {'attempt_id':attempt_id,'click_once':True,'click_before':click_deadline.isoformat(),'application_day':a['day']}

    def acceptance_grant(self,owner,token,job_keys,evidence,seconds=1800):
        if not evidence.get('refs') or not evidence.get('legacy_schedule_paused') or not evidence.get('legacy_stages_stopped'):
            raise Denied('exclusive acceptance requires observed stopped legacy worker and paused schedule')
        if not job_keys or seconds>1800:raise Denied('bounded acceptance job list and <=30 minutes required')
        with self.tx():
            self.check(owner,token)
            for key in job_keys:self.job(key)
            grant=dict(owner=owner,job_keys=job_keys,expires=self.clock().timestamp()+seconds,evidence=evidence)
            self.con.execute("INSERT OR REPLACE INTO jr_meta VALUES('acceptance_grant',?)",(json.dumps(grant),))
        return grant

    def end_acceptance(self,owner,token):
        with self.tx():
            self.check(owner,token)
            self.con.execute("DELETE FROM jr_meta WHERE key='acceptance_grant'")

    def verify_pdf(self,job,reviewed_hash,required=False,policy=None):
        library=self.path.parent/'my-resumes'; manifest=library/'manifest.json'
        if not manifest.exists():
            if required: raise Denied('resume manifest missing')
            return
        from .backup import file_hash
        data=json.loads(manifest.read_text())
        selected=(policy if policy is not None else self.latest_policy()).get('resume_only') or job.get('resume')
        entries={r['file']:r for r in data['resumes']}
        if selected not in entries or Path(selected).name!=selected: raise Denied('reviewed resume not in active manifest')
        actual=file_hash(library/selected)
        if actual!=reviewed_hash or actual!=entries[selected].get('sha256'): raise Denied('PDF bytes changed; revalidate attachment and manifest')
        if (library.parent/'resume-publish-journal.json').exists(): raise Denied('resume publication interrupted; recover first')

    def adopt_prepared(self,owner,token,attempt_id,run_id,evidence):
        if not evidence.get('refs') or not evidence.get('form_observed') or not evidence.get('answers_preserved'):raise Denied('observed existing form and preserved answers required')
        with self.tx():
            self.check(owner,token)
            a=self.con.execute('SELECT * FROM jr_attempts WHERE id=?',(attempt_id,)).fetchone()
            if not a or a['state']!='prepared':raise Denied('only an unsubmitted prepared form may be resumed')
            r=self.con.execute('SELECT * FROM jr_runs WHERE id=?',(run_id,)).fetchone()
            if not r or (r['owner'],r['token'])!=(owner,token):raise Denied('new run ownership required')
            j=self.job(a['job_key']);p=self.latest_policy();j['requirements']=json.loads(a['review']).get('requirements',{})
            why=self.application_gate(j,p,a['channel'])
            if why:raise Denied(why)
            self.verify_pdf(j,a['resume_hash'])
            day=self.clock().astimezone(ET).date().isoformat();self._budget(j,p,day,a['channel'],run_id,a['id'])
            self.con.execute('UPDATE jr_attempts SET run_id=?,day=?,policy_hash=?,updated=? WHERE id=?',(run_id,day,p['hash'],self.now(),attempt_id))
        return {'attempt_id':attempt_id,'state':'prepared','resume_existing_tab':True}

    def reconcile(self,owner,token,attempt_id,outcome,evidence):
        if outcome not in ('confirmed','not_submitted','unknown'): raise ValueError('invalid reconciliation')
        from . import canary, receipts
        with canary.transaction(self):
            self.check(owner,token)
            a=self.con.execute('SELECT * FROM jr_attempts WHERE id=?',(attempt_id,)).fetchone()
            if not a: raise Denied('attempt missing')
            if a['state']=='confirmed':
                if outcome=='confirmed': return
                raise Denied('cannot undo a confirmed application')
            if outcome in ('confirmed','unknown') and a['state']!='submit_started':
                raise Denied('no durable submit marker')
            observed=timestamp(evidence.get('observed_at'))
            if not evidence.get('refs') or not observed or observed>self.clock():
                raise canary.UnsafeAttempt('observed reconciliation evidence required')
            if a['state']=='not_submitted':
                if outcome=='not_submitted': return
                raise Denied('attempt already closed')
            if outcome!='unknown':
                try:
                    receipts.validate(self.job(a['job_key']),evidence,not_submitted=outcome=='not_submitted')
                except ValueError as error:
                    raise canary.UnsafeAttempt(str(error)) from error
            state='submit_started' if outcome=='unknown' else outcome
            self.con.execute('UPDATE jr_attempts SET state=?,evidence=?,updated=? WHERE id=?',(state,json.dumps(evidence),self.now(),attempt_id))
            canary.record(self,a,outcome,evidence)
            if state=='confirmed':
                j=self.job(a['job_key']); at=self.clock().astimezone(ET).isoformat(); appkey='app|'+a['job_key']+'|'+a['day']
                sender=evidence.get('submitted_by','task')
                if sender not in ('task','unattributed'):raise Denied('unsupported submission attribution')
                submitted=timestamp(evidence.get('submitted_at')) if sender=='unattributed' else None
                if sender=='unattributed' and (not submitted or submitted>self.clock() or not evidence.get('attribution_reason')):
                    raise Denied('external receipt requires an observed send time and attribution reason')
                # Preserve the marker even if reconciliation takes place after ET midnight.
                marker=self.con.execute("SELECT at FROM events WHERE kind='submit-answers' AND key=? ORDER BY at LIMIT 1",(attempt_id,)).fetchone()
                click_time=timestamp(marker[0]) if marker else None
                submitted_time=timestamp(evidence.get('submitted_at'))
                if sender=='task' and click_time and (observed<click_time or submitted_time and submitted_time<click_time):
                    raise canary.UnsafeAttempt('receipt predates this worker click; reconcile external attribution')
                actual_click=timestamp(evidence.get('clicked_at'))
                if sender=='task' and actual_click and (actual_click > observed or click_time and actual_click < click_time):
                    raise canary.UnsafeAttempt('observed click time is inconsistent with the marker or receipt')
                applied_at=(submitted or actual_click or submitted_time or click_time or timestamp(a['updated'])).astimezone(ET).isoformat()
                if self.codex_production() and sender=='task' and applied_at[:10] != a['day']:
                    cohort = canary.state(self)
                    cohort.update(phase='halted',why='observed submission outside its authorized Eastern day',stopped_at=self.now())
                    canary.save(self,cohort)
                prior=self.con.execute("SELECT source_id FROM applications WHERE job_key=?",(j['job_key'],)).fetchone()
                if prior:
                    raise canary.UnsafeAttempt('another application is already recorded for this opening')
                appkey='app|'+a['job_key']+'|'+applied_at[:10]
                self.con.execute('INSERT INTO applications (app_key,job_key,company,company_key,title,link,applied_at,submitted_by,confirmed_via,result,result_at,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(appkey,j['job_key'],j['company'],j['company_key'],j['title'],j['link'],applied_at,sender,evidence['proof']+(' easy apply' if a['channel']=='one_click' else ''),'applied','',attempt_id,at))
                self.con.execute("UPDATE jobs SET status='Applied',updated_at=? WHERE job_key=?",(at,j['job_key']))
                self.resolve_legacy_holds(a['job_key'],evidence)
                self.con.execute("UPDATE jr_backlog SET state='resolved' WHERE provider='application_handoff' AND item_key=?",(a['job_key'],))
