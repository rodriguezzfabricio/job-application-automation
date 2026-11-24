"""Provider-neutral local health decisions, injected clocks and fake-sink friendly."""
from .policy import ET,timestamp

# What a deferred provider is called on a phone line.
LABEL={'requirements':'match','application_handoff':'handoff',
       'legacy_unknown_submission':'reconcile','gmail':'intake','linkedin-discovery':'linkedin',
       'linkedin_cards':'linkedin cards','handshake_cards':'handshake cards'}


def cause(store,run_id,audit):
    """Why the last run left work behind, in one short line.

    The four alert messages are liveness only ("no run finished"), which is what made the
    owner read sqlite by hand. The run's own receipts hold the reason: a stage that failed,
    a stage that left no receipt, or the deferred backlog that is actually holding work.
    """
    bits=[]
    bad=sorted(r['stage'] for r in store.con.execute("SELECT stage FROM jr_actions WHERE run_id=? AND status<>'succeeded'",(run_id,)))
    if bad:bits.append(', '.join(s+' failed' for s in bad))
    missing=sorted(audit.get('missing') or [])
    if missing:bits.append('no receipt: '+', '.join(missing))
    counts={};oldest=None
    for r in store.con.execute("SELECT provider,first_seen FROM jr_backlog WHERE state='pending'"):
        counts[r['provider']]=counts.get(r['provider'],0)+1
        when=timestamp(r['first_seen'])
        if when and (oldest is None or when<oldest):oldest=when
    if counts:
        top=', '.join('%s %d'%(LABEL.get(p,p),n) for p,n in sorted(counts.items(),key=lambda kv:(-kv[1],kv[0]))[:3])
        age='' if not oldest else ', oldest %.0fh'%max(0.0,(store.clock()-oldest).total_seconds()/3600)
        bits.append('deferred: '+top+age)
    return '; '.join(bits)[:160]


def _note(state):
    text=str(state.get('cause') or '').strip()
    return lambda message: (message+' | '+text)[:300] if text else message


def health_events(now,state,run,previous):
    local=now.astimezone(ET)
    if local.hour<6 or (local.hour==6 and local.minute<30):return []
    events=[];problems=[]
    say=_note(state)
    def age(value):return (now-timestamp(value)).total_seconds()/60 if value else float('inf')
    if age(state.get('last_success_at'))>60:problems.append(('finish',say('No job run has finished in 60 minutes.')))
    if run.get('started_at') and not run.get('finished_at') and age(run['started_at'])>(70 if run.get('mode')=='FULL' else 45):problems.append(('run',say('The active job run exceeded its expected duration.')))
    if age(state.get('browser_last_success_at'))>120:problems.append(('browser',say('No browser stage has succeeded in 120 minutes.')))
    for kind,text in problems:
        sent=previous.get(kind)
        if not sent or age(sent)>120:events.append({'key':kind,'text':text,'severity':'warning'})
    warned=[timestamp(previous[k]) for k in ('finish','run','browser') if previous.get(k)]
    recovered=timestamp(previous.get('recovery'))
    if not problems and warned and (not recovered or recovered<max(warned)):events.append({'key':'recovery','text':'Job automation recovered; current run and browser health are fresh.','severity':'info'})
    return events
