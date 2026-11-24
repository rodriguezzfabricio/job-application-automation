"""Private SQLite state for the local prototype. Unknown sends never auto-retry."""
import contextlib
import os
from pathlib import Path
import sqlite3
import fcntl
import threading
from policy import instant, budget_allowed

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and 'meta' not in tables:raise ValueError('unknown schema; migration required')
            if tables and 'deliveries' not in tables and 'intents' in tables and db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]:raise ValueError('legacy delivery identities require explicit migration; use a fresh archive output')
            db.execute('CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL)')
            row=db.execute('SELECT version FROM meta').fetchone()
            if row and row[0]!=2:raise ValueError('unsupported schema; migration required')
            if row is None:db.execute('INSERT INTO meta VALUES(2)')
            db.execute('CREATE TABLE IF NOT EXISTS intents(profile TEXT,job TEXT,payload BLOB NOT NULL,status TEXT NOT NULL DEFAULT \'pending\',claimed_at TEXT,claim_day TEXT,claim_month TEXT,PRIMARY KEY(profile,job))')
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(candidate TEXT,canonical TEXT,profile TEXT,job TEXT,PRIMARY KEY(candidate,canonical))')
            db.execute('CREATE TABLE IF NOT EXISTS budgets(period TEXT PRIMARY KEY,cap INTEGER NOT NULL,spent INTEGER NOT NULL DEFAULT 0,reserved INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS reservations(period TEXT,id TEXT,amount INTEGER NOT NULL,PRIMARY KEY(period,id))')
            db.execute('CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,last_success TEXT,last_error TEXT,failures INTEGER NOT NULL DEFAULT 0,successes INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs(source TEXT,id TEXT,first_seen TEXT,last_seen TEXT,missing INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,content_hash TEXT,first_generation INTEGER NOT NULL,PRIMARY KEY(source,id))')
        os.chmod(self.path,0o600)
    @contextlib.contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()
    @contextlib.contextmanager
    def runner_lock(self):
        # OS releases flock on process exit; do not infer liveness from a clock lease.
        fd=os.open(str(self.path)+'.runner.lock',os.O_RDWR|os.O_CREAT,0o600)
        acquired=False
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);acquired=True
            except BlockingIOError:pass
            if acquired:self._runner_lock_owner=(os.getpid(),threading.get_ident())
            yield acquired
        finally:
            if acquired:self._runner_lock_owner=None;fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)
    def delivery_identity(self,candidate,canonical,profile,job):
        if not all(isinstance(x,str) and x for x in (candidate,canonical,profile,job)):raise ValueError('stable identity required')
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?)',(candidate,canonical,profile,job))
            return dict(db.execute('SELECT * FROM deliveries WHERE candidate=? AND canonical=?',(candidate,canonical)).fetchone())
    def current(self,source,job,sha,now):
        with self.connection() as db:
            row=db.execute('SELECT j.*,s.last_success,s.last_error FROM jobs j JOIN sources s ON s.id=j.source WHERE j.source=? AND j.id=?',(source,job)).fetchone()
        if not row or row['last_error'] or not row['last_success']:return False
        age=(instant(now)-instant(row['last_success'])).total_seconds()
        return 0<=age<=900 and row['active']==1 and row['missing']==0 and row['content_hash']==sha
    def enqueue(self,profile,job,payload):
        if not profile or not job or not isinstance(payload,bytes) or not payload or len(payload)>3*1024*1024:raise ValueError('invalid frozen packet')
        with self.connection() as db:
            c=db.execute('INSERT OR IGNORE INTO intents(profile,job,payload) VALUES(?,?,?)',(profile,job,payload))
            return c.rowcount==1
    def intent(self,profile,job):
        with self.connection() as db:
            row=db.execute('SELECT * FROM intents WHERE profile=? AND job=?',(profile,job)).fetchone()
            return dict(row) if row else None
    def claim(self,profile,job,now,daily_cap=20,monthly_cap=600):
        if type(daily_cap) is not int or type(monthly_cap) is not int or daily_cap<1 or monthly_cap<1:raise ValueError('invalid caps')
        at=instant(now).isoformat();day=at[:10];month=at[:7]
        with self.connection() as db:
            if db.execute('SELECT COUNT(*) FROM intents WHERE claim_day=?',(day,)).fetchone()[0]>=daily_cap:return False
            if db.execute('SELECT COUNT(*) FROM intents WHERE claim_month=?',(month,)).fetchone()[0]>=monthly_cap:return False
            c=db.execute("UPDATE intents SET status='sending',claimed_at=?,claim_day=?,claim_month=? WHERE profile=? AND job=? AND status='pending'",(at,day,month,profile,job))
            return c.rowcount==1
    def finish(self,profile,job,status):
        if status not in {'capture_accepted','delivery_unknown'}:raise ValueError('only local capture or uncertain outcome supported')
        with self.connection() as db:
            c=db.execute("UPDATE intents SET status=? WHERE profile=? AND job=? AND status='sending'",(status,profile,job))
            if c.rowcount!=1:raise ValueError('no claimed send to finish')
    def recover(self,now,lease_seconds=120):
        if getattr(self,'_runner_lock_owner',None)!=(os.getpid(),threading.get_ident()):
            with self.runner_lock() as acquired:
                if not acquired:return 0
                return self.recover(now,lease_seconds)
        from datetime import timedelta
        cutoff=(instant(now)-timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as db:
            return db.execute("UPDATE intents SET status='delivery_unknown' WHERE status='sending' AND claimed_at<=?",(cutoff,)).rowcount
    def reserve(self,period,id,amount,cap):
        if not budget_allowed(cap,0,0,amount) or not id:return False
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO budgets(period,cap) VALUES(?,?)',(period,cap))
            row=db.execute('SELECT * FROM budgets WHERE period=?',(period,)).fetchone()
            if row['cap']!=cap or not budget_allowed(cap,row['spent'],row['reserved'],amount):return False
            if db.execute('SELECT 1 FROM reservations WHERE period=? AND id=?',(period,id)).fetchone():return False
            db.execute('INSERT INTO reservations VALUES(?,?,?)',(period,id,amount));db.execute('UPDATE budgets SET reserved=reserved+? WHERE period=?',(amount,period));return True
    def settle(self,period,id,actual):
        if type(actual) is not int or actual<0:raise ValueError('invalid actual cost')
        with self.connection() as db:
            row=db.execute('SELECT amount FROM reservations WHERE period=? AND id=?',(period,id)).fetchone()
            if not row or row['amount']<=0 or actual>row['amount']:raise ValueError('missing, settled or exceeded reservation')
            db.execute('UPDATE budgets SET spent=spent+?,reserved=reserved-? WHERE period=?',(actual,row['amount'],period))
            # Retain a zero tombstone: a completed identity cannot be reserved again.
            db.execute('UPDATE reservations SET amount=0 WHERE period=? AND id=?',(period,id))
    def budget(self,period):
        with self.connection() as db:
            row=db.execute('SELECT * FROM budgets WHERE period=?',(period,)).fetchone()
            return dict(row) if row else None
    def snapshot(self,source,ids,now,error=None,hashes=None,active_ids=None):
        at=instant(now).isoformat()
        if ids is not None and (not isinstance(ids,list) or len(ids)>10000 or len(set(ids))!=len(ids) or any(not isinstance(x,str) or not x for x in ids)):raise ValueError('invalid complete snapshot')
        if active_ids is not None and (not isinstance(active_ids,list) or ids is None or any(not isinstance(x,str) for x in active_ids) or len(set(active_ids))!=len(active_ids) or not set(active_ids)<=set(ids)):raise ValueError('invalid active source identities')
        active=set(ids or []) if active_ids is None else set(active_ids)
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO sources(id) VALUES(?)',(source,))
            last=db.execute('SELECT last_success FROM sources WHERE id=?',(source,)).fetchone()[0]
            if last and instant(at)<instant(last):raise ValueError('observation predates latest successful snapshot')
            if error is not None or ids is None:
                db.execute('UPDATE sources SET last_error=?,failures=failures+1 WHERE id=?',(error or 'incomplete snapshot',source));return
            generation=db.execute('SELECT successes FROM sources WHERE id=?',(source,)).fetchone()[0]+1
            db.execute('UPDATE sources SET last_success=?,last_error=NULL,failures=0,successes=? WHERE id=?',(at,generation,source))
            db.execute('UPDATE jobs SET missing=missing+1 WHERE source=?',(source,))
            for id in ids:
                db.execute('INSERT INTO jobs(source,id,first_seen,last_seen,first_generation,active) VALUES(?,?,?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET last_seen=excluded.last_seen,missing=0,active=excluded.active',(source,id,at,at,generation,int(id in active)))
            if hashes:
                for id,sha in hashes.items():db.execute('UPDATE jobs SET content_hash=? WHERE source=? AND id=?',(sha,source,id))
            db.execute('UPDATE jobs SET active=0 WHERE source=? AND missing>=2',(source,))
    def job(self,source,id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM jobs WHERE source=? AND id=?',(source,id)).fetchone();return dict(row) if row else None
    def health(self,source,now):
        with self.connection() as db:row=db.execute('SELECT last_success,last_error FROM sources WHERE id=?',(source,)).fetchone()
        if not row:return 'unknown'
        if row['last_error']:return 'failed'
        if not row['last_success']:return 'unknown'
        age=(instant(now)-instant(row['last_success'])).total_seconds()
        return 'healthy' if 0<=age<=2700 else 'stale'
    def status(self):
        with self.connection() as db:return {r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) AS n FROM intents GROUP BY status')}
