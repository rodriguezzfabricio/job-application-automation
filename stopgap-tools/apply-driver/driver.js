// Optional legacy form driver. The lease-based runtime in job_runtime handles submission.

const fs = require('fs');
const path = require('path');

function loadProfile(privateDir) {
  const config = JSON.parse(fs.readFileSync(path.join(privateDir, 'driver-profile.json'), 'utf8'));
  const keys = ['first_name','last_name','email','phone','address1','city','state','zip','linkedin',
    'work_title','work_company','work_location','work_from_month','work_from_year','work_to_month',
    'work_to_year','school','degree','field','gpa','edu_from_year','edu_to_year','hear_option'];
  if (!config.facts || keys.some(key => typeof config.facts[key] !== 'string' || !config.facts[key])) {
    throw new Error('Incomplete local driver profile');
  }
  const answers = (config.questions || []).map(({label,type,value}) => {
    if (!label || !['text','radio','select'].includes(type) || typeof value !== 'string' || !value) {
      throw new Error('Invalid local question binding');
    }
    return [new RegExp('^' + esc(label) + '$', 'i'), type, value];
  });
  return {facts: config.facts, answers};
}

function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
const log = (m) => console.error(m); // progress -> stderr; stdout is JSON only

async function pickCombobox(page, label, optionText) {
  // Workday's custom combobox: click the input, then click the option text itself.
  const box = page.getByLabel(label, { exact: false }).first();
  await box.click();
  await page.waitForTimeout(500);
  const opt = page.getByRole('option')
    .filter({ hasText: new RegExp('^\\s*' + esc(optionText)) }).first();
  await opt.click();
  await page.waitForTimeout(300);
}

async function fillDates(page, fromMM, fromYY, toMM, toYY) {
  const spinners = page.getByRole('spinbutton');
  const n = await spinners.count();
  log(`    date spinners found: ${n}`);
  if (fromMM !== null) {
    if (n >= 4) {
      await spinners.nth(0).fill(fromMM);
      await spinners.nth(1).fill(fromYY);
      await spinners.nth(2).fill(toMM);
      await spinners.nth(3).fill(toYY);
    }
  } else if (n >= 2) {
    await spinners.nth(0).fill(fromYY);
    await spinners.nth(1).fill(toYY);
  }
}

async function fillStep1(page, facts) {
  log('  step 1: My Information');
  await pickCombobox(page, 'How Did You Hear About Us?', facts.hear_option);
  await page.getByLabel('First Name', { exact: false }).first().fill(facts.first_name);
  await page.getByLabel('Last Name', { exact: false }).first().fill(facts.last_name);
  await page.getByLabel('Email', { exact: true }).first().fill(facts.email);
  await page.getByLabel('Phone Number', { exact: false }).first().fill(facts.phone);
  for (const [label, val] of [['Address Line 1', facts.address1], ['City', facts.city], ['Postal Code', facts.zip]]) {
    const f = page.getByLabel(label, { exact: false }).first();
    if (await f.isVisible().catch(() => false)) await f.fill(val);
  }
  const stateBtn = page.getByRole('button', { name: /State.*Select One/ }).first();
  if (await stateBtn.isVisible().catch(() => false)) {
    await stateBtn.click();
    await page.getByRole('option').filter({ hasText: facts.state }).first().click();
  }
  await page.getByRole('button', { name: 'Next' }).first().click();
  await page.waitForTimeout(1800);
}

async function fillStep2(page, facts, resumePath) {
  log('  step 2: My Experience');
  await page.getByLabel('Job Title', { exact: false }).first().fill(facts.work_title);
  await page.getByLabel('Company', { exact: true }).first().fill(facts.work_company);
  const loc = page.getByLabel('Location', { exact: true }).first();
  if (await loc.isVisible().catch(() => false)) await loc.fill(facts.work_location);
  await fillDates(page, facts.work_from_month, facts.work_from_year, facts.work_to_month, facts.work_to_year);

  await page.getByLabel('School or University', { exact: false }).first().fill(facts.school);
  const degreeBtn = page.getByRole('button', { name: /Degree.*Select One/ }).first();
  await degreeBtn.click();
  await page.getByRole('option').filter({ hasText: facts.degree }).first().click();
  await page.waitForTimeout(300);
  await pickCombobox(page, 'Field of Study', facts.field);
  const gpa = page.getByLabel('Overall Result (GPA)', { exact: false }).first();
  if (await gpa.isVisible().catch(() => false)) await gpa.fill(facts.gpa);
  await fillDates(page, null, facts.edu_from_year, null, facts.edu_to_year);

  await page.locator('input[type="file"]').first().setInputFiles(resumePath);
  await page.waitForTimeout(1800);

  const li = page.getByLabel('Share your LinkedIn Profile site', { exact: false }).first();
  if (await li.isVisible().catch(() => false)) await li.fill(facts.linkedin);
  await page.getByRole('button', { name: 'Next' }).first().click();
  await page.waitForTimeout(1800);
}

function currentStep(page) {
  const headings = page.getByRole('heading', { level: 3 });
  return headings.first().innerText({ timeout: 3000 }).catch(() => '');
}

async function fillQuestions(page, answers) {
  // Generic filler for Application Questions / Voluntary Disclosures: match each known
  // question by label and answer it. Unknown questions are skipped (logged), never guessed.
  let filled = [];
  for (const [labelRe, type, value] of answers) {
    try {
      if (type === 'radio') {
        const group = page.getByRole('group', {name: labelRe});
        if (await group.count() !== 1) continue;
        const radio = group.getByRole('radio', {name: value, exact: true});
        if (await radio.count() === 1 && await radio.isVisible()) {
          await radio.check();
          filled.push('radio');
        }
      } else if (type === 'select') {
        if ((await page.locator('body').getByText(labelRe).count()) === 0) continue;
        await pickCombobox(page, labelRe.source.replace(/\\/g, '').slice(0, 40), value);
        filled.push('select');
      } else if (type === 'text') {
        if ((await page.locator('body').getByText(labelRe).count()) === 0) continue;
        const f = page.getByLabel(labelRe).first();
        if (await f.isVisible().catch(() => false)) { await f.fill(value); filled.push('text'); }
      }
    } catch (e) { /* skip and continue */ }
  }
  if (filled.length) log('  configured questions filled: ' + filled.length);
  return filled;
}

async function fillRemainingSteps(page, answers) {
  // Advance through Application Questions / Voluntary Disclosures to Review. Stops at
  // Review (submit is the account / CAPTCHA handoff). Best-effort; reports the step it reached.
  for (let i = 0; i < 4; i++) {
    const step = await currentStep(page);
    if (/review/i.test(step)) return { reached: 'review', filled: [] };
    if (/application question|voluntary disclosure/i.test(step)) {
      const filled = await fillQuestions(page, answers);
      await page.getByRole('button', { name: 'Next' }).first().click().catch(() => {});
      await page.waitForTimeout(1800);
      continue;
    }
    break; // unknown step — do not guess
  }
  return { reached: await currentStep(page), filled: [] };
}

async function detectChallenge(page) {
  const url = page.url().toLowerCase();
  let text = '';
  try { text = (await page.locator('body').innerText({ timeout: 3000 })).toLowerCase(); } catch (_) {}
  for (const m of ['captcha', 'recaptcha', 'verify you are human', "i'm not a robot", 'turnstile', 'press and hold', 'arkose']) {
    if (text.includes(m) || url.includes(m)) return m;
  }
  return null;
}

async function detectBlocked(page) {
  // Cloudflare/Workday rate-limit: "An error occurred … Support ID … Client IP" for the
  // whole domain once the IP has been hammered. Back off; do not retry in a loop.
  let text = '';
  try { text = (await page.locator('body').innerText({ timeout: 3000 })).toLowerCase(); } catch (_) {}
  if (/an error occurred/.test(text) && /support id/.test(text)) return 'workday-rate-limit';
  if (/support id/.test(text) && /client ip/.test(text)) return 'workday-rate-limit';
  return null;
}

async function run(jobUrl, privateRoot, resumeFile, outDir, headless, profileDir, delayMs) {
  const privateDir = path.resolve(privateRoot);
  const resume = resumeFile ? path.join(privateDir, 'my-resumes', resumeFile) : null;
  if (!resume || !fs.existsSync(resume)) throw new Error(`resume not found: ${resume}`);
  const {facts, answers} = loadProfile(privateDir);
  const {chromium} = require('playwright');
  const result = {steps: [], handoff: null};
  const viewport = { width: 1366, height: 900 };
  let browser = null;
  let context;
  if (profileDir && fs.existsSync(profileDir)) {

    context = await chromium.launchPersistentContext(profileDir, {
      channel: 'chrome', headless, viewport, locale: 'en-US',
    });
    log(`using persistent profile ${profileDir}`);
  } else {
    browser = await chromium.launch({ channel: 'chrome', headless });
    context = await browser.newContext({ locale: 'en-US', viewport });
  }
  const page = context.pages()[0] || await context.newPage();
  const pause = () => page.waitForTimeout(delayMs || 1500);

  log(`open ${jobUrl}`);
  await page.goto(jobUrl, { timeout: 45000 });
  await page.waitForTimeout(2500);
  const blocked = await detectBlocked(page);
  if (blocked) {
    result.blocked = blocked;
    result.current_url = page.url();
    log(`  BLOCKED: ${blocked} — back off, do not retry`);
    if (browser) await browser.close();
    else await context.close();
    return result;
  }
  const applyBtn = page.getByRole('button', { name: 'Apply' }).first();
  await applyBtn.waitFor({ state: 'visible', timeout: 45000 });

  await applyBtn.click();
  const manualBtn = page.getByRole('button', { name: 'Apply Manually' }).first();
  await manualBtn.waitFor({ state: 'visible', timeout: 45000 });
  await manualBtn.click();
  await page.waitForTimeout(3500);

  await fillStep1(page, facts);
  result.steps.push('my_information');
  await fillStep2(page, facts, resume);
  result.steps.push('my_experience');

  const remaining = await fillRemainingSteps(page, answers);
  if (remaining.reached === 'review') {
    result.steps.push('reached_review');
    result.handoff = 'review';
  }

  const challenge = await detectChallenge(page);
  if (challenge) { result.handoff = challenge; log(`  CHALLENGE: ${challenge}`); }
  result.current_url = page.url();
  if (browser) await browser.close();
  else await context.close();
  return result;
}

async function main() {
  const args = {};
  for (let i = 2; i < process.argv.length; i++) {
    const a = process.argv[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      const v = process.argv[i + 1];
      if (v && !v.startsWith('--')) { args[k] = v; i++; }
      else args[k] = true;
    }
  }
  if (!args.job || !args.private || !args.resume) {
    console.error('usage: node driver.js --job URL --private DIR [--resume FILE] [--out DIR] [--profile DIR] [--delay MS] [--headed]');
    process.exit(2);
  }
  const privateDir = path.resolve(args.private);
  const out = args.out ? path.resolve(args.out) : path.join(privateDir, 'codex-runtime', 'apply-shots');
  const profile = args.profile ? path.resolve(args.profile) : path.join(privateDir, 'chrome-profile');
  const delay = args.delay ? parseInt(args.delay, 10) : 1500;
  const result = await run(args.job, privateDir, args.resume, out, !args.headed, profile, delay);
  console.log(JSON.stringify(result, null, 2));
}

if (require.main === module) {
  main().catch((e) => { console.error('DRIVER_ERROR:', e && e.message); process.exit(1); });
}

module.exports = { loadProfile, detectChallenge, detectBlocked };
