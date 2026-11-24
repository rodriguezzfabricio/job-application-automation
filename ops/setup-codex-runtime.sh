#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -m venv "$ROOT/stopgap-private/codex-venv"
"$ROOT/stopgap-private/codex-venv/bin/python" -m pip install -r "$ROOT/requirements-codex.txt"
"$ROOT/stopgap-private/codex-venv/bin/python" -m unittest discover -s "$ROOT/tests/migration" -p 'test_*.py' -q
