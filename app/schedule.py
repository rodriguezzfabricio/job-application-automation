"""Generate a user launchd schedule; execution/activation remains explicit."""
from pathlib import Path
import plistlib,os,stat

def write_agent(config,python,entry,destination,logs):
    config,python,entry,destination,logs=map(Path,[config,python,entry,destination,logs])
    if any(not p.is_absolute() for p in [config,python,entry,destination,logs]):raise ValueError('absolute schedule paths required')
    info=config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('private regular config required')
    if not python.is_file() or not entry.is_file():raise ValueError('existing runtime and entry point required')
    logs.mkdir(parents=True,mode=0o700,exist_ok=True)
    if logs.is_symlink() or logs.stat().st_uid!=os.getuid() or logs.stat().st_mode&0o077:raise ValueError('owned private log directory required')
    args=[str(python),str(entry),'service','--config',str(config)]
    value={'Label':'local.jobtools.job-assistant','ProgramArguments':args,'WorkingDirectory':str(entry.parent),'RunAtLoad':True,'StartInterval':300,'KeepAlive':False,'ProcessType':'Background','Umask':0o077,'StandardOutPath':str(logs/'service.stdout.log'),'StandardErrorPath':str(logs/'service.stderr.log')}
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(plistlib.dumps(value));out.flush();os.fsync(out.fileno())
    return value
