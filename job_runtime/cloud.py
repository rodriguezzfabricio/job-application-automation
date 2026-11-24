"""Pure cloud-compatible policy helpers. Hosting/delivery must be independently verified."""
import datetime as dt
from .policy import ET, timestamp

def phone_decision(now,status=None,last_sent=None,drive_available=True):
    """Live routine boundaries: latest-created STATUS <=26h, <=100 fresh, <=160 early stale."""
    local=now.astimezone(ET); hhmm=local.strftime('%H:%M'); day=local.date().isoformat()
    if not 6<=local.hour<=22 or local.minute!=45:return {'send':False,'reason':'outside hourly :45 schedule'}
    if not drive_available:return {'send':True,'dedup':'drive-failure|'+day,'text':'[Jobs] Phone update could not read Drive; check the Job Dashboard sheet.'}
    created=timestamp(status.get('created_at')) if status else None
    if created and (now-created).total_seconds()>26*3600:status=None;created=None
    age=(now-created).total_seconds()/60 if created else float('inf')
    if age<0:return {'send':False,'reason':'future STATUS clock mismatch'}
    title_time=(status or {}).get('title_time','yesterday')
    if age<=100:
        if status.get('phone_updates')=='daily' and local.hour!=int(status.get('push_hour',18)):return {'send':False,'reason':'daily mode outside push hour'}
        out={'send':True,'dedup':'fresh|'+created.isoformat()+'|'+local.strftime('%Y%m%d%H'),'text':status.get('line','')[:200]}
    elif age<=160:
        out={'send':True,'dedup':'early-stale|'+created.isoformat(),'text':f'[Jobs {hhmm}] No run has finished since {title_time}. Check the account usage limit, Mac, ChatGPT app and Brave.'}
    elif local.hour in (9,13,18):
        out={'send':True,'dedup':'reminder|'+local.strftime('%Y%m%d%H'),'text':f'[Jobs {hhmm}] Still no finished run since {title_time}. Check the account usage limit, Mac, ChatGPT app and Brave.'}
    else:return {'send':False,'reason':'stale reminder outside 09/13/18 ET'}
    if out['dedup']==last_sent:return {'send':False,'reason':'already delivered'}
    out['text']=out['text'][:200];return out

def validate_record(record):
    required={'link','company','title','location','source','posted_at','found_at','eligibility','reason','score','status','resume_doc_url','manifest','synced_to_tracker'}
    missing=required-set(record)
    if missing:raise ValueError('cloud REC fields missing: '+','.join(sorted(missing)))
    if record['resume_doc_url'] is not None or record['manifest'] is not None:raise ValueError('cloud must not invent local resume assignments')
    if not timestamp(record['posted_at']):raise ValueError('canonical first posted timestamp required')
    if len(record.get('description_excerpt',''))>1500:raise ValueError('REC description excerpt exceeds 1500')
    return record
