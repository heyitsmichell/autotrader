# QA Report — AI Trader (forensic audit)

**Date:** 2026-11-24 · **Scope:** entire repository (read-only pass; no source files modified)
**Method:** six parallel audit tracks (Sonnet, low effort) → orchestrator cross-check and targeted re-verification of every HIGH claim and every claim where tracks disagreed (Opus) → dynamic checks run in isolated scratch environments (scratch venv for pytest, a copy of `web/` for `tsc`/`next build`), so no `node_modules`, `.venv`, or caches were written into the repo.

Legend for evidence: **[V]** = re-verified by the orchestrator against the source; **[T]** = reported by a track with file:line, not independently re-read; **[U]** = track flagged as unverified.

---

## 1. Executive Summary & Health Scorecard

The equity path is the most mature part of the system. It has a persisted kill switch, an atomic proposal claim with `_exec_lock`, uuid4 client order IDs used as idempotency keys, startup `reconcile_orders()`, a 429-aware broker wrapper with jittered backoff, loopback-only binding with an `X-Trader` CSRF header, and deterministic (no-LLM) proposal generation. The weak spots are elsewhere:

1. **The options path is not production-safe.**
   - The gate fails open on every missing datum. The IV "rank" is raw IV.
   - There are no quote, IV or Greeks staleness checks.
   - There is no stop or target and no bracket.
   - **No code anywhere exits a held option, including before expiry.**
   - `OPTIONS_ENABLED=false` by default is the only thing keeping this dormant.
2. **Spec vs. reality divergence.** `app/agent.py`, the LLM agent that `strategy.md` and `lessons.md` were written for, is **never invoked** [V]. Several things follow from that:
   - Trading is purely deterministic.
   - The strategy text edited in Settings has no effect on live decisions.
   - The free-text lessons loop is disconnected; only structured DB lessons change parameters.
   - The default backtest runs rules-only, while live runs ML-first, so the baseline metrics do not describe the live rule.
3. **Fail-open guards.**
   - The daily-loss guard passes when broker P&L parses to `None` [V].
   - The option execute path never re-checks the kill switch [V].
   - `Broker.place_order` has no last-line notional, quantity or kill gate [T].
4. **Secret hygiene.** The Telegram bot token is embedded in the request URL, and `httpx` logs request URLs at INFO, which the root logger enables [V].
5. **Tests.**
   - On a default dev install, collection aborts on a top-level `import torch` [V].
   - With that file excluded: **307 passed, 9 failed**. All 9 are Windows `WinError 32` teardown errors, because `DB` has no `close()`.
   - With torch installed, the ML tests pass (20/20).
   - Line coverage is **50%**. `main.py` and `feed.py` are at **0%**, and `engine.py` is at 47%.
6. **Frontend.**
   - It type-checks and builds cleanly [V].
   - There is no error boundary, so a crash in any panel takes the kill switch down with it.
   - Paper approvals place orders with no confirmation.
   - The live confirm hardcodes "shares", so it is wrong for option contracts.
   - Lesson actions are `alert()` stubs.
   - A flat 10 s poll of 9 endpoints generates about 54 req/min and re-renders the whole tree.

| Area | Score /10 | One-line rationale |
|---|---|---|
| Backend & Crash Safety | **6.5** | Solid equity lifecycle and reconciliation. Fail-open daily-loss guard, no broker-level gate, fast-loop TOCTOU. |
| Strategy & Options Parity | **3.5** | Options gate fails open with no exits. Backtest ≠ live rule (ML off). Strategy text is not enforced. |
| API / Real-Time Readiness | **5** | MQTT stream with poll fallback exists. yfinance has no timeout or negative cache. Run lock held across network I/O. No OPRA streaming. |
| Security | **6** | Loopback bind plus CSRF header. Token leaks to logs. Any local process can approve or flip to auto. |
| Frontend / API Integration | **6** | No 404 calls. Unwired lesson, ML and scheduler controls. Silent fallbacks and status-enum drift. |
| UI/UX | **5** | Good env banner and confirmed flatten/kill. No error boundary, no staleness surfacing, weak approve review, a11y gaps. |
| Test / Ops Readiness | **4.5** | Suite blocked by the torch import. Ops scripts are Unix-only on a Windows host. `stop` can kill itself. `package.py` can leak `.env.*`. |

### Cross-track reconciliation (contradictions resolved by the orchestrator)

| Claim (track) | Verdict | Evidence |
|---|---|---|
| "A stuck LLM call blocks fast-loop stop/target checks" (2B) | **Refuted as stated.** The fast loop *never sells* (`app/fastloop.py:16`). The real effect is that the slow cycle holds `_run_lock` across `research.refresh_if_due()` and `signals.refresh()` (`app/engine.py:359-375`). That delays signal-based exits and makes the fast loop skip (`fastloop.py:163`). The LLM is only inside this lock when `SENTIMENT_LLM=true` (default false, `config.py:499`). | [V] |
| "Lessons are injected into the LLM system prompt, so the free-text loop is wired" (1) | **Refuted.** `build_system_prompt`/`run_analysis` (`app/agent.py:152,274`) have no callers. `main.py:20` imports only `read_lessons`/`read_strategy` for display. `lessons.md` is read only by the learning reviewer (`learning.py:255`). | [V] |
| "Options execute bypasses the kill switch" (2A, HIGH) | **Confirmed, downgraded to MEDIUM.** `execute()` calls `option_risk.check_single_leg(p)` with no kill argument (`engine.py:1101`). However, `set_kill_switch` dismisses all pending proposals (`engine.py:262`) and `_on_proposal` checks the kill switch before any proposal is stored (`engine.py:609`). The gap is defence-in-depth, not an open path. | [V] |
| "`/api/status` may leak API keys" (2B, U) | **Refuted.** The only reference is a truthiness check of `nvidia_nim_api_key` that selects a label (`main.py:~237`). The real leak is via logs (BUG-H4). | [V] |
| "Strategy text overwritten with empty string on failed load" (3) | **Confirmed, impact reduced.** `PUT /api/strategy` writes `body.text.strip()` with no empty guard (`main.py:1109`). The UI swallows the initial GET error (`useDashboardData.ts:93`). Because `strategy.md` does not drive trading, the damage is loss of the document plus a degraded learning-review context (`learning.py:256`). | [V] |
| "Kill switch dismisses pending proposals — surprising" (4) | **Confirmed as designed** (`engine.py:262`). Keep the behaviour, but name it in the UI, and add a separate "Pause engine" control. | [V] |

---

## 2. All Bugs & Defects (ranked)

No CRITICAL findings exist in the **default configuration** (`BROKER_MODE=mock`, `OPTIONS_ENABLED=false`, `TRADING_MODE=approve`, `ALLOW_LIVE_AUTO_TRADING=false`). Items tagged **⚠ blocker-if-enabled** become CRITICAL the moment options are enabled on a live account.

### HIGH

| ID | File:Line | Category | Description / Impact | Recommended Fix |
|---|---|---|---|---|
| H1 ⚠ | `app/engine.py` (no option exit path); `app/exits.py` (equity-only); `app/risk.py:291` | Options exits | No code exits a held option. There is no time-to-expiry exit, no pre-close exit and no stop or target. Option proposals store `sl/tp=None` (`engine.py:~1030`) and are sent as single orders without a bracket (`engine.py:1194-1220`). The `exits.decide_exit` loop keys off equity signals. A long option can therefore decay to zero or be auto-exercised unattended. [V: grep for expiry/DTE/option-exit in engine.py returns nothing] | Add an `OptionExitManager` that runs in the fast loop: a premium stop (for example −50%), a premium target, forced exit at T−N DTE, and forced close at 15:45 ET on expiry day. Place a protective resting order where the broker supports it. |
| H2 ⚠ | `app/engine.py:924-1015` | Options gate | Every missing datum passes the check. A failed snapshot gives `snapshot={}`, then IV, delta, volume, OI and spread each `add(..., True, "not available, skipping")`. A bid or ask of 0/None also skips the spread check. A contract with no quote therefore passes the liquidity gates. [T] | Fail closed. A missing snapshot, a missing bid or ask, or a crossed or locked market means reject. |
| H3 ⚠ | `app/engine.py:955-969`, `config.py:320` | Options quant | "IV Rank" is absolute IV compared with `options_iv_rank_max=0.85`. The code comment admits it is a proxy. It blocks high-vol names permanently and never blocks rich IV on low-vol names. [T] | Store a daily ATM IV per underlying (252-day window) and compute a true IV rank or percentile. Until that exists, rename the setting so users are not misled. |
| H4 | `app/alerts.py:173`, `app/main.py:40` | Security / secrets | The Telegram URL contains `bot{token}`. `logging.basicConfig(level=INFO)` lets the `httpx` logger emit `HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage` to console and log files. Nothing silences `httpx`/`httpcore` (grep is empty). [V] | `logging.getLogger("httpx").setLevel(logging.WARNING)` (same for `httpcore`), plus a redacting log filter for `bot\d+:[\w-]+` and `api_key=`. |
| H5 | `app/risk.py:198-204` | Financial guardrail | The daily-loss guard fails open. `hit = pnl is not None and pnl <= -limit`, so a `None` from `broker.parse_day_pnl` passes as "n/a". (A `BrokerError` correctly fails closed.) The guard also applies only to BUY, which is correct, but only if `max_daily_loss_usd > 0`. [V] | When the guard is enabled and `pnl is None`, reject the BUY with "day P&L unavailable". |
| H6 | `app/agent.py:152-298` (no callers); `app/main.py:20,1107-1118`; `web/src/components/SettingsPanel.tsx:35` | Spec fidelity / dead code | The LLM agent is dead code. `strategy.md` rules that exist only in prose are enforced nowhere: "pullback 2-5% below the 10-day high", "add at most once", "never add to a loser". "Save strategy" and "Save lessons" in Settings change nothing about trading, but the UI implies they do. [V] | Either (a) delete the agent and relabel Settings as "Notes for the learning reviewer", or (b) encode the missing rules as deterministic gates in `consensus.py`/`risk.py`. (b) is recommended for the pullback and add-once rules. |
| H7 | `app/backtest/signals.py:76`, `config.py:551`, `run_backtest_manual.py:199`; `app/consensus.py:44-70` | Backtest/live parity | `BACKTEST_ML_ENABLED=False` and `ml_predictor=None`, so the backtest uses `consensus_ta_min=0.15`. Live (`ML_ENABLED`, `ml_first=True`) uses `ml_first_ta_floor=0.0` and a `ml_p>0.7` downtrend override. The published baseline therefore does not describe the decision rule that trades. [T] | Make the backtest default mirror the live settings snapshot (ML on, using the promoted checkpoint with walk-forward retraining). Report both variants side by side. |
| H8 | `web/src/app/` (only `layout.tsx`, `page.tsx`, `globals.css`) | Frontend resilience | There is no `error.tsx`, `global-error.tsx` or ErrorBoundary. Any render exception in any panel unmounts the whole tree, **including Kill switch and Flatten**. [T, `ls` V] | Add `error.tsx`/`global-error.tsx` and wrap each panel in an ErrorBoundary. Render TopBar outside the boundaries. |
| H9 | `tests/test_ml_provenance.py:4` | Test infra | The top-level `import torch` aborts the whole collection when `requirements-ml.txt` is not installed, so 0 tests run. `requirements-dev.txt` does not pull in torch. [V: reproduced] | Use `torch = pytest.importorskip("torch")`, or move the import inside the tests. Add a `ml` pytest marker. |
| H10 | `app/main.py` (0% coverage), `app/feed.py` (0%) | Test coverage | The whole HTTP control plane (approve, kill, flatten, mode, the auth middleware) and the real-time feed have no test coverage at all. [V: coverage run] | Add API tests with `httpx.AsyncClient`, and feed tests with a fake MQTT client (§8.3). |

### MEDIUM

| ID | File:Line | Category | Description / Impact | Recommended Fix |
|---|---|---|---|---|
| M1 | `app/engine.py:1099-1107` | Kill switch | The option execute path does not re-check `self.kill_switch`. Mitigated by the pending-dismissal on kill (`engine.py:262`) and the check at `_on_proposal` (`engine.py:609`). [V] | Add `if self.kill_switch: reject` at the top of `execute()` for every asset type. |
| M2 | `app/broker.py:666-692` (+ `place_bracket`, `place_stop`, `place_option_*`, `replace_option_order`) | Defence in depth | The broker methods have no notional, quantity or kill gate. `WEBULL_MAX_ORDER_*` is only passed as env to the MCP subprocess (`broker.py:392-393`), and there is no evidence the server enforces it. [T] | Add a `Broker._guard(order)` callback: kill (with a flatten-only exemption flag), max notional, max qty, and a symbol whitelist. |
| M3 | `app/fastloop.py:163, 251`; `app/engine.py:609` | Concurrency (TOCTOU) | The fast loop checks `_run_lock.locked()` and then calls `engine._on_proposal` without the lock. The pending-BUY dedupe (`fastloop.py:~175`) is a read followed by a write. A slow cycle that starts mid-trigger can therefore create duplicate BUY proposals. [T] | Serialize `_on_proposal` behind its own `asyncio.Lock`, or enforce a DB `UNIQUE(symbol, side) WHERE status IN ('pending','submitting')` partial index. |
| M4 | `app/engine.py:359-375` | Hot/cold coupling | `_run_lock` is held across `research.refresh_if_due()`, `signals.refresh()` (sentiment fetch, optional LLM scoring, yfinance) and proposal generation. Slow network I/O delays exits and silences the fast loop for its whole duration. [V] | Move research and signal refresh outside the lock, into background workers writing a TTL cache. Lock only the proposal-generation step (see §3.3). |
| M5 | `app/db.py:420-422` | Persistence | There is one `sqlite3` connection shared across the event loop and worker threads (`check_same_thread=False`). WAL is on, but no `PRAGMA busy_timeout` is set and there is no explicit write lock. External writers (`scripts/migrate_db_namespace.py`) can hit `database is locked`. [V] | `PRAGMA busy_timeout=5000`, `PRAGMA synchronous=NORMAL`, and a `threading.Lock` around writes. Alternatively, use a dedicated writer task fed by a queue. |
| M6 | `app/yfinance_service.py:14,65,122` | External API | yfinance runs in a 4-thread pool with **no timeout**, no retry and no negative cache, so failures re-hit Yahoo on every call. `ticker.info` fans out to several scraped requests. yfinance is unofficial and IP-throttled. [T] | Wrap calls in `asyncio.wait_for(..., 8)`. Negative-cache failures for 60-300 s. Add a circuit breaker after N consecutive failures. Keep yfinance off the trading path. |
| M7 | `app/engine.py:1005-1020` | Options data | There is no age check on the snapshot, IV or Greeks. A quote minutes old is treated as current. [T] | Carry `as_of` through the snapshot and reject when the age exceeds 5 s for entries. |
| M8 | `app/backtest/signals.py:25-52,63-120`; `app/eligibility.py:46,125-170`; `config.py:649` | Parity | The backtest skips sentiment (always neutral), the earnings blackout and the first-15-min rule. Live `_check_earnings` and gap checks fail open on error. The earnings blackout default is 2 days while `strategy.md` says 3. [T] | Align the default to 3 or update the spec. Make the earnings check fail closed when the calendar is unavailable for held-overnight entries. Document the backtest limitations in the report output. |
| M9 | `app/engine.py:139-214` | Learning loop | Only 7 whitelisted parameters auto-apply, and only with sample ≥ 30 and p < 0.05. Lessons with a `None` p-value or a `None` old value are silently skipped. Applied values mutate settings in memory only and are re-applied at init. Autopsy output is display-only and does not feed decisions. [T] | Log every skipped lesson with its reason. Persist the effective-parameter snapshot per run, for provenance. Decide whether autopsy should feed sizing. |
| M10 | `web/src/components/LearningView.tsx:62,72` | Unwired feature | Lesson status-change and rollback are `alert("...would call API")` stubs. The backend routes exist (`POST /api/lessons/{id}/status` at `main.py:675`, `/rollback` at `:695`) and require `approved_by` / `rolled_back_by`. [T] | Wire them through the shared `api()` helper (which sends `X-Trader`), with the required body fields. |
| M11 | `web/src/components/Dashboard.tsx:140-158` | Trade approval UX | Paper and sandbox approvals execute on a single click with no confirm. The live confirm says "`{qty}` shares", which is wrong for option contracts. It shows no notional, max loss, stop/target or broker. [V] | Always show a review drawer: side, qty or contracts ×100, limit, notional, max loss, SL/TP, buying-power impact, env/mode. Keep type-to-confirm for live. |
| M12 | `web/src/lib/useDashboardData.ts:68-73, 93-98` | Silent failure | Portfolio fetch failures set `null` and are excluded from the failed-request counter. The strategy and learning loads `.catch(() => {})`. That enables the empty-strategy overwrite (`main.py:1109` has no empty guard). [V] | Count every endpoint. Disable Save until the load succeeds. Reject empty text server-side. |
| M13 | `web/src/lib/proposalStatus.ts:19-29` vs `app/db.py:679`, `app/engine.py` | Enum drift | The backend writes `submitting` and `waiting`, and possibly `expired` (U). The frontend maps unknown values to a raw label and a non-existent tone class. `submitting` is not counted as in-flight. [T] | Generate a shared enum (OpenAPI → TS types) and handle every status explicitly. |
| M14 | `web/src/lib/useDashboardData.ts:10,13-27,49-108` | Performance | There are 14 `useState` slices, all set on every 10 s tick, with new object identities and no `React.memo`. The whole Dashboard re-renders. 9 parallel requests per tick come to about 54 req/min, with no backoff on failure. [T] | Tiered polling (status, portfolio and proposals fast; ML, learning and research slow), structural equality, memoized panels, then SSE (§3.4). |
| M15 | `web/src/components/ConfirmModal.tsx:33-37`, `DecisionModal.tsx:14`, `AutopsyView.tsx:167`, `LearningView.tsx:187`, `TradeLedgerView.tsx:157` | Accessibility | There is no focus trap or focus return. Danger dialogs do not default focus to Cancel. The detail overlays are `<div onClick>` with no role or Escape handling. [T] | One shared `<Dialog>` primitive (focus trap, Escape, `role=dialog`, focus return) used everywhere. |
| M16 | `web/src/components/TopBar.tsx:34-67` | Emergency UX | The kill switch is styled `button-quiet` next to a `button-danger` Flatten, which invites a misclick. There is no hotkey and no separate "pause engine". Flatten is disabled offline with no explanation. There is no data-age indicator. [T] | See §7. |
| M17 | `scripts/trader.py:308-311` | Ops | The Unix stop fallback `pkill -f trader.py` matches the running `trader.py stop` itself, and any unrelated `trader.py`. [T] | Kill only the PID from the lock file. If falling back, match the exact `-m app.main` with the repo path and exclude `os.getpid()`. |
| M18 | `scripts/trader.py:292-304` | Ops (Windows) | `tasklist /FO CSV /NH` never includes command lines, so the `"app.main" in line` fallback is dead code on Windows, the user's platform. [T] | Use `wmic`/`Get-CimInstance Win32_Process` for `CommandLine`, or rely on the lock-file PID only. |
| M19 | `setup.sh`, `start.sh`, `stop.sh`, `auth.sh`, `_bootstrap.sh`, `install_autostart.sh` | Ops (Windows) | These hardcode `.venv/bin/python`. Windows has only `Start_Trader.bat`, with no stop, auth or autostart equivalents. [T] | Add `Stop_Trader.bat`/`Auth_Trader.bat` (or document `python scripts/trader.py stop|auth`). Have the shell scripts detect `.venv/Scripts/python.exe`. |
| M20 | `scripts/package.py:16-33,106-111` | Secret leakage | The excludes match only `^\.env$`. `.env.local`, `.env.prod`, `*.db`, `*.pem`, `*.key` and `*.log` outside the excluded dirs would be zipped. The post-check also misses `.env.*`. [T] | Exclude `^\.env(\..*)?$` except `.env.example`, plus `*.db`, `*.pem`, `*.key` and `*.log`. Fail the build if any match. |
| M21 | `app/main.py:179-193, 889-938` | Local auth | The only protection on mutating routes is loopback Host plus the `X-Trader: 1` header. Any local process or malware can approve orders, switch to `auto`, release the kill switch, or rewrite strategy and lessons. There is no confirmation on kill *release*. [T] | Use a per-launch random token, written to a 0600 file and injected into the served HTML, required on mutating routes. Require a confirm body for kill release and for mode=auto on live. |
| T1 | `app/db.py` (no `close()`); `tests/test_signals.py:36…146`, `test_kill_switch.py:37`, `test_risk_checks.py:73`, `test_benchmark.py:70` | Test infra / lifecycle | This causes all 9 suite failures on Windows (`WinError 32` at temp-dir cleanup). The app also never closes or checkpoints the DB on shutdown. [V: reproduced] | Add `DB.close()` (WAL checkpoint, then close), call it on lifespan shutdown, and use `tmp_path` in the tests. |
| M22 | `app/sentiment.py:11,72-73`; `app/learning.py:255-298` | Prompt injection | Third-party headline text is defended against only by the system prompt. The learning reviewer feeds model output **back into `lessons.md`** (`learning.py:298`). Impact today is limited: free-text lessons do not drive trades, and sentiment LLM scoring is off by default. [T/V] | Sanitize and length-cap headlines, validate the LLM JSON against a schema with numeric clamps, and keep LLM-written lessons in `proposed` state until a human approves them. |

### LOW

| ID | File:Line | Category | Description | Fix |
|---|---|---|---|---|
| L1 | `app/broker.py:494-545` | Caching | The negative cache keys on `hash(arguments)` and also covers mutating calls. A cached 404 on `cancel_order` blocks a later legitimate retry. | Exclude mutating tools from the negative cache. |
| L2 | `app/llm.py:60-82,110,128-185` | LLM latency | A 120 s timeout × retries × 3-provider fallback, with up-to-60 s `Retry-After` sleeps, can take minutes. It is async, but it can sit inside `_run_lock` when `SENTIMENT_LLM=true`. | 20-30 s timeout, an overall deadline, and running outside the run lock (M4). |
| L3 | `app/config.py:426`, `broker.py:378` | Mode safety | A missing or typo'd `.env` silently yields `BROKER_MODE=mock`. This is the safe direction, but it surprises users. | Warn loudly when Webull credentials exist but the mode is mock. |
| L4 | `app/db.py:93-314` | Indexes | `news_items`, `events`, `llm_usage` and `equity_snapshots` appear to be unindexed on time and symbol, and they grow unbounded. [U] | Add `(symbol, ts)`/`(ts)` indexes and a retention job. |
| L5 | `scripts/migrate_db_namespace.py:12,85-133` | Schema drift | It uses a cwd-relative `data/trader.db`, applies ad-hoc ALTERs that duplicate `db.py:431-519`, has no `user_version`, and takes no lock against the live app. | Use numbered migrations with `PRAGMA user_version`, resolve paths from `config.DATA_DIR`, and refuse to run while `trader.lock` is held. |
| L6 | `scripts/backup_db.py:44-47` | Backup | The SQLite backup API is used correctly, but backups land in the same directory and there is no `PRAGMA integrity_check` on the copy. | Verify the copy and support an off-disk target. |
| L7 | `run_backtest_matrix.py:87` | Portability | Hardcoded `/Users/<name>/Downloads/Trader/run_backtest_manual.py` (a macOS path). | Use `Path(__file__).parent`. |
| L8 | `requirements.txt` | Deps | `requests` is used by `scripts/trader.py:241` but arrives only transitively via yfinance. `tzdata`, `pytest` and `pytest-asyncio` are unpinned. | Pin them explicitly. |
| L9 | `.env.example` vs `app/config.py` | Config drift | About 35 vars read by config are undocumented in `.env.example`, including `SL_ATR_MULT`, `ML_MIN_P`, `CONSENSUS_*`, `ELIGIBILITY_*` and `MAX_RISK_PER_TRADE_USD`. Conversely, `.env.example` ships `CYCLE_MINUTES=1` and `SIGNAL_REFRESH_MINUTES=1`, far more aggressive than the code defaults. [T, heuristic] | Generate `.env.example` from the `Settings` definition. |
| L10 | `web/src/components/OptionsStatus.tsx:42-51` | Frontend | The local `const fetch` shadows the global, it fetches once, and it never refreshes. | Rename it and add it to the tiered poll. |
| L11 | `web/src/components/TradeLedgerView.tsx:21` vs `main.py:631-648` | Contract | `status=all` falls to a default branch that returns raw `trade_ledger` rows, a different shape from `closed_trades()`. [U] | Use one response model for all filters. |
| L12 | `web/src/components/PendingProposals.tsx:27-31`, `OptionProposals.tsx:31` | Perf | A 1 s interval re-renders the whole list to tick a countdown. | Move it to a `<Countdown>` leaf. |
| L13 | `web/src/app/globals.css:1-6, 72-79, 208-212` | CSS | Partly minified one-line blocks, duplicated 820/580 px media groups, `--faint #596b76` on `#0b1014` at about 3.4:1 for 8-9 px text, and `max-width:1720px` that wastes ultrawide screens. | See §6/§7. |
| L14 | `app/ta.py` `_score_v1`/`_score_v2` | Dead code | Two scoring models coexist and v1 is reachable only via `TA_MODEL`. | Retire v1 after confirming no saved backtests depend on it. |
| L15 | `Start_Trader.bat:~30` | Ops | The browser opens on a fixed 4 s timer, which races server startup. | Poll `/api/status` before opening. |
| L16 | `web/package.json` | Tooling | No ESLint and no `lint` script. | Add `eslint-config-next` and a `lint` script. Run it in CI. |

---

## 3. API Inventory, Rate-Limit Audit & Production Real-Time Solution

### 3.1 External API inventory

| API / transport | Call site | Current cadence | Caching / retry / breaker | Failure behaviour | Throttle / latency risk |
|---|---|---|---|---|---|
| **Webull OpenAPI via MCP** (stdio subprocess `webull-openapi-mcp`, HTTPS behind it) | `app/broker.py:419-447` (start), `:519-558` (`call`) | On demand. Snapshot prefetch each fast-loop tick and feed poll. | 10 s snapshot TTL (`:356`), 300 s negative cache (`:353`), exponential backoff with jitter (base 2 s, cap 60 s), a global pause honoring `Retry-After` on 429. No true breaker. 90 s start timeout. Per-call MCP `read_timeout` is disabled (mcp 1.30 bug, `:447`). | `BrokerError`. The fast loop returns early, the feed logs at debug, and a send error marks the proposal `unknown` for reconciliation. | A single stdio pipe serializes every call. JSON-text parsing is shared between mock and live (`broker.py:199`, `main.py:362-375`), so format drift could misparse silently. |
| **Webull market-data MQTT** (`DataStreamingClient`) | `app/feed.py:168-217` | Push | 5 min backoff after repeated failures (`:53`), 60 s negative cache (`:55`), `STALE_SECONDS` | Falls back to snapshot polling | Equities only in the current code. There is no OPRA options subscription. |
| **Webull snapshot poll** (fallback) | `app/feed.py:140-154` | `POLL_SECONDS=2.0` (`config.py:480`), 1.0 in mock | Batched, behind the 10 s TTL | Logged at debug | 30 calls/min nominal, about 6/min unique after the TTL |
| **yfinance** (`Ticker.info`, `fast_info`, `calendar`; macro `^VIX ^GSPC ^IXIC ^TNX`) | `app/yfinance_service.py:65,122` | On demand (dashboard, research, eligibility) | 300 s success cache, 4 threads. **No timeout, no retry, no negative cache.** | `{"error": ...}` | Unofficial scraping. IP throttling and blocks on bursts. Some fields lag up to about 15 min. A slow request blocks a pool thread indefinitely. |
| **Groq chat completions** (`openai/gpt-oss-120b`, alternate `gpt-oss-20b`) | `app/llm.py:18,60-82,110` | Learning review, plus sentiment scoring when `SENTIMENT_LLM=true`. The agent loop is dead (H6). | 120 s timeout. Retries on 429/5xx honoring `Retry-After`. Cooldown. | Fallback to the alternate model, then NVIDIA NIM | Free-tier RPM and TPM caps per model. A bursty 20-iteration review can 429. |
| **NVIDIA NIM** (`nemotron`) | `app/llm.py:19,162-173` | Fallback only | Same path | `LLMError` | — |
| **Google News RSS** / **Yahoo Finance RSS** | `app/sentiment.py:216-246`, `config.py:503` | `NEWS_REFRESH_MINUTES` (15), 72 h lookback | 10 s timeout, per-symbol/per-source exponential backoff, cached headlines on failure. A new `AsyncClient` per fetch. | Uses the cache | Spoofed User-Agent and `follow_redirects=True`. RSS endpoints throttle aggressive pollers. |
| **Webull news** (via MCP) | `app/sentiment.py:248` | Same | Same | Same | Shares the MCP pipe |
| **Telegram / ntfy / SMTP / webhook alerts** | `app/alerts.py:160-180` | Event-driven | 15 s timeout | — | The token is in the URL (H4) |
| **Research scan** | `app/research.py:105` | `RESEARCH_SCAN_MINUTES=60` (60 s tick) | — | Logs a warning and the run continues | Runs inside `_run_lock` (M4) |

### 3.2 Request burn-rate math (Webull)

Definitions: N = effective watchlist size. The default is 5 (`config.py:78`). A tiered universe can be 50-350 (`UNIVERSE_TOP_N`, tiers 50/100/200).

| Source | Formula | N=5 | N=50 | N=350 |
|---|---|---|---|---|
| Feed poll (only if MQTT is down) | 60 / `POLL_SECONDS` (batched) | 30 nominal, about 6 unique | 30 / 6 | 30 / 6 (assumes one batch covers N; verify the endpoint's symbol cap) |
| Fast loop | (60 / `FAST_LOOP_SECONDS`) × 2 (positions plus a batched snapshot) | 6 (20 s) to 24 (5 s minimum) | same | same |
| Signal refresh (bars per symbol) | N / `SIGNAL_REFRESH_MINUTES` | 5 (`.env.example`=1 min) vs 0.33 (code default 15) | 50 vs 3.3 | **350/min** vs 23 |
| Webull news | N / 15 | 0.33 | 3.3 | 23 |
| Options chain (when enabled) | underlyings × expiries × chain pages per cycle | — | — | Unbounded today, with no subscription budget |
| **Total, `.env.example` profile** | | **about 20-40/min** | **about 70-90/min** | **about 380-400/min** |

**Where 429s will occur.**
1. With a tiered universe of 50 or more symbols and `SIGNAL_REFRESH_MINUTES=1`, every minute starts with a burst of N bar requests through one stdio pipe. That collides with per-endpoint market-data quotas, and the global 429 pause then also stalls exits and fast-loop quotes.
2. Options-chain fan-out when `OPTIONS_ENABLED=true`.
3. yfinance bursts when the Research and Yahoo panels are open over many symbols.

Webull publishes per-endpoint limits in its OpenAPI docs; check the numbers against your account tier. These calculations show the cadence the code generates, not the exact quota.

**Hot-path blocking.**
- There are no synchronous `requests`/`time.sleep` calls in async code [T].
- The real blockers are lock scope (M4) and the missing yfinance timeout (M6).
- The 120 s LLM timeout (L2) matters only when `SENTIMENT_LLM=true`.

### 3.3 Recommended production real-time architecture

```
                 ┌──────────────── COLD PATH (async workers, TTL caches) ───────────────┐
 RSS / Webull news ─► SentimentWorker ─┐                                                │
 yfinance (fundamentals only) ─► ResearchWorker ─┤──► SignalStore (in-mem + SQLite, as_of)│
 Daily bars (1×/day + intraday close-out) ─► TA/ML Worker ─┘                              │
 Groq/NIM ─► LearningReviewer (nightly; writes *proposed* lessons only)                  │
                 └──────────────────────────────────┬──────────────────────────────────┘
                                                    │ read-only, age-checked
 ┌──────────── HOT PATH (single asyncio task group, no network except broker) ─────────┐
 │ Webull MQTT (L1 equities) ─┐                                                         │
 │ OPRA stream (held + top-K) ─┴─► TickBus ─► RingBuffer[symbol] ─► BarAggregator(1m/5m) │
 │                                     │                                                │
 │                                     ├─► RiskLoop (250 ms–1 s): kill/PnL/drawdown,     │
 │                                     │   equity+OPTION exits, expiry/pre-close close    │
 │                                     └─► EntryLoop (fastloop): gate = SignalStore +    │
 │                                         live price; stale (>N s) ⇒ fail closed        │
 │                         ▼                                                            │
 │        OrderRouter (single writer, idempotency key, broker-level guard) ─► Webull SDK │
 └──────────────────────────────────────────────────────────────────────────────────────┘
                         │ events
                         ▼
        EventHub ─► SSE /api/stream (status, portfolio, proposals, quotes, events) ─► UI
```

**1. Execution layer: direct Webull OpenAPI SDK vs. MCP**
- **Use the direct SDK in-process for the order path**: place, cancel, replace, order status, positions and account. Reasons:
  - A pooled HTTPS keep-alive client, typed responses instead of MCP text parsing (removes the mock/live parse-drift risk), deterministic per-call timeouts (MCP `read_timeout` is disabled today), and no single stdio pipe shared with market data.
- **Keep MCP** for the mock server and tests (`app/mock_mcp_server.py`), and for ad-hoc tooling and agent experiments. Put both behind the existing `Broker` interface, so `BROKER_MODE` selects `SdkBroker` | `McpBroker` | `MockBroker`.
- **Idempotency**:
  - Keep the uuid4 `client_order_id` (`engine.py:71`).
  - Persist the coid in the proposal row *before* the network send. `_flatten_sell` already does this; make `_send` do the same for every path.
  - Retries must reuse the same coid.
  - Exclude mutating calls from the negative cache (L1).
- **Startup reconciliation** (extend `engine.reconcile_orders()`, `main.py:116`):
  1. Take the instance lock.
  2. Load the kill state.
  3. Pull broker open orders, positions (including options) and today's fills.
  4. Match them to `submitting`/`unknown`/`placed` rows by coid.
  5. Adopt unknown broker orders as `orphan` and alert.
  6. Rebuild `trade_ledger` deltas from fills.
  7. Arm protective orders or OptionExitManager for every held position.
  8. Only then start the schedulers.
- **Broker-level guard** in the OrderRouter (M2): kill, max notional, max qty, whitelist, and a per-symbol in-flight lock.

**2. Real-time market and OPRA options data**

| Option | Fit for this repo | Notes |
|---|---|---|
| **Webull OpenAPI MQTT** (already in `feed.py`) plus OPRA entitlement | **First choice for underlyings and held contracts.** No new vendor, and it uses the same account and credentials. | Requires an options market-data subscription on the Webull account. Budget subscriptions to held contracts plus the top-K candidates per underlying. |
| **Polygon.io / Massive** options WebSocket | Best general-purpose upgrade for chains and Greeks | Quotes, trades and aggregates per contract. Snapshot REST for chains. Paid tiers. |
| **Tradier** streaming | Cheap if you hold a Tradier brokerage account | Greeks via ORATS on chain requests. WebSocket quotes. |
| **Databento** OPRA | Institutional-grade, tick-level | Expensive. Overkill for 1-10 contract retail sizing. |

Implementation details:
- **Subscription budget**: `held_contracts ∪ top_k_per_underlying(k=4, ±delta band)` ≤ the entitlement cap. Re-evaluate every 5 min from the cold-path chain snapshot.
- **Ring buffer**: a `collections.deque(maxlen=…)` of `(ts, bid, ask, last, size)` per symbol. A `BarAggregator` emits 1m/5m bars, and the indicators in `app/indicators.py` are updated incrementally.
- **Staleness**: every quote carries `as_of`. Entry gates fail closed when `age > 5 s` for options or `> 15 s` for equities (fixes H2, M7).
- **Replace yfinance on any decision path.** Keep it only for fundamentals and earnings calendars, behind a timeout and a breaker (M6).

**3. Hot-path vs. cold-path decoupling**
- `_run_lock` covers only proposal generation (M4). Research, signals, sentiment and ML scoring run as independent workers that write `SignalStore` entries with `as_of` and a TTL.
- A new **RiskLoop** (sub-second) owns the following:
  - kill, PnL and drawdown supervision (move it from `supervisor.py` polling into tick-driven evaluation);
  - equity signal exits (`exits.decide_exit` on live price);
  - **option exits (H1)**.
- RiskLoop never awaits anything except the OrderRouter.
- The fast loop becomes the EntryLoop. It reads `SignalStore` and the live ring buffer, and it serializes `_on_proposal` (M3).
- LLM calls run only in cold workers: a 30 s deadline, schema-validated output (M22), with results cached per headline hash.

**4. Backend-to-frontend streaming**
- `/api/stream` (SSE) already exists (`main.py:825`) but only `MarketChart` uses it.
- Promote it to a multiplexed **EventHub**: `event: status|portfolio|proposals|events|quote|fill|kill`, with server-side diffing and a 15 s heartbeat.
- The frontend subscribes once. It keeps REST for on-demand views (ledger, autopsy, ML, learning) with SWR at a 60 s+ revalidate.
- The result goes from about 54 req/min to 1 persistent connection plus a few requests per minute. Approve buttons auto-disable on `stale`/`disconnected` events.
- SSE is preferred over WebSockets here: one-way, works through the existing loopback/`X-Trader` model, and has auto-reconnect via `EventSource`.

---

## 4. Strategy & Options Alignment Matrix

| Rule (source) | Code location | Status |
|---|---|---|
| Buy only if daily close > SMA20 (`strategy.md` Buying) | `app/consensus.py:70` "TA regime" (trend ≠ down) | **Partial/Divergent**: a trend-not-down test, bypassed by `ml_p>0.7` or an oversold weak downtrend |
| Prefer pullbacks 2-5% below the 10-day high | `app/ml.py` FEATURES `pct_from_high10` only | **Missing** as a rule (the prose was meant for the dead agent, H6) |
| Skip earnings within 3 trading days | `app/eligibility.py:46`, default 2 (`config.py:649`), fails open | **Partial/Divergent** |
| One starter position, add at most once | Backtest one-position (`backtest/engine.py`). No live add-once. | **Partial** |
| Never add to a losing position | Not found in `risk.py`/`consensus.py` [U] | **Missing/Unverified** |
| No trades in the first 15 minutes | `app/eligibility.py:57-59` (live) | **Implemented** live; absent in backtest |
| ATR stop/target bracket (2.0× / 3.0×) | `config.py:492-493`, `signals.py:~101`, `backtest/signals.py:126` | **Implemented** (equity); **Missing** (options) |
| 20-day time stop | `exits.decide_exit` via `engine.py:550`; `config.py:399` | **Implemented** (live and backtest) |
| Trend-break exit (N confirmations, floor −0.10) | `exits.py`, `engine.py:494-559` | **Implemented** |
| Selling never throttled by the gate | `engine.py:614` | **Implemented** |
| ML is a filter, not a forecast | `consensus.py:44-92` (`ml_first` lowers the TA floor and overrides trend) | **Partial/Divergent** |
| Consider spread, fees and slippage | Backtest `costs.py:20-24`; no live equity spread check; option spread check fails open | **Partial** |
| Walk-forward evaluation with realistic costs | `backtest/walkforward.py`, `ml.py` (AUC ≥ 0.52, ≥ 30 rows, ≥ 3 symbols) | **Implemented** (but the default backtest has ML off, H7) |
| Daily loss limit | `risk.py:193-204` | **Partial** (fails open on `None`, H5) |
| Kill switch blocks all new orders | `engine.py:336,609,1113`; `risk.py:79` | **Implemented** equity / **Partial** options execute (M1) |
| Options not authorized for the stock strategy | `options_enabled=False` (`config.py:311`) | **Implemented** (flag) |
| Option qty integer, ×100, ≤ 10 contracts, ≤ $5 000 notional | `risk.py` `OptionRiskManager`, `config.py:312-313` | **Implemented** [T] |
| Option DTE 7-45 (no 0DTE entries) | `risk.py:291-299` [V] | **Implemented** |
| Option IV/delta/volume/OI/spread gate | `engine.py:924-1015` | **Partial/Divergent** (fails open, IV proxy: H2, H3) |
| Option pre-expiry / pre-close forced exit | none | **Missing** (H1) |
| Fractional orders by AMOUNT, no bracket or STOP for fractional (`FRACTIONAL_OPTIONS_NOTES.md`) | `broker.py` preview/place; `engine.py:_flatten_sell_fractional` | **Implemented** [T]; fractional protective-stop handling unverified |
| Lessons auto-apply guardrails (n ≥ 30, p < 0.05, bounds, never risk limits) | `engine.py:139-214` | **Implemented** |
| Free-text lessons steer decisions (`LEARNING_LOOP_VERIFICATION.md`) | `agent.py:145-165` (dead) | **Missing/Bypassed** (H6) |

---

## 5. Incomplete, Unwired & Stubbed Features

| Feature | Location | State |
|---|---|---|
| LLM trading agent (`run_analysis`, tool registry, system prompt) | `app/agent.py:152-298` | **Dead code**: no callers |
| Lesson status transitions and rollback | `web/src/components/LearningView.tsx:62,72` → `main.py:675,695` | **UI stub** (`alert`) |
| ML lifecycle (train, promote, reject, deactivate) | `main.py:1059-1101`; `MLScorecard.tsx` is read-only | **Backend only** |
| Scheduler on/off | `POST /api/scheduler` `main.py:924`; `CommandBar.tsx:27` display only | **Backend only** |
| Manual analysis run | `POST /api/run` `main.py:876` | **Backend only** |
| Signal, research and learning refresh | `main.py:1008,1026,1158` | **Backend only** |
| Sentiment LLM scoring | `POST /api/sentiment/score` `main.py:1040` | **Backend only** |
| Chart overlays | `GET /api/chart/overlays` `main.py:799` | **Backend only** |
| Fast-loop status, execution quality, options snapshot, news, sentiment, alerts summary, autopsy and trade detail | `main.py:341,498,454,568,575,976,622,651` | **Backend only** |
| Shutdown | `POST /api/shutdown` `main.py:981` | Used by `scripts/trader.py` only |
| Option exit management | — | **Not built** (H1) |
| True IV rank | `engine.py:955` | **Proxy** (H3) |
| Autopsy → decision feedback | `app/autopsy.py` | **Display/DB only** |
| OptionsStatus refresh | `OptionsStatus.tsx:42` | Fetches once |
| Windows stop/auth/autostart | `Start_Trader.bat` only | **Not built** (M19) |

---

## 6. Redundant Code & Refactoring Targets

**Redundancy.**
- `PendingProposals.tsx` (138 lines) and `OptionProposals.tsx` (158 lines) are near-copies.
- `HistoryTable` ⊂ `TradeLedgerView`.
- There are four ad-hoc overlay implementations (`ConfirmModal`, `DecisionModal`, and the Autopsy, Learning and Ledger overlays).
- Raw `fetch()` in `LearningView`, `AutopsyView`, `TradeLedgerView` and `MLScorecard` bypasses `api()`.
- `/api/ml` is fetched twice (`useDashboardData.ts:61`, `MLScorecard.tsx:30`).
- `ta._score_v1` vs `_score_v2`.
- The `migrate_db_namespace.py` ALTERs duplicate `db.py:431-519`.
- `app/agent.py` is dead.

**Decomposition plan.**

| File (lines) | Proposed modules |
|---|---|
| `app/engine.py` (1948) | `engine/core.py` (init, mode, kill: 98-281) · `engine/cycle.py` (282-408) · `engine/proposals.py` (409-923) · `engine/options_flow.py` (924-1080) · `engine/execution.py` (1081-1451: execute, requote, `_send`, brackets) · `engine/reconcile.py` (1452-1520) · `engine/flatten.py` (1521-1685) · `engine/rebalance.py` (1686-1896) · `engine/scheduler.py` (1897-1948) |
| `app/broker.py` (896) | `broker/parsers.py` (105-341) · `broker/transport.py` (342-559: lifecycle, `call`, rate limit) · `broker/market_data.py` (560-707) · `broker/orders.py` (651-693, 837-896) · `broker/options.py` (708-815) · `broker/crypto.py` (816-836) · later `broker/sdk.py` (direct SDK, §3.3) |
| `app/db.py` (1375) | `db/connection.py` (connect, PRAGMAs, write lock) · `db/migrations/` (numbered, `user_version`) · repositories: `proposals.py`, `ledger.py`, `signals.py`, `lessons.py`, `ml.py`, `events.py`, `kv.py` |
| `app/main.py` (1222) | `api/app.py` (lifespan, middleware, static) plus `APIRouter`s: `status.py`, `portfolio.py`, `proposals.py`, `options.py`, `learning.py`, `ml.py`, `research.py`, `controls.py` (kill, flatten, mode, scheduler, shutdown), `stream.py` (EventHub SSE). Add Pydantic response models so OpenAPI can generate `web/src/lib/types.ts`. |
| `app/mock_mcp_server.py` (1107) | `mock/state.py` (persisted account) · `mock/fills.py` (matching and slippage) · `mock/quotes.py` · `mock/options.py` · `mock/server.py` (MCP tool registration) |
| `app/config.py` (700) | `config/settings.py` (grouped Pydantic sub-models: `RiskSettings`, `OptionsSettings`, `MLSettings`, `FeedSettings`, `AlertSettings`) · `config/env.py` (loader) · a generator script for `.env.example` (fixes L9) |
| `web/src/app/globals.css` (~34 KB) | `styles/tokens.css` (colour, spacing and type scale, plus a contrast-checked `--faint`) · `styles/base.css` · one CSS Module per component · one consolidated breakpoint set (1366, 1100, 820, 580, 2200+) |

---

## 7. UI/UX Audit & Redesign Blueprint

**Component-level fixes.**

| Component | Fix |
|---|---|
| `TopBar` | Make it sticky. Merge the env badge (LIVE red frame around the whole app / PAPER blue) with the auto/approve mode. Show a broker chip with "data Ns old". Make **Kill** red and large with a hotkey (`Shift+K`) and spatially separated from **Flatten**. Add **Pause engine** (scheduler off, no dismissal). When the broker is offline, explain why Flatten is disabled and offer "Reconnect". |
| `Dashboard.approveProposal` | Always open a review drawer (paper too). Label in contracts ×100 for options. Show notional, max loss, SL/TP, buying-power impact and env. Disable Approve when the data is stale, the broker is offline, or a duplicate pending exists. |
| `PendingProposals` + `OptionProposals` | Merge into one `ProposalCard kind="equity"|"option"` with a `useProposalActions` hook. Separate Approve and Dismiss visually. Show the delta in max loss and notional on requote. |
| `ConfirmModal`, `DecisionModal`, overlays | One `<Dialog>` primitive: focus trap, Escape, focus return, Cancel focused by default for danger dialogs. |
| `LearningView` | Wire status and rollback (M10). Show the "applied / skipped (reason)" state for each lesson (M9). |
| `MLScorecard` | Add train, promote, reject and deactivate actions behind a confirm. |
| `CommandBar` | Scheduler toggle and "Run now" (`/api/run`). |
| `SettingsPanel` | Relabel strategy as "Reviewer notes (does not change trading)" until H6 is resolved. Disable Save until the load succeeds. |
| `useDashboardData` | Split by domain. Tiered polling, then SSE. Structural-equality setState. Count every failure, including portfolio. |
| `globals.css` | Lift `--faint` to ≥ 4.5:1 and the minimum text to 11 px. Add ▲/▼ glyphs next to coloured P&L. Remove the 1720 px cap above 2200 px. |

**Proposed layout.**
```
┌ Sticky header: [LIVE|PAPER · AUTO|APPROVE] [Broker ● 1.2s] [NetLiq | Day P&L | BP | Open risk] [Pending 3] [Pause] [KILL] [Flatten…] ┐
├ Banner rail: stale data · broker offline · duplicate instance · login expiring · daily-loss guard tripped                           ┤
├──────────────────────── Primary workspace ───────────────────────────┬────── Action queue (sticky) ──────┤
│ Chart (SSE) ⇄ Option chain (toggle)                                   │ Proposal cards (equity + options) │
│ Positions & working orders (with per-position exit plan / DTE clock)  │ Review drawer on Approve          │
├──────────────────────── Secondary tabs ──────────────────────────────┴───────────────────────────────────┤
│ Signals │ Ledger (+Autopsy drawer) │ Research (+Yahoo) │ Learning & ML │ Settings                          │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
≥2200px: three columns (workspace │ positions │ queue). ≤1366px: queue collapses to a drawer with a badge.
```

**Top UX upgrades, by impact:**
1. ErrorBoundaries with a header outside them.
2. Always-on approve review.
3. Kill, Pause and Flatten separation, with a hotkey.
4. Staleness banners plus auto-disabled actions.
5. Live frame.
6. SSE.
7. Unified proposal card.
8. Dialog primitive and a11y.
9. Contrast and glyph pass.
10. Pinned action queue on every tab. This also fixes the `#pending-proposals` anchor, which only exists on the Market tab (`Dashboard.tsx:200-224`).

---

## 8. Test Suite Results & Coverage Gaps

**How these were run (non-destructive).**
- pytest ran from a throwaway venv in the session scratchpad (Python 3.12.x, `requirements.txt` + `requirements-dev.txt` + `pytest-cov`, then `requirements-ml.txt`), with `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`.
- The tests are isolated: `tests/conftest.py:18-40,95-132` forces `BROKER_MODE=mock` and a `mkdtemp` data dir, so they never touch `data/*.db` or Webull.
- The frontend ran on a copy of `web/` in the scratchpad (`npm ci`, `tsc --noEmit`, `next build`).
- `scripts/uat_order_test.py` and `scripts/smoke_test.py` were **not** run, because they can place orders.
- The `.coverage` file written by pytest-cov was deleted afterwards.

### 8.1 Results

| Check | Result |
|---|---|
| `pytest tests` (default dev install, no torch) | **Collection aborted: 1 error, 0 tests run.** `tests/test_ml_provenance.py:4 import torch` → `ModuleNotFoundError` (H9) |
| `pytest tests --ignore=tests/test_ml_provenance.py` | **307 passed, 9 failed** in 207 s |
| `pytest tests/test_ml_provenance.py tests/test_ml_first.py` (with torch 2.6.0) | **20 passed** |
| `npx tsc --noEmit` (web) | **0 errors** |
| `npm run build` (Next.js 16.4.6, Turbopack) | **Success.** Compiled in 2.1 s. Routes `/` and `/_not-found` are static. |
| `npm run lint` | **Not available.** No `lint` script and no ESLint installed (L16). |
| Static TS hygiene | 2 × `any` (`MarketChart.tsx:58`, `types.ts:58`). 0 `@ts-ignore`/`eslint-disable`. |

**All 9 failures have a single cause: a Windows-only test-hygiene defect (new finding T1, MEDIUM).** Each test fails at teardown with `PermissionError: [WinError 32] ... trader.db is being used by another process` (`tempfile.py:905`). The tests build a `DB` inside `tempfile.TemporaryDirectory()`, but `app/db.py` has **no `close()` method**, so the open SQLite and WAL handles block directory removal on Windows. No assertion failed.

Affected tests:
- `test_benchmark.py::test_performance_reports_no_data` (`:70`)
- `test_kill_switch.py::TestKillSwitch::test_kill_switch_survives_restart` (`:37`)
- `test_risk_checks.py::TestRiskChecks::test_rejects_when_no_capacity_for_risk_limit` (`:73`)
- `test_signals.py` ×6 (`:36,58,79,101,123,146`)

**Fix:**
- Add `DB.close()`, which runs a WAL checkpoint and then `conn.close()`.
- Close the DB in those tests, or switch them to the `tmp_path` fixture, or use `TemporaryDirectory(ignore_cleanup_errors=True)`.
- The same missing `close()` means the app never checkpoints or closes cleanly on shutdown.

`pytest.ini` filters `DeprecationWarning`, so deprecations are hidden. No other warnings surfaced.

### 8.2 Coverage (line coverage, `--cov=app`, 307-test run; **TOTAL 50%** of 10 071 statements)

| Module | Cover | Module | Cover |
|---|---|---|---|
| `main.py` | **0%** | `risk.py` | 77% |
| `feed.py` | **0%** | `fastloop.py` | 74% |
| `yfinance_service.py` | **0%** | `sentiment.py` | 73% |
| `mock_mcp_server.py` | 14% (runs as a subprocess, so coverage under-counts) | `broker.py` | 69% |
| `autopsy.py` | 15% | `db.py` | 65% |
| `llm.py` | 17% | `alerts.py` | 65% |
| `platform.py` | 17% | `eligibility.py` | 64% |
| `research.py` | 18% | `portfolio.py` | 54% |
| `learning.py` | 19% | `exits.py` | 54% |
| `lessons.py` | 34% | `consensus.py` | 54% |
| `ml.py` | 35% (no-torch run) | `backtest/engine.py` | 86% |
| `supervisor.py` | 40% | `ta.py` | 95% |
| `engine.py` | **47%** (602 of 1 145 statements missed) | `config.py` / `market.py` | 93% |

### 8.3 Critical untested paths

| Module | Path | Why it matters |
|---|---|---|
| `app/main.py` (0%) | Every route, the `X-Trader`/loopback middleware (`:179-193`), the lifespan startup reconcile (`:116`), and the flatten confirm body | This is the entire control plane, and none of it is exercised. Add `httpx.AsyncClient(app=…)` tests. |
| `app/feed.py` (0%) | MQTT → poll fallback, stale detection, backoff | This is the real-time price source for the fast loop and the chart. |
| `app/engine.py` (47%) | Option branch of `execute` (`:1099-1107`), the option gate (`:924-1015`), `check_core_rebalance` (`:1686+`), the flatten fractional path, and the `_send` failure → `unknown` path | These are the options and money paths behind H1-H3, M1 and M2. |
| `app/supervisor.py` (40%) | The `KILL_ACTION` = pause/cancel_entries/flatten branches, and the drawdown and error-count triggers | These are the automated emergency actions. |
| `app/db.py` (65%) | Concurrency under threads, migrations against old schemas, `claim_pending` races | These cover M5 and M3. |
| `app/broker.py` (69%) | The real Webull transport (only the mock is exercised), token refresh, and negative-cache behaviour on mutating calls | These cover L1, and the risk of live-only regressions. |
| `app/yfinance_service.py`, `app/llm.py` | Timeouts, fallbacks, error caching | These cover M6 and L2. |
| `scripts/trader.py`, `scripts/package.py` | stop/kill matching, package exclude patterns | These cover M17, M18 and M20. |

---

## 9. Prioritized Action Plan

**Phase 1: Blockers, crash/security and rate-limit fixes (1-2 weeks)**
1. H9 + T1: make the torch import skippable and add `DB.close()`, so the suite goes green on Windows and on a plain `requirements-dev.txt` install. Add CI on Windows and Linux.
2. H5: make the daily-loss guard fail closed. M1: add a kill check at the top of `execute()`. M2: add the broker-level guard.
3. H4: silence the `httpx`/`httpcore` loggers and add a redaction filter. M20: harden the `package.py` excludes.
4. H8: add ErrorBoundaries and `error.tsx`/`global-error.tsx`. M11: always-on approve review with correct contract units.
5. M4 and M6: move research and signal refresh out of `_run_lock`, and add a yfinance timeout, negative cache and breaker. Set `SIGNAL_REFRESH_MINUTES` in `.env.example` ≥ 5 with a tiered universe.
6. M5: SQLite `busy_timeout` plus a write lock. M3: serialize `_on_proposal` and add a partial unique index on pending BUYs.
7. M12/M13: count all fetch failures, add a server-side empty-text guard, and share the status enum.
8. M17-M19: Windows-safe stop/auth scripts, and a `stop` that never kills itself.
9. **Keep `OPTIONS_ENABLED=false` until Phase 2 items 1-3 ship.**

**Phase 2: Real-time streaming and strategy/options parity (2-4 weeks)**
1. H1: OptionExitManager (premium stop/target, DTE exit, expiry-day 15:45 ET close).
2. H2/M7: fail-closed option gate with quote age checks. H3: true IV rank from the stored ATM IV history.
3. H7/M8: backtest defaults mirror live (ML on, earnings blackout and sentiment notes), and resolve the 2 vs 3 day blackout.
4. H6: decide the agent's fate. Encode the pullback, add-once and no-add-to-loser rules deterministically, and relabel Settings.
5. §3.3: OrderRouter on the direct Webull SDK, pre-send coid persistence on every path, extended startup reconciliation including options.
6. §3.3: Webull MQTT with OPRA entitlement for held and top-K contracts, ring buffer and bar aggregator, RiskLoop.
7. §3.4: EventHub SSE, with the frontend moved off the 10 s poll.
8. M9/M22: lesson skip logging, a per-run parameter snapshot, LLM output schema validation, and LLM-written lessons kept in `proposed` state.

**Phase 3: Decomposition and UI/UX redesign (3-6 weeks)**
1. Split `engine.py`, `broker.py`, `db.py`, `main.py`, `mock_mcp_server.py` and `config.py` per §6. Add numbered DB migrations. Generate the OpenAPI → TS types.
2. Frontend: unified ProposalCard, Dialog primitive, per-domain hooks, CSS tokens and modules, the layout from §7, ESLint in CI.
3. Wire the backend-only features (ML lifecycle, scheduler, run-now, refreshes, overlays) or delete the routes.
4. Add coverage targets: main.py routes and middleware, supervisor kill actions, the broker SDK path with recorded fixtures, OptionExitManager, and reconciliation crash simulations.
