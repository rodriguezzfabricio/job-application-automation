#!/usr/bin/env python3
"""Install the current user's model-free public job scanner on this Mac."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('private_root',type=Path)
    args = parser.parse_args()
    worktree = Path(__file__).resolve().parents[1]
    private = args.private_root.resolve(strict=True)
    if not (private/'tracker.sqlite').is_file():
        parser.error('private root must contain the existing tracker.sqlite')
    python = worktree/'stopgap-private'/'codex-venv'/'bin'/'python'
    if not python.is_file():
        parser.error('installed runtime Python unavailable')
    label = 'local.jobtools.job-shortlist'
    domain = f'gui/{os.getuid()}'
    logs = Path.home()/'Library'/'Logs'/'JobApplicationWorker'
    logs.mkdir(mode=0o700,parents=True,exist_ok=True)
    path = Path.home()/'Library'/'LaunchAgents'/(label+'.plist')
    path.parent.mkdir(parents=True,exist_ok=True)
    config = dict(Label=label,WorkingDirectory=str(worktree),
                  ProgramArguments=[str(python),'-B','-m','job_runtime.shortlist','--scheduled'],
                  EnvironmentVariables={'JOB_AUTOMATION_PRIVATE_ROOT':str(private),'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
                  StartCalendarInterval=[{'Minute':m} for m in (0,20,40)],RunAtLoad=True,Umask=0o077,
                  StandardOutPath=str(logs/'shortlist.out.log'),StandardErrorPath=str(logs/'shortlist.err.log'))
    encoded = plistlib.dumps(config)
    installed = path.is_file() and path.read_bytes()==encoded
    loaded = subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode==0
    if loaded and not installed:
        parser.error('a different scanner is already registered under this label')
    if not installed:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
            out.write(encoded)
        os.replace(out.name,path)
    if not loaded:
        subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True)
    print(path)


if __name__=='__main__':
    main()
