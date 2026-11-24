// A toolbar click (or its shortcut) grants activeTab access to this tab only.
function accountForm(mode, expectedURL, secrets) {
  if (location.href !== expectedURL || location.protocol !== 'https:') return false;
  const visible = el => el.getClientRects().length && !el.disabled && !el.readOnly;
  const forms = [...document.forms].filter(form =>
    new URL(form.action || location.href, location.href).origin === location.origin &&
    [...form.querySelectorAll('input[type=password]')].some(visible));
  if (forms.length !== 1) return false;
  const pwd = [...forms[0].querySelectorAll('input[type=password]')].filter(visible);
  const email = [...forms[0].querySelectorAll('input[type=email],input[autocomplete=username],input[autocomplete=email]')].filter(visible);
  if (email.length !== 1 || pwd.length > 2 || (mode === 'sign_in' && pwd.length !== 1)) return false;
  if (mode === 'register' && pwd.some(el => el.autocomplete === 'current-password')) return false;
  if (mode === 'sign_in' && pwd.some(el => el.autocomplete === 'new-password')) return false;
  if (!secrets) return true;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (const [el, value] of [[email[0], secrets.email], ...pwd.map(el => [el, secrets.password])]) {
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return true;
}

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id || !tab.url || !tab.url.startsWith('https://')) return;
  const target = {tabId: tab.id, frameIds: [0]};
  try {
    const [observed] = await chrome.scripting.executeScript({target, func: accountForm, args: [null, tab.url, null]});
    if (!observed?.result) return;
    const reply = await chrome.runtime.sendNativeMessage('local.jobs.portal_autofill', {page_url: tab.url});
    if (!reply?.password || !reply?.email) return;
    const [filled] = await chrome.scripting.executeScript({target: {tabId: tab.id, documentIds: [observed.documentId]},
      func: accountForm, args: [reply.mode, tab.url, {email: reply.email, password: reply.password}]});
    if (filled?.result) await chrome.action.setBadgeText({tabId: tab.id, text: 'OK'});
  } catch {
    // Fail closed when the broker is unavailable or this document navigated.
  }
});

// A public-page toolbar click without a credential form is a non-secret health check.
chrome.runtime.onInstalled.addListener(async () => {
  try {
    const reply = await chrome.runtime.sendNativeMessage('local.jobs.portal_autofill', {op:'health'});
    if (reply?.ready) {
      await chrome.action.setTitle({title:'Job portal autofill · private host connected'});
      await chrome.action.setBadgeText({text:'ON'});
    }
  } catch {}
});
