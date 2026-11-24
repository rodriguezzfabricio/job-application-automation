"""Run the archive demo or one private source polling pass. No automatic sending."""
import argparse
import json
import os
from pathlib import Path
from policy import source_record, fit, role_gate, novelty
from runner import ArchiveIO, Runner, NoExternalNetwork
from state import Store
from packet import job_fingerprint
from polling import poll_once

HERE=Path(__file__).resolve().parent

def run_demo(output):
    io=ArchiveIO(HERE/'fixtures',output,{'profile.sample.json','jobs.sample.json','greenhouse.sample.json'})
    store=Store(output/'state.sqlite');runner=Runner('archive',io,store)
    with NoExternalNetwork():
        profile=io.read('profile.sample.json');fixture=io.read('jobs.sample.json');now=fixture['fixture_clock']
        if fixture.get('synthetic') is not True:raise ValueError('synthetic fixture label required')
        store.snapshot('archive-fixture',[j['id'] for j in fixture['jobs']],now,hashes={j['id']:job_fingerprint(j) for j in fixture['jobs']})
        packets=[runner.process(profile,job,now) for job in fixture['jobs']]
        saved=io.read('greenhouse.sample.json');job=source_record('greenhouse','example',saved['job'])
        # Preserve uncertainty; both the posting and candidate are synthetic.
        decision=fit(profile,job);reasons=decision['reasons']+role_gate(job,now)
        sample_review={'id':job['id'],'title':job['title'],'url':job['url'],'workplace':job['workplace'],'status':'review' if reasons else decision['status'],'reasons':reasons,'publication':job['published_at'],'bootstrap_notify':novelty([],job,True,now),'provenance':saved['provenance'],'note':'Saved historical posting; not a fresh alert and not a recommendation for the operator.'}
        result={'mode':'archive','synthetic':True,'fixture_clock':now,'packets':packets,'sample_job':sample_review,'intents':store.status(),'external_messages_sent':0,'paid_model_calls':0,'production_ready':False}
        output.mkdir(parents=True,exist_ok=True,mode=0o700)
        (output/'run-report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False))
        (output/'sample-review.json').write_text(json.dumps(sample_review,indent=2,ensure_ascii=False))
        return result

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['demo','status','poll','outbox','service','health','engine']);parser.add_argument('--output',type=Path)
    parser.add_argument('--config',type=Path,help='owner-only service configuration; secrets stay in separate protected files')
    parser.add_argument('--semantic',choices=['none','deepseek'],default='none',help='opt-in paid, review-only requirement inspection; never enables mail')
    parser.add_argument('--deepseek-key-path',type=Path,default=Path.home()/'.config/job-assistant/deepseek.key')
    parser.add_argument('--board');parser.add_argument('--profile',type=Path);parser.add_argument('--approval',type=Path);parser.add_argument('--packet-limit',type=int,default=3)
    parser.add_argument('--outbox-action',choices=['status','drain'],default='status')
    parser.add_argument('--engine-action',choices=['status','expire'],default='status',help='corrected single-file delivery engine inspection (status is read-only)')
    parser.add_argument('--recipient',help='pinned operator recipient for engine handoff (expire only)')
    parser.add_argument('--policy-version',help='qualified packet policy version for engine handoff (expire only)')
    parser.add_argument('--send',action='store_true',help='explicit transport opt-in; never qualifies a review packet')
    parser.add_argument('--credentials',type=Path);parser.add_argument('--sender')
    args=parser.parse_args()
    if args.send and (args.command!='outbox' or args.outbox_action!='drain'):parser.error('--send requires outbox --outbox-action drain')
    if args.send and not all([args.credentials,args.sender,args.profile,args.approval]):parser.error('--send requires --credentials, --sender, --profile and --approval')
    if args.semantic!='none' and args.command!='poll':parser.error('--semantic is only valid for poll')
    output=args.output or (HERE/'candidate/poll-state' if args.command in {'poll','outbox','engine'} else HERE/'artifacts-v3')
    output=output.absolute() if args.command in {'poll','outbox','engine'} else output.resolve()
    if args.command in {'service','health'}:
        if not args.config:parser.error('service/health requires --config')
        from service import run_service,service_health
        from datetime import datetime,timezone
        result=run_service(args.config) if args.command=='service' else service_health(args.config,datetime.now(timezone.utc).isoformat())
    elif args.command=='poll':
        if not args.board or not args.profile or not args.approval:parser.error('poll requires --board, --profile and --approval')
        extractor=None
        if args.semantic=='deepseek':
            from deepseek_transport import DeepSeekExtractor
            runtime=Path.home()/'.config/job-assistant/deepseek-runtime'
            extractor=DeepSeekExtractor(args.deepseek_key_path,runtime/'budget.sqlite',runtime/'responses',mode='segments')
        result=poll_once(args.board,args.profile,args.approval,output,packet_limit=args.packet_limit,semantic_extractor=extractor)
    elif args.command=='outbox':
        from delivery import DeliveryQueue,DeliveryWorker,personal_readiness
        from datetime import datetime,timezone
        queue_path=output/'delivery.sqlite'
        if args.outbox_action=='status':result={'mode':'personal_outbox','queue':DeliveryQueue(queue_path).status() if queue_path.exists() else {},'live_sending_enabled':False,'delivery_verified':False}
        else:
            now=datetime.now(timezone.utc).isoformat();transport=None;ready=None
            if args.send:
                from mail_transport import GmailTransport
                from profile_store import load_profile
                profile,_=load_profile(args.profile,args.approval)
                transport=GmailTransport(args.credentials,recipient=profile['recipient_email'],sender=args.sender)
                ready=personal_readiness(args.profile,args.approval,output,now)
            result=DeliveryWorker(DeliveryQueue(queue_path),transport,readiness=ready,enabled=args.send).run(now)
            result.update(mode='personal_outbox',qualified_packet_issuer_available=False)
    elif args.command=='engine':
        from engine import DeliveryEngine
        from datetime import datetime,timezone
        if args.engine_action=='status':
            result={'mode':'corrected_engine','engine':DeliveryEngine.status_only(output),'live_sending_enabled':False}
        else:
            if not args.recipient or not args.policy_version:parser.error('engine expire requires --recipient and --policy-version')
            now=datetime.now(timezone.utc)
            engine=DeliveryEngine(output,version=args.policy_version,recipient=args.recipient)
            before=engine.status();changed=engine.expire_stale(now.isoformat())
            result={'mode':'corrected_engine','action':'expire','changed':changed,'before':before,'after':engine.status(),'live_sending_enabled':False}
    elif args.command=='status':
        result={'mode':'archive','disabled':(output/'DISABLED').exists(),'intents':Store(output/'state.sqlite').status() if (output/'state.sqlite').exists() else {},'live_sending_available':False}
    else:
        if (output/'DISABLED').exists():result={'mode':'archive','status':'disabled','external_messages_sent':0}
        else:result=run_demo(output)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    if args.command=='poll' and result.get('status') in {'source_failed','completed_with_holds'}:raise SystemExit(1)

if __name__=='__main__':main()
