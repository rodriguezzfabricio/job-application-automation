/** Read-only DOM projection. browser_form.reader packages this with the shared label helpers. */
export async function inspectForm([body]) {
  if (!body) return [];
  const doc = body.ownerDocument;
  const view = doc.defaultView;
  const H = createFormReadHelpers(doc);
  const ACCOUNT = /\b(?:password|passcode|one[ -]time (?:code|password)|verification code|security question|credential|mfa code|otp)\b/i;
  const REVIEW_ONLY = /\b(?:token|secret|signature|social security|ssn|passport|payment|bank|legal agreement|attestation)\b/i;
  const SECRET_VALUE = /(?:\b(?:password|passcode|token|secret|otp|verification code)\s*[:=]|-----BEGIN|\bBearer\s+\S+|\bsk-(?:proj-|live-|test-)?[\w-]{12,}|\bgh[pousr]_[\w-]{10,}|\beyJ[\w-]{10,}\.)/i;
  const RESUME = /(?:^|[^\p{L}\p{N}])(?:resume|résumé|curriculum vitae|cv)(?=$|[^\p{L}\p{N}])/iu;
  const COVER = /\b(?:cover letter|portfolio|additional (?:file|document)|transcript)\b/i;
  const CHOOSE = /^(?:attach|upload|browse|choose|select)(?: (?:a|your|new))?(?: (?:file|resume|résumé|cv|document))?$/i;
  const LIMIT = { fields: 100, controls: 60, options: 40 };

  const text = (value, limit = 200) => {
    const s = H.norm(value);
    return s.length <= limit && !SECRET_VALUE.test(s) ? s : '';
  };

  function safeUrl(value) {
    try {
      const input = new URL(value, view.location.href);
      if (!['https:', 'http:'].includes(input.protocol) || input.username || input.password ||
          /\b(?:password|passcode|oauth|verification|auth|reset)[-/]/i.test(input.pathname) ||
          /[A-Za-z0-9_-]{64,}/.test(input.pathname)) return '';
      const allowed = /^(?:gh_jid|job(?:[_.-]?id|ReqId)?|req(?:[_.-]?id)?|requisition(?:[_.-]?id)?|posting(?:[_.-]?id)?|for)$/i;
      const isGreenhouse = /^(?:job-boards|boards)\.greenhouse\.io$/i.test(input.hostname);
      for (const [name, val] of [...input.searchParams]) {
        if (!((allowed.test(name) && /^[A-Za-z0-9_.-]{1,100}$/.test(val)) ||
          (isGreenhouse && name === 'token' && /^\d{1,16}$/.test(val)))) input.searchParams.delete(name);
      }
      if (!/^\/[\w/_.%-]{1,250}$/.test(input.hash.slice(1))) input.hash = '';
      return input.href;
    } catch { return ''; }
  }

  function reqId(url) {
    if (!url) return null;
    const u = new URL(url);
    const names = /^(?:gh_jid|job(?:[_.-]?id|ReqId)?|req(?:[_.-]?id)?|requisition(?:[_.-]?id)?|posting(?:[_.-]?id)?|token)$/i;
    const queries = new Set([...u.searchParams].filter(([name]) => names.test(name)).map(([, value]) => value));
    if (queries.size) return queries.size === 1 ? [...queries][0] : null;
    for (const path of [u.pathname, u.hash.slice(1)]) {
      const parts = path.split('/');
      const found = new Set(parts.flatMap((part, i) => {
        if (/^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(part)) return [part];
        if (/^\d{1,16}$/.test(part) && /^(?:jobs?|posting|requisitions)$/i.test(parts[i - 1] || '')) return [part];
        const suffixed = part.match(/(?:^|[^\w]|_)((?:R|JR|REQ)[-_]?\d+)(?=$|[^\w]|_)/i);
        return suffixed ? [suffixed[1]] : [];
      }));
      if (/\.myworkday(?:jobs|site)\.com$/i.test(u.hostname)) {
        for (const piece of parts) { const id = piece.match(/_((?:REF)?\d+[A-Z]?)(?:-\d+)?$/); if (id) found.add(id[1]); }
      }
      if (found.size) return found.size === 1 ? [...found][0] : null;
    }
    return null;
  }

  const roots = H.roots();
  function uniqueSelector(el) {
    const id = el.getAttribute('id');
    const name = el.getAttribute('name');
    const type = el.getAttribute('type');
    const tag = el.localName;
    const ident = /^[-_A-Za-z0-9:.]{1,100}$/;
    const candidates = [
      id && ident.test(id) ? '#' + CSS.escape(id) : null,
      name && ident.test(name) ? tag + '[name="' + name + '"]' +
        (tag === 'input' && type === 'radio' && ident.test(el.value) ? '[value="' + el.value + '"]' : '') : null,
      ...['data-testid', 'data-automation-id', 'data-hook', 'aria-labelledby'].map((key) => {
        const value = el.getAttribute(key);
        return value && ident.test(value) ? tag + '[' + key + '="' + value + '"]' : null;
      }),
    ];
    return candidates.find((s) => s && roots.reduce((count, r) => count + r.querySelectorAll(s).length, 0) === 1) || null;
  }

  function credentialField(el, label) {
    return el.type === 'password' ||
      /^(?:current-password|new-password|one-time-code|cc-number|cc-csc)$/i.test(el.autocomplete || '') ||
      ACCOUNT.test([el.name, el.id, label].join(' '));
  }

  function uploadGroup(el) {
    return el.closest('[aria-labelledby],fieldset,[role="group"],[data-resume-upload]');
  }

  function groupLabel(group) {
    if (!group) return '';
    const names = (group.getAttribute('aria-labelledby') || '').split(/\s+/);
    const labels = names.map((name) => name && (group.getRootNode().getElementById?.(name) || doc.getElementById(name)));
    const heading = group.querySelector('legend,label,[role="heading"]');
    return text(labels.filter(Boolean).map((label) => H.vis(label) ? label.innerText : '').join(' ') ||
      group.getAttribute('aria-label') || (heading && H.vis(heading) ? heading.innerText : ''));
  }

  function labelled(el) {
    if (el.type === 'file') {
      const scoped = groupLabel(uploadGroup(el));
      if (RESUME.test(scoped) || COVER.test(scoped)) return scoped;
    }
    return text(H.labelOf(el) || H.visualLabel(el) || H.nearText(el) || H.contextOf(el));
  }

  function knownLabel(label) {
    if (ACCOUNT.test(label) || REVIEW_ONLY.test(label)) return '';
    const name = H.norm(label).toLowerCase().replace(/^\*\s*|[ *?:]+$/g, '');
    if (/^(?:(?:legal|candidate) )?(?:full |first |last )?name$|^(?:preferred|given|family|sur) ?name$/.test(name)) return 'name';
    if (/^(?:(?:confirm|re-?enter) )?e-?mail(?: address)?(?: confirmation)?$/.test(name)) return 'email';
    if (/^(?:(?:mobile|primary|contact) )?(?:phone|telephone)(?: number)?$/.test(name)) return 'phone';
    if (/^(?:linkedin|github)(?: (?:profile|url|link)){0,2}$|^(?:(?:personal|portfolio) )?website(?: url)?$|^portfolio(?: url)?$/.test(name)) return 'url';
    if (/^(?:(?:current )?city|city of residence|state(?: ?\/ ?province)?|state or province)$/.test(name)) return 'place';
    if (/^(?:street )?address(?: line)?(?: 1)?$/.test(name)) return 'address';
    if (/^(?:zip|postal)(?: code)?$/.test(name)) return 'postal';
    if (/^(?:(?:earliest|available|availability|desired)(?: work| employment)? start date|date available to start|when can you start|(?:expected |anticipated )?graduation date|date of graduation)$/.test(name)) return 'date';
    if (/^(?:(?:current|most recent|last) (?:employer|company)(?: name)?|current or most recent employer)$/.test(name)) return 'employer';
    if (/^(?:(?:are you )?(?:legally )?(?:authorized|eligible) to work in (?:the )?(?:u\.?s\.?a?\.?|united states)|(?:will|do) you (?:now or in the future )?(?:require|need) (?:visa |employment |immigration )?sponsorship(?: now or in the future)?|(?:do you (?:currently )?have an? )?(?:active )?security clearance|(?:are you )?(?:at least |over )18(?: years old| years of age)?|gender(?: identity)?|sex|veteran(?: status)?|protected veteran(?: status)?|are you a (?:protected )?veteran|disability status|do you have a disability|ethnicity|race(?: ?\/ ?ethnicity)?|are you (?:hispanic|latino|hispanic or latino))$/.test(name)) return 'choice';
    return '';
  }

  function observedValue(label, candidate) {
    const value = text(candidate, 180);
    if (!value) return undefined;
    switch (knownLabel(label)) {
      case 'name': return /^[\p{L}][\p{L}.'’ -]{0,70}$/u.test(value) ? value : undefined;
      case 'email': return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ? value : undefined;
      case 'phone': return /^[+()0-9 .-]{7,24}$/.test(value) && value.replace(/\D/g, '').length >= 10 ? value : undefined;
      case 'url': return safeUrl(value) === value && new URL(value).protocol === 'https:' ? value : undefined;
      case 'place': return /^[\p{L}][\p{L}.,'’ -]{0,70}$/u.test(value) ? value : undefined;
      case 'address': return /^\d+ [\p{L}\p{N}.,'# -]{2,100}$/u.test(value) ? value : undefined;
      case 'postal': return /^\d{5}(?:-\d{4})?$/.test(value) ? value : undefined;
      case 'date': return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : undefined;
      case 'employer': return /^[\p{L}\p{N}][\p{L}\p{N}.'’ ,&()-]{0,90}$/u.test(value) ? value : undefined;
      case 'choice': return /^(?:yes|no|male|female|man|woman|hispanic|latino|i am not|not a|do not|decline|prefer not|none)\b/i.test(value) ? value : undefined;
      default: return undefined;
    }
  }

  function safeKey(el, sequence) {
    // Preserve Desktop field keys for local locator/readback, with an ordinal for anonymous fields.
    const key = H.keyOf(el);
    return /^[a-z]+:[-_A-Za-z0-9:.]{1,100}$/.test(key) ? key : 'field-' + sequence;
  }

  function option(el, label, value) {
    const choice = text(label, 160);
    const opaque = text(value, 80);
    return choice && opaque && /^[\p{L}\p{N}/ _.,:()@+-]+$/u.test(opaque)
      ? { label: choice, value: opaque, selector: el.tagName === 'INPUT' ? uniqueSelector(el) : null } : null;
  }

  async function selectedFile(el) {
    if (el.files?.length !== 1) return null;
    const file = el.files[0];
    const name = text(file.name, 155);
    if (!/^[\p{L}\d][\p{L}\d ._()-]*\.pdf$/iu.test(name) || !file.size || file.size > 20 * 1024 * 1024) return null;
    let sha256 = null;
    try {
      if (view.crypto?.subtle) {
        const hash = await view.crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        sha256 = Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
      }
    } catch { /* Metadata is still available to the filechooser readback path. */ }
    return { observed_name: name, observed_bytes: file.size, observed_sha256: sha256 };
  }

  const current = view.location.href;
  const url = safeUrl(current);
  const headings = roots.flatMap((root) => [...root.querySelectorAll('h1,h2,[role="heading"]')]
    .filter(H.vis).map((el) => H.norm(el.innerText))).slice(0, 15);
  const nodes = H.allCtl().filter((el) => !el.disabled && !el.readOnly &&
    (H.vis(el) || (el.type === 'file' &&
      ([...(el.labels || [])].some(H.vis) || RESUME.test(groupLabel(uploadGroup(el))) || COVER.test(groupLabel(uploadGroup(el)))))));
  const authRoute = /(?:^|[\/_-])(?:login|log[-_]?in|sign[-_]?in|register|registration|create[-_]?account|verify)(?:$|[\/_-])/i;
  const account = authRoute.test(view.location.pathname) || headings.some((line) =>
    /^(?:sign in|log in|create (?:an )?account|register|verify (?:your |the )?(?:email|account)|enter (?:the |a )?verification code)\b/i.test(line)) ||
    nodes.some((el) => credentialField(el, labelled(el)));
  const iframes = roots.flatMap((root) => [...root.querySelectorAll('iframe')]).filter(H.vis);
  const captcha = iframes.some((el) => /recaptcha|hcaptcha|turnstile|arkoselabs|funcaptcha/i.test(el.src)) ||
    roots.some((root) => [...root.querySelectorAll('[data-sitekey],.g-recaptcha,.h-captcha,.cf-turnstile')].some(H.vis)) ||
    headings.some((line) => /verify you are human|checking your browser|just a moment/i.test(line));
  const status = { url, requisition_id: reqId(url), headings: headings.map((s) => text(s, 150)).filter(Boolean).slice(0, 4),
    account, captcha, errors: [], fields: [], controls: [], attachments: [],
    embedded_form: iframes.length > 0, incomplete: nodes.length > LIMIT.fields,
    url_redacted: Boolean(url && url !== current) };
  if (account || captcha || !url) return [{ ...status, headings: [] }];

  // Keep an explicitly displayed requisition anchored to a visible value. A mismatch is never guessed.
  const reqs = roots.flatMap((root) => [...root.querySelectorAll('[data-job-id],[data-requisition-id]')])
    .filter(H.vis).map((el) => el.getAttribute('data-requisition-id') || el.getAttribute('data-job-id'))
    .filter((id) => /^[A-Za-z0-9_-]{1,80}$/.test(id));
  const IDs = new Set([status.requisition_id, ...reqs].filter(Boolean));
  status.identity_ambiguous = IDs.size > 1;
  status.requisition_id = IDs.size === 1 ? [...IDs][0] : null;
  if (status.identity_ambiguous) return [status];

  // Never mark a form complete when an unsupported required ARIA control is visible.
  for (const root of roots) {
    for (const el of root.querySelectorAll(
      '[role="combobox"],[role="radiogroup"],[role="checkbox"],[role="textbox"],[contenteditable="true"]')) {
      if (!H.vis(el) || el.getAttribute('aria-disabled') === 'true' ||
          ![el, el.closest('[role="group"],fieldset')].some((node) => node?.getAttribute('aria-required') === 'true')) continue;
      if (!nodes.some((native) => native === el || el.contains(native))) status.incomplete = true;
    }
  }

  const groups = new Set();
  const fileNodes = [];
  for (const el of nodes.slice(0, LIMIT.fields)) {
    if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) || ['button', 'submit', 'image', 'reset'].includes(el.type)) continue;
    let label = labelled(el);
    if (credentialField(el, label)) continue;
    const key = safeKey(el, status.fields.length);
    let row = { key, selector: uniqueSelector(el), label, type: el.type || 'text',
      required: Boolean(el.required || el.getAttribute('aria-required') === 'true'), options: [], filled: false };
    if (el.type === 'radio' && el.name) {
      if (groups.has(el.name)) continue;
      groups.add(el.name);
      const group = nodes.filter((node) => node.type === 'radio' && node.name === el.name &&
        node.getRootNode() === el.getRootNode() && node.form === el.form);
      label = text(H.contextOf(el) || H.labelOf(el));
      const selected = group.find((node) => node.checked);
      row = { ...row, label, key: 'radio:' + text(el.name, 95), selector: null, type: 'radio',
        required: group.some((node) => node.required || node.getAttribute('aria-required') === 'true'),
        filled: Boolean(selected), options: group.slice(0, LIMIT.options)
          .map((node) => option(node, H.labelOf(node) || node.value, node.value)).filter(Boolean) };
      if (group.length > LIMIT.options) status.incomplete = true;
      row.observed_value = selected ? observedValue(label, H.labelOf(selected) || selected.value) : undefined;
    } else if (el.tagName === 'SELECT') {
      row.type = 'select';
      row.options = [...el.options].filter((item) => !item.disabled).slice(0, LIMIT.options)
        .map((item) => option(item, item.textContent, item.value)).filter(Boolean);
      if (el.options.length > LIMIT.options) status.incomplete = true;
      const selected = el.selectedOptions[0];
      row.filled = Boolean(el.value && selected && !selected.disabled &&
        !/^(?:select|choose|please select|please choose)(?: an? option)?[.…-]*$/i.test(H.norm(selected.textContent)));
      if (row.filled) row.observed_value = observedValue(label, selected.textContent);
    } else if (el.type === 'checkbox') {
      row.checked = el.checked;
      row.filled = el.checked;
    } else if (el.type === 'file') {
      row.filled = Boolean(el.files?.length);
      if (RESUME.test(label) && !COVER.test(label)) fileNodes.push([el, row]);
    } else {
      row.filled = Boolean(el.value);
      if (row.filled) row.observed_value = observedValue(label, el.value);
    }
    if (row.required && !row.label) status.incomplete = true;
    if (el.getAttribute('aria-invalid') === 'true' || (el.validity && !el.validity.valid && !el.validity.valueMissing)) {
      status.errors.push({ field_key: row.key, reason: 'visible field validation' });
    }
    status.fields.push(row);
  }
  const alerts = roots.flatMap((root) => [...root.querySelectorAll('[role="alert"],[data-invalid="true"]')])
    .filter((el) => H.vis(el) && /\b(?:error|invalid|required|must|failed|missing|could not)\b/i.test(el.innerText || ''));
  if (alerts.length) status.errors.push({ reason: 'visible validation alert' });

  for (const root of roots) {
    for (const el of root.querySelectorAll('button,input[type="button"],input[type="submit"],[role="button"],a[href]')) {
      if (!H.vis(el)) continue;
      const label = text(el.getAttribute('aria-label') || (el.tagName === 'INPUT' ? el.value : el.innerText), 100);
      if (!label || (el.tagName === 'A' && !/^(?:apply|start|begin|continue|next|review)\b/i.test(label))) continue;
      if (status.controls.length >= LIMIT.controls) { status.incomplete = true; break; }
      status.controls.push({ key: 'control-' + status.controls.length, selector: uniqueSelector(el), label,
        role: el.tagName === 'A' ? 'link' : 'button', href: el.tagName === 'A' ? safeUrl(el.href) : null,
        disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true') });
    }
  }

  for (const [el, field] of fileNodes) {
    const group = uploadGroup(el);
    const candidates = group ? [...group.querySelectorAll('button,[role="button"]')].filter((button) =>
      H.vis(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
      CHOOSE.test(H.norm(button.getAttribute('aria-label') || button.innerText))) : [];
    const target = candidates.length === 1 ? candidates[0] : (!candidates.length && H.vis(el) ? el : null);
    status.attachments.push({ field_key: field.key, file_selector: field.selector, chooser_selector: target ? uniqueSelector(target) : null,
      chooser_group_selector: group ? uniqueSelector(group) : null, chooser_role: target === el ? null : 'button', chooser_label: target ? text(target.getAttribute('aria-label') || (target === el ? field.label : target.innerText), 100) : null,
      selected: await selectedFile(el) });
  }
  if (status.attachments.length > 1) status.attachment_ambiguous = true;
  return [status];
}
