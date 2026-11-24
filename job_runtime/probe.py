"""Read-only production snapshot; safe for unattended local environment verification."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
from .backup import table_counts
from .policy import UTC,effective
from .planner import plan
from .store import Store

def probe(authority,private_root):
    destination=Path(private_root);destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination,prefix='probe-') as tmp:
        src=sqlite3.connect(f'file:{Path(authority).resolve()}?mode=ro',uri=True);copied=sqlite3.connect(Path(tmp)/'tracker.sqlite');src.backup(copied);src.close()
        before=table_counts(copied);copied.close();s=Store(Path(tmp)/'tracker.sqlite')
        try:
            # This connection is an isolated backup. Planning only reads it;
            # copied production leases must remain untouched and need not be acquired.
            if s.con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('copied database integrity check failed')
            queues=plan(s,effective({}),'FULL')
            assert all(table_counts(s.con)[k]==n for k,n in before.items())
        finally:
            s.con.close()
    out={'observed_at':dt.datetime.now(UTC).isoformat(),'authority_read_only':True,'snapshot_integrity':'ok',
         'legacy_counts':before,'queue_counts':{k:len(v) for k,v in queues.items() if isinstance(v,(list,dict))},
         'employer_submissions':0,'real_phone_messages':0,'limitations':['defaults-only planner probe; current connector policy must be imported before real work']}
    (destination/'latest-probe.json').write_text(json.dumps(out,indent=2));return out

if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--authority',required=True);ap.add_argument('--private-output',required=True);a=ap.parse_args()
    print(json.dumps(probe(a.authority,a.private_output)))
