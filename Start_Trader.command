#!/usr/bin/env bash
# Double-click on macOS (or run on Linux). First run installs everything; later runs just start.
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
# The Webull MCP package supports Python 3.10-3.12 only; a newer python3 builds a broken .venv.
PY=""
for c in python3.12 python3.11 python3.10; do command -v $c >/dev/null 2>&1 && { PY=$c; break; }; done
if [ ! -x .venv/bin/python ] && [ -z "$PY" ]; then
  echo "Python 3.10, 3.11 or 3.12 is required (found: $(python3 --version 2>&1))."
  echo "Install one, e.g.:  brew install python@3.12   then run this again."
  read -n 1 -s -r -p "Press any key to close"; exit 1
fi
if [ -x .venv/bin/python ]; then
  V=$(./.venv/bin/python -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  case "$V" in 3.10|3.11|3.12) ;; *)
    echo ".venv uses Python $V, which the Webull package does not support. Delete the .venv folder and run again with Python 3.12."
    read -n 1 -s -r -p "Press any key to close"; exit 1;; esac
fi
if [ ! -x .venv/bin/python ] || [ ! -d web/out ] || [ ! -d web/node_modules ]; then
  echo "== First-time setup (Python venv, dependencies, dashboard build) =="
  # The venv is created and filled first; trader.py imports project packages lazily.
  { [ -x .venv/bin/python ] || $PY -m venv .venv; } \
    && ./.venv/bin/python -m pip install --upgrade pip \
    && ./.venv/bin/python -m pip install -r requirements.txt \
    && ./.venv/bin/python scripts/trader.py setup || { echo "Setup failed."; read -n 1 -s -r -p "Press any key to close"; exit 1; }
fi
# Open the browser only once the server answers (poll /api/status, ~1s interval, 60s max).
( for _ in $(seq 1 60); do
    if curl -fsS -m 2 http://127.0.0.1:8000/api/status >/dev/null 2>&1; then
      open http://127.0.0.1:8000 2>/dev/null || xdg-open http://127.0.0.1:8000 2>/dev/null
      break
    fi
    sleep 1
  done ) &
./.venv/bin/python scripts/trader.py run
read -n 1 -s -r -p "Stopped. Press any key to close"