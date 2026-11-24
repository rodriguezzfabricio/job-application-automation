# Employer applications

1. Verify the queue entry, original posting day, selected résumé and history. The worker needs a valid lease and the enrolled opening key.
2. Observe the existing form with [the grouped driver](FORM_READER.md). Preserve a conflicting answer. Save only the required evidence in the private directory.
3. At an account wall, use the operator's explicitly configured account flow for that exact employer tenant. Verify the return to the original job. A verification email must match the armed request.
4. Before final review, check required answer readbacks, the attached PDF name and hash, and current dashboard settings. Record `submit_started` before the authorized click.
5. Save the outcome for that opening with `reconcile`. If confirmation is unclear, record an unknown outcome and investigate the existing receipt.

Private answer sources include `form-answers.md`, `facts.md` and `answer-bank.md`. An optional `form-options.json` contains an array of exact field bindings with `meaning`, `field`, `value` and `quote`. The quote must also appear as a complete line in the private answer bank. A private `approved-terms.json` records `company`, `opening_id`, `label` and the full approved `wording` when an opening requires a wording hash.
