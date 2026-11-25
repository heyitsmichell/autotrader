#!/usr/bin/env bash
# Turns on the multi-strategy sleeves on the server (approved 2026-11-25, docs/SLEEVES_ARCHITECTURE.md section 9
# "Go-live"). Backs up ~/trader/.env first, then sets the 8 keys. Does NOT restart the bot: the code
# deploy (push_to_server.ps1 -CodeOnly) restarts it right after.
#   ssh -i %USERPROFILE%\.ssh\oracle_trader.key ubuntu@<IP> "bash -s" < deploy/oracle/apply_sleeves_env.sh
set -euo pipefail
cd ~/trader
mkdir -p data/backups
B="data/backups/.env.before_sleeves_$(date -u +%Y%m%d-%H%M%S)"
cp -p .env "$B" && chmod 600 "$B"
echo "backup: $B"
setk() { if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi; }
setk SLEEVES_ENABLED true
setk MAX_RISK_PER_TRADE_USD 2500
setk MAX_POSITION_USD 50000
setk MAX_ORDER_USD 50000
setk MAX_ORDER_QTY 5000
setk MAX_DAILY_LOSS_USD 10000
setk KILL_DAILY_LOSS_USD 60000
setk KILL_MAX_DRAWDOWN_PCT 25
grep -E '^(SLEEVES_ENABLED|MAX_RISK_PER_TRADE_USD|MAX_POSITION_USD|MAX_ORDER_USD|MAX_ORDER_QTY|MAX_DAILY_LOSS_USD|KILL_DAILY_LOSS_USD|KILL_MAX_DRAWDOWN_PCT)=' .env
