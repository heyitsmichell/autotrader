#!/usr/bin/env bash
# One-time setup: installs Python and Node dependencies, then builds the dashboard.
# Creates .venv first if it does not exist, then delegates to scripts/trader.py setup

set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
source ./_bootstrap.sh
ensure_venv
"$VENV_PY" scripts/trader.py setup
