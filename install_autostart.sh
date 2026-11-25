#!/usr/bin/env bash
# Install or remove autostart for AI Trader.
# Delegates to cross-platform scripts/trader.py install/uninstall
# (launchd on macOS, systemd user service on Linux).

set -euo pipefail
cd "$(dirname "$0")"
if [ -f PC_BOT_DISABLED ]; then
  echo "The bot runs on the Oracle server (ubuntu@168.107.87.233). Starting it on this PC would run two bots on the same account. Delete PC_BOT_DISABLED to override." >&2
  exit 1
fi
export PYTHONUTF8=1

case "$(uname -s 2>/dev/null || echo unknown)" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Autostart via this script is not supported on Windows (Git Bash)." >&2
    echo "Use Task Scheduler instead, for example (run in cmd or PowerShell):" >&2
    echo "  schtasks /Create /TN AITrader /SC ONLOGON /RL LIMITED /F /TR \"\\\"$(pwd -W 2>/dev/null || pwd)\.venv\Scripts\python.exe\\\" \\\"$(pwd -W 2>/dev/null || pwd)\scripts\trader.py\\\"\"" >&2
    echo "Remove it with:  schtasks /Delete /TN AITrader /F" >&2
    echo "Or from a Windows shell:  .venv\Scripts\python.exe scripts\trader.py install" >&2
    exit 1 ;;
  *)
    echo "Autostart is not supported on this platform ($(uname -s 2>/dev/null || echo unknown))." >&2
    echo "Supported: macOS (launchd) and Linux (systemd user service). Start the app manually with ./start.sh." >&2
    exit 1 ;;
esac

source ./_bootstrap.sh
ensure_venv

if [[ "${1:-}" == "-Remove" || "${1:-}" == "--remove" || "${1:-}" == "uninstall" ]]; then
    "$VENV_PY" scripts/trader.py uninstall
else
    "$VENV_PY" scripts/trader.py install
fi
