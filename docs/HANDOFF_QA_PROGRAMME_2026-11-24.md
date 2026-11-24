# ARCHIVE: QA fix programme handoff (complete, 2026-11-24)

> Moved out of HANDOFF.md on 2026-11-24. Kept for history only. The current state is in HANDOFF.md.


The fix programme from `QA_REPORT.md` is finished. Per-finding status: **`REMEDIATION.md`**. Contracts and file ownership: `docs/ARCHITECTURE_CONTRACTS.md` (§9 = APIs as delivered). Stream payloads: `docs/API_STREAM.md`.

---

## 0. TL;DR

| | |
|---|---|
| **Overall** | Waves 1A, 1B, 1C, 2A-1, 2A-2, 2B and 3 are done. Blockers B1-B3 are cleared. |
| **Tests** | `pytest tests`: see §1 for the command (last run on 2026-11-24: 612 passed, 0 failed, 0 xfailed in 5m20s). Baseline before the programme: 307 passed, 9 failed. |
| **Web** | `npm ci`, `npx tsc --noEmit`, `npm run lint` and `npm run build` all pass (`web/`). |
| **App runnable?** | **Yes.** `python -m app.main` with `BROKER_MODE=mock` starts: reconcile, then workers, then the risk loop. A mock end-to-end run passed (§5). |
| **Next phase** | New options features (debit spreads, covered calls). Options stay off by default until then. |
| **Rollback** | `C:\Users\User\Downloads\Trader_backup_pre_fixes_2026-11-24.tar.gz` (the repo before any change). The folder is not a git repo. |

## 1. How to run the checks

```bash
# Python: requirements.txt + requirements-dev.txt (+ requirements-ml.txt for the ML tests)
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider -o addopts="" --tb=short
python scripts/gen_env_example.py --check       # must exit 0 after any Settings change
cd web && npm ci && npx tsc --noEmit && npm run lint && npm run build
```

The test session runs against temporary `data/` and `logs/` dirs: `tests/conftest.py` sets `TRADER_DATA_DIR` and the new `TRADER_LOG_DIR` before any app import.

## 1b. Resume session (2026-11-24, afternoon): what changed

- **B1:** `app/engine.py` was replaced by the `app/engine/` package (formerly `engine_new`). Every old method and re-export is kept. The original is in the tarball backup.
- **B2:** `web/src/lib/api.ts` is the only HTTP client. It sends `X-Trader-Token` (meta tag; dev fallback `GET /api/auth/token`; one retry after a 403). All raw `fetch` calls go through it.
- **B3:** the hub drops a re-publish of an identical snapshot (the DB and the API publisher both publish `proposals`).
- **2A-1 finished:**
  - The fast loop reads the `SignalStore` (fail-closed) and re-checks the kill switch after its awaits.
  - `docs/API_STREAM.md`: the `events`, `fill` and `kill` shapes are final.
  - 43 new tests in `tests/test_engine_runtime.py`.
- **2A-2 finished:**
  - `DB.add_review()` stores trigger/status (the xfail was removed).
  - Option DTE uses the ET date.
  - `run_backtest_manual.py --compare` was verified.
- **Bugs found on the way** (tests in `tests/test_resume_fixes.py`):
  - Lesson auto-apply never worked (`setattr` on the frozen Settings). It now uses `dataclasses.replace`; status and the fast loop read `engine.s`.
  - The ML scorecard only looked at the newest model.
  - H9 (`importorskip("torch")`).
- **Wave 3 (frontend):**
  - The SSE-fed data hook with tiered polling.
  - The `<Dialog>` primitive with `ConfirmProvider`/`useConfirm`.
  - Error boundaries and `error.tsx`/`global-error.tsx`.
  - A sticky header (LIVE|PAPER · AUTO|APPROVE, data-age chip, Pause engine, Flatten, KILL with Shift+K).
  - The banner rail.
  - A unified `ProposalCard`/`ProposalQueue` with a `<Countdown>` leaf, pinned as an action queue on every tab (3 columns ≥ 2200 px).
  - The always-on `ApproveDrawer`.
  - LearningView and MLScorecard actions wired.
  - Run now, chart overlays, and the reviewer-notes relabel.
  - The CSS split (`src/styles/`), with text ≥ 11 px, `--faint` contrast ≥ 4.98:1, and ▲/▼ on P&L.
  - ESLint 9 flat config.

### Follow-up session (same day): the open items

- **CSS Modules (L13):**
  - 20 `src/components/*.module.css` files; global CSS is now only `src/styles/tokens.css` and `base.css`; everything is Prettier-formatted.
  - Verified by a 1,098-element computed-style comparison before and after, plus a static cascade-order check.
  - Rule of thumb: a class used by one component lives in its module. Shared primitives and data-driven modifier classes (`up`/`down`/`range`, status tones, `banner-*`, `dialog-*`, …) stay global.
- **ESLint 10:** `npm ci` prints a peer warning for `eslint-plugin-react`/`import`/`jsx-a11y`. Their rules were verified to work.
- **Browser click-through:** done in Chrome against the mock broker. Three UI bugs were fixed (Resume engine sent pause, stale chart limit lines, ▲ on $0.00).
- **`SdkTransport` on Webull UAT: verified and completed.**
  - Authenticated via Webull UAT sandbox with credentials from `.env`.
  - Added UAT endpoint routing to `SdkTransport` via `webull_openapi_mcp.sdk_client.UAT_ENDPOINTS`.
  - Added combo order leg normalization (including required `entrust_type: "QTY"`).
  - Silenced Webull SDK's verbose exception/stream/file loggers that leaked payload headers and credentials to console and `webull_trade_sdk.log`.
  - Executed order flow tests in both `BROKER_EXECUTION=sdk` and `BROKER_EXECUTION=mcp`. Both transports successfully authenticate, obtain quotes, and preview orders. (Note: Webull rejects extended-hours bracket orders with `OPENAPI_GTX_NOT_SUPPORT_MARKET` as expected; fallback and single orders succeed and clean up cleanly).
  - Added `OPENAPI_GTX_NOT_SUPPORT` pattern match to `_session_rejection` in [util.py](file:///c:/Users/User/Downloads/Trader/app/engine/util.py) so the engine automatically falls back to single orders during extended hours.

## 2. What is DONE

### Pre-work (orchestrator)
- **Backup:** `Downloads\Trader_backup_pre_fixes_2026-11-24.tar.gz`.
- **Shared modules:**
  - `app/eventhub.py` (`hub.publish/subscribe/unsubscribe/latest`; types `status`, `portfolio`, `proposals`, `events`, `quote`, `fill`, `kill`, `heartbeat`);
  - `app/quotes.py` (`Quote` with `as_of`, `is_fresh()`, `two_sided`, `spread_pct`, `mid`);
  - `app/strategy_docs.py` (`read_strategy`/`read_lessons`, replacing the dead agent's helpers).
- `docs/ARCHITECTURE_CONTRACTS.md`.

### Wave 1A: config, DB, migrations ✅
- **Closed:** T1, M5, L4, L5, L6, L9. Storage side of M3, H3 and M9.
- **Config:**
  - `app/config.py` became the package **`app/config/`** (`env.py`, `settings.py`, `loader.py`, `pricing.py`, `groups.py`).
  - `Settings` is still a flat frozen dataclass, with group views `s.risk`, `.options`, `.ml`, `.feed`, `.alerts`, `.backtest`, `.llm`.
  - `ROOT` is fixed for the new depth.
  - All new fields are listed in contracts §2.
  - **Default changes:** earnings blackout 2→3, `BACKTEST_ML_ENABLED` → true.
- **DB:**
  - `app/db.py` became the package **`app/db/`**: `connection.py` (WAL, busy_timeout 5000, synchronous NORMAL, foreign keys, an RLock around every statement), `schema.py`, `migrations/0001-0004` (tracked by `PRAGMA user_version`), and domain mixins.
  - New: `DB.close()`, `prune()`, `record_atm_iv`/`atm_iv_series`, `save_run_params`/`get_run_params`, `record_lesson_skip`/`record_lesson_applied`.
  - New partial unique index **`ux_proposals_open`**, plus `DuplicateProposalError` raised by `add_proposal`/`update_proposal`.
  - Fixed a latent bug: the `lessons.updated_at` column was missing.
- **Scripts:** `scripts/gen_env_example.py` (with `--check`); `.env.example` regenerated (`SIGNAL_REFRESH_MINUTES=5`).
- `scripts/migrate_db_namespace.py` uses `DATA_DIR` and the numbered migrations, and refuses to run while the app lock is held.
- `scripts/backup_db.py` runs an integrity check and takes `--dest`. It also fixes a prune bug where `-wal`/`-shm` files counted toward the kept backups.
- **Tests:** `tests/conftest.py` closes DBs (`db_tmpdir()`, and `tmp_path` in fixtures). New tests: `test_db_core.py`, `test_config_groups.py`, `test_db_scripts.py`.

### Wave 1B: broker, feed, external APIs ✅
- **Closed:** M2, L1, L3, H4, M6, L2, M22 (sentiment side), plus the broker and mock decomposition.
- **Broker:**
  - `app/broker.py` became **`app/broker/`**: `parsers`, `transport`, `market_data`, `orders`, `options`, `crypto`, `guard`, `sdk`, `router`, `core`, `errors`.
  - Last-line **`OrderGuard`**:
    - On *opening* orders it enforces the kill switch, notional/qty caps (options ×100, contracts cap) and the symbol whitelist.
    - Closing orders always pass.
    - Flatten bypasses it via `async with broker.flatten_mode():` or the kw-only `allow_when_killed=True`.
  - `set_guard_context(kill_switch=, db=)`.
  - Mutating tools are excluded from the negative cache.
  - The caller's `client_order_id` is reused on retries.
  - `broker.mode_warning`.
  - `quote()`/`option_quote()` return a `Quote`, and snapshots carry `as_of`.
  - `OrderRouter` handles mock/mcp/sdk.
  - `SdkTransport` (Webull python SDK) covers stock LIMIT/STOP, combo and cancel only. It **is not verified against a live account**, and options via the SDK fail closed.
- **Feed:** `app/feed.py` became **`app/feed/`** (`TickBus`, `RingBuffer`, `BarAggregator` 1m/5m, `set_subscriptions` budget, throttled `quote` hub events). The old `MarketFeed` API is unchanged.
- **Mock server:** `app/mock_mcp_server.py` is now a shim over **`app/mock/`**. A mock option-snapshot parse bug was fixed.
- **yfinance:** timeouts, negative cache and a circuit breaker.
- **LLM:** 25 s per-request timeout, 90 s overall deadline.
- **Sentiment:** headline sanitizer, LLM JSON validation with clamps, and a pooled client (`SentimentEngine.aclose()`).
- **Logging:** `app/logging_setup.py` silences httpx/httpcore and adds a redaction filter (Telegram token, api_key, Bearer, secrets).
- **New tests:** `test_broker_guard`, `test_feed_bus`, `test_yf_service`, `test_llm_deadline`, `test_llm_sentiment`, `test_logging_redaction`.

### Wave 1C: ops, packaging, deps ✅
- **Closed:** M17, M18, M19, M20, L7, L8, L15.
- **`scripts/trader.py stop`:**
  - It tries the API shutdown first (with the token), then the lock-file PID verified by its command line, then a precise process scan.
  - It never matches itself.
  - On Windows it uses `Get-CimInstance` (or `wmic`).
- **Scripts:**
  - The shell scripts auto-detect `.venv/bin/python` or `.venv/Scripts/python.exe` (`_bootstrap.sh` `detect_venv_py`).
  - New `Stop_Trader.bat` and `Auth_Trader.bat`.
  - `Start_Trader.bat` and `.command` poll `/api/status` before opening the browser.
  - `install_autostart.sh` gives clear messages on unsupported platforms.
- **Packaging:** `scripts/package.py` excludes `.env.*`, `*.db*`, `*.pem`, `*.key`, `*.log` and `api_token`, and the post-check fails the build on a hit.
- **Other:**
  - `requests`, `tzdata`, `pytest` and `pytest-asyncio` are pinned. `pytest-cov>=5.0` is not exact-pinned.
  - `run_backtest_matrix.py` uses a repo-relative path.
- **New tests:** `test_ops_trader_stop`, `test_ops_package`, `test_ops_scripts`.

### Wave 2B: FastAPI, auth, SSE ✅
- **Closed:** M21, M12, L11, H10, §3.4, and the new status keys.
- **Structure:**
  - `app/main.py` is now a thin shim (`app = create_app()`, the lock, the loopback check, `__main__`).
  - **`app/api/`** holds `app.py` (lifespan, middleware, `TokenStaticFiles`), `deps`, `models`, `auth`, and the routers `status`, `portfolio`, `proposals`, `options`, `learning`, `ml`, `research`, `controls`, `chart` and `stream`.
- **Auth:**
  - A per-launch token is written to `settings.api_token_file` (`data/api_token`) and injected as `<meta name="trader-token">`.
  - Non-GET requests need `X-Trader: 1` plus `X-Trader-Token`.
  - `GET /api/auth/token` is gated on `X-Trader`.
- **Confirmations:**
  - Kill release: `{"on": false, "confirm": "RELEASE"}`.
  - Live auto mode: `{"mode": "auto", "confirm": "AUTO"}`.
- **Other route changes:**
  - Empty strategy or lessons text gets 400.
  - `/api/trades` returns one row shape for every filter. This also fixed a latent `closed_trades(limit, symbol)` TypeError.
- **SSE:**
  - `/api/stream` with no params is the multiplexed EventHub stream (prime, diff, heartbeat). With `symbol`/`symbols` it gives the old chart ticks.
  - A publisher task snapshots status, portfolio and proposals every 2 s, but only when someone is subscribed.
  - Payloads are documented in `docs/API_STREAM.md`. `events` and `fill` are provisional.
- **`/api/status` adds:** `data_age_seconds`, `broker_mode_warning`, `paused`, `daily_loss_guard{enabled,tripped,pnl,limit}`, `token_required`.
- **Lifespan:**
  - Startup order: reconcile → workers → risk loop → the rest.
  - Shutdown is the reverse, with each step guarded, then `sentiment.aclose()`, then `broker.stop()`, then `db.close()`.
- **New tests:** `test_api_auth`, `test_api_routes`, `test_api_stream`, `test_api_lifespan`, and `test_api_support` (stubs).

---

## 5. Final integration checklist

- [x] Full pytest suite green (see TL;DR).
- [x] `gen_env_example.py --check` exits 0.
- [x] `python -m app.main` starts on the mock broker. The lifespan order is logged (reconcile, then workers, then the risk loop), and shutdown stops them in reverse and closes the DB.
- [x] Mock E2E (API level, with the token):
  - proposal → approve → placed → filled;
  - kill on: a BUY is blocked while an exit SELL is placed;
  - release without RELEASE → 400, with RELEASE → ok;
  - mode auto/approve, scheduler on/off;
  - flatten: orders cancelled, position sold, kill switch left on.
- [x] Options are still off by default. The fail-closed gate, the stale cutoff and the DTE/expiry exits are covered by unit tests.
- [x] The web build succeeds, and the served `index.html` carries the injected `<meta name="trader-token">`.
- [x] Stray `__pycache__` dirs removed.
- [x] `REMEDIATION.md` written.

## 6. Decisions made along the way (keep or revisit)

1. **The kill switch blocks opening orders only.** Closing and reducing orders (equity SELL of a held position, `*_TO_CLOSE`, protective stops, flatten) always pass, both in the broker guard and in the engine. Blocking exits during a kill would be more dangerous than the problem the kill switch solves.
2. **The option exit manager acts in approve mode too.** It also acts when the kill switch is on. Protective exits are not discretionary.
3. **The pullback rule is opt-in** (`ENTRY_PULLBACK_GATE=false`), because `strategy.md` says "prefer". Max 1 add and no-add-to-loser are **on**.
4. **LLM-written lessons never reach `lessons.md`.** They go to `lessons.proposed.md` and stay `proposed` in the DB until a human approves them.
5. **The `ux_proposals_open` migration resolves existing duplicates:** older `pending` rows become `dismissed`, and an older `submitting` row becomes `unknown`, so reconcile checks it rather than hiding a live order.
6. **`SdkTransport` is minimal and off by default** (`BROKER_EXECUTION=mcp`). It is not validated against a live Webull account, so validate it on UAT before using `sdk`.
7. **`CYCLE_MINUTES=1` was kept** in `.env.example` (the code minimum is 1 and the default is 30). Consider raising it.
8. **Earnings fail-closed is skipped in mock mode** (2A-2 choice), so tests and dev don't depend on a calendar feed.

---

## 7. Housekeeping notes

- `data/alert.flag`, `data/models` and `logs/mcp_server.log` were being written by the test suite (`DATA_DIR`/`LOG_DIR` resolve at import time). This is fixed: conftest isolates both before any app import.
- `web/node_modules` is now installed in the repo (`npm ci`). It is excluded by packaging.
- The durable copy of the pre-programme code is the tarball.
