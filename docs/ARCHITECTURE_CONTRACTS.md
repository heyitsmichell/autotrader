# Architecture Contracts for the QA_REPORT.md fix programme

Every workstream reads this file before editing. It fixes **file ownership** (no two agents edit
the same file at the same time) and the **cross-module interfaces** that agents code against.
Finding IDs (H1, M4, L9, ...) refer to `QA_REPORT.md`.

## 0. Ground rules (all agents)

- **Backward compatibility of imports.** When a module becomes a package, `app/<name>.py` becomes
  `app/<name>/__init__.py`. Do not keep both. The `__init__` re-exports **every** name that
  existing code or tests import from the old module, private helpers included (for example
  `_is_option_symbol`, `_session_rejection`, `SCHEMA`, `is_rate_limited`, `retry_after_seconds`,
  `resolve_session`, `bar_day`). Tests that monkeypatch module attributes must keep working.
  Either keep the patched attribute reachable where the code reads it, or update the test you own.
- **Entry points stay put:**
  - `python -m app.main` and `uvicorn app.main:app` must work, so `app/main.py` stays as a thin shim.
  - The mock MCP server must still start the same way `app/broker` launches it.
  - `python run_backtest_manual.py` must still run.
- **Safe defaults.** Never make a default more aggressive. These stay at their defaults:
  `BROKER_MODE=mock`, `OPTIONS_ENABLED=false`, `TRADING_MODE=approve`, `ALLOW_LIVE_AUTO_TRADING=false`.
- **Exits are never blocked.** Kill switch, guard and stale-data checks block *opening* orders.
  Orders that close or reduce a position (equity SELL of a held position, option `SELL_TO_CLOSE`,
  protective stops, flatten) always go through. A stale quote on an exit falls back to a
  marketable limit or stop as the existing code does. It never refuses to exit.
- **Tests.** Run them with the shared scratch venv (Python 3.12, deps plus torch installed):
  ```
  PY="C:/Users/User/AppData/Local/Temp/claude/5b1d5a1d6e0ced79/1e26cead-f164-4957-89ea-1d306ccd729c/scratchpad/venv/Scripts/python.exe"
  cd /c/Users/User/Downloads/Trader && PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest tests -q -p no:cacheprovider -o addopts="" -x --tb=short
  ```
  Baseline before the programme: 307 passed, 9 failed. The 9 were WinError 32 at teardown (T1),
  and test_ml_provenance failed to collect without torch. Other agents edit in parallel, so a
  failure in a module you do not own is reported, not fixed. Never delete a test to make the suite
  pass. Rewrite it only when the behaviour it pins was intentionally changed by a finding, and say
  so in your report.
- **No pip/npm installs into the system Python.** If you need a package, add it to
  `requirements*.txt` (owned by 1C, so report it) and install it into the scratch venv only.
- Do not create `.coverage`, `__pycache__` or `.pytest_cache` in the repo. Use the flags above.
- Report format at the end: files changed or created or deleted, finding IDs closed, any finding
  NOT done or done partially with the reason, contract deviations, and test results (exact counts).

## 1. Ownership

| Wave | Agent | Owns (exclusive write) |
|---|---|---|
| 1 | **1A** Config / DB / migrations | `app/config.py`→`app/config/`, `app/db.py`→`app/db/`, `scripts/migrate_db_namespace.py`, `scripts/backup_db.py`, `scripts/gen_env_example.py` (new), `.env.example`, `tests/conftest.py`, `tests/test_db_migration.py`, the T1 teardown fix in `tests/test_benchmark.py`, `tests/test_kill_switch.py`, `tests/test_risk_checks.py`, `tests/test_signals.py`, new `tests/test_db_*.py`, `tests/test_config_*.py` |
| 1 | **1B** Broker / feed / external APIs | `app/broker.py`→`app/broker/`, `app/feed.py` (may become `app/feed/`), `app/yfinance_service.py`, `app/llm.py`, `app/sentiment.py`, `app/research.py`, `app/alerts.py`, `app/mock_mcp_server.py` (may split into `app/mock/` with the shim kept), `app/logging_setup.py` (new), the logging lines only in `app/main.py` (lines ~40-75), `tests/test_rate_limit.py`, `tests/test_parsers.py`, `tests/test_webull_login.py`, `tests/test_alerts.py`, `tests/test_fractional.py`, `tests/test_mcp.py` *except* the agent tests, new `tests/test_broker_*.py`, `tests/test_feed_*.py`, `tests/test_yf_*.py`, `tests/test_llm_*.py`, `tests/test_logging_*.py` |
| 1 | **1C** Ops / packaging / deps | `scripts/trader.py`, `scripts/package.py`, `setup.sh`, `start.sh`, `stop.sh`, `auth.sh`, `_bootstrap.sh`, `install_autostart.sh`, `Start_Trader.bat`, `Start_Trader.command`, `Stop_Trader.bat` (new), `Auth_Trader.bat` (new), `requirements*.txt`, `run_backtest_matrix.py`, new `tests/test_ops_*.py` |
| 2 | **2A** Engine / strategy / parity | `app/engine.py`→`app/engine/`, `app/exits.py`, `app/risk.py`, `app/consensus.py`, `app/eligibility.py`, `app/ta.py`, `app/indicators.py`, `app/signals.py`, `app/signal_store.py` (new), `app/learning.py`, `app/autopsy.py`, `app/lessons.py`, `app/agent.py` (delete), `app/fastloop.py`, `app/supervisor.py`, `app/sizing.py`, `app/portfolio.py`, `app/ml.py`, `app/universe.py`, `app/execution_quality.py`, `app/core.py`, `app/metrics.py`, `app/performance.py`, `app/benchmark.py`, `app/backtest/*`, `run_backtest_manual.py`, `run_baseline_backtest.py`, `scripts/smoke_test.py`, `scripts/ta_evaluate.py`; **plus `app/config/*` and `app/db/*` in Wave 2**; `tests/conftest.py` in Wave 2; every test file not owned by 2B |
| 2 | **2B** API / auth / SSE | `app/main.py`, `app/api/*` (new), `app/eventhub.py`, `app/strategy_docs.py`, `app/platform.py`, new `tests/test_api_*.py`, `tests/test_item5_ops.py` (only the non-agent parts), `docs/API_STREAM.md` (new) |
| 3 | **3** Frontend | `web/**` (`web/src/**`, `web/package.json`, `web/package-lock.json`, eslint config, `web/node_modules` install) |

In Wave 2, 2B must not edit `app/config/*` or `app/db/*`. It uses the fields and methods listed
below. Anything missing goes in its report for 2A or the orchestrator to add.

## 2. Settings to ADD in Wave 1 (owner 1A, read by everyone)

Add each field to the `Settings` grouping with its env var and default. Existing fields keep
their names. `.env.example` is generated from `Settings`.

| Field | Env | Default | Used by |
|---|---|---|---|
| `options_exit_stop_pct` | `OPTIONS_EXIT_STOP_PCT` | `0.50` (exit when premium is −50%) | 2A H1 |
| `options_exit_take_profit_pct` | `OPTIONS_EXIT_TAKE_PROFIT_PCT` | `0.50` (+50%) | 2A H1 |
| `options_exit_dte` | `OPTIONS_EXIT_DTE` | `2` (force exit when DTE ≤ N) | 2A H1 |
| `options_expiry_close_time` | `OPTIONS_EXPIRY_CLOSE_TIME` | `"15:45"` (ET, expiry day) | 2A H1 |
| `risk_loop_seconds` | `RISK_LOOP_SECONDS` | `1.0` (min 0.25) | 2A §3.3 |
| `options_quote_max_age_seconds` | `OPTIONS_QUOTE_MAX_AGE_SECONDS` | `5.0` | 2A H2/M7 |
| `equity_quote_max_age_seconds` | `EQUITY_QUOTE_MAX_AGE_SECONDS` | `15.0` | 2A, 3 |
| `options_iv_rank_min_history` | `OPTIONS_IV_RANK_MIN_HISTORY` | `60` (days needed for a true rank) | 2A H3 |
| `entry_pullback_gate` | `ENTRY_PULLBACK_GATE` | `false` (strategy.md says "prefer", so it is opt-in) | 2A H6 |
| `entry_pullback_min_pct` / `entry_pullback_max_pct` | `ENTRY_PULLBACK_MIN_PCT` / `_MAX_PCT` | `0.02` / `0.05` | 2A H6 |
| `max_adds_per_symbol` | `MAX_ADDS_PER_SYMBOL` | `1` | 2A H6 |
| `block_add_to_loser` | `BLOCK_ADD_TO_LOSER` | `true` | 2A H6 |
| `eligibility_earnings_blackout_days` | (existing env) | **change default 2 → 3** | 2A M8 |
| `eligibility_earnings_fail_closed` | `ELIGIBILITY_EARNINGS_FAIL_CLOSED` | `true` | 2A M8 |
| `backtest_ml_enabled` | (existing env) | **change default False → True** | 2A H7 |
| `db_retention_days` | `DB_RETENTION_DAYS` | `90` | 1A L4 |
| `broker_execution` | `BROKER_EXECUTION` | `"mcp"` (`"mcp"`, `"sdk"`; mock mode ignores it) | 1B §3.3 |
| `symbol_whitelist` | `WEBULL_SYMBOL_WHITELIST` | empty means no restriction (comma list) | 1B M2 |
| `llm_timeout_seconds` / `llm_deadline_seconds` | `LLM_TIMEOUT_SECONDS` / `LLM_DEADLINE_SECONDS` | `25` / `90` | 1B L2 |
| `yfinance_timeout_seconds` / `yfinance_negative_ttl_seconds` | `YFINANCE_TIMEOUT_SECONDS` / `YFINANCE_NEGATIVE_TTL_SECONDS` | `8` / `120` | 1B M6 |
| `yfinance_breaker_failures` / `yfinance_breaker_cooldown_seconds` | ... | `5` / `300` | 1B M6 |
| `headline_max_chars` | `HEADLINE_MAX_CHARS` | `300` | 1B M22 |
| `feed_ring_size` | `FEED_RING_SIZE` | `2000` ticks per symbol | 1B §3.3 |
| `feed_max_subscriptions` / `feed_options_top_k` | `FEED_MAX_SUBSCRIPTIONS` / `FEED_OPTIONS_TOP_K` | `100` / `4` | 1B §3.3 |
| `signal_refresh_minutes` | (existing) | keep the code default. **`.env.example` uses 5** (was 1) | 1A L9 |
| `api_token_file` | `API_TOKEN_FILE` | `DATA_DIR / "api_token"` | 2B M21 |
| `stream_heartbeat_seconds` | `STREAM_HEARTBEAT_SECONDS` | `15` | 2B §3.4 |

Also expose `MAX_ORDER_NOTIONAL` and `MAX_ORDER_QTY`. The existing `max_order_usd` and
`max_order_qty` stay. `WEBULL_MAX_ORDER_NOTIONAL_USD` and `WEBULL_MAX_ORDER_QUANTITY` are
documented as the MCP-server copies.

## 3. DB facade additions (owner 1A in Wave 1; `from app.db import DB` keeps working)

- `DB.close()`: `PRAGMA wal_checkpoint(TRUNCATE)` then `conn.close()`. It is idempotent.
- Connection setup: `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`,
  `foreign_keys=ON`. A `threading.RLock` serializes **all** statement execution on the shared
  connection (reads included, because the connection is shared across threads).
- Migrations: `app/db/migrations/NNNN_name.py` (or `.sql`), applied in order, tracked by
  `PRAGMA user_version`. The existing ad-hoc ALTER and drift logic becomes migration 0001
  (idempotent on any existing DB).
- `DuplicateProposalError(ValueError)` is exported from `app.db`. `create_proposal` (or whatever
  inserts proposals) raises it when the partial unique index
  `ux_proposals_open ON proposals(symbol, side) WHERE status IN ('pending','submitting')` rejects
  the row. **The migration first dismisses older duplicates** (status → `dismissed`, reason
  "duplicate (migration)") so index creation cannot fail on existing data. If proposals have a
  namespace column, the index includes it.
- `DB.prune(retention_days: int | None = None) -> dict[str, int]` deletes old rows from
  `news_items`, `events`, `llm_usage` and `equity_snapshots` (never ledger, proposals, lessons or
  autopsies). It is called daily by whoever schedules maintenance (2A).
- Indexes on `(symbol, ts)` and `(ts)` (actual column names) for those four tables.
- `atm_iv_history(underlying TEXT, day TEXT, iv REAL, PRIMARY KEY(underlying, day))`, with:
  - `DB.record_atm_iv(underlying, day: str, iv: float)` (upsert);
  - `DB.atm_iv_series(underlying, days=252) -> list[float]` (oldest first).
- `DB.save_run_params(run_id: int, params: dict)` and `DB.get_run_params(run_id) -> dict | None`,
  backed by a `run_params` table or column (M9 provenance).
- `DB.record_lesson_skip(lesson_id, reason: str)` and the lessons query results include
  `apply_state` (`applied` | `skipped` | `pending`) and `apply_reason` (M9, displayed by the UI).
- Existing method names and return shapes are unchanged.

## 4. Broker additions (owner 1B; `from app.broker import Broker, BrokerError, ...` keeps working)

- `OrderGuardError(BrokerError)`.
- `Broker.set_guard_context(kill_switch: Callable[[], bool])` is called by the engine at init (2A).
- `Broker._guard(order: dict)` runs inside every order-placing method (`place_order`,
  `place_bracket`, `place_stop`, `place_option_*`, `replace_option_order`, crypto). It enforces:
  - kill switch on an **opening** order means refuse;
  - notional above `max_order_usd`, or qty above `max_order_qty` (options: contracts above
    `options_max_contracts_per_order`, notional = price × qty × 100), means refuse;
  - symbol (or underlying) outside a non-empty `symbol_whitelist` means refuse.
  Closing and reducing orders pass the kill check (rule 0). A `flatten` context
  (`async with broker.flatten_mode():`, or an `allow_when_killed=True` kwarg) is the explicit
  exemption the flatten path uses.
- The negative cache never applies to mutating tools (place, cancel, replace, modify) (L1).
- Every order path accepts and reuses a caller-supplied `client_order_id`, and never generates a
  new one on retry. The engine persists the coid **before** calling the broker (2A).
- `OrderRouter` lives in `app/broker/router.py`. It selects the transport by
  `settings.broker_mode` / `settings.broker_execution`: `McpTransport` (default), `SdkTransport`
  (Webull OpenAPI python SDK, pooled HTTPS, used only when `BROKER_EXECUTION=sdk`), or the mock
  (MCP mock server). The `Broker` public API is unchanged, and `SdkTransport` must fail closed
  (raise `BrokerError`) on anything it cannot map.
- Snapshots: option snapshot dicts gain `"as_of"` (UTC epoch seconds; fetch time if the API has
  none). Add `Broker.option_quote(symbol) -> app.quotes.Quote | None` and
  `Broker.quote(symbol) -> Quote | None`.
- Startup warning (L3): `BROKER_MODE=mock` while `WEBULL_APP_KEY` or `WEBULL_APP_SECRET` is set
  gives a WARNING log plus `db.log("warn", ...)` when a db is available (or a returned flag that
  2B shows in `/api/status`): `broker.mode_warning: str | None`.

## 5. Feed additions (owner 1B)

- `MarketFeed` keeps its current API (`start`, `stop`, `subscribe`, `unsubscribe`, `last`,
  `mode`, the tick dict format sent to queues).
- New:
  - `feed.quote(symbol) -> Quote | None` (latest, with `as_of`);
  - `feed.ring(symbol) -> RingBuffer` (ticks);
  - `feed.bars(symbol, interval="1m"|"5m", n=...) -> list[dict]` (incremental `BarAggregator`);
  - `feed.set_subscriptions(held: set[str], candidates: dict[str, list[str]])`, which applies
    the budget `held ∪ top_k_per_underlying` up to `feed_max_subscriptions` and re-subscribes
    MQTT topics.
- The feed publishes `hub.publish("quote", {"symbol", "price", "bid", "ask", "ts", "as_of"})`
  (throttled to at most 4 per second per symbol).

## 6. Engine contract (owner 2A)

- `from app.engine import Engine` keeps working. `TradingEngine = Engine` is an alias.
- Lifecycle methods that the 2B lifespan calls, in this order:
  1. `await engine.startup_reconcile()`: the extended `reconcile_orders` (options, orphans
     adopted with status `orphan` plus an alert, ledger deltas, arming exits);
  2. `await engine.start_workers()`: the cold-path SignalStore workers;
  3. `await engine.start_risk_loop()`: RiskLoop plus OptionExitManager;
  4. the existing scheduler or fast-loop start.

  Shutdown is the reverse: `stop_risk_loop()`, then `stop_workers()`, then the existing stops,
  then `db.close()`. Keep any existing methods that main.py calls today.
- The engine publishes on `app.eventhub.hub`:
  - `kill` (`{"on", "reason", "at"}`) on every kill change;
  - `fill` on fills seen by reconcile;
  - `proposals` (the list, same shape as `GET /api/proposals`) whenever a proposal is
    created or changes status;
  - `events` when `db.log` writes. 2A may do this inside `DB.log` in Wave 2, since it owns
    `app/db/*` then.
- `engine.kill_switch`, `set_kill_switch(on, reason)`, `trading_mode` and the scheduler state
  are unchanged. Add `engine.paused` (scheduler off but no dismissal). That is
  `stop_scheduler()` today, so expose it as "Pause engine". No new API is needed beyond the
  `/api/scheduler` route.
- Proposal statuses (the full set, documented in `app/engine/__init__.py` as `PROPOSAL_STATUSES`):
  `pending, submitting, waiting, placed, filled, rejected, dismissed, expired, failed, unknown,
  orphan`. Use whatever subset the code actually writes, and list it exactly.

## 7. API contract (owner 2B; consumed by Wave 3)

- Auth (M21):
  - At startup the server generates a 32-byte urlsafe token and writes it to
    `settings.api_token_file` (`chmod 0600` on POSIX; best-effort on Windows).
  - Served HTML gets `<meta name="trader-token" content="...">` injected before `</head>`.
  - Every non-GET `/api/*` request needs loopback Host + `X-Trader: 1` +
    `X-Trader-Token: <token>`. A missing or wrong token gets 403
    `{"detail": "missing or invalid X-Trader-Token"}`.
  - Dev-mode fallback: `GET /api/auth/token` returns `{"token": ...}` only when the request has
    `X-Trader: 1` (the custom header forces a CORS preflight, which fails cross-origin because
    no CORS is configured).
- Confirmations (M21):
  - `POST /api/kill {"on": false, "confirm": "RELEASE"}` is required to release the kill switch.
  - `POST /api/mode {"mode": "auto", "confirm": "AUTO"}` is required when `is_live`.
  - Otherwise the API returns 400 with a clear detail.
- `PUT /api/strategy` and `PUT /api/lessons` reject empty or whitespace-only text with 400 (M12).
- `GET /api/trades?status=open|closed|all` returns one row shape for every filter (L11). It is
  documented in the Pydantic model.
- SSE (§3.4):
  - `GET /api/stream` with no `symbol` or `symbols` param is the multiplexed EventHub stream.
    It sends `event: <type>\ndata: <json>\n\n` for the types in `app/eventhub.EVENT_TYPES`,
    primes the client with `hub.latest()` plus a fresh status, portfolio and proposals snapshot,
    diffs on the server (skipping identical payloads per type), and sends
    `event: heartbeat` every `stream_heartbeat_seconds`.
  - With `symbol`/`symbols` it keeps today's chart-tick behaviour exactly (MarketChart
    compatibility).
  - The server also runs a publisher task that snapshots status, portfolio and proposals every
    2 s and publishes them only when they change.
  - Document every event payload in `docs/API_STREAM.md`.
- `GET /api/status` gains:
  - `data_age_seconds` (age of the newest quote or portfolio read);
  - `broker_mode_warning`;
  - `paused`;
  - `daily_loss_guard` (`{"enabled", "tripped", "pnl", "limit"}`);
  - `token_required: true`.

## 8. Shared modules created by the orchestrator (already exist)

- `app/eventhub.py`: `hub.publish(type, data)`, `hub.subscribe()`, `hub.unsubscribe(q)`,
  `hub.latest()`.
- `app/quotes.py`: `Quote` (bid, ask, last, as_of, greeks, `is_fresh()`, `two_sided`,
  `spread_pct`, `mid`).
- `app/strategy_docs.py`: `read_strategy()` and `read_lessons()`, which replace
  `app.agent.read_*`.

## 9. Wave 1 as delivered (authoritative where it differs from §3-§5)

**DB (`app/db/`)**
- The proposal insert is `DB.add_proposal(...)`. There is no `create_proposal`. It raises
  `DuplicateProposalError`.
- `update_proposal` also raises `DuplicateProposalError` when a status change would violate
  `ux_proposals_open`, and `claim_pending` returns False in that case.
- The index is `ux_proposals_open ON proposals(symbol, side, COALESCE(broker_mode,''),
  COALESCE(webull_environment,''), COALESCE(account_id,'')) WHERE status IN ('pending','submitting')`.
- Engine call sites that can now raise: the flatten paths (`_flatten_sell*`, which insert a
  `submitting` SELL) and the core-rebalance inserts. **Flatten must never be blocked by it**:
  reuse or settle the existing open row instead.
- Other methods:
  - `DB.close()`;
  - `prune(retention_days=None) -> {"news_items", "events", "llm_usage", "equity_snapshots": n}`.
    **It is not scheduled yet**, so 2A schedules it daily;
  - `record_atm_iv(underlying, day, iv)` and `atm_iv_series(underlying, days=252)`;
  - `save_run_params(run_id, params)` and `get_run_params(run_id)`;
  - `record_lesson_skip(lesson_id, reason)` and `record_lesson_applied(lesson_id, reason="")`.
    Lesson rows carry `apply_state` and `apply_reason`.
- `db.conn.execute(...)` still works (`LockedConnection`, serialized by an RLock).
- Tests: `tests/conftest.py` has `db_tmpdir()`, a drop-in for `TemporaryDirectory` that closes
  DBs first. `tmp_data_dir` uses `tmp_path`.

**Config (`app/config/`)**
- `Settings` is still a flat frozen dataclass, so `dataclasses.replace` works. Group views:
  `s.risk`, `s.options`, `s.ml`, `s.feed`, `s.alerts`, `s.backtest`, `s.llm`.
- Every §2 field exists. `s.max_order_notional` is a property equal to `max_order_usd`.
- `scripts/gen_env_example.py --check` must pass after any Settings change. Re-run the generator
  if you add fields.

**Broker (`app/broker/`)**
- Guard and flatten:
  - `broker.set_guard_context(kill_switch: Callable[[], bool] | None = None, db=None)`;
  - `async with broker.flatten_mode():` (task-local ContextVar);
  - every order method takes the kw-only `allow_when_killed: bool = False`;
  - `OrderGuardError(BrokerError)`.
- Closing orders (equity SELL, `*_TO_CLOSE`) skip the kill, cap and whitelist checks.
- Quotes and snapshots:
  - `await broker.quote(symbol) -> Quote|None` (equity: last and as_of only);
  - `await broker.option_quote(symbol) -> Quote|None`;
  - option and equity snapshot dicts carry `"as_of"`.
- `broker.mode_warning: str|None`. `broker.router.mode` is `"mock" | "mcp" | "sdk"`.
- `SdkTransport` covers stock LIMIT/STOP, combo and cancel only. Options via the SDK fail closed.

**Feed (`app/feed/`)**
- `feed.quote(symbol) -> Quote|None`.
- `feed.ring(symbol)`.
- `feed.bars(symbol, "1m"|"5m", n=None)` returns `{"t", "open", "high", "low", "close", "volume"}`.
- `feed.set_subscriptions(held: set[str], candidates: dict[str, list[str]]) -> list[str]`.
- `quote` events are published on the hub.

**Other**
- `app.logging_setup.setup_logging(files=True)` and `redact()`.
- `SentimentEngine.aclose()` must be awaited at shutdown.
- Build `YahooFinanceService(settings)` with settings.
- `sentiment.sanitize_headline` and `sentiment.validate_llm_rows`.
- `llm.chat_completion` has a deadline and raises `LLMError`.

**Baseline after Wave 1:** 451 passed, 0 failed.
