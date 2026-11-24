# Changes

## Patch round 5: migration, 429s, launcher

- **Schema migration**: `DB.__init__` now syncs every table to `SCHEMA` (new `_sync_schema_columns`) before the first write, so a column added to a `CREATE TABLE` can no longer be missing from an upgraded database. `ADDED_COLUMNS` still works. `_insert` also adds a missing column once and retries instead of failing the run ("table signals has no column named relative_strength"). Tests: `tests/test_db_migration.py`.
- **HTTP 429**: removed the second `Broker.last_price`, which silently replaced the cached/batched version, so each symbol was a separate uncached request. `_fetch_snapshots` is serialised and re-checks the cache, so 17 symbols become one request. Broker calls are capped (`BROKER_MAX_CONCURRENCY`), a 429 pauses all callers, retries use exponential backoff with jitter and honour `Retry-After` (`BROKER_RATE_LIMIT_RETRIES`, `BROKER_BACKOFF_SECONDS`), and a price such as "429.50" is no longer mistaken for a 429. The fast loop pre-fetches its armed symbols in one batch. Tests: `tests/test_rate_limit.py`.
- **Fast loop sessions**: it now uses the scheduler's session rule (regular hours with `MARKET_HOURS_ONLY=true`, extended hours otherwise, overnight with `OVERNIGHT_TRADING`). It still never sells. Tests: `tests/test_fastloop_session.py`.
- **Launcher**: `scripts/trader.py` imports `app.*` lazily, so `setup`, `status`, `stop` run before `.venv` exists; `--no-sleep-prevent` no longer crashes. New `_bootstrap.sh` creates `.venv` and installs requirements for `setup.sh`, `start.sh`, `auth.sh`, `install_autostart.sh`; `stop.sh` exits cleanly with no venv.

## Trading sessions: premarket, regular, after-hours and overnight

- **Cause of "Bracket orders not supported outside regular hours"**: the warning came from
  the app, not from Webull. The engine built a bracket flag regardless of the clock and then
  disabled it just before placement whenever the resolved session was not `CORE`. Meanwhile
  `preview_stock_order` always sent `trading_session=CORE`, so previews were wrong outside
  regular hours too.
- **Session clock** (`app/market.py`): `session_phase()` now reports `premarket`,
  `regular`, `afterhours`, `overnight` or `closed`; `order_session()` maps those to Webull's
  `ALL`, `CORE`, `NIGHT` or `None`. Extended hours are 04:00-20:00 ET; overnight is
  20:00-04:00 ET, Sunday evening through Friday morning. Holidays and 13:00 early closes are
  honored. `market_is_open_extended()` now includes after-hours (it previously stopped at
  the regular close).
- **Orders** (`app/broker.py`): `preview_order`, `place_order`, `place_bracket` and
  `place_stop` all resolve the session for the current time (`AUTO` is the default). Bracket
  legs carry the same `support_trading_session`, so an extended-hours entry can keep its
  take-profit and stop-loss. A session that is not open is rejected before anything is sent.
- **Engine** (`app/engine.py`): the session is resolved once before preview. Weekends and
  holidays fail with a clear reason instead of falling back to `CORE`. A bracket rejected by
  the broker for the current session is retried as a single entry in that same session and
  logged; the app's own exit rules still apply. Flatten is allowed in any open session.
- **Overnight brackets**: Webull UAT rejected a combo with `support_trading_session=NIGHT`
  (`OPENAPI_PARAM_ERR: invalid support_trading_session`), while single `NIGHT` limit orders
  were accepted for all 16 watchlist symbols. The engine therefore retries the entry as a
  single `NIGHT` order; the app's exit rules still run every overnight cycle.
- **Scheduler and signals**: with `MARKET_HOURS_ONLY=false`, cycles and signal refreshes run
  in premarket, regular and after-hours. Webull's overnight session is opt-in: set
  `OVERNIGHT_TRADING=true` and list only broker-approved names in `OVERNIGHT_SYMBOLS`.
  Orders in `NIGHT` are refused for symbols not on that list, so an ineligible symbol cannot
  trip the consecutive-order-failure kill switch. With `MARKET_HOURS_ONLY=true`, only
  09:30-16:00 ET runs.
- **Risk** (`app/risk.py`): the execution-time check now accepts any open session when
  `MARKET_HOURS_ONLY=false`, and names the session in the check detail.
- **Tests**: `tests/test_trading_session.py` covers phases, preview/place/bracket/stop
  sessions, the extended-hours bracket fallback and closed-market refusal. Tests now pin
  `WEBULL_ACCOUNT_ID` to the mock account and share the frozen clock with the mock broker
  subprocess via `TRADER_TEST_TIME`.

## ML-first mode

- Training data: the model now trains on triple-barrier labels for every symbol and day in
  the universe, not only on trades the rules approved. Real ledger outcomes override the
  simulated label for the same symbol and entry day (previously they replaced the whole
  dataset once there were 400 of them).
- Selection: with a promoted model, the TA threshold drops to `ML_FIRST_TA_FLOOR`, the model
  must still pass `ML_MIN_P`, and the downtrend and sentiment checks still apply.
- Ranking: BUY candidates are sorted by model probability, and at most `ML_FIRST_TOP_N` new
  BUYs are proposed per cycle.
- Sizing: BUY quantity is scaled by model confidence (`ML_SIZE_*`). It only ever shrinks the
  quantity allowed by the hard limits.
- Promotion: nothing in the app used to promote a model that passed walk-forward testing, so
  it stayed a challenger and was never used. It is now promoted automatically
  (`ML_AUTO_PROMOTE=true`), or by hand with `POST /api/ml/promote` / `/api/ml/reject`.
- Costs: ML training labels are now net of the backtester's round-trip costs (slippage,
  impact, commission), so the walk-forward promotion test compares models after costs. The
  consensus gate also has a "Cost edge" check: a BUY is rejected unless its take-profit
  distance is at least `COST_EDGE_MULT` (default 3) times the estimated round-trip cost.
- Fallback: with no promoted model (or `ML_ENABLED=false` / `ML_FIRST=false`) behaviour is
  unchanged. Retrain the model after upgrading, since the dataset changed.
- Tests: `tests/test_ml_first.py`.

# Changes in this copy

## Phase 0: performance baseline (done)

- **`app/metrics.py`** (new): pure functions for total and annualized return,
  volatility, max drawdown, Sharpe, beta, alpha, tracking error, information
  ratio, daily excess hit rate and turnover. Returns are time-weighted, so an
  external deposit or withdrawal is removed before the day's return is measured.
- **Significance**: `metrics.significance()` reports the mean daily excess return
  vs the benchmark, a Newey-West corrected standard error (automatic lag), a 95%
  confidence interval and a two-sided p-value. Under the minimum window (default
  60 trading days) it returns an explicit `insufficient_data` state.
- **`app/benchmark.py`** (new): daily job that stores one row per completed US
  session in the new `benchmark_daily` table: net liquidation, cash, market
  value, benchmark close, broker day P&L, and an estimated external cash flow
  (`net_liq_t - net_liq_{t-1} - day_pnl_t`). It backfills the sessions still
  present in `equity_snapshots` (up to 40 days), uses the snapshot closest to the
  session close, and is idempotent per day. It is throttled to one broker bar
  request every five minutes by `Supervisor.daily_benchmark()`.
- **`app/performance.py`** (new): assembles the account-vs-SPY report from the
  stored rows. `GET /api/performance` and the dashboard **Account vs SPY** panel
  show account and benchmark return, excess return, drawdown, Sharpe, beta,
  alpha, tracking error, information ratio, hit rate, turnover, the estimated
  flow, and the p-value or the "not enough data yet" state. The panel labels all
  figures as live, never in-sample or out-of-sample.
- **Settings** (`.env.example`): `PERFORMANCE_DAILY_ENABLED` (code default
  false, set to true in `.env.example`; read-only), `PERFORMANCE_BENCHMARK_SYMBOL`
  (SPY), `PERFORMANCE_MIN_DAYS` (60), `PERFORMANCE_HISTORY_DAYS` (400),
  `PERFORMANCE_RISK_FREE_RATE` (0.0).
- **Tests**: `tests/test_metrics.py` (12 cases) covers time-weighted returns with
  deposits, drawdown, Sharpe/beta/alpha, turnover, Newey-West standard errors and
  the insufficient-data/significant/not-significant significance states.
  `tests/test_benchmark.py` (6 cases) covers the daily recorder (close selection,
  flow estimation, unfinished-session skip, idempotency), the report's no-data,
  not-enough-days and enough-days paths, and the new settings. `pytest` ran:
  **103 passed** (18 new). `npm run typecheck` and `npm run build` also passed.
- **Still open**: only ~40 days of equity snapshots are retained, so the
  significance test needs time to reach 60 trading days. The flow estimate is an
  approximation when the app is not running near the close. Core/satellite
  columns exist but stay empty until phase 4.

## Fixed
- **News fallback for Webull UAT**: `get_stock_news` and `search_news` return
  HTTP 404 on the sandbox. `SENTIMENT_RSS_FALLBACK=true` now fetches a public RSS
  feed (Google News search by default, `SENTIMENT_RSS_URL={symbol}` template) only
  after both Webull calls fail. RSS items have no Webull tag, so pair it with
  `SENTIMENT_LLM=true`; the existing Groq scorer rates them and
  `SENTIMENT_SOURCE=blend` uses those scores. Parse failures and network errors
  keep the existing per-symbol backoff, so a dead feed cannot hammer the network.
  `parse_rss_news` strips HTML and parses `pubDate`; tested in `tests/test_parsers.py`.
- **broker.py**: added `Broker.account_balance()`. The core-satellite rebalancer
  (`check_core_rebalance`, `_satellite_headroom_usd`) called it, but the method did
  not exist, so enabling `CORE_ENABLED` would fail every watchdog tick.
- **platform.py**: the single-instance lock only counts LISTEN sockets now.
  `lsof -ti tcp:8000` also returned browsers holding a connection to the app, so a
  clean restart could be refused with "AI Trader is already running (PID <browser>)".
- **scripts/smoke_test.py**: the orphan-exit check sells the whole MSFT position;
  `SIZING_MODE=code` can size the entry above the proposed one share.
- **ml.py**: walk-forward embargo now equals the 20-bar label horizon (was 5, so training labels overlapped the test block). Removed dead `fwd_ret` code and the bogus `hold_hours` formula. Stored models with no provenance or mean AUC > MAX_AUC are refused at load (this blocks the legacy mock-trained model #1, AUC 0.989).
- **consensus.py**: ML check text now says "take-profit before stop-loss" (it is not a 5-day-gain model).
- **lessons.py**: parameter names now match the engine's Settings fields (before, nothing a lesson proposed could ever apply). Splits use chronological order (before, "train" was the newest trades). Current values come from live settings. Pure-Python Mann-Whitney test, no silent p=1.0 without scipy. Proposals require p < 0.05 and improvement on both validation and test blocks. Removed dead `apply_active_lessons`. Fixed `excessive_exit_slippage` mapping (removed).
- **learning.py**: after each review, parameter lessons are proposed into the `lessons` table (status `proposed`, human approval still required). Before, nothing ever created a lesson.
- **engine.py**: a lesson with NULL p-value is no longer auto-applied; `consensus_sent_min` added to auto-applicable params.
- **supervisor.py**: SPY benchmark now compares the real account equity curve with SPY over `SPY_BENCHMARK_WINDOW_DAYS` (cash drag included). Message wording fixed. Skips while the kill switch is on.
- Unused imports removed; `requirements-dev.txt` added (pytest); `requirements-ml.txt` no longer pins unused scipy.

## Removed from the package (not in this copy)
`.env` (rotate those keys), `.venv`, `node_modules`, `.next`, `web/out`, `__MACOSX`, `.DS_Store`, `.pytest_cache`, `__pycache__`, all DB backups and test DBs, lock/flag/log files, `_archive/`, `.scratch/`, `trader_fix_prompt.md`, `trader_followup_prompt.md`, `docs/hft-prompt-audit.md`, `scripts/package.ps1`.

## Operational fixes (round 2)
- **Order sizing**: `SIZING_MODE=code` (default). For BUYs the code sets the quantity from the existing limits (max order qty/USD, per-symbol cap, risk budget at the ATR stop); the model only chooses symbol and price. `SIZING_MODE=llm` keeps the model's quantity and only clamps it down.
- **Groq HTTP 413**: older tool results are shrunk before each model call (`compact_history`; newest 4 stay whole) and one tool result is capped at `AGENT_RESULT_CHARS` (6000, was 12000). A 20-call run's request went from ~240k to ~58k chars in a synthetic test.
- **News failures**: a failing `get_stock_news` symbol now backs off (2 min doubling to 1 h) and logs on the 1st/5th/20th failure instead of every cycle. Sentiment stays neutral while it fails; the root cause (Webull UAT news endpoint erroring) is not fixed here.
- **Stuck runs**: runs still `running` at startup are closed as `error` (`DB.close_orphan_runs`).
- **Cycle mismatch**: new `LLM_MIN_CYCLE_MINUTES` (default 10). The scheduler never wakes faster than this, and logs that it raised `CYCLE_MINUTES` if needed.
- New env vars are documented at the bottom of `.env.example`.
- Added `tests/test_item5_ops.py`. Verified manually (not via pytest): orphan-run cleanup on a real SQLite DB, request-size reduction, settings load.

## Phase 1: event-driven daily-bar backtester (done)

- **`app/backtest/engine.py`** (new): main backtest engine with `Portfolio` class
  (cash, positions, risk accounting) and `run_backtest()` function. Event loop:
  signal on bar[i] close → limit fill on bar[i+1] open → bracket exits on
  bar[i+1] high/low (stop wins when both hit; gap through stop fills at open).
  Reuses live code paths exactly: `indicators.feature_series`, `ta.analyze`,
  `consensus.evaluate`, `sizing.max_allowed_qty`. Portfolio rules mirror the
  live engine: per-order/position USD caps, gross exposure cap, daily loss limit,
  one position per symbol.
- **`app/backtest/costs.py`** (new): pessimistic `CostModel` with commission per
  order, price-bps slippage, ATR-scaled dollar slippage, and square-root market
  impact capped by `BACKTEST_MAX_IMPACT_BPS`. Fill helpers (`buy_fill`,
  `sell_fill`, `stop_exit_price`, `target_exit_price`) model limit/stop/target
  behavior on daily bars (gap handling, stop-before-target priority).
- **`app/backtest/data.py`** (new): `BarStore` CSV cache under
  `BACKTEST_DATA_DIR`, `load_bars()` with optional broker refresh (sequential,
  delayed, one failure never stops the run), user-supplied CSV dir support.
- **`app/backtest/signals.py`** (new): point-in-time signal adapter. Builds
  features for every bar using only history up to that bar (`feature_series`).
  Calls live `ta.analyze` and `consensus.evaluate` with neutral sentiment
  (`sent_n=0`) and optional ML predictor. No look-ahead property validated.
- **Settings** (`.env.example`): `BACKTEST_DATA_DIR`, `BACKTEST_RESULTS_DIR`,
  `BACKTEST_INITIAL_CASH` (25k), `BACKTEST_UNIVERSE` (empty=WATCHLIST),
  `BACKTEST_BENCHMARK_SYMBOL` (SPY), `BACKTEST_MIN_BARS` (60, clamped ≥20),
  `BACKTEST_COMMISSION_PER_ORDER` (1.0), `BACKTEST_SLIPPAGE_BPS` (5.0),
  `BACKTEST_ATR_SLIPPAGE_MULT` (0.03), `BACKTEST_IMPACT_BPS` (5.0),
  `BACKTEST_MAX_IMPACT_BPS` (100.0), `BACKTEST_MAX_GROSS_EXPOSURE_PCT` (100%),
  `BACKTEST_MAX_HOLD_DAYS` (20), `BACKTEST_EXIT_ON_TREND_BREAK` (true),
  `BACKTEST_ML_ENABLED` (false), `BACKTEST_FETCH_COUNT` (1000),
  `BACKTEST_FETCH_DELAY_SECONDS` (1.0).
- **Outputs**: equity curve (one point per completed bar), trade list (matching
  `trade_ledger` fields: entry/exit day/price, qty, SL/TP, MAE/MFE, hold days,
  commission, slippage, exit reason), Phase-0 metrics vs buy-and-hold benchmark
  (account/benchmark/excess return, Sharpe, alpha, tracking error, info ratio,
  hit rate, Newey-West significance).
- **Tests**: `tests/test_backtest.py` (22 cases) — 8 cost model fills (limit,
  stop, target, gap, stop-wins), 5 sizing limits (order USD, position USD,
  risk, no-stop), point-in-time features, no look-ahead property (future data
  doesn't change past signal), full integration on synthetic data, trade pattern
  verification, gross exposure cap.
- **Verified**: `pytest tests/test_backtest.py tests/test_metrics.py
  tests/test_benchmark.py` → **40 passed**. Backtest runs on 3-symbol universe
  with realistic settings, produces ~40 trades, computes all metrics.

## Phase 2: purged/embargoed walk-forward validation (done)

- **`app/backtest/walkforward.py`** (new): shared walk-forward module replacing the
  single walk-forward in `ml.py`. Properly handles:
  - Purge + embargo: no training label overlaps test period, AND test samples whose
    labels depend on training-period data are excluded
  - Overlapping labels: sample weights by uniqueness (inverse of label overlap count)
    or subsampling non-overlapping events; effective sample size (Kish's formula)
    reported
  - Time-expiry outcomes kept as labelled events (barrier not hit = 0), not dropped
  - Multiple-testing control: log every candidate, deflated Sharpe (Bailey & Lopez
    de Prado) / FDR-adjusted p-value (Benjamini-Hochberg) / Bonferroni
  - Minimum independent trades and non-overlapping folds requirement
  - Configurable via `WalkForwardConfig` dataclass
  - Returns `WalkForwardResult` with full fold details and summary statistics
- **`app/ml.py`** (modified): updated to use shared walkforward module via
  `create_ml_walkforward_config()` and `walk_forward()`. Legacy `walk_forward()`
  wrapper maintained for backward compatibility.
- **Tests**: `tests/test_walkforward.py` (16 cases) covers overlap weights,
  effective sample size, subsampling, purge/embargo, BH/FDR, Bonferroni, deflated
  Sharpe, walk-forward integration, purge/embargo no-lookahead property, edge
  cases. `tests/test_ml_provenance.py` (6 cases) passes including leakage/
  saturation guards and sine-wave data rejection.
- **Verified**: `pytest tests/test_walkforward.py tests/test_ml_provenance.py`
  → **22 passed**. ML training now uses purged/embargoed walk-forward with proper
  overlap handling.

## Phase 3: wider universe (done)

- **`app/universe.py`** (new): shared universe module with predefined lists and
  filtering logic:
  - `SECTOR_ETFS`: 15 broad sector + market ETFs (SPY, QQQ, DIA, IWM + 11 Select Sector SPDRs)
  - `BROAD_ETFS`: 23 major equity ETFs across regions, styles, and factors
  - `get_predefined_universe()`: returns predefined list by name
  - `fetch_symbol_info()`: fetches price, volume, history from broker with rate limiting
  - `filter_universe()`: applies min price, min avg dollar volume, min history filters
  - `get_universe()` / `get_universe_sync()`: main entry points for research/backtest/ML
  - ETFs without broker info are kept (assumed liquid); custom symbols are filtered
  - One symbol failing doesn't stop the run; batched fetches with delay
- **`app/config.py`** (modified): added universe settings:
  - `UNIVERSE_NAME`: "sector_etfs" | "broad_etfs" | "sp500_top" | "custom"
  - `UNIVERSE_CUSTOM`: custom symbol list (when name = "custom")
  - `UNIVERSE_TOP_N`: for "sp500_top" (top N by dollar volume)
  - `UNIVERSE_MIN_PRICE`, `UNIVERSE_MIN_AVG_DOLLAR_VOLUME`, `UNIVERSE_MIN_HISTORY_DAYS`
- **`app/ml.py`** (modified): `train()` now uses universe module when symbols not provided;
  adds WATCHLIST as extra symbols; logs universe used; stores universe in result
- **`app/backtest/engine.py`** (modified): `run_backtest()` uses universe from settings
  when not explicitly provided; adds WATCHLIST to predefined universe
- **Settings** (`.env.example`): `UNIVERSE_NAME=sector_etfs`, `UNIVERSE_CUSTOM=`,
  `UNIVERSE_TOP_N=50`, `UNIVERSE_MIN_PRICE=10.0`, `UNIVERSE_MIN_AVG_DOLLAR_VOLUME=20000000`,
  `UNIVERSE_MIN_HISTORY_DAYS=250`
- **Survivorship bias note**: `sp500_top` not fully implemented (no point-in-time S&P 500
  constituents); falls back to sector ETFs with documentation in dashboard/results
- **Tests**: `tests/test_universe.py` (8 cases) covers predefined universes, config
  building, price/volume/history filters, ETF pass-through without info
- **Verified**: `pytest tests/test_universe.py tests/test_backtest.py tests/test_ml_provenance.py`
  → **all pass**

## Phase 4: core-satellite portfolio (done)

- **`app/core.py`** (new): core-satellite portfolio management module:
  - Core holds a target percentage of equity in a broad market ETF (default SPY at 80%)
  - Satellite is the active strategy, bounded by configured limits (default 20% max)
  - Rebalancing: only during market hours (configurable), only when core drifts beyond rebalance band (+/-2% of equity), only above minimum trade size ($100)
  - Uses the same risk-checked execution path (`Engine.execute` / `RiskManager.check`)
  - Selling core to fund satellite buys goes through normal checks
- **Drawdown guard**: when satellite drawdown exceeds threshold (default 10%), can shrink/pause/flatten satellite. Never sells core automatically (except via kill-switch flatten). (The satellite budget cap was only wired into sizing in the patch round below.)
- **`app/config.py`** (modified): added core-satellite settings:
  - `CORE_SYMBOL` (SPY), `CORE_TARGET_PCT` (80), `SATELLITE_MAX_PCT` (20)
  - `CORE_REBALANCE_BAND_PCT` (2.0), `CORE_MIN_TRADE_USD` (100)
  - `CORE_REBALANCE_MARKET_HOURS_ONLY` (true)
  - `SATELLITE_DRAWDOWN_GUARD_PCT` (10.0), `SATELLITE_DRAWDOWN_ACTION` (shrink | pause | flatten_satellite)
- **`app/engine.py`** (modified): added `check_core_rebalance()` method that:
  - Calculates current core/satellite state from broker positions
  - Checks satellite drawdown guard and updates peak equity
  - Creates and executes rebalance plans through normal risk checks
  - Handles `flatten_satellite` action by selling all non-core positions
- **`app/supervisor.py`** (modified): calls `engine.check_core_rebalance()` periodically (every 5 ticks ~2.5 min at 30s watchdog)
- **Settings** (`.env.example`): added `CORE_SYMBOL`, `CORE_TARGET_PCT`, `SATELLITE_MAX_PCT`, `CORE_REBALANCE_BAND_PCT`, `CORE_MIN_TRADE_USD`, `CORE_REBALANCE_MARKET_HOURS_ONLY`, `SATELLITE_DRAWDOWN_GUARD_PCT`, `SATELLITE_DRAWDOWN_ACTION`
- **Tests**: `tests/test_core.py` (20 cases) covers core state calculation, rebalance plans (buy/sell/none, market hours filter, band filter, min trade filter), drawdown guard (disabled, below threshold, shrink, pause, flatten), satellite budget (normal, with drawdown guard, respects core minimum), report formatting, and SPY tracking verification
- **Verified**: `pytest tests/test_core.py tests/test_backtest.py tests/test_metrics.py tests/test_benchmark.py tests/test_universe.py tests/test_walkforward.py tests/test_ml_provenance.py` → **90 passed**

## Still open (not done)
- Lessons are observational (later vs earlier trades), not a counterfactual replay of the proposed value. Needs a backtester.
- Champion/challenger: a new model still replaces the active one if it passes; no shadow period.

## Patch round 3: core-satellite safety and housekeeping
Fixes after reviewing the phase 0-4 delivery:
- **`CORE_ENABLED` (default false)**: core rebalancing and the satellite cap do nothing until it is true. Before, the rebalancer ran every ~2.5 minutes by default and would have tried to build an 80% SPY position.
- **Live/auto guard**: `check_core_rebalance` returns without acting unless trading mode is `auto`, and on a live account unless `ALLOW_LIVE_AUTO_TRADING=true`. Before, it called `_send` directly and skipped the guard in `execute`.
- **Satellite budget is real now**: with `CORE_ENABLED=true`, BUY sizing is capped by `satellite_headroom_usd` (satellite share of equity minus what the satellite already holds, using the drawdown guard's reduced max). Fails closed if account or prices can't be read. Core-symbol buys are exempt. The drawdown guard's `pause`/`shrink` therefore take effect (they only logged before). `calculate_satellite_budget` is no longer used by the engine (still tested; it returns equity not allocated to the core, not remaining headroom).
- **Housekeeping**: removed the `DEBUG` prints from `tests/conftest.py` and `tests/test_backtest.py`, and the stray "Not run" line; removed `.pytest_cache`, `web/out`, `tsbuildinfo`, `mcp_server.log`, `alert.flag` and the copy of the prompt file.
- **Tests added** in `tests/test_core.py`: headroom arithmetic, `CORE_ENABLED` default, paused guard gives zero headroom, engine headroom excludes core, rebalance blocked when disabled / not auto / live without approval.
- **Verified here without pytest** (stub runner, fixture-free tests only): core 25/25, metrics 12/12, walk-forward 16/16, universe 8/8, backtest 11/13 (2 need `fastmcp`, not installed here). Run the real `pytest` on your machine.

### Still open (found in review, not changed)
- **Phase 6 was done without being documented or flagged**: `run_cycle` no longer calls the LLM, and there is no `DECISION_MODE`, so the LLM path can't be compared. `app/agent.py` (including the 413 request compaction) is now unused.
- **Champion/challenger settings (`ML_CHALLENGER_*`) exist in `config.py`/`ml.py`/`db.py` but are undocumented**; no strategy-versions table, shadow mode or rollback was verified. Treat phase 5 as unreviewed.
- **No baseline backtest result has been reported.** Run the backtester on cached bars versus buy-and-hold SPY before tuning anything.
- Phase 7 (nightly learning loop) not started.

## Patch round 4: fast loop, execution-quality report, and a crash fix
- **Crash fix (found while building this)**: `DB.pending_proposals()` did not exist, but the deterministic trading cycle (`_has_actionable_work`, the BUY loop and `_propose_sell`) calls it. Without it every scheduler cycle would raise `AttributeError`. Added to `db.py` with a test. This was in the phase 6 delivery and my earlier review missed it.
- **Fast loop (`app/fastloop.py`, `FAST_LOOP_ENABLED`, default false)**: between slow cycles it arms symbols whose TA score is within `FAST_LOOP_ARM_MARGIN` of `CONSENSUS_TA_MIN` (sentiment/ML already passing, not held, no pending BUY, not on cooldown), reads their live price from the existing `MarketFeed` (push stream, or one batched snapshot poll; REST `last_price` only as fallback), puts the live price into the last daily bar, recomputes TA and runs the same `consensus.evaluate` gate. On a pass it submits a BUY through `Engine._on_proposal` (new optional `sig_override`), so eligibility, code sizing, risk checks, kill switch and approve/auto mode all apply. It never sells. It does nothing when the market is closed, the kill switch is on, or the slow cycle holds the run lock. Per-symbol cooldown `FAST_LOOP_COOLDOWN_MINUTES`; at most `FAST_LOOP_MAX_ARMED` symbols. `SignalService` now keeps the bars of its last refresh (`cached_bars`). Status at `GET /api/fastloop`.
- **Execution quality (`app/execution_quality.py`, `GET /api/execution-quality`)**: signal-to-proposal, proposal-to-order (auto), order-to-fill delays (median/p90), slippage vs the reference price at proposal time, fill rate, split by trigger (`cycle` vs `fast_loop`), plus the list of unfilled orders with their 1/3/5-day forward return from the limit price (was it a winner we missed?).
- **Tests**: `tests/test_fastloop.py` (live-price bar adjustment, arming rules, trigger once then cooldown, kill switch/closed market, defaults off, execution-quality maths, `pending_proposals`). Verified here with a stub runner, not real pytest.
- **Not done**: no dashboard panel for the new endpoints yet (API only). The fast loop uses daily-bar features with a live last bar; it is not an intraday strategy. Whether your Webull feed is in stream or poll mode shows in the startup log ("Live prices: ...").
