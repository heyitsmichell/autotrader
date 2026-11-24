#!/usr/bin/env bash
# Stops the unattended app cleanly.
# Delegates to cross-platform scripts/trader.py stop

set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
source ./_bootstrap.sh
if ! detect_venv_py; then
  echo "No .venv found (looked for .venv/bin/python and .venv/Scripts/python.exe), so the app was never set up here. Nothing to stop."
  exit 0
fi
"$VENV_PY" scripts/trader.py stop
