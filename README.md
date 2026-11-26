# AI Trader

A personal trading bot with a web dashboard. It watches a watchlist through the official
**Webull MCP server**, scores each symbol with rules (technical analysis, news sentiment,
optional ML), and places swing trades. An LLM (Groq first, then NVIDIA NIM as fallback)
reads the data and writes proposals; code checks every proposal against hard risk limits.
You can approve each trade by hand (`TRADING_MODE=approve`, the default) or let it trade on
its own (`TRADING_MODE=auto`). It runs against a mock broker, the Webull paper account (UAT)
or a live account.

## How a trade happens

1. A run starts from the scheduler (every `CYCLE_MINUTES`) or from **Run analysis now**.
2. The app reads balance, positions, quotes, bars and news, guided by your rules in `strategy.md`.
3. A setup becomes a proposal. Code checks it at once: watchlist, order size, per-symbol
   cap, trade count, daily loss, eligibility (for example the earnings blackout) and the
   signal consensus gate. The LLM is told the result.
4. Passing proposals wait under **Pending proposals** (approve mode) or go straight to the
   broker (auto mode). Before sending, every check runs again with fresh prices.
5. Open positions get a stop, and the exit logic (`app/exits.py`) closes them on a stop
   hit or the time stop.

The LLM never gets the order tools (`place_*`, `cancel_*`, `replace_*`). Only the app's own
code places orders.

## Quick start (mock broker, no Webull account)

Needs Python 3.10 to 3.12 (the Webull package supports nothing newer) and Node.js 20.9+.

1. Get a Groq key at https://console.groq.com/keys .
2. Copy `.env.example` to `.env`. Set `GROQ_API_KEY=...`. Leave `BROKER_MODE=mock`.
3. Start it:
   - Windows: double-click `Start_Trader.bat` (first run creates `.venv`, installs
     `requirements.txt` and builds the dashboard).
   - macOS: `Start_Trader.command`. Linux/macOS shell: `./setup.sh`, then `./start.sh`.
   - Any OS: `python scripts/trader.py setup`, then `python scripts/trader.py run`.
4. Open http://127.0.0.1:8000 and click **Run analysis now**. The mock broker serves
   synthetic prices and a simulated account. Set `MARKET_HOURS_ONLY=false` to test outside
   market hours. Reset the mock account by deleting `data/mock_state.json`.

## Webull setup

1. **OpenAPI access.** Apply at https://developer.webull.com/apis/home (review takes time).
   Create an app to get an App Key and App Secret. Subscribe to market data (else quotes fail with 401/403). Log in to the Webull mobile app with the same account and register the
   device, or auth fails with "Device Not Registered".
2. **Paper trading (UAT).** In `.env`: `BROKER_MODE=webull`, `WEBULL_ENVIRONMENT=uat`,
   `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`. Run `Auth_Trader.bat` (or `./auth.sh`, or
   `python scripts/trader.py auth`) and approve the request in the Webull app. The token lasts
   about 15 days and renews while in use; the app alerts you before it expires.
3. **Going live.** Set `WEBULL_ENVIRONMENT=prod`, run auth again, start with small limits
   (`MAX_ORDER_USD`). The badge turns red. Auto mode on prod is refused unless
   `ALLOW_LIVE_AUTO_TRADING=true`.

## Running it

Run the bot in one place only. Two bots on one account would double every order.

### (a) Hosted on a Linux server (current setup)

Full guide: `deploy/oracle/README.md` (Oracle Cloud, Ubuntu).

- One-time, on the server, from the trader folder: `bash deploy/oracle/server_setup.sh`. It
  adds swap, installs Python 3.12 and the requirements, and registers the systemd **user**
  service `ai-trader`. It does not start the bot.
- First copy of code, database and settings from the PC (the PC bot must be stopped):
  `powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@<server>`
- Later code updates: the same command with `-CodeOnly`. It leaves the server's database,
  `.env` and Webull login alone and restarts the service if it was running. Build the dashboard
  first if you changed `web/`. Only files tracked by git (plus the built `web/out/`) are sent, so
  `git add` new files first. It backs up the server database before the restart and skips the
  restart if the backup fails; packages install pinned by `requirements.lock`.
  The first full copy refuses to run if the server already has a database, `.env` or `conf/`,
  or if the check over SSH fails.
- On the server (`cd ~/trader`): `./auth.sh` to log in to Webull, then
  `systemctl --user start ai-trader`.

| Task | Command (on the server) |
|---|---|
| Status | `systemctl --user status ai-trader` |
| Follow the service log | `journalctl --user -u ai-trader -f` |
| App log | `tail -f ~/trader/logs/app.log` |
| Stop / start / restart | `systemctl --user stop\|start\|restart ai-trader` |
| Renew Webull login | `./auth.sh`, then restart the service |

Change settings by editing `~/trader/.env` on the server and restarting. That file is the live
one; the PC `.env` is not.

**Dashboard.** The dashboard has no password login, so never open it to the internet. Use
either:
- an SSH tunnel: `ssh -i <your key> -N -L 8000:127.0.0.1:8000 ubuntu@<server>`, then open
  http://127.0.0.1:8000; or
- a private-network name (for example Tailscale) listed in `TRUSTED_HOSTS`. The dashboard
  then answers to that host name too. Token and header checks still apply.

### (b) On this PC

Launchers: `Start_Trader.bat`, `Start_Trader.command`, `start.sh`, `Stop_Trader.bat` /
`stop.sh`, `Auth_Trader.bat` / `auth.sh`, and `install_autostart.sh` (all call
`scripts/trader.py`). `python scripts/trader.py install` registers start at sign-in.

**`PC_BOT_DISABLED`.** If this marker file exists in the project root, the launchers
(`Start_Trader.bat`, `Start_Trader.command`, `start.sh`, `install_autostart.sh`) refuse to start.
It is there so a hosted bot and a PC bot never trade the same account. `push_to_server.ps1`
does not copy it. To run on the PC, stop the server bot first, then delete the file.
The launchers and `python scripts/trader.py run` / `install` all check it and refuse to start
(`--override` skips the check). The marker is never copied to the server.

## Safety design

| Layer | What it does |
|---|---|
| Tool allowlist | The LLM gets read-only tools plus `propose_trade`, never order tools. No shorting. Webull keys never reach the LLM |
| Risk checks (`app/risk.py`) | Run on proposal and again before sending; a data error means reject |
| Eligibility and consensus | Earnings blackout, TA/sentiment/news gate, negative-event veto |
| Entry orders | Limit orders. `ENTRY_ORDER_TYPE=marketable` uses a limit just through the touch (`MARKETABLE_OFFSET_BPS`) and chases for `ENTRY_CHASE_SECONDS`; entries are never true market orders. Code default is plain `limit` |
| Exit orders | `EXIT_ORDER_TYPE=market` sends market orders, in the core session only; otherwise limit |
| Stops | Broker stops are always CORE session and GTC |
| Sessions | The order session follows the clock: CORE, ALL (pre/after-hours), NIGHT (overnight, opt-in with `OVERNIGHT_TRADING` and `OVERNIGHT_SYMBOLS`). Webull region `us` accepts only CORE, ALL and NIGHT (no ALL_DAY), so extended-hours entry limits expire at each session boundary. Extended-hours entries are sized down (`OVERNIGHT_SIZE_FACTOR`) and spread-guarded (`MAX_SPREAD_PCT_EXTENDED`) |
| Soft stop | Outside CORE the broker holds no stop, so the app watches prices and sells itself (`EXTENDED_HOURS_SOFT_STOP`). The app must stay up |
| Webull MCP guards | The MCP server enforces `WEBULL_SYMBOL_WHITELIST`, `WEBULL_MAX_ORDER_NOTIONAL_USD`, `WEBULL_MAX_ORDER_QUANTITY`; market calendar in `app/market.py` (`MARKET_CLOSED_DATES`) |
| Order guard (`app/broker/guard.py`) | Caps on every opening order (`MAX_ORDER_USD`, `MAX_ORDER_QTY`, option contracts, whitelist); closing sells must be above 0 and no larger than the position. Exits are never capped by `MAX_ORDER_QTY`, so a large holding can always be closed. Bracket prices are checked before sending |
| Broker calls | Every Webull call has a deadline (`BROKER_CALL_TIMEOUT_SECONDS`, 30). A timed-out order is never resent blindly: the app looks it up by its client order id first, so stops are not doubled |
| Flatten | One flatten at a time; it cancels stops, re-reads open orders and sells only what is not already covered by a working sell. Outside CORE it prices from the live session quote |
| Kill switch | Blocks new opening orders, stops the scheduler, dismisses pending proposals. Exits, stops and flatten still pass. Saved across restarts. Does not cancel orders already at Webull |
| Automatic kill switch (`app/supervisor.py`) | Trips on drawdown, daily loss or repeated failures; only you can release it. `KILL_ACTION`: `pause`, `cancel_entries` or `flatten` |
| Network | Binds to 127.0.0.1 and accepts loopback host names plus those in `TRUSTED_HOSTS` (`app/api/auth.py`). Every state-changing API call needs `X-Trader: 1` and the per-launch token in `X-Trader-Token` (file `data/api_token`). The token reaches the dashboard inside the served page; `/api/auth/token` only answers on localhost or with `TRADER_DEV_TOKEN_ENDPOINT=1`. Responses forbid framing. Behind `tailscale serve`, limit who can reach the machine with Tailscale access rules |

## Options trading

Off by default (`OPTIONS_ENABLED=false`). When on, the app can propose and risk-check
single-leg and multi-leg option orders (OCC symbols, `OPTIONS_ALLOWED_STRATEGIES`). Limits:
`OPTIONS_MAX_CONTRACTS_PER_ORDER`, `OPTIONS_MAX_NOTIONAL_USD`, `OPTIONS_MIN_DTE`/`OPTIONS_MAX_DTE`,
`OPTIONS_ALLOWED_UNDERLYINGS`, plus IV rank, delta, volume, open interest and bid-ask filters.
Option proposals use the same approval, kill switch and live-trading rules as stocks.

**Auto-entry** (`OPTIONS_AUTO_ENTRY=false` by default; needs `OPTIONS_ENABLED`). When the stock
gate passes a long signal on an allowed underlying, the app also buys one long call
(`app/engine/option_entries.py`): expiry nearest `OPTIONS_ENTRY_TARGET_DTE` (30), strike nearest
`OPTIONS_ENTRY_TARGET_DELTA` (0.70), liquid enough, sized in whole contracts to the premium
budget (`OPTIONS_ENTRY_MAX_PREMIUM_USD`, also capped by `OPTIONS_MAX_NOTIONAL_USD` and
`MAX_ORDER_USD`; skipped if one contract costs more). It skips the open and close minutes
(`OPTIONS_ENTRY_SKIP_OPEN_CLOSE_MINUTES`), names with earnings within
`OPTIONS_ENTRY_EARNINGS_DAYS`, and obeys `OPTIONS_ENTRY_MAX_OPEN` and
`OPTIONS_ENTRY_MAX_PER_DAY` (0 = no limit). Core session only.

**Exits** (`app/engine/option_exits.py`): premium stop (`OPTIONS_EXIT_STOP_PCT`), take-profit
(`OPTIONS_EXIT_TAKE_PROFIT_PCT`, 0 turns it off), forced exit at `OPTIONS_EXIT_DTE` days to
expiry, expiry close, and the underlying's stop or time stop (`OPTIONS_EXIT_UNDERLYING_STOP`).
`OPTIONS_SPREAD_LOG` samples real spreads; `OPTIONS_ML_ENABLED` turns on the options learning mode.

## Configuration

- Settings are read from `.env` (environment variables) by `app/config/` (`settings.py` holds
  the typed defaults, `loader.py` reads them). `.env` holds secrets and is git-ignored.
- `.env.example` is generated from the code. After changing a setting, run
  `python scripts/gen_env_example.py --check`; it must exit 0.
- Key groups: `BROKER_MODE`, `TRADING_MODE`, `WEBULL_*`; LLM keys and models; `WATCHLIST`
  and `CYCLE_MINUTES`; risk limits (`MAX_*`, `KILL_*`); exits (`SL_ATR_MULT`,
  `EXIT_MAX_HOLD_DAYS`); orders and sessions (`ENTRY_*`, `EXIT_*`, `OVERNIGHT_*`); gates
  (`ELIGIBILITY_*`, `CONSENSUS_*`); news and sentiment (`SENTIMENT_*`); `OPTIONS_*`; alerts
  (`ALERT_*`); `TRUSTED_HOSTS`.

## Files

```
app/main.py, app/api/   web server, routes, auth and host checks (auth.py)
app/engine/             scheduler, cycle, proposals, execution, flatten, option entries/exits
app/broker/             Webull MCP client      app/mock/, app/mock_mcp_server.py  simulated broker
app/config/             settings.py (defaults), loader.py (.env), groups.py (grouped read-only views)
app/portfolio.py        positions, fills and the trade ledger (+ portfolio_parsing.py, portfolio_analytics.py)
app/sleeves/            the four strategies: manager.py + valuation, orders, rebalance_ops, putwrite_ops, views
app/shadows/            virtual-money shadow strategies
app/db/                 SQLite repositories, migrations, db.transaction()
app/risk.py, eligibility.py, consensus.py, exits.py   limits, entry gates, exit logic
app/supervisor.py       unattended operation: reconnect, unknown orders, auto kill switch
app/market.py, llm.py, alerts.py   market clock, LLM client, alert channels
app/backtest/, strategy_lab/, ml_lab/, rotation_lab/   research code
web/                    dashboard (Next.js, built to web/out)
scripts/trader.py       launcher: run, setup, auth, stop, install, status
scripts/                research and maintenance (experiments.py, gen_env_example.py, smoke_test.py ...)
scripts/archive/        one-off migrations and fixes that were already run; do not re-run
deploy/oracle/          README.md, server_setup.sh, push_to_server.ps1
Start_Trader.bat/.command, start.sh, stop.sh, auth.sh, setup.sh, install_autostart.sh   launchers
strategy.md             your trading rules (the analyst reads it each run)
docs/  data/  logs/  conf/   catalogs and notes; SQLite and experiments; logs; Webull token
```

## Tests

Install the dev requirements (`pip install -r requirements-dev.txt`, plus `requirements-ml.txt`
for the ML tests). Tests freeze the clock at 2026-11-20 and never read your `.env`
(`TRADER_SKIP_DOTENV=1`).

| Command | What it does |
|---|---|
| `python -m pytest` | Full suite, serial by default; use `-n 2` for parallel (`pytest-xdist`, memory-limited) |
| `python -m pytest -m "not slow"` | Skips the 13 tests marked `slow`; a slightly quicker pre-check |
| `python -m pytest -p no:xdist` | Strictly serial, to rule out order-dependent bugs |

Parser tests also run against real Webull output saved in `tests/fixtures/webull/`. `MOCK_REALISM=1`
makes the mock broker fill at bid/ask, fill partially and charge fees.

After changing a setting, also run `python scripts/gen_env_example.py --check`.
Frontend: `cd web && npx tsc --noEmit && npm run lint`.

## Research

Reports in `data/experiments/` cover a
strategy sweep, the rotation lab (`rotation/REPORT.md`, `REPORT_CATALOG.md`), ML (`ml/REPORT.md`),
options (`options_entry/REPORT.md`, `options_research/REPORT.md`), momentum variants and
5-minute entry timing. Catalogs are in `docs/`: `STRATEGY_CATALOG.md`,
`OPTIONS_STRATEGY_CATALOG.md`, `SOCIAL_STRATEGIES.md`, `MOMENTUM_VARIANTS.md`. Most
active strategies lose to the index after costs; nothing here promises otherwise.

## Troubleshooting

- **Red "disconnected" pill:** hover it and check `logs/mcp_server.log`. Common causes:
  missing keys, an expired token (run auth again), "Device Not Registered" (finish device setup
  in the Webull app).
- **News tools return 404 on UAT:** the sandbox has no news endpoint. Set
  `SENTIMENT_RSS_FALLBACK=true` (Google News RSS; sends watchlist symbols to the feed) and
  `SENTIMENT_LLM=true` with `SENTIMENT_SOURCE=blend`.
- **"GROQ_API_KEY is missing":** set it in `.env` and restart.
- **Launcher says the bot runs on the server:** see `PC_BOT_DISABLED` above.
- **Dashboard returns "forbidden host":** add the host name to `TRUSTED_HOSTS`, or use the
  SSH tunnel and http://127.0.0.1:8000.
- **Logs:** `logs/app.log`, `logs/mcp_server.log`; on the server also `journalctl --user -u ai-trader`.

Not investment advice. You are responsible for every order this places.
