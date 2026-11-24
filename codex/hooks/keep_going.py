"""Codex Stop hook entry point (installed in ~/.codex/hooks.json). Logic and reasons: job_runtime/stop_hook.py.

Every Codex session runs this when its model tries to end a turn. A session that owns no running job
run pays one read-only SQLite query and stops normally. Any error also stops normally: this hook must
never trap a session. Each call is logged to codex-runtime/stop-hook.log so it is visible whether the
hook fires in scheduled runs (an untrusted hook is skipped silently by Codex).
"""
import datetime as dt
import json
import os
import sqlite3
import sys

W = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRIVATE = os.environ.get('JOB_AUTOMATION_PRIVATE_ROOT') or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'stopgap-private'))
DB = os.path.join(PRIVATE, 'tracker.sqlite')
LOG = os.path.join(PRIVATE, 'codex-runtime', 'stop-hook.log')


def log(sid, what):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as f:
            f.write('%s %s %s\n' % (dt.datetime.now().isoformat(timespec='seconds'), (sid or '?')[:13], what[:300]))
    except OSError:
        pass


def main():
    try:
        data = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        return
    sid = str(data.get('session_id') or '')
    if not sid:
        return
    try:
        con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=3)
        hit = con.execute("SELECT 1 FROM jr_runs WHERE owner=? AND status='running'", (sid,)).fetchone()
        con.close()
    except sqlite3.Error as e:
        log(sid, 'db error: %s' % e)
        return
    if not hit:
        log(sid, 'not a running job run: stop allowed')
        return
    sys.path.insert(0, W)
    os.environ['JOB_AUTOMATION_PRIVATE_ROOT'] = PRIVATE
    from job_runtime.store import Store
    from job_runtime.stop_hook import decide
    store = Store(DB)
    try:
        out = decide(store, sid, data.get('last_assistant_message') or '')
    finally:
        store.con.close()
    if out:
        log(sid, 'BLOCK: ' + out['reason'][:200])
        print(json.dumps(out))
    else:
        log(sid, 'job run, stop allowed')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:          # never trap a session on a hook bug
        log('?', 'hook error: %r' % e)
    sys.exit(0)
