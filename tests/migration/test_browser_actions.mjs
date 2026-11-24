import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { runFormStep } from '../../job_runtime/browser_form_operation.mjs';
import { useVerification } from '../../job_runtime/browser_verification.mjs';

const sha = value => createHash('sha256').update(value).digest('hex');
test('only a single verified non-final control advances', async()=>{
  const url='https://example.test/jobs/1234';
  const packet={opening:url,job_key:url,requisition:'1234',fields:[],plan:{ops:[],answers:[],blocked:[]}};
  for(const [labels,verify,clicks] of [[['Next'],true,1],[['Continue'],true,1],[['Continue'],false,0],[['Submit application'],true,0],[['Continue','Submit application'],true,0]]) {
    let actual=0;
    const tab={playwright:{getByRole:()=>({count:async()=>1,isVisible:async()=>true,isEnabled:async()=>true,click:async()=>{actual++}})}};
    const page={url,requisition_id:'1234',fields:[],attachments:[],errors:[],controls:labels.map(label=>({role:'button',label}))};
    const result=await runFormStep(tab,async()=>[page],packet,async()=>({verified:verify}));
    assert.equal(actual,clicks);
    assert.equal(result.advanced,!!clicks);
  }
});
test('browser verification acts on one matching message and employer control without emitting its contents',async()=>{
  const portal='fixture.wd5.myworkdayjobs.com', origin='https://'+portal, tenant=portal+'/careers';
  const page=origin+'/en-US/Careers/challenge', href=origin+'/en-US/Careers/activate?key=fixture';
  const id='1234abcde', now=Date.now()/1000;
  const request={portal,tenant,after_epoch:now-20,before_epoch:now+60,recipient_sha256:sha('fixture@example.invalid'),code_challenge_sha256:sha(page)};
  const match={authenticated:true,purpose:'account_verification',recipient_sha256:request.recipient_sha256,message_id:id,
    destination_origin:origin,destination_tenant:tenant,method:'link',action_sha256:sha(href)};
  let clicked=0,filled='';
  const element={count:async()=>1,isVisible:async()=>true,isEnabled:async()=>true,getAttribute:async()=>href,
    click:async()=>{clicked++},fill:async value=>{filled=value}};
  const msg={...element,getByRole:()=>element,innerText:async()=>'Your verification code is 001234.'};
  const mailbox={url:async()=>'https://mail.google.com/mail/u/0/#inbox/'+id,playwright:{locator:()=>msg}};
  const target={url:async()=>page,playwright:{getByRole:()=>element}};
  assert.equal((await useVerification(mailbox,target,match,request)).action,'activation_link_clicked');
  assert.equal(clicked,1);
  await assert.rejects(()=>useVerification(mailbox,target,{...match,action_sha256:sha('wrong')},request));
  const code={...match,method:'code',action_sha256:sha('code|'+id+'|'+sha(page))};
  const result=await useVerification(mailbox,target,code,request);
  assert.equal(filled,'001234');
  assert.ok(!JSON.stringify(result).includes(filled));
  await assert.rejects(()=>useVerification(mailbox,{...target,url:async()=>page+'/another'},code,request));
});

test('final button uses an unexpired marker once within its deadline', async () => {
  const {clickWithinGrant} = await import('../../job_runtime/browser_submit.mjs');
  const clicks = [];
  const target = {click: async options => clicks.push(options.timeoutMs)};
  const before = '2026-11-25T00:00:00-05:00';
  const grant = {click_once:true,attempt_id:'fixture-grant',click_before:before};
  await assert.rejects(clickWithinGrant(target,grant,grant.attempt_id,()=>Date.parse(before)));
  await clickWithinGrant(target,grant,grant.attempt_id,()=>Date.parse(before)-2200);
  await assert.rejects(clickWithinGrant(target,grant,grant.attempt_id,()=>Date.parse(before)-2200));
  assert.deepEqual(clicks,[1200]);
});
