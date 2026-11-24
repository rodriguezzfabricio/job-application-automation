import { createHash } from 'node:crypto';

const digest = text => createHash('sha256').update(text).digest('hex');
const activationLabel = /^(?:verify|confirm|activate)(?:\s+(?:my|your|the))?\s+(?:e-?mail(?: address)?|account|profile|registration)(?:\s+now)?$/i;

/** Use only matched metadata in the browser process. Never return message contents or credentials. */
export async function useVerification(mailTab, portalTab, match, request) {
  const now = Date.now() / 1000;
  if (!match?.authenticated || !/^[a-f\d]{8,32}$/i.test(match.message_id) ||
      match.purpose !== 'account_verification' || match.recipient_sha256 !== request.recipient_sha256 ||
      match.destination_origin !== 'https://' + request.portal || match.destination_tenant !== request.tenant ||
      !(now >= request.after_epoch && now <= request.before_epoch)) {
    throw Error('Current matched registration request required');
  }
  if (new URL(await mailTab.url()).origin !== 'https://mail.google.com' ||
      new URL(await portalTab.url()).origin !== match.destination_origin) {
    throw Error('Mailbox or employer tab changed');
  }
  const msg = mailTab.playwright.locator('[data-legacy-message-id="' + match.message_id + '"]');
  if (await msg.count() !== 1 || !await msg.isVisible()) throw Error('Open the exact verified message');
  if (match.method === 'link') {
    const link = msg.getByRole('link',{name:activationLabel});
    if (await link.count() !== 1 || !await link.isVisible()) throw Error('One visible activation link required');
    const href = await link.getAttribute('href');
    if (!href || digest(href) !== match.action_sha256 || new URL(href).origin !== match.destination_origin) {
      throw Error('Displayed activation destination does not match');
    }
    await link.click();
    return {action:'activation_link_clicked',activation_confirmed:false};
  }
  if (match.method !== 'code' || !request.code_challenge_sha256 ||
      digest(await portalTab.url()) !== request.code_challenge_sha256 ||
      digest('code|' + match.message_id + '|' + request.code_challenge_sha256) !== match.action_sha256) {
    throw Error('Employer code challenge does not match');
  }
  const input = portalTab.playwright.getByRole('textbox',{name:/(?:verification|activation) code/i});
  if (await input.count() !== 1 || !await input.isVisible() || !await input.isEnabled()) {
    throw Error('One visible employer verification code input required');
  }
  const body = await msg.innerText({timeoutMs:5000});
  const codes = new Set([...body.matchAll(/(?:verification|activation) code(?: is)?[\s:–-]*(\d{4,8})\b/gi)].map(m=>m[1]));
  if (codes.size !== 1) throw Error('One verification code required in the matched message');
  await input.fill([...codes][0]);
  return {action:'verification_code_filled',activation_confirmed:false};
}
