#!/usr/bin/env bash
# Webull login. Run once, and again whenever the token expires (about every 15 days).
# When it says so, approve the request in the Webull mobile app.
# The token is saved in ./conf (WEBULL_TOKEN_DIR in .env).

set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
source ./_bootstrap.sh
ensure_venv
"$VENV_PY" scripts/trader.py auth
