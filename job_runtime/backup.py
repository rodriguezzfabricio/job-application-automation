"""Consistent backup and restore rehearsal. Does not replace a live database."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

def file_hash(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def table_counts(con):
    tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t:con.execute('SELECT count(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}

def backup(private,destination):
    private=Path(private).resolve(); destination=Path(destination).resolve()
    if destination==private or private in destination.parents: raise ValueError('backup must be outside private source')
    destination.mkdir(parents=True,mode=0o700,exist_ok=False)
    shutil.copytree(private,destination/'private',ignore=shutil.ignore_patterns('*.sqlite','*.sqlite-*','*.db','*.db-*'))
    src=sqlite3.connect(f'file:{private}/tracker.sqlite?mode=ro',uri=True); dst=sqlite3.connect(destination/'private/tracker.sqlite')
    src.backup(dst); counts=table_counts(dst); src.close()
    restore=sqlite3.connect(destination/'restore-rehearsal.sqlite'); dst.backup(restore)
    assert restore.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert counts==table_counts(restore)
    dst.close();restore.close()
    hashes={str(p.relative_to(private)):file_hash(p) for p in private.rglob('*.pdf')}
    assert all(file_hash(destination/'private'/p)==h for p,h in hashes.items())
    report={'counts':counts,'integrity':'ok','restored_copy_verified':True,'pdf_hashes':hashes}
    (destination/'verification.json').write_text(json.dumps(report,indent=2));return report
