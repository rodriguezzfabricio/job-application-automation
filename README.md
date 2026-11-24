# Job application automation

Run a synthetic job-matching demo from the repository root with Python 3.12 or newer.

```sh
python3 -m venv app/.venv
app/.venv/bin/python -m pip install -r app/requirements.txt
demo_dir=$(mktemp -d)
app/run-demo demo --output "$demo_dir"
```

The demo processes sample postings and prints the match decisions, generated packets, external message count and paid model call count. Its network guard keeps the run offline.

## Components

| Directory | Purpose |
| --- | --- |
| `job_runtime/` and `codex/` | Worker leases, application eligibility, saved-answer verification, browser form handling, receipts and operator instructions |
| `stopgap-tools/` and `board_apis/` | Source readers, tracker utilities, résumé matching and optional local integrations |
| `app/` | Standalone matching, eligibility, packet generation and the offline demo |
| `webapp/` | HTTP service with a SQLite database, polling and health checks |
| `ops/` and `tests/` | Optional macOS installers and migration regression tests |

## Configure a worker

1. Choose an absolute, owner-only directory and set `JOB_AUTOMATION_PRIVATE_ROOT` to it. The default is the ignored `stopgap-private/` directory at the repository root.
2. Run `sh ops/setup-codex-runtime.sh` to install the worker's Python dependencies and run its migration tests. The separate `app/.venv` holds the demo's dependencies.
3. Run `codex/jr -h` to inspect the commands. Populate your own tracker, dashboard configuration, résumés and saved answers before enabling a live worker. See [the worker brief](codex/BRIEF.md).

The public checkout contains synthetic samples. Each operator supplies account settings, application permissions, documents and credentials locally. Browser-assisted stages require a supported browser host. A final submission is counted only after reconciliation against the same opening.

## Run the local checks

```sh
python3 -m unittest discover -s tests/migration -p 'test_*.py'
(cd app && .venv/bin/python -m unittest discover -s tests -p 'test_*.py')
(cd webapp && python3 -m pytest -q tests)
(cd stopgap-tools && python3 -m pytest -q .)
node --test tests/migration/*.mjs stopgap-tools/apply-driver/test/*.js
```

The web and tool suites use pytest. The browser replay test uses a local Chromium installation and a temporary profile. `JR_REPLAY_CHROMIUM` selects an alternative Chromium executable. The optional routing-hook test reads the module named by `JOB_ROUTING_HOOK`.
