#!/usr/bin/env bash
# One-time setup on the Oracle Cloud Ampere A1 server (Ubuntu 24.04, ARM).
# Run it from the trader folder on the server:   bash deploy/oracle/server_setup.sh
# Safe to run again: every step checks whether it is already done.
#
# It does NOT start the bot. Log in to Webull first (./auth.sh), then:
#   systemctl --user start ai-trader

set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"

echo "== 1/5 Swap file (a safety margin for memory spikes) =="
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "2 GB swap added."
else
  echo "Swap already set up."
fi

echo "== 2/5 System packages =="
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3.12 python3.12-venv sqlite3

echo "== 3/5 Fix Windows line endings in files copied from the PC =="
find "$ROOT" -path "$ROOT/.venv" -prune -o \( -name '*.sh' -o -name '*.command' \) -type f -print0 \
  | xargs -0 -r sed -i 's/\r$//'
if [ -f "$ROOT/.env" ]; then sed -i 's/\r$//' "$ROOT/.env"; chmod 600 "$ROOT/.env"; fi
chmod +x "$ROOT"/*.sh "$ROOT"/deploy/oracle/*.sh

echo "== 4/5 Python environment (this takes a few minutes) =="
export PYTHONUTF8=1
# shellcheck source=/dev/null
source ./_bootstrap.sh
ensure_venv
# PyTorch for the ML entry model (Results tab: train / score). CPU wheels exist for ARM64.
"$VENV_PY" -m pip install -q -r requirements-ml.txt
# The dashboard is built on the PC and copied over, so the server needs no Node.js.
if [ ! -f "$ROOT/web/out/index.html" ]; then
  echo "WARNING: web/out is missing, so the dashboard page will not load." >&2
  echo "         On the PC run 'npm run build' in web\\, then push again with -CodeOnly." >&2
fi

echo "== 5/5 Background service (starts on boot, restarts after a crash) =="
mkdir -p "$HOME/.config/systemd/user" "$ROOT/data" "$ROOT/logs"
cat > "$HOME/.config/systemd/user/ai-trader.service" <<EOF
[Unit]
Description=AI Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONUTF8=1
Environment=PATH=$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$ROOT/.venv/bin/python $ROOT/scripts/trader.py run --no-sleep-prevent
Restart=on-failure
RestartSec=15
TimeoutStopSec=60
# SIGTERM only the app; it closes the Webull MCP child itself (control-group killed the child first,
# so every loop errored until the SIGKILL)
KillMode=mixed
StandardOutput=append:$ROOT/data/trader.out.log
StandardError=append:$ROOT/data/trader.err.log

[Install]
WantedBy=default.target
EOF
# "linger" keeps your user services running when you are not logged in over SSH.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable ai-trader >/dev/null
echo "Service installed and enabled (not started yet)."

cat <<'EOF'

Setup done. Next:
  1. ./auth.sh                          (approve the request in the Webull app)
  2. systemctl --user start ai-trader
  3. systemctl --user status ai-trader  (should say "active (running)")
  4. tail -f logs/app.log               (Ctrl+C to stop watching)
EOF
