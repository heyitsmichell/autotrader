#!/usr/bin/env bash
# Starts the dashboard at http://127.0.0.1:8000 (Ctrl+C to stop).
# Delegates to cross-platform scripts/trader.py run

set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
source ./_bootstrap.sh
ensure_venv
"$VENV_PY" scripts/trader.py run
