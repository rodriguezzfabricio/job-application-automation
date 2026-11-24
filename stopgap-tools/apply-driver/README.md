# Legacy draft driver

Use the grouped worker in `job_runtime/hosted_form_driver.mjs` for production forms and submissions. This optional Playwright driver prepares a draft through review. Supply the résumé filename explicitly and keep the developer-owned browser profile and private facts outside Git.

Run `npm ci` in this directory to install Playwright. Run `node --test` for the local checks. `driver.js` reads the operator's `driver-profile.json` from the directory passed to `--private`. It requires a `facts` object with the contact, education and employment fields listed by `loadProfile`. Optional `questions` entries specify an exact label, the control type and the saved answer. Use a profile and draft that you control.
