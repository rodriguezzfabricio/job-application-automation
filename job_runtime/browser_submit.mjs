const consumedSubmissionGrants = new Set();

/** Call with the observed final button's supported Playwright locator. A late callback never clicks. */
export async function clickWithinGrant(locator, grant, attemptId, now = Date.now) {
  const deadline = Date.parse(grant?.click_before);
  if (!grant?.click_once || grant.attempt_id !== attemptId || !Number.isFinite(deadline)) {
    throw new Error('A committed same-day submission marker is required');
  }
  const remaining = deadline - now() - 1000;
  if (remaining < 1) throw new Error('Final click window expired; reconcile before any retry');
  if (consumedSubmissionGrants.has(attemptId)) throw new Error('Submission grant already used');
  consumedSubmissionGrants.add(attemptId);
  await locator.click({timeoutMs: Math.min(3000, remaining)});
}
