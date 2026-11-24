"""Load an immutable source-bound profile from trusted local configuration paths."""
import json,os,stat
from pathlib import Path
from packet import canonical,digest
from personal import validate_evidence

def _private_json(path):
    fd=os.open(Path(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('profile/approval must be owner-only regular files')
        data=f.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise ValueError('profile/approval too large')
    value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('profile/approval object required')
    return value

def load_profile(profile_path,approval_path):
    # Both paths belong to trusted root config, never fields extracted from a posting.
    approval=_private_json(approval_path);profile=_private_json(profile_path);sha=digest(canonical(profile))
    if not approval.get('authority') or approval.get('profile_sha256')!=sha or approval.get('source_pdf_sha256')!=profile.get('source',{}).get('sha256'):raise ValueError('external approval/source binding mismatch')
    validate_evidence(profile,approval_sha256=sha)
    return profile,sha
