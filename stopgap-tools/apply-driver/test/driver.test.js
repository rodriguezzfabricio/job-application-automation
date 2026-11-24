'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const { loadProfile, detectChallenge, detectBlocked } = require('../driver.js');

// A fake Playwright page: url() and locator('body').innerText() return what we say.
function fakePage(body, url = 'https://adobe.wd5.myworkdayjobs.com/x') {
  return {
    url: () => url,
    locator: () => ({ innerText: async () => body }),
  };
}

test('a missing private profile fails before opening a form', () => {
  assert.throws(() => loadProfile('/nonexistent-job-application-profile'));
});

test('detectBlocked recognizes the Workday rate-limit page', async () => {
  const page = fakePage('An error occurred.\nPlease contact Workday Support if the problem persists.\nSupport ID: abc – Client IP: 1.2.3.4');
  assert.strictEqual(await detectBlocked(page), 'workday-rate-limit');
});

test('detectBlocked returns null on a real form page', async () => {
  const page = fakePage('2027 University Graduate - Data Scientist | Apply Manually | First Name');
  assert.strictEqual(await detectBlocked(page), null);
});

test('detectChallenge recognizes a CAPTCHA', async () => {
  const page = fakePage('verify you are human', 'https://x/challenge');
  assert.ok(await detectChallenge(page));
});

test('detectChallenge returns null when no challenge text is present', async () => {
  assert.strictEqual(await detectChallenge(fakePage('First Name Last Name Email')), null);
});
