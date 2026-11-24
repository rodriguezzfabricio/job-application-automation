import { runFormStep } from './browser_form_operation.mjs';

/** A static read-only projection for the hosted browser's permitted DOM. */
function projectForm(knownLabels) {
    const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
    const visible=e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0;
    const url=new URL(document.location.href);
    const ids=[...url.pathname.matchAll(/(?:\/(?:jobs?|requisitions)\/|_)((?:REF|R|JR[-_]?|REQ[-_]?)?\d+[A-Za-z]?)(?:-\d+)?(?=\/|$)/gi)].map(m=>m[1]);
    for(const segment of url.pathname.split('/')){
      if(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(segment))ids.push(segment);
    }
    const active=[...document.querySelectorAll('input,select,textarea')].filter(e=>visible(e)&&!e.disabled&&!e.readOnly);
    const auth=active.some(e=>e.type==='password'||/one-time-code|new-password|current-password/.test(e.autocomplete||''));
    const secret=/password|token|passcode|verification code|secret|otp|social security|passport|payment|bank/i;
    const challenge=[...document.querySelectorAll('iframe,[data-sitekey]')].some(e=>visible(e)&&(/captcha|turnstile/i.test(e.getAttribute('src')||'')||e.hasAttribute('data-sitekey')));
    const rawURL=url.href;
    const jobParameter=/^(?:jobid|gh_jid|jobreqid|requisitionid|posting_id)$/i;
    for(const [key,value] of [...url.searchParams]){
      if(jobParameter.test(key))ids.push(value);
      else if(!/^(?:company|partnerid)$/i.test(key))url.searchParams.delete(key);
    }
    if(!/^#\/[\w/.-]+$/.test(url.hash))url.hash='';
    const req=[...new Set([...ids,...[...document.querySelectorAll('[data-requisition-id],[data-job-id]')].filter(visible).map(e=>e.getAttribute('data-requisition-id')||e.getAttribute('data-job-id'))].filter(Boolean))];
    const out={url:url.href,url_redacted:rawURL!==url.href,requisition_id:req.length===1?req[0]:null,identity_ambiguous:req.length>1,account:auth,captcha:challenge,fields:[],controls:[],attachments:[],errors:[],incomplete:false};
    if(auth||challenge||req.length>1)return out;
    const selector=e=>{for(const a of ['id','name','data-automation-id','data-testid']){
      const v=e.getAttribute(a);if(!v||!/^[-\w:.]{1,120}$/.test(v))continue;
      const sel=e.tagName.toLowerCase()+'['+a+'="'+v+'"]';if(document.querySelectorAll(sel).length===1)return sel;} return null;};
    const seen=new Set();
    for(const e of active){
      if(['submit','button','reset','hidden'].includes(e.type))continue;
      const label=norm(e.labels?.[0]?.innerText||e.getAttribute('aria-label')||e.placeholder);
      const typ=e.tagName==='SELECT'?'select':e.type||'text', sel=selector(e), key=sel||'unresolved-'+out.fields.length;
      const f={key,selector:sel,label:secret.test(label)?'Private field':label,type:typ,required:e.required||e.getAttribute('aria-required')==='true',filled:false,options:[]};
      if(secret.test(label)||!sel||seen.has(key)){if(f.required)out.incomplete=true;continue;} seen.add(key);
      if(typ==='select')f.options=[...e.options].filter(o=>!o.disabled).map(o=>({value:o.value,label:norm(o.textContent)}));
      if(typ==='file'){
        f.filled=!!e.value;
        if(/(?:resume|résumé|curriculum vitae|\bcv\b)/i.test(label)&&!/cover letter/i.test(label)){
          const file=e.files?.length===1?e.files[0]:null;
          const name=file?.name||String(e.value||'').split(/[\\/]/).pop();
          const group=e.closest('fieldset,[role="group"],[data-resume-upload]')||e.parentElement;
          const summary=norm(group?.innerText);const byte=summary.match(/\b([\d,]+)\s+bytes\b/i);
          const size=file?.size||(name&&summary.includes(name)&&byte?Number(byte[1].replaceAll(',','')):null);
          out.attachments.push({field_key:key,chooser_selector:sel,selected:name?{observed_name:name,observed_bytes:size}:null});
        }
      }else if(typ==='checkbox'||typ==='radio'){f.filled=f.checked=!!e.checked;if(typ==='radio'&&f.required)out.incomplete=true;}
      else{
        f.filled=!!e.value;
        if(f.filled&&(knownLabels.includes(label)||/^(?:(?:legal |full |first |last |preferred |given |family )?name|email(?: address)?|phone(?: number)?|(?:current )?city|state|zip(?: code)?|linkedin(?: url)?|github(?: url)?|are you legally authorized to work in the united states\??)$/i.test(label))){
          const v=typ==='select'?e.selectedOptions?.[0]?.textContent:e.value;if(v&&v.length<180&&!secret.test(v))f.observed_value=norm(v);
        }
      }
      out.fields.push(f);
      if(e.getAttribute('aria-invalid')==='true')out.errors.push({key,reason:'visible validation error'});
    }
    if([...document.querySelectorAll('[role="alert"]')].some(e=>visible(e)&&/invalid|required|error|failed/i.test(e.innerText||'')))out.errors.push({reason:'visible validation alert'});
    if([...document.querySelectorAll('iframe,[contenteditable="true"],[role="combobox"][aria-required="true"],[role="radiogroup"][aria-required="true"]')].some(visible))out.incomplete=true;
    for(const e of [...document.querySelectorAll('button,input[type=submit],[role=button],a[href]')].filter(visible)){
      const label=norm(e.getAttribute('aria-label')||e.innerText||e.value);if(!label||label.length>100)continue;
      if(e.tagName==='A'&&!/^(?:apply|start|next|review|continue)\b/i.test(label))continue;
      out.controls.push({key:'control-'+out.controls.length,label,selector:selector(e),role:e.tagName==='A'?'link':'button',disabled:!!e.disabled,href:e.tagName==='A'?e.getAttribute('href'):null});
    }
    return out;
}

export async function drive(tab,bridge) {
  let knownLabels=[];
  const read=()=>tab.playwright.evaluate(projectForm,knownLabels);
  const exchange=async(phase,page)=>{
    await bridge.playwright.getByRole('textbox',{name:'Form observation'}).fill(JSON.stringify({phase,page}));
    await bridge.playwright.expectNavigation(()=>bridge.playwright.getByRole('button',{name:'Process form observation',exact:true}).click());
    const result=JSON.parse(await bridge.playwright.locator('#result').innerText({timeoutMs:5000}));
    if(result.error)throw Error(result.error);return result;
  };
  const visited=new Set(), pages=[];
  for (let step=0;step<8;step++) {
    const first=await read();
    if(first.account||first.captcha||first.incomplete||first.identity_ambiguous)throw Error('Application needs targeted review');
    const signature=JSON.stringify([first.url,first.fields.map(f=>[f.key,f.label]),first.controls.map(c=>c.label)]);
    if(visited.has(signature))return {...pages[pages.length-1],pages:pages.length,stop:'observe the result of the last Continue'};
    visited.add(signature);
    const packet=await exchange('plan',first);
    knownLabels=packet.plan.answers.map(a=>a.field);
    let receipt;
    const result=await runFormStep(tab,async()=>[await read()],packet,async page=>{
      receipt=await exchange('verify',page);
      return receipt;
    });
    pages.push({writes:result.writes,verified_answers:receipt.answer_count,attachment_verified:!!result.attachment,
      questions:receipt.questions,advanced:result.advanced,proof_file:receipt.saved});
    if(!result.advanced)break;
  }
  return {...pages[pages.length-1],pages:pages.length,total_writes:pages.reduce((n,p)=>n+p.writes,0)};
}
