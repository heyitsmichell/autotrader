# AI Trader

A personal, local-only website where Groq analyzes your watchlist through the
official **Webull MCP server** and proposes stock trades. Code checks every
proposal against hard risk limits. You approve trades with one click, or turn on
auto-trade once you trust it.

Groq is the primary LLM provider. The shared LLM client first tries the smaller
Groq model if the primary is unavailable, then NVIDIA NIM. It honors Groq's
`Retry-After` before a final primary retry. This applies to trade analysis,
optional news scoring, and learning reviews.

```
 Browser: http://127.0.0.1:8000  (Next.js static dashboard)
        |
 FastAPI app (app/main.py)
   |-- Groq (openai/gpt-oss-120b) → Groq fallback (openai/gpt-oss-20b)
   |       → NVIDIA NIM (nemotron-3.5-lightning-30b-a3b)
   |       sees READ-ONLY Webull tools + propose_trade
   |       |
   |       v  proposals
   |-- Risk checks (app/risk.py)  -> wait for your approval, or auto mode
   |       |
   |       v  preview_stock_order -> place_stock_order (LIMIT, DAY)
   |-- MCP client (app/broker.py) --stdio--> webull-openapi-mcp --> Webull OpenAPI
```

## How a trade happens

1. A run starts, either when you click **Run analysis now** or from the scheduler
   (every `CYCLE_MINUTES`, market hours only).
2. Groq reads your balance, positions, quotes, bars and news through the Webull
   MCP tools, guided by your rules in `strategy.md`.
3. When it sees a setup, it calls `propose_trade`. Code checks the proposal right
   away: watchlist, $ and share limits, limit price close to the live price, no
   shorting, per-symbol cap, daily trade count, daily loss stop. Groq is told
   the result, so it can fix a rejected proposal.
4. Proposals that pass appear under **Pending proposals**. When you click
   **Approve & place**, every check runs again with fresh prices. Then the
   app previews the order with Webull and places it as a DAY limit order.

Groq never gets the order tools (`place_*`, `cancel_*`, `replace_*`). Only the
app's own code places orders, and only after the checks pass.

## Getting started

The dashboard is a Next.js App Router project in `web/`, exported as static
files to `web/out/` and served by FastAPI. Node.js 20.9 or newer is required
when installing or building the dashboard. `setup.ps1` installs its npm
dependencies and builds it; `start.ps1` builds it automatically if `web/out/`
is missing. The production dashboard uses the same origin as the API, so it
does not need a separate frontend server.

For frontend work, start the FastAPI app, then run `npm run dev` in `web/`.
The development server proxies `/api/*` to FastAPI on port 8000. Rebuild with
`npm run build` before viewing the production version through FastAPI.

### Step 1: Try it with the mock broker (no Webull account needed)

The project is already set up in this folder. On another machine, run `.\setup.ps1` first.

1. Get a Groq API key at https://console.groq.com/keys and, optionally, an NVIDIA API key at https://build.nvidia.com/.
2. Copy `.env.example` to `.env`; set `GROQ_API_KEY=...`, `LLM_MODEL=openai/gpt-oss-120b`, and optionally `NVIDIA_NIM_API_KEY=...`.
   NVIDIA NIM uses `nvidia/nemotron-3.5-lightning-30b-a3b` by default; override with `NVIDIA_NIM_MODEL` if needed.
   Leave `BROKER_MODE=mock`.
3. Run `.\start.ps1` and open http://127.0.0.1:8000.
4. Click **Run analysis now**. The mock broker serves synthetic prices and a
   $25,000 simulated account, so you can watch the full loop: proposals, approvals,
   fills. Set `MARKET_HOURS_ONLY=false` in `.env` to approve trades outside
   market hours while testing. Orders automatically use the matching Webull
   session: `CORE` for regular hours, `ALL` for premarket/after-hours, and
   `NIGHT` for the overnight session; outside every session orders are refused.
   Overnight trading is opt-in with `OVERNIGHT_TRADING=true` and a symbol list in
   `OVERNIGHT_SYMBOLS` (Webull only allows some names overnight).
5. Edit **Strategy** in the dashboard until the proposals match how you think.

To check the setup without calling Groq (free):
`.venv\Scripts\python -m scripts.smoke_test`

### Step 2: Get Webull OpenAPI access

1. Apply for API access at https://developer.webull.com/apis/home. Webull reviews
   applications, so this can take a while.
2. Create an app to get an **App Key** and **App Secret**.
3. Subscribe to market data (quotes). Without it, quote tools return 401/403.
4. In the Webull mobile app, log in with the same account and complete device
   registration (otherwise auth fails with "Device Not Registered").

### Step 3: Paper-trade against the Webull sandbox

1. In `.env`: `BROKER_MODE=webull`, `WEBULL_ENVIRONMENT=uat`, plus your
   `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
2. Run `.\auth.ps1` and approve the request in the Webull app. The token lasts
   about 15 days and renews when used. Rerun `.\auth.ps1` if it expires.
3. Run `.\start.ps1`. The badge at the top shows which environment you're on.

### Step 4: Going live (real money)

Do this only after weeks of sandbox runs whose proposals you agree with.

- Set `WEBULL_ENVIRONMENT=prod`, run `.\auth.ps1` again, and start with small limits
  (`MAX_ORDER_USD`, `MAX_POSITION_USD`, `MAX_TRADES_PER_DAY`).
- The badge turns red: **LIVE (prod)**. Approving asks for confirmation.
- Auto-trade against prod is refused unless you also set
  `ALLOW_LIVE_AUTO_TRADING=true`. Stay in approve mode until you're sure.

## Running unattended

The app can run on its own all day: it restarts after crashes, reconnects to the
broker, settles orders whose status got lost, stops itself when something goes
wrong, and tells you on your phone.

1. **Alerts.** Set at least one channel in `.env`. The easiest is ntfy: install the
   ntfy app, subscribe to a long random topic name, and set `ALERT_NTFY_TOPIC` to
   it. Telegram, a Discord/Slack webhook and email work too. Restart, then click
   **Send test alert** under Operations.
2. **Settings.** In `.env`, use `AUTO_START_SCHEDULER=true` to run scheduled
   analysis and `RESEARCH_SCAN_MINUTES=60` for hourly read-only research scans
   during the U.S. equity day. Screen-qualified equities are passed to Groq for
   review; they still need the signal consensus and hard risk checks. Set
   `AUTO_TRADE_RESEARCH_CANDIDATES=true` to permit those candidates in auto mode.
   `TRADING_MODE=auto` is required for unattended order placement. Start in UAT;
   live auto-trading is separately blocked unless `ALLOW_LIVE_AUTO_TRADING=true`.
   Review the automatic kill switch settings (`KILL_*`).
3. **Start with Windows.** Run `.\install_autostart.ps1` once. It creates a
   scheduled task, "AI Trader", that runs `run_forever.ps1` hidden when you sign in.
   Start it right away with `Start-ScheduledTask "AI Trader"`. Stop the app with
   `.\stop_trader.ps1`, and remove the task with `.\install_autostart.ps1 -Remove`.
4. **Keep the PC awake** during market hours: `powercfg /change standby-timeout-ac 0`.
5. **Webull login.** The token lasts about 15 days and renews while the app uses it.
   If it is about to expire, or needs approval again, you get an alert: run
   `.uth.ps1` and approve it in the Webull app.

What runs in the background (`app/supervisor.py`, every `WATCHDOG_SECONDS`):

| Job | What it does |
|---|---|
| Restarts | `run_forever.ps1` restarts the app if it exits (5 s, doubling to 5 min after quick crashes). The kill switch is saved, so a restart never resumes trading you stopped |
| Broker | Reconnects a dropped Webull MCP connection (30 s, doubling to 10 min); alerts after `BROKER_DOWN_ALERT_MINUTES` |
| Unknown orders | Looks up orders that got no reply by their client order id: found means placed or failed, never found after `RECONCILE_GIVE_UP_MINUTES` means failed. Stops cancelled for such a sell are restored if it didn't fill |
| Scheduler | Starts it when `AUTO_START_SCHEDULER=true`, unless you switched it off or the kill switch is on |
| Automatic kill switch | Net liquidation `KILL_MAX_DRAWDOWN_PCT` below its peak, day P&L below `-KILL_DAILY_LOSS_USD`, or `KILL_MAX_CONSECUTIVE_ERRORS` failed runs/orders in a row. Then `KILL_ACTION`: `pause`, `cancel_entries` (keep stops) or `flatten` |
| Login | Alerts `TOKEN_WARN_DAYS` before the Webull token expires |
| Daily summary | 10 minutes after the close: equity, day P&L, orders, open positions, Groq cost |

Only you can release the kill switch. Releasing it restarts the drawdown peak from the
current value (deposits and withdrawals move net liquidation too, so a large
withdrawal can trip the drawdown trigger).

**Flatten all** (dashboard, or `KILL_ACTION=flatten`) turns the kill switch on, cancels
every open order including stops and targets, and sells every position with DAY limit
orders at the last price minus `FLATTEN_SLIPPAGE_PCT`, split to fit `MAX_ORDER_USD`
and `MAX_ORDER_QTY`. It only runs during regular hours. Check Webull afterwards: a
limit sell in a fast market may not fill.

Logs: `logs/app.log` (rotated), `logs/supervisor.log` (restarts), `logs/mcp_server.log`.

## Safety design

| Layer | What it does |
|---|---|
| Tool allowlist (`app/agent.py`) | Groq only gets read-only tools plus `propose_trade` |
| Risk checks (`app/risk.py`) | Run on proposal and again before sending; any data error means reject |
| Webull MCP guards | The server itself enforces `WEBULL_SYMBOL_WHITELIST`, `WEBULL_MAX_ORDER_NOTIONAL_USD` and `WEBULL_MAX_ORDER_QUANTITY`. They default to your watchlist and limits |
| Order shape | LIMIT, DAY, regular session only. No market orders, no shorting, no options |
| Kill switch | Stops the scheduler, dismisses pending proposals, blocks new orders. It does **not** cancel orders already open at Webull. Saved across restarts |
| Automatic kill switch (`app/supervisor.py`) | Trips on drawdown, daily loss or repeated failures; only you can release it |
| Market calendar (`app/market.py`) | NYSE holidays and 1 pm early closes; add unscheduled closures in `MARKET_CLOSED_DATES` |
| Local only | Binds to 127.0.0.1 and rejects other hostnames. Every change needs an `X-Trader` header, so other websites can't trigger trades from your browser |
| Credentials | Webull keys go only to the MCP server process, never to Groq. `.env` and `conf/` are in `.gitignore` |

Known limits: unscheduled market closures aren't known in advance (add them to
`MARKET_CLOSED_DATES`; otherwise the broker rejects those orders). If a place request
times out, the order is marked **unknown** until the supervisor finds it at the broker
(usually within a minute).

## Options Trading (Phase B)

Options trading is **OFF by default**. When enabled, the app supports:

- **Single-leg orders**: Long CALL/PUT, short CALL/PUT (covered or cash-secured), with position intent (BUY_TO_OPEN, BUY_TO_CLOSE, SELL_TO_OPEN, SELL_TO_CLOSE).
- **Multi-leg strategies**: VERTICAL, STRADDLE, STRANGLE, CALENDAR, DIAGONAL, BUTTERFLY, CONDOR, COLLAR_WITH_STOCK, IRON_BUTTERFLY, IRON_CONDOR (configurable via `OPTIONS_ALLOWED_STRATEGIES`).
- **OCC symbols**: Contracts use standard OCC format (e.g., `AAPL240119C00150000`).
- **Risk limits**: Max contracts per order (`OPTIONS_MAX_CONTRACTS_PER_ORDER`), max premium notional per order (`OPTIONS_MAX_NOTIONAL_USD`, multiplier=100), DTE limits (`OPTIONS_MIN_DTE` to `OPTIONS_MAX_DTE`), allowed underlyings (`OPTIONS_ALLOWED_UNDERLYINGS`).
- **Option consensus gate**: IV rank, delta, and liquidity filters (`OPTIONS_IV_RANK_MAX`, `OPTIONS_IV_RANK_MIN`, `OPTIONS_DELTA_MIN`, `OPTIONS_DELTA_MAX`, `OPTIONS_MIN_VOLUME`, `OPTIONS_MIN_OPEN_INTEREST`, `OPTIONS_MAX_BID_ASK_PCT`).

To enable:
1. Set `OPTIONS_ENABLED=true` in `.env` (off by default).
2. Configure the limits above for your risk tolerance.
3. For live trading, `ALLOW_LIVE_AUTO_TRADING=true` AND `TRADING_MODE=auto` are required (same as equities).
4. The broker must support options (Webull UAT or prod; mock broker simulates options).

The proposal flow for options bypasses the equity consensus gate (TA/sentiment/ML). Instead, it runs the `OptionRiskManager` checks (valid OCC symbol, DTE, notional, contracts, allowed underlying/strategy) **and** the option consensus gate:
- **IV Rank**: For long options, rejects if IV rank exceeds `OPTIONS_IV_RANK_MAX` (default 85%) to avoid overpriced volatility. For short options, rejects if IV rank is below `OPTIONS_IV_RANK_MIN` (default 10%) to avoid underpriced volatility.
- **Delta**: For long directional options, requires absolute delta between `OPTIONS_DELTA_MIN` (default 0.20) and `OPTIONS_DELTA_MAX` (default 0.80).
- **Liquidity**: Minimum volume (`OPTIONS_MIN_VOLUME`, default 100 contracts) and open interest (`OPTIONS_MIN_OPEN_INTEREST`, default 500 contracts).
- **Bid-Ask Spread**: Maximum spread as % of mid price (`OPTIONS_MAX_BID_ASK_PCT`, default 5%).

The dashboard shows option proposals under **Pending proposals** with contract details, premium, and Greeks (from snapshot). Approve/Reject works the same as equity proposals.

**Safety**: Options are never traded in `auto` mode on a live account unless `ALLOW_LIVE_AUTO_TRADING=true`. The kill switch and automatic kill switch apply to options orders too. Option positions are tracked in the database and shown in the dashboard.

## Costs

The configured Groq model is intended for the free tier; token usage is shown in the dashboard, while Groq Console is authoritative for quota and current account limits. Free-tier availability and limits can change. Raise `CYCLE_MINUTES` or reduce `MAX_AGENT_ITERATIONS` to use fewer requests.

## Performance vs SPY (baseline measurement)

The app measures the real account against SPY on the same capital. Once per
completed US session, `app/benchmark.py` stores one row in `benchmark_daily`
with net liquidation, cash, market value, the SPY close, the broker's day P&L
and an estimated external cash flow. The dashboard's **Account vs SPY** panel
and `GET /api/performance` read those rows; no order is placed by this job.

- `app/metrics.py` contains the pure math: total and annualized return,
  volatility, max drawdown, Sharpe, beta, alpha, tracking error, information
  ratio, daily excess hit rate and turnover. Returns are time-weighted: a
  deposit is removed before the daily return is measured, so funding the
  account is not counted as performance.
- The significance block uses a Newey-West corrected standard error of the
  mean daily excess return, which allows for autocorrelation. It reports the
  annualized excess return, a 95% confidence interval and a p-value. Below
  `PERFORMANCE_MIN_DAYS` (default 60 trading days) it says **not enough data
  yet** rather than showing a p-value.
- Cash-flow estimates use the broker's day P&L at the close snapshot. A large
  transfer made while the app was off can still distort one day; the report
  names the method and the total estimated flow.
- This is live measurement, not a backtest. Nothing here guarantees beating the
  index, and most active strategies lose to it after costs.

The daily job is read-only measurement. The code default is off, so enable it
with `PERFORMANCE_DAILY_ENABLED=true` (`.env.example` already does). In this
patched copy the setup scripts are `./setup.sh`, `./start.sh`, `./auth.sh`,
`./stop.sh` and `./install_autostart.sh` (the Windows `.ps1` names elsewhere in
this README are historical).

## Files

```
app/main.py            web server + API
app/engine.py          runs, proposals, order placement, scheduler, kill switch, flatten
app/supervisor.py      unattended operation: reconnect, unknown orders, auto kill switch, login
app/alerts.py          ntfy / Telegram / webhook / email alerts
app/market.py          market clock with NYSE holidays and early closes
app/agent.py           Groq analyst: system prompt, tool allowlist, propose_trade
app/risk.py            hard limits
app/broker.py          MCP client for the Webull server + output parsers
app/mock_mcp_server.py simulated broker with the same tool names and output
web/                   dashboard (Next.js, built to web/out)
run_forever.ps1        runs the app and restarts it if it stops
install_autostart.ps1  Windows scheduled task that starts run_forever.ps1 at sign-in
stop_trader.ps1        stops the unattended app cleanly
strategy.md            your trading rules in plain English (the analyst reads it each run)
.env                   your settings and keys (copy from .env.example)
data/                  SQLite history + mock account state
logs/                  app.log, supervisor.log, mcp_server.log (server stderr), webull_audit.log
conf/                  Webull auth token
```

## Troubleshooting

## Research and candidate discovery

The dashboard's **Research candidates** panel can query Webull US stock movers,
most active stocks, listed option contracts, and crypto spot instruments. These
are read-only source packets with excerpts and caveats, not recommendations.
Missing tools, subscriptions, or region support are reported as source errors.
The analyst's read-only research tools also include Webull SEC-filing results, issuer
profile/news/earnings data, the macroeconomic calendar, and Webull community posts.
Community content is treated as an unverified lead; filing dates and source links
must be checked before using a claim.

Up to ten discovered equities can be added to the in-memory watchlist alongside
the configured `WATCHLIST`; a discovery must appear in both the mover and activity
scans, with price at least $5 and daily volume at least 250,000. Configured symbols
are preserved. Auto-discovered
symbols stay approval-only. In `auto` mode their proposals remain pending until
the symbol is explicitly promoted into `WATCHLIST` and the app is restarted.
Option and crypto research never enters the stock-share proposal/execution path.
The installed Webull MCP package has no multi-leg option or crypto order preview,
so live risk eligibility for those products remains disabled until broker-backed
collateral checks exist. Buying power is not treated as a maximum-loss estimate.

The full 1,569-entry prompt catalog review, including per-entry disposition and
capability/license metadata, is in [`docs/hft-prompt-audit.md`](docs/hft-prompt-audit.md).

## Troubleshooting

- **Red "disconnected" pill**: hover over it, or check `logs/mcp_server.log`.
  Common causes are missing keys, an expired token (run `.\auth.ps1`), or
  "Device Not Registered" (finish device setup in the Webull app).
- **Quotes fail with 401/403**: subscribe to market data in Webull.
- **News tools return HTTP 404 on UAT**: the Webull sandbox has no news endpoint.
  Set `SENTIMENT_RSS_FALLBACK=true` to fetch Google News RSS instead, and set
  `SENTIMENT_LLM=true` with `SENTIMENT_SOURCE=blend` so Groq scores those untagged
  headlines. Without the LLM step, RSS headlines count as neutral. This sends
  watchlist symbols to the feed provider; keep the fallback off if you prefer not to.
  `get_market_sentiment` (Fear & Greed) already works on UAT.
- **"GROQ_API_KEY is missing"**: set `GROQ_API_KEY` in `.env` and restart.
- **Reset the mock account**: delete `data/mock_state.json`.

Not investment advice. Software can be wrong, and so can AI. You are responsible for
every order this places.

---
## Notes on this patched copy
- Setup scripts are `./setup.sh`, `./start.sh`, `./auth.sh`, `./stop.sh`, `./install_autostart.sh` (the `.ps1` names above are from the old Windows setup).
- Dev/test deps: `pip install -r requirements-dev.txt`, then `pytest`.
- See `docs/CHANGES.md` for what was fixed and what is still open.
