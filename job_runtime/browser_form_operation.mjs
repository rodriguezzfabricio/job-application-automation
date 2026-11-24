/** One bounded action in the current hosted tab. Never presses a final control. */
export async function runFormStep(tab, read, packet, commit) {
  const norm = value => String(value ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
  const sameRoute = (a,b) => {
    const left = new URL(a), right = new URL(b);
    return left.origin === right.origin && left.pathname.replace(/\/$/,'') === right.pathname.replace(/\/$/,'') && left.search === right.search;
  };
  const observe = async () => {
    const [page] = await read();
    if (!page?.url || page.account || page.captcha || page.incomplete || page.identity_ambiguous ||
        new URL(page.url).origin !== new URL(packet.opening).origin ||
        (page.requisition_id && packet.requisition && String(page.requisition_id)!==String(packet.requisition)) ||
        !(sameRoute(page.url,packet.opening) || (packet.requisition && String(page.requisition_id)===String(packet.requisition)))) {
      throw new Error('Inspect opening identity or access wall');
    }
    return page;
  };
  const expected = new Map(packet.plan.answers.map(a => [a.key,a]));
  const blocked = new Map(packet.plan.blocked.filter(b => b.reason!=='verify selected PDF attachment').map(b => [b.key,b]));
  const hold = (f,reason) => blocked.set(f.key,{key:f.key,label:f.label,reason});
  const find = (page,key) => page.fields.find(f => f.key===key);
  const readValue = f => f?.type==='checkbox' ? (f.checked ? 'checked' : '') : f?.observed_value;
  const matches = (f,a) => f?.filled && (f.type==='checkbox' ? f.checked : norm(readValue(f))===norm(a.value));
  const unique = async locator => (await locator.count())===1 && await locator.isVisible() && await locator.isEnabled();
  let page = await observe();
  const originalUrl = page.url;
  const roster = (fields) => JSON.stringify(fields.map(f=>[f.key,f.label,f.type,!!f.required]));
  if (roster(page.fields)!==roster(packet.fields)) throw new Error('Form changed since saved-answer planning');
  let writes=0;
  for (const op of packet.plan.ops) {
    page = await observe();
    if (page.url!==originalUrl || roster(page.fields)!==roster(packet.fields)) throw new Error('Form changed during grouped fill');
    const f = find(page,op.key), answer=expected.get(op.key);
    if (!f || !answer || f.label!==answer.field) throw new Error('Field identity changed');
    if (f.filled || f.checked) {
      if (!matches(f,answer)) hold(f,'preserve conflicting prefilled answer');
      continue;
    }
    const option = (f.options||[]).find(o => String(o.value)===String(op.value));
    const selector = op.mode==='radio' ? option?.selector : f.selector;
    if (!selector) {hold(f,'ambiguous locator'); continue;}
    const el = tab.playwright.locator(selector);
    if (!(await unique(el))) {hold(f,'field is unavailable'); continue;}
    if (op.mode==='text') await el.fill(String(op.value));
    else if (op.mode==='select' && option) await el.selectOption(String(op.value));
    else if (op.mode==='check' || (op.mode==='radio' && option)) await el.check();
    else {hold(f,'inspect custom control'); continue;}
    writes++;
  }
  page = await observe();
  let attachment=null, chose=false;
  const pdf=packet.pdf, slots=page.attachments;
  const fileMatches = f => f && f.observed_name===pdf.name && f.observed_bytes===pdf.bytes && f.observed_sha256===pdf.sha256;
  if (slots.length===1 && pdf && !page.attachment_ambiguous) {
    const slot=slots[0], field=find(page,slot.field_key);
    if (!slot.selected && !field.filled) {
      const target=slot.chooser_selector ? tab.playwright.locator(slot.chooser_selector) :
        slot.chooser_group_selector && slot.chooser_label && tab.playwright.locator(slot.chooser_group_selector).getByRole(slot.chooser_role,{name:slot.chooser_label,exact:true});
      if (target && await unique(target)) {
        const [chooser]=await Promise.all([tab.playwright.waitForEvent('filechooser',{timeoutMs:10000}),target.click()]);
        await chooser.setFiles([pdf.path]);
        chose=true;
        page=await observe();
      }
    }
    const current=page.attachments.find(s=>s.field_key===slot.field_key)?.selected;
    if (fileMatches(current) || (chose && current && !current.observed_sha256 && current.observed_name===pdf.name && current.observed_bytes===pdf.bytes)) {
      attachment={...current,method:current.observed_sha256?'browser_file':'filechooser',job_key:packet.job_key,refs:[page.url]};
      if (attachment.method==='filechooser') Object.assign(attachment,{selected_path:pdf.path,selected_sha256:pdf.sha256});
    } else hold(field,'verify selected PDF attachment');
  } else if (slots.length) hold({key:'attachment',label:'Résumé'},'ambiguous or unverified résumé slot');
  page=await observe();
  if (page.url!==originalUrl || roster(page.fields)!==roster(packet.fields)) throw new Error('Replan changed application page');
  const verified=[];
  for (const f of page.fields) {
    if (f.type==='file') { if (attachment && page.attachments.some(a=>a.field_key===f.key)) f.attachment_verified=true; continue; }
    const answer=expected.get(f.key);
    if (answer && matches(f,answer)) verified.push({...answer,observed_value:readValue(f)});
    else if (answer || f.required) hold(f,blocked.get(f.key)?.reason || 'saved answer needs verified readback');
  }
  const relevant=page.controls.filter(c=>!c.disabled && /^(?:next(?: step| page)?|(?:save and )?continue|review(?: application)?|submit(?: my| this)?(?: application)?|send(?: application)?|apply(?: now)?)$/i.test(c.label));
  const result={job_key:packet.job_key,fields:page.fields,controls:page.controls,errors:page.errors,verified,attachment,
    questions:[...blocked.values()],advanced:false,writes,observed_url:originalUrl};
  const receipt=commit ? await commit(result) : {verified:true};
  result.proof_file=receipt.saved;
  let advanced=false;
  if (receipt.verified && !blocked.size && !page.errors.length && relevant.length===1 && /^(?:next(?: step| page)?|(?:save and )?continue)$/i.test(relevant[0].label)) {
    const c=relevant[0];
    if (!c.href || new URL(c.href,originalUrl).origin===new URL(originalUrl).origin) {
      const el=c.selector?tab.playwright.locator(c.selector):tab.playwright.getByRole(c.role,{name:c.label,exact:true});
      if (await unique(el)) {await el.click(); advanced=true;}
    }
  }
  return {...result, advanced};
}
