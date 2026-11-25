#!/usr/bin/env bash
# Starts the dashboard at http://127.0.0.1:8000 (Ctrl+C to stop).
# Delegates to cross-platform scripts/trader.py run

set -euo pipefail
cd "$(dirname "$0")"
if [ -f PC_BOT_DISABLED ]; then
  echo "The bot runs on the Oracle server (ubuntu@168.107.87.233). Starting it on this PC would run two bots on the same account. Delete PC_BOT_DISABLED to override." >&2
  exit 1
fi
export PYTHONUTF8=1
source ./_bootstrap.sh
ensure_venv
"$VENV_PY" scripts/trader.py run
