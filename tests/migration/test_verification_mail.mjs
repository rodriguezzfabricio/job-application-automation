import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { verificationMetadata } from '../../job_runtime/verification_mail.mjs';

const sha = value => createHash('sha256').update(value).digest('hex');
const recipient = 'fixture@example.invalid';
const portal = 'fixture.wd5.myworkdayjobs.com';
const tenant = portal + '/careers';
const now = Date.now() - 1000;
const expected = {portal, tenant, recipient_sha256:sha(recipient), after_epoch:now/1000-60, before_epoch:now/1000+60};
const href = 'https://' + portal + '/en-US/Careers/verify?ticket=fixture';
function message(body, changes={}) {
  const headers = {'From':'Workday <account@workday.com>', 'Delivered-To':recipient,
    Subject:'Verify your candidate account', 'Authentication-Results':'mx.google.com; dkim=pass header.d=workday.com; dmarc=pass header.from=workday.com',
    ...changes};
  return {id:'fixture-message', internal_date:String(now),payload:{mime_type:'text/html',body:{content:body},
    headers:Object.entries(headers).map(([name,value])=>({name,value}))}};
}
const link = '<a href="' + href + '">Activate your account</a>';
test('unique, authenticated registration link returns only its digest', ()=>{
  const good=verificationMetadata([message(link)],expected);
  assert.equal(good.action_sha256,sha(href));
  assert.equal(good.method,'link');
  assert.equal(good.authenticated_sender_domain,'workday.com');
  assert.ok(!JSON.stringify(good).includes('ticket='));
});
test('recipient, mail authentication, purpose, time, destination and uniqueness are required', ()=>{
  for(const changes of [{'Delivered-To':'another@example.invalid'}, {'Authentication-Results':'mx.google.com; dkim=fail; dmarc=pass header.from=workday.com'},
    {'Authentication-Results':'untrusted.example; dkim=pass header.d=workday.com; dmarc=pass header.from=workday.com'},
    {From:'attacker@elsewhere.invalid'}, {Subject:'Verify your account password reset'}])
    assert.equal(verificationMetadata([message(link,changes)],expected),null);
  for(const other of [href.replace('/Careers/', '/Other/'), href.replace(portal, portal+'.invalid'), href+'&redirect=https://example.invalid'])
    assert.equal(verificationMetadata([message(link.replace(href,other))],expected),null);
  assert.equal(verificationMetadata([{...message(link),internal_date:now-120000}],expected),null);
  assert.equal(verificationMetadata([message(link),{...message(link),id:'second'}],expected),null);
});
test('verification code requires a bound employer challenge and independently matching destination', ()=>{
  const challenge=sha('fixture challenge');
  const target={...expected,code_challenge_sha256:challenge};
  const html='Your verification code is 012345. <a href="'+href+'">Candidate portal</a>';
  const result=verificationMetadata([message(html)],target);
  assert.equal(result.method,'code');
  assert.equal(result.action_sha256,sha('code|fixture-message|'+challenge));
  assert.ok(!JSON.stringify(result).includes('012345'));
  for(const [body,filter] of [[html,expected],[html.replace('Careers','Other'),target],[html+' Verification code: 001122',target]])
    assert.equal(verificationMetadata([message(body)],filter),null);
});
