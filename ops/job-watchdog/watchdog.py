#!/usr/bin/env python3
"""Provider-neutral health observer. Uses the existing, separately installed notifier."""
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def read(path,default=None):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,ValueError):return {} if default is None else default


def decisions(now,state,run):
    local=now.astimezone(ZoneInfo('America/New_York'))
    if local.hour<6 or local.hour==6 and local.minute<30:return []
    def age(value):
        try:return (now-dt.datetime.fromisoformat(value.replace('Z','+00:00'))).total_seconds()/60
        except (ValueError,TypeError,AttributeError):return float('inf')
    def say(message):
        # The runtime publishes why it left work behind; this is the phone line, not a log.
        cause=str(state.get('cause') or '').strip()
        return (message+' | '+cause)[:300] if cause else message
    started=run.get('started_at');finished=run.get('finished_at')
    if started and not finished and age(started)>(70 if run.get('mode')=='FULL' else 45):
        return [('stuck',say('The active job run is overdue. Check Codex and Brave on the Mac.'))]
    if age(state.get('last_completed_at') or state.get('last_success_at'))>60:
        return [('stopped',say('No job run has finished in 60 minutes. Check the Mac, Codex usage and Brave.'))]
    if age(state.get('browser_last_success_at'))>120:
        return [('browser',say('The browser stages have not succeeded in 120 minutes. Check Brave and the Codex connection.'))]
    if state.get('last_run_status')=='partial':
        return [('partial',say('The latest job run finished with a blocker. Open Job Dashboard Today for the exact action.'))]
    return []


def run_once(folder,now=None):
    folder=Path(folder);now=now or dt.datetime.now(dt.timezone.utc)
    if (folder/'paused').exists():return {'paused':True}
    events=decisions(now,read(folder/'desktop-state.json'),read(folder/'run-state.json'))
    ledger=read(folder/'codex-alerts.json');out=folder/'outbox';out.mkdir(parents=True,exist_ok=True)
    queued=[]
    for key,message in events:
        previous=ledger.get(key,{})
        if previous.get('file') and (out/previous['file']).exists():continue
        sent=folder/'sent'/previous.get('file','_missing')
        if sent.is_file() and now.timestamp()-sent.stat().st_mtime<7200:continue
        # A failed/uncertain dispatch is not delivery; preserve it for inspection.
        if previous.get('file') and not sent.is_file():continue
        name='codex-watchdog-'+key+'-'+now.strftime('%Y%m%dT%H%M%SZ')+'.txt'
        staged=folder/('.'+name);staged.write_text('[Jobs] '+message);os.replace(staged,out/name)
        ledger[key]={'file':name,'queued_at':now.isoformat(),'delivery':'pending'};queued.append(key)
    if not events and ledger and 'recovery' not in ledger:
        name='codex-watchdog-recovery-'+now.strftime('%Y%m%dT%H%M%SZ')+'.txt'
        staged=folder/('.'+name);staged.write_text('[Jobs] Job automation recovered. A current run and browser checks finished.');os.replace(staged,out/name)
        ledger['recovery']={'file':name,'queued_at':now.isoformat(),'delivery':'pending'}
    if events:ledger.pop('recovery',None)
    temp=folder/'codex-alerts.json.tmp';temp.write_text(json.dumps(ledger));os.replace(temp,folder/'codex-alerts.json')
    return {'queued':queued,'problems':[k for k,_ in events]}


if __name__=='__main__':
    folder=Path.home()/'Library/Application Support/job-watchdog'
    result=run_once(folder)
    with (folder/'watchdog.log').open('a') as log:log.write(dt.datetime.now(dt.timezone.utc).isoformat()+' '+json.dumps(result)+'\n')
