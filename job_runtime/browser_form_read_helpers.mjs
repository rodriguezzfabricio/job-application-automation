/** Read-only label and visibility helpers shared by the Desktop runner and hosted form reader. */
export function createFormReadHelpers(document) {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // Shadow-DOM aware: an ATS widget may render its form inside a shadow root, where
  // document.querySelectorAll cannot see it.
  const roots = () => {
    const acc = [document];
    for (let i = 0; i < acc.length; i++) {
      for (const host of acc[i].querySelectorAll('*')) if (host.shadowRoot) acc.push(host.shadowRoot);
    }
    return acc;
  };
  const allCtl = () => roots().flatMap((r) => Array.from(r.querySelectorAll('input, select, textarea')));
  const byName = (n) => allCtl().filter((e) => e.name === n);
  const byId = (id) => allCtl().find((e) => e.id === id) || document.getElementById(id);
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (r.width < 2 || r.height < 2) return false;
    if (el.type === 'hidden') return false;
    return true;
  };
  const sigOf = (el) => el.tagName.toLowerCase() + ':' + (el.type || '') + ':' + norm(labelOf(el) || contextOf(el)).slice(0, 90);
  const locate = (key) => {
    if (key.startsWith('id:')) return byId(key.slice(3));
    if (key.startsWith('name:')) return byName(key.slice(5))[0] || null;
    if (key.startsWith('radio:')) return byName(key.slice(6));
    if (key.startsWith('tag:')) {
      const parts = key.split(':');
      const tag = parts[1]; const type = parts[2]; const idx = Number(parts[3]);
      const list = tag === 'input'
        ? allCtl().filter((e) => e.tagName.toLowerCase() === 'input' && (e.type || '') === type)
        : allCtl().filter((e) => e.tagName.toLowerCase() === tag);
      return list[idx] || null;
    }
    if (key.startsWith('sig:')) {
      // signature key survives node replacement: re-derive the same signature set
      const rest = key.slice(4);
      const cut = rest.lastIndexOf(':');
      const sig = rest.slice(0, cut);
      const idx = Number(rest.slice(cut + 1));
      const peers = allCtl().filter((e) => !e.disabled && sigOf(e) === sig);
      return peers[idx] || null;
    }
    return null;
  };
  const keyOf = (el) => {
    if (el.id && byId(el.id) === el && allCtl().filter((e) => e.id === el.id).length === 1) return 'id:' + el.id;
    if (el.name && byName(el.name).length === 1) return 'name:' + el.name;
    const sig = sigOf(el);
    const peers = allCtl().filter((e) => !e.disabled && sigOf(e) === sig);
    return 'sig:' + sig + ':' + peers.indexOf(el);
  };
  const labelOf = (el) => {
    const bits = [];
    try { if (el.labels) for (const l of el.labels) bits.push(l.innerText); } catch (e) { /* noop */ }
    const al = el.getAttribute('aria-label'); if (al) bits.push(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) for (const id of lb.split(/\s+/)) { const n = document.getElementById(id); if (n) bits.push(n.innerText); }
    if (bits.some(Boolean)) return norm(bits.filter(Boolean)[0]).slice(0, 220);
    if (el.placeholder) bits.push(el.placeholder);
    const ti = el.getAttribute('title'); if (ti) bits.push(ti);
    let p = el; let d = 0;
    while (p && d++ < 4) {
      const prev = p.previousElementSibling;
      if (prev && /^(LABEL|LEGEND|SPAN|DIV|P|H[1-6]|STRONG|B)$/.test(prev.tagName)) {
        const t = norm(prev.innerText);
        if (t && t.length < 120) { bits.push(t); break; }
      }
      p = p.parentElement;
    }
    const nxt = el.nextElementSibling;
    if (nxt && /^(LABEL|SPAN)$/.test(nxt.tagName)) { const t = norm(nxt.innerText); if (t && t.length < 80) bits.push(t); }
    return norm(bits.filter(Boolean).join(' | ')).slice(0, 220);
  };
  const contextOf = (el) => {
    const bits = [];
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && el.type === 'radio') return norm(lg.innerText).slice(0, 220);
      if (lg) bits.push(lg.innerText);
    }
    const grp = el.closest('[role="radiogroup"],[role="group"],[data-automation-id*="formField"],.form-group,.field-set,.field');
    if (grp) {
      const h = grp.querySelector('label,legend,[class*="label"],[data-automation-id*="label"]');
      if (h && h !== el) bits.push(h.innerText);
      const t = norm(grp.innerText);
      if (t) bits.push(t.slice(0, 200));
    }
    return norm(bits.filter(Boolean).join(' | ')).slice(0, 300);
  };
  const nearText = (el) => {
    let p = el.parentElement, d = 0;
    while (p && d++ < 5) {
      const ctrls = p.querySelectorAll('input,select,textarea').length;
      const t = norm(p.innerText);
      if (t && t.length <= 240 && ctrls <= 2) return t;
      p = p.parentElement;
    }
    return '';
  };
  // Visual association: ATS widgets render the question in a DIFFERENT shadow root than
  // the <input>, so search every root and pick the nearest text block. Text fields: the
  // block directly above and horizontally overlapping. Checkboxes/radios: same row, to the right.
  const visualLabel = (el) => {
    const er = el.getBoundingClientRect();
    if (!er.width && !er.height) return '';
    const isBox = el.type === 'checkbox' || el.type === 'radio';
    let best = ''; let bestDist = Infinity;
    for (const root of roots()) {
      for (const cand of root.querySelectorAll('label, span, div, p, strong, b, h1, h2, h3, h4, legend, dt, dd, spl-typography-label, sr-question-field-text')) {
        if (cand.contains(el) || el.contains(cand)) continue;
        if (cand.querySelector('input,select,textarea')) continue;
        const t = norm(cand.innerText);
        if (!t || t.length > 240) continue;
        if (!/[a-z0-9]/i.test(t)) continue;   // skip bare "*" required-markers
        const r = cand.getBoundingClientRect();
        if (r.height < 4 || r.width < 4) continue;
        let ok = false; let dist = 0;
        if (isBox) {
          const vOverlap = Math.min(r.bottom, er.bottom) - Math.max(r.top, er.top);
          ok = vOverlap > 0 && r.left >= er.left - 4 && r.width > r.height;
          dist = Math.abs((r.top + r.bottom) / 2 - (er.top + er.bottom) / 2);
        } else {
          const hOverlap = Math.min(r.right, er.right) - Math.max(r.left, er.left);
          ok = r.bottom <= er.top + 6 && hOverlap > 20;
          dist = er.top - r.bottom;
        }
        if (ok && dist < bestDist) { bestDist = dist; best = t; }
      }
    }
    return best;
  };
  return { norm, vis, locate, keyOf, labelOf, contextOf, nearText, visualLabel, byName, allCtl, roots };
}
