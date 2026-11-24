# HANDOFF

> **State (2026-11-24 10:10 ET): the app runs the new code with VARIANT D exits** (UAT, auto). It was restarted at 10:06 ET. The `.env` changes were made with the user's OK; backup in `data/backups/.env.pre_variant_d_2026-11-24`. Next: watch the live feed; then §7 items 5-8.
> History: the completed QA programme is archived in `docs/HANDOFF_QA_PROGRAMME_2026-11-24.md`. Per-finding status for it is in `REMEDIATION.md`.

---

## 0. Read this first (TL;DR for whoever takes over)

1. **The running app is OLDER than the code on disk.**
   - The app (UAT sandbox, `TRADING_MODE=auto`) was restarted at 20:35 local (07:35 ET) and runs the **wave-1 code only** (§2).
   - Everything in §3 is **on disk but not loaded**: waves 2a, 2b and 2c, market orders, the stale-price fix, stop re-anchoring, and the swing settings.
   - The **6 `.env` edits** (§5) and DB migrations **0005/0006** also take effect only at the next restart.
2. **Nothing in §3 has had the independent Opus verification yet.** Wave 1 did.
   - The plan was: Opus-verify §3, then restart, then watch the live feed (§7, steps 1-3).
   - **Two pieces were stopped mid-work** and are the least trustworthy:
     - stop re-anchoring (§3.6);
     - the swing-strategy backtests (§3.7).
3. **A critical live bug is fixed on disk, but the running app still has it:** premarket prices are stale (§3.5).
   - Until the restart, premarket BUYs are priced off **yesterday's close**. ADI was bought at limit $378.75 and filled at $343, below its own $348.45 stop.
   - The user chose **not** to pause entries.
4. **The preliminary backtests say the current strategy loses money after costs** (§3.7).
   - They are unverified, but the signal is strong. Look at it before turning on any aggressive sizing.
5. **Test suite on the current disk state (wrap-up run):** see §6. Last fully green run before the two stopped agents: 766 passed, 0 failed.

---

## 1. Quick reference

- **Run the app:**
  - Start: `.venv\Scripts\python.exe scripts\trader.py run`.
  - Stop: `.venv\Scripts\python.exe scripts\trader.py stop`, or Start_Trader.bat / Stop_Trader.bat.
  - UI and API are at http://127.0.0.1:8000.
  - Token: in `data/api_token`. Send `X-Trader-Token`, plus `X-Trader: 1` on non-GET requests.
- **Restarting from a shell:**
  - `trader.py stop` (it may take ~20 s and then kill the process).
  - Then start it detached: `Start-Process .venv\Scripts\python.exe -ArgumentList 'scripts\trader.py','run' -WindowStyle Minimized`.
  - Poll `/api/status`.
  - Starting via `cmd /c Start_Trader.bat` from an agent shell left no server running once (20:33). Double-clicking the .bat works.
- **Audit feed:** `GET /api/events?limit=200`.
- **Logs:** `logs/app.log`, `logs/mcp_server.log` (requests only, no responses) and `logs/webull_audit.log`.
- **Checks:**
  ```bash
  PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider -o addopts="" --tb=short   # ~10 min
  python scripts/gen_env_example.py --check     # must exit 0 after any Settings change
  cd web && npm ci && npx tsc --noEmit && npm run lint && npm run build
  ```
  - `tests/conftest.py` sets `TRADER_DATA_DIR`/`TRADER_LOG_DIR` (temporary dirs) and **`TRADER_SKIP_DOTENV=1`**, so the real `.env` no longer leaks into tests.
  - The suite freezes the clock at 2026-11-20. Use `app.market.now_utc()` / `set_test_time` in tests.
- **Python environments:**
  - The project `.venv` lacks pytest.
  - Tests were run from a scratch venv in the session scratchpad (`...\scratchpad\testvenv`), which may be gone. Recreate it with `python -m venv X && X\Scripts\pip install -r requirements.txt -r requirements-dev.txt -r requirements-ml.txt`.
- **Architecture:**
  - `docs/ARCHITECTURE_CONTRACTS.md`.
  - Stream payloads: `docs/API_STREAM.md`.
  - Strategy rules: `strategy.md`. Note that `strategy.md` has NOT been updated for this programme's changes.
- **Standing decisions (keep unless the user says otherwise):**
  - **Kill switch:** it blocks *opening* orders only. Exits, stops, the soft stop and flatten always pass.
  - **Option exits:** they act in approve mode and under a kill.
  - **LLM lessons:** they only go to `lessons.proposed.md` until a human approves them.
  - **Execution:** `BROKER_EXECUTION=mcp` is the default.
  - **Earnings fail-closed:** skipped in mock mode.
  - **`.env`:** never edit it without the user's explicit OK. Back it up first.
  - **Subagent model (user, 2026-11-24 ~09:20 ET):** run **every** subagent on **Sonnet 5.5 with low effort** (`model: sonnet`, `effort: low`). This includes checkers, fixers and verifiers. It replaces the earlier Sonnet-medium / Opus-verifier split. **Only the orchestrator (main session) runs on Opus 5.5 High.** No subagent may use Opus.
  - **Code-writing agents:** give each one exclusive file ownership.
  - **Updates:** update this file after every step.

---

## 2. LIVE NOW: wave 1 (loaded since the 20:35 restart; Opus-verified)

| Problem in the feed/logs | Root cause | Fix |
|---|---|---|
| Auto mode still needed a manual approve ("ACCEPTED ... will be placed automatically", then "placed (user)") | `FastLoop._check`/`_check_exit` called `_on_proposal` but never executed | `Engine._auto_execute_run(run_id)` (`app/engine/execution.py`), used by `cycle.py` and both fast-loop paths. **Confirmed live:** orders now say "placed (auto)". |
| XLY bought 5× (20 shares; MAX_ADDS=1) | A placed-but-unfilled BUY was neither held nor pending; `FAST_LOOP_COOLDOWN_MINUTES=1` | `risk.working_buys`/`working_buy_symbols`, the "Working entry" risk check, and the fast-loop exclusion. **Confirmed live:** duplicate BUYs are rejected. |
| `could not re-place the stop ... [OPENAPI_GTX_NOT_SUPPORT_MARKET]` | `place_stop` sent session ALL premarket; Webull rejects STOP with extended hours | `place_stop` always sends **CORE + GTC** (`resolve_session(queue_for_core=True)`). **Confirmed live:** Webull UAT accepts CORE GTC STOP_LOSS premarket (SUBMITTED). Such a stop is active only 09:30-16:00 ET. |
| Stop alert spam | `alert=True` on every check | `reconcile._stop_alert` dedupes per (symbol, reason) |
| Min volume rejected XLV/XLF/TLT premarket | It used today's partial volume | 20-day average daily $ volume from bars (`eligibility._avg_daily_dollar_volume`) |
| `Signal refresh failed: TimeoutError()`; 429 storms on `get_stock_bars` | 260-bar refetch of ~56 symbols every 2 min, no pacing | `transport.CallSpacer`, a bar cache with incremental fetch, and a 100 s partial-result deadline. **Live:** 429s still appear during the startup burst. Re-check after the next restart. |
| QRVO `INVALID_SYMBOL` forever | No negative cache | 24 h negative cache. **QRVO was removed from `app/universe.py`** (it doesn't trade; likely gone after the Skyworks deal). |
| Webull `search_news` failing for every symbol | Only a per-symbol backoff | Provider breaker: 5 failures → 30 min pause. **Confirmed live.** |
| "BUY 4.0 XLY" | Formatting | `:g` |

Also live:
- **pandas fixed:** `.venv` had pandas 3.1.1, which needs numpy ≥ 2.0.2, against the pinned numpy 1.26.4. Every yfinance call failed. pandas 3.0.6 is installed and pinned in `requirements.txt`.

Tests: `tests/test_autotrade_fixes.py`, `test_stop_session_fixes.py`, `test_data_pipeline_fixes.py`.

---

## 3. ON DISK, NOT LIVE, NOT YET OPUS-VERIFIED

### 3.1 Wave 2a: correctness follow-ups (complete; suite 643 passed at the time)
- **`reconcile.py`:**
  - It writes the ledger row when it settles an `unknown`/`submitting` BUY to placed, which closes the second duplicate-buy route.
  - Per-symbol backoff on stop re-place failures: 150 s, doubling, max 30 min.
- **`risk.working_buys`:** it also counts recent `placed` BUYs that have no ledger row.
- **`proposals.py`:** the slow cycle skips symbols with a working BUY.
- **`signals.py`:**
  - Tasks are cancelled on an outer cancel.
  - The deadline starts before the lock.
  - Bar TTL ≤ the refresh interval while a session is open.
  - Bars are merged by trading day.
- **`consensus.py`:** the ML downtrend override needs `ml_p > max(0.70, ML_MIN_P)` (it was 0.52, against `strategy.md`'s >70%).
- **`config/env.py`:** `TRADER_SKIP_DOTENV`.
- **Mock:** it accepts a CORE GTC STOP_LOSS outside hours.
- Tests: `tests/test_followup_correctness.py`.

### 3.2 Wave 2b: extended-hours protection (complete; suite 653 passed)
- **`execution.py`:**
  - Outside CORE the bracket is skipped up front and a single entry is sent.
  - `_auto_execute_run` catches `Exception` and continues.
- **`portfolio._protect_filled_entry`:** places the CORE stop **as soon as the fill is recorded**. It is idempotent.
- **Soft stop** (`risk_loop._soft_stops`, `EXTENDED_HOURS_SOFT_STOP=true`):
  - **When:** in session ALL (or NIGHT for allowed overnight symbols), when the fresh price is ≤ the stop.
  - **What:** it cancels the CORE stop and sends a LIMIT SELL at price × 0.995, once per trade (kv key `soft_stop:<trade id>`).
  - **Needs a review decision:** equity snapshots have no bid/ask, so it triggers on one `last` print. Consider requiring 2 consecutive prints or N seconds below the stop.
- **Limitations:**
  - No stop resize on later partial fills.
  - A soft-stop sell that expires unfilled does not re-fire; reconcile re-places the CORE stop.
- Tests: `tests/test_extended_hours_protection.py`. `test_trading_session.py` was updated.

### 3.3 Wave 2c: news and Yahoo (complete; suite 723 passed)
- **Root causes:**
  - The Google query became `NVDAstock`: the space was lost in `.env` parsing, the edition was SG, and there was no pacing, so 429s.
  - LLM scoring truncated at `max_tokens=2048` on a reasoning model, so **0 scores were stored**. Only 41 of 1,849 headlines were ever scored.
  - There was no relevance or age filter, and the Yahoo earnings calendar was stale.
- **New `app/news_events.py`:**
  - Query builder: `"NVDA" stock when:3d`. Word-like tickers (ON/ARM/MU/NOW/ALAB) use the company name.
  - Title relevance filter.
  - Local lexicon scorer.
  - Weighting: 24 h half-life, source tier, scorer weight.
  - Hard-event classifier.
- **`sentiment.py`:**
  - Google pacing plus a 20 min 429 breaker.
  - Every relevant headline is scored locally; the LLM upgrades them in batches of 10 (6 max per run) and halves the batch on truncation.
  - `summary().n` = relevant scored headlines.
- **`eligibility.py`:**
  - Event veto: a negative hard event within 48 h blocks BUYs. That covers downgrades, guidance cuts, offerings, investigations/lawsuits, FDA rejections, bankruptcy, being acquired, and delisting.
  - Earnings dates come from yfinance (the calendar ∪ history, 12 h cache).
- **Migration `0005_news_scoring.py`.**
- **Not done:** analyst data (targets/recommendations), the macro regime, the news-surge trigger, and measuring news value per trade.
- **Check after the restart:** `CONSENSUS_MIN_NEWS=3` (§5). Confirm whether a symbol with fewer than 3 relevant headlines is *blocked* or treated as *neutral* by `consensus.py`. It may cut trade frequency noticeably.
- Tests: `tests/test_news_pipeline.py` (68 tests).

### 3.4 Market-style orders (complete; suite 758 passed; enabled in `.env`, §5)
- **Settings:**
  - `EXIT_ORDER_TYPE=market`: true MARKET exits in CORE only. A rejected MARKET is re-sent once as a LIMIT.
  - `ENTRY_ORDER_TYPE=marketable`: limit = last × (1 + `MARKETABLE_OFFSET_BPS`/10⁴), capped by the price band, tick-rounded.
  - `ENTRY_CHASE_SECONDS=45`: after 45 s unfilled in CORE, re-send once as marketable. Restart-safe via the kv keys `chase_cancel:<pid>`/`chase:<pid>`.
  - `MARKET_NOTIONAL_BUFFER_PCT`.
- **MARKET payload:** DAY with no `limit_price`, verified from `webull_openapi_mcp/tools/trading/stock_order.py` and `guards.py`.
  - **No MARKET order has been sent to Webull yet.** Watch the first one after 09:30 ET.
- **Entries never use true MARKET.** This is by design and the user was told.
- **New files:** `app/order_style.py`, `app/engine/chase.py`, migration `0006_order_type.py`, and `execution_quality.by_order_type`.
- Tests: `tests/test_market_orders.py` (34 tests).

### 3.5 CRITICAL: stale premarket prices (complete; suite 766 passed, 0 failed)
- **Observed live (premarket):**
  - ADI: limit $378.75 (stop 348.45, target 401.48), filled $343.00, now about $328.
  - TXN: 291.19 → 280.41. SNPS: 426.99 → 426. NXPI: 256.03 → 251.90.
- **Root cause:**
  - `parsers.parse_snapshot` skipped the `ExtHr`/`OVN` lines of the MCP snapshot (format: `webull_openapi_mcp/formatters.py:359-425`).
  - So outside CORE every "live" price was the previous regular close. That fed the limit, the band check (stale against stale = 0% → pass), the stop/target, the fast loop and the soft stop.
- **Fix:**
  - `parse_snapshot` reads `ext_price`/`ovn_price`.
  - `live_price_for_session()` is applied in `market_data._fetch_snapshots`, so every consumer gets the session-correct price.
  - `Quote.stale` and `Broker.is_price_stale`.
  - `risk.py`: a BUY outside CORE with only a stale close is rejected, and a BUY with `last <= stop` is rejected.
  - `proposals.py`: stale symbols are skipped, and the limit/stop/target are anchored to the live price.
- **Limitation:** the snapshot text has no print timestamps. Before the first premarket print, yesterday's after-hours ExtHr price looks current. Exposing the MCP `last_trade_time` would fix it.
- Tests: `tests/test_extended_hours_prices.py`.

### 3.6 STOPPED MID-WORK: stop re-anchoring after a bad fill
- **Goal:**
  - When an entry fills at or below its planned stop (the ADI case), or the live price is at or below it, re-anchor the stop and target from the fill with `ta.levels(fill, atr)`.
  - Never re-anchor upward.
  - Persist the new levels and alert.
  - Also fix existing resting stops priced above the fill.
- **On disk:**
  - `app/portfolio.py` ~350-470: `_reanchored_levels`, `reanchor_stop` and `correct_stale_stops`, using the kv key `reanchored:<pid>`.
  - It is called from `_protect_filled_entry` (~498) and from `reconcile._verify_protective_stops` (~226, ~245).
  - `app/db/ledger.py` got a helper.
  - `tests/test_stop_reanchor.py` (143 lines).
- **Status: the agent was stopped before it reported.** Its own test results are unknown; see §6 for the wrap-up run.
- **Must review:**
  - The cancel-then-replace logic in `correct_stale_stops`: it must only replace after a **confirmed** cancel.
  - That a failure can never leave a position with no stop.
- **Live case:** ADI (1 share) still has a resting CORE stop at **348.45**, above the market (about $328). Unless the re-anchor code fixes it at restart, or someone changes it in Webull, it will **sell ADI at the 09:30 ET open**.

### 3.7 STOPPED MID-WORK: swing strategy part A (payoff and risk sizing, with backtests)
- **Code on disk** (new settings, all default to today's behaviour, `# swing-strategy` block in `settings.py`/`loader.py`):
  - `BRACKET_TAKE_PROFIT` (default true; false means a stop-only bracket and code-managed exits). Used in `execution.py:331` and the backtester.
  - `SIZE_FROM_RISK` (default false). `RISK_PER_TRADE_PCT` (default 0). Both in `app/sizing.py`.
  - `BACKTEST_LIVE_EXITS` (default false): a new daily-bar model of the live exit stack in `app/backtest/engine.py` (`process_bar_live`, ~403). **New and unverified.**
- **Scripts:**
  - `scripts/fetch_yahoo_bars.py`: **87 symbols** of Yahoo daily bars are in `data/bars_yahoo/`.
  - `scripts/swing_backtest.py`: a variant matrix with an out-of-sample slice. It writes to `data/backtests/swing_2026-11-24*/`. It crashed once on Windows multiprocessing (`WinError 87`); use `--workers 1`.
- **Tests:** none were written for the new settings (no `tests/test_swing_strategy.py`).
- **No report was written** (`docs/BACKTEST_SWING_2026-11-24.md` does not exist).
- **Preliminary results** (copied to `docs/backtest_prelim_2026-11-24/`, unverified; for direction only).
  - The universe is today's (survivorship bias).
  - `exp R` = expectancy per trade in R.

  | variant | trades | win% | exp R | return | maxDD |
  |---|---|---|---|---|---|
  | baseline (live `.env` settings, with costs) | 9,897 | 48 | **−0.242** | −98% | 99% |
  | baseline, give-back exit OFF (`gb0`) | 12,923 | — | −0.064 | −28% | 43% |
  | baseline, zero cost | 24,960 | 59 | +0.028 | +68% | 15% |
  | baseline, gb0, zero cost | 13,519 | 60 | +0.108 | +131% | 12% |
  | plain 2-ATR stop + 20-day time exit, zero cost | 6,213 | 35 | +0.130 | +73% | 19% |
  | wider TP (3/4/6 ATR), no TP, trailing variants (with costs) | ~7,600 | ~30 | −0.30 to −0.36 | ≈ −99% | ≈ 99% |

- **What this suggests (needs verification):**
  - **Costs dominate.** `BACKTEST_COMMISSION_PER_ORDER=1.0` is likely wrong for Webull (US stocks are commission-free), and ~10k trades means very high turnover.
  - **The give-back exit (`.env`: `PROFIT_GIVEBACK_PCT=20`, `PROFIT_GIVEBACK_MIN_GAIN_PCT=1.0`) cuts winners hard.** Turning it off was the single biggest improvement.
  - **Wider targets and trailing did not help** in this model, possibly because give-back/scale-out exit first. Re-test with give-back off.
  - **Verify the backtester first:** the zero-cost baseline's 25k trades vs 10k with costs is odd; `BACKTEST_LIVE_EXITS` fidelity; slippage/impact settings.
- **Not started:**
  - step 3: pyramiding on winners (add once at +1R, stop to breakeven, total risk ≤ 1R);
  - step 4: breakout entries;
  - step 5: top-N ranking, sector cap, risk-off throttle;
  - step 6: the final backtest report.

---

## 3A. Opus verification results (step 2, 2026-11-24 evening)

### Verifier C: news, consensus, migrations (done; 83 related tests pass)
- **`CONSENSUS_MIN_NEWS=3`:** a symbol with fewer than 3 headlines is treated as **NEUTRAL (it passes)**, not blocked (`consensus.py:100-103`).
  - Simulated on a live-DB copy: 76 symbols, median n = 4. **0 of 76 blocked** by sentiment, because `aggregate` divides by `den+1`, which squeezes scores.
  - So the sentiment gate has about zero effect on trade frequency.
  - The real lever is the **event veto**: it blocks 3 of 76 symbols (NVDA is a false positive, INTU arguable, ZS valid).
- **Migrations 0005/0006:** apply cleanly on a copy (user_version 4 → 6), are idempotent, and lose no data (integrity ok).
  - Back up with the sqlite3 backup API, or copy db+wal+shm after stopping.
- **Correct:** the ML override threshold of 0.70, `TRADER_SKIP_DOTENV`, the Google 429 breaker, the signals.py items, and the proposals working-BUY skip.
- **Should-fix (none are blockers):**
  1. `news_events.py:338`: `_ACQ_VERB_RX` matches "to buy", so "3 Reasons to Buy Nvidia" is read as `acquisition_target` and triggers a 48 h veto. **Top priority.**
  2. `news_events.py:299/333`:
     - "reverses downgrade" still vetoes (NVDA would be vetoed right after the restart).
     - The event is not bound to the clause that names the symbol ("upgrades AMD, downgrades Intel" vetoes AMD).
     - The acquirer gets tagged as the target.
  3. `news_events.py:311-316`: resolutions still veto (dismissal, "SEC drops probe", "cleared", "no fine"), and so does "X sues Y" when X is the symbol.
  4. `news_events.py:310`: insider sales ("Huang sells $15M of shares") are read as an `offering`.
  5. `news_events.py:472` with `db/signals.py:104`: the keyword `event_type` is never overridden by the LLM's `llm_event`/`llm_relevance`.
  6. `yfinance_service.py:144-166`: the earnings lookup **fails open**. With yfinance 1.8 `hide_exceptions`, an empty calendar plus a None frame becomes `[]`, cached for 12 h. This is the ADI-into-earnings route. Fix: fail closed and use a short cache for empty results.
  7. `sentiment.py:697-707`: Groq's 400 `json_validate_failed` should go to the batch-halving path, not a 10 min pause.
  8. `sentiment.py:456-469` with `signals.py:151-154`:
     - The news fetch runs inside the 100 s signal deadline, and Google is serialised (≥ 53 s).
     - Google waiters also hold the shared NEWS_CONCURRENCY slots.
     - On timeout, nothing is stored and there's no retry for 15 min.
     - Fix: move the news fetch to a background task.
  9. Nit: the lexicon reads "drops" as negative in "SEC drops investigation".

### Verifier A: stop safety (done; 31 related tests pass; 5 probe tests in scratchpad `verifyA/test_probe_stops.py` confirm the defects)

**Blockers**
- **B1:** a transport error on `place_stop` leaves the protective row `unknown`. That row counts as an armed stop for 24 h (`portfolio.py:457-467`, `:501-509`, `reconcile.py:232`, `risk_loop.py:207-210`). Only `_sync` (`portfolio.py:188`) clears it, after 86,400 s.
  - In `correct_stale_stops` the old stop is already cancelled by then, so the position has **no stop**.
  - Fix: don't count an `unknown` row older than ~3-5 min whose coid is absent from open orders and history. Or close it after `RECONCILE_GIVE_UP_MINUTES`.
- **B2:** a restart while ADI's 348.45 stop is still working would re-anchor it to 315.56 (target 363.58) and **override the user's "let it sell" decision**.
  - Restart only after the stop has filled, or set kv `reanchored:22` first.
  - A restart after the fill is safe.

**Should-fix**
- **S1:** `portfolio.py:397` re-anchors when the fill was above the stop but the live price is now at or below it. That moves a valid stop DOWN (TXN probe: 267.89 → 257.98). It also happens after a rejected soft-stop sell. Fix: only when `fill <= planned`.
- **S2:** `correct_stale_stops` matches on the symbol, not the trade (`portfolio.py:429-433`).
  - With a starter trade plus an add-on, it cancels the add-on's valid stop, under-sizes the new one and moves the starter's ledger stop up.
  - It compares only against the fill, so a profit-locking stop above the fill gets moved down.
  - Fix: match by `proposal_id` or per symbol. Act only when stop ≥ live and the session is not CORE.
- **S3:** cancel success is assumed (`portfolio.py:440-449`; same pattern at `execution.py:535`).
  - The MCP reports a 404 cancel as "Order not found; it may have been filled…" and a 403 as "Permission denied…". Neither matches `ERROR_PREFIXES`, so they count as success.
  - Webull's cancel is also only an acknowledgement and takes effect later. A new SELL stop on a flat position means short risk.
  - Fix: two-phase. Cancel, then place only after `_sync` shows CANCELLED. The missing-stop branch already re-places at the updated ledger stop.
- **S4:** the soft stop fires once per trade, not per symbol (`risk_loop.py:158-188`). Two trades in one symbol give two full-quantity SELLs (short). Fix: dedupe per symbol and skip if a working SELL exists.
- **S5:** an unfilled soft-stop DAY limit keeps working into CORE with the CORE stop cancelled (`risk_loop.py:204`). On a gap down there is no protection all session. Fix: at 09:30, or after N s unfilled, cancel it and exit at MARKET (or restore the CORE stop).
- **S6:** the soft stop can fire on a single `last` print, `as_of` is the fetch time, and `q.stale` is ignored (`risk_loop.py:176-185`). Fix: require `not q.stale` and 2+ readings below the stop, 30-60 s apart.
- **S7 (unverified):** in CORE with re-anchored stop ≥ live, Webull may reject a sell stop above the market and nothing exits (`portfolio.py:407-411`). Fix: send an exit order instead.

**Nits**
- The soft stop calls an uncached `positions()` every 1 s in extended hours. Throttle it to 5-10 s.
- ADI's stop row 91 has no proposal id, so its exit is recorded as `manual`.

**Correct:**
- the kill switch never blocks exits, stops, the soft stop or flatten;
- `_auto_execute_run` lets `CancelledError` through;
- the bracket is skipped outside CORE;
- reconcile runs before the risk loop at startup;
- re-anchoring never moves a stop up and doesn't keep lowering it;
- the wave-2a ledger row and the stop re-place backoff.
### Verifier B: prices and order styles (done; 51 related tests pass; probes in scratchpad `verifyB/test_probe_b.py`)
1. **Should-fix (near-blocker in auto):** a chase can re-send onto a closed ledger row.
   - Where: `chase.py:173-200`.
   - While the chase waits for the cancel, `portfolio.track_trade_fills` can `cancel_trade(pid)`. The re-sent BUY then has a `cancelled` ledger row: its fill is never tracked, no stop is placed on fill, and the working-entry guard can't see it.
2. **Should-fix:** an ambiguous MARKET exit error (HTTP 5xx / ReadTimeout counts as non-transport) is re-sent as a LIMIT, so the SELL can go out twice.
   - Where: `execution.py:427`, `flatten.py:203`, options close `:229`.
   - Fall back only on clear rejections.
3. **Should-fix:** the first print of a session can be old (`parsers.py:245-264`). NIGHT at 20:00 may use last night's OVN; ALL just after 16:00 may use this morning's ExtHr.
4. **Nit:** `risk.py:230` skips the stale check when the snapshot fetch fails.
5. **Nit:** the risk, size and target checks aren't re-run at the marketable/chase re-price (`execution.py:294-301`, `chase._plan`).
6. **Pre-existing:** `proposals.py:263-265`, the kill switch rejects exit proposals (time stop, trend, ML) as well as openings. This conflicts with "exits always pass".

**Correct:**
- the parser edge cases;
- session pricing;
- the stale guard is BUY-only;
- the band checks;
- the MARKET payload, which is sent only in CORE;
- the kill switch at the broker guard;
- chase single-shot and restart safety;
- migration 0006 idempotency.

Not verified: whether cancelling a bracket master leg cancels the whole combo.

### Fix wave F (Sonnet)
- **F-C (news/earnings): DONE.** All 9 items are fixed. New `tests/test_news_veto_fixes.py`. `-k "news or sentiment or eligib or signal"`: 126 passed.
  - **Veto logic:**
    - `classify_event` binds an event to the clause that names the symbol; a clause of three words or fewer joins its neighbour.
    - `_RESOLVED_RX` stops dismissed or dropped cases from vetoing.
    - `effective_event()` makes the LLM override work.
    - "merger with" is now neutral.
  - **Earnings:** an `unknown` result (no dates, not a fund) returns None and is not cached, so live fails closed.
  - **News timing:** the news fetch now runs as a `refresh_background` task, and a signal cycle waits at most 8 s for it (`NEWS_GRACE_SECONDS`).
  - **Untested:** the VMware example (VMware is not in the alias list).
- **F-A (stop safety): DONE.** Named files: 70 passed. `-k "stop or reconcile or portfolio or risk_loop or soft"`: 110 passed.
  - **B1 (stuck `unknown` stop rows):**
    - `_sync` closes an `unknown` row the broker never showed after `reconcile_give_up_minutes`, instead of 24 h.
    - The verify pass closes an `unknown` sl row older than 5 min whose coid is in neither open orders nor history, then re-places it.
    - If history can't be read, nothing is dropped.
  - **S1:** re-anchor only when `fill <= planned`.
  - **S2+S3:** `correct_stale_stops` is now two-phase and per trade.
    - Phase 1 cancels and writes kv `reanchor_pending:<pid>`, but only on a confirmed cancel reply.
    - Phase 2 (`_finish_reanchor`) places the new stop once the old row is cancelled; if the old stop filled, it clears the marker.
    - It acts only when stop ≥ fill, stop ≥ live and the session is not CORE.
  - **S4:** one soft-stop sell per symbol, and none while another SELL is working.
  - **S6:** needs a non-stale quote and 2 readings at least 30 s apart.
  - **Nit:** `positions()` is throttled to every 5 s.
  - **S5:** `_soft_followups` cancels an unfilled soft-stop sell at CORE, then restores the CORE stop, or exits if price ≤ stop.
  - **S7:** `Portfolio.stop_above_market`/`exit_position`. In CORE, a stop at or above the price becomes a MARKET exit instead (at most one per 15 min per symbol).
  - **Chase guard:** `track_trade_fills` skips an entry that is mid chase-cancel.
  - **Note:** `tests/test_stop_reanchor.py` now runs premarket, because at the suite's 10:00 ET clock the CORE rules exit instead of placing a stop.
- **Full suite after wave F: 839 passed, 0 failed** (13 min). `gen_env_example --check` exits 0. The orchestrator's later S-1/Q3 edits were re-run in their own files: 52 passed.
- **Wave-F verification (Sonnet low, read-only):** started 09:28 ET.
  - **V-FA (stop-safety fixes): DONE.** 49 tests passed and no blockers. It confirms:
    - no path leaves a position bare with nothing pending;
    - no oversell path;
    - the kill switch exempts all exits and stops;
    - re-anchoring is never upward;
    - the B1 safety holds;
    - the exit dedupe is in kv, so it survives a restart.
  - **S-1:** phase 2 waits forever if the old stop row stays `active` (the broker no longer reports it). The orchestrator added a one-time alert after 10 min (`reanchor_stuck_alert:<pid>`, `portfolio._finish_reanchor`). It deliberately does not auto-place, to avoid an oversell.
  - **Nits left open:**
    - N-1: a rejected exit is re-tried every pass with no backoff.
    - N-2: the settled `soft_stop:<trade>` kv blocks a second extended-hours soft stop on the same trade.
    - N-3: `portfolio.py:301` lacks `allow_when_killed` (harmless).
    - N-4: `execution.py:567` marks rows cancelled before the broker confirms (CORE only).
  - **V-FB (order and news fixes): DONE.** 138 tests passed and no blockers. It confirms:
    - an ambiguous MARKET error never re-sends;
    - the chase can't double-buy, and the kill switch is re-checked;
    - the kill switch blocks openings only;
    - the earnings lookup fails closed (including when the quoteType lookup raises);
    - the veto spot-checks pass;
    - no overlap in the background news task.
  - **Q3 fix:** `flatten.cancel_open_orders` no longer marks a protective row cancelled when its cancel was unconfirmed or raised. The orchestrator fixed it, with `test_flatten_cancel_keeps_unconfirmed_stop_tracked`.
  - **Coverage gap:** a deal target missing from the alias list (e.g. VMware) gets no `acquisition_target` veto.
  - **Re-run after the orchestrator's S-1 and Q3 edits:** order_style_fixes + stop_safety_fixes + stop_reanchor, 52 passed.
  - Owns `portfolio.py`, `reconcile.py`, `risk_loop.py`, `db/protective.py` and the stop tests.
- **F-B (order styles): DONE.** Targeted tests: 49 + 22 passed, and the `-k` sweep is clean. New `tests/test_order_style_fixes.py`.
  1. **Chase race:** `ledger.reopen_trade_for_chase` reopens the ledger row before the re-send; if it can't, the chase doesn't send. The `portfolio.py` complement was handed to F-A.
  2. **Ambiguous MARKET errors:** `order_style.market_rejection_is_definitive`. A Validation/Parameter/GTX/"not allowed" error falls back to LIMIT. Anything else re-raises as transport, so the proposal goes `unknown` and reconcile settles it by coid.
  3. **Unconfirmed cancels:** `order_style.cancel_reply_confirmed`. "Order not found" and "Permission denied" now count as unconfirmed, and the exit sell, the chase re-send and the flatten count all respect it.
     - **Review:** after an unconfirmed cancel, flatten still marks the protective rows cancelled.
  4. **Leftover session prints** (`parsers.live_price_for_session`): during the first 15 min of each session, a NIGHT print is marked stale, and an ALL print more than 1% away from the regular price is too. This only blocks BUYs, so early-premarket gap entries may be skipped.
  5. **One quote for the stale check:** `risk.py` takes `last` and `stale` from a single `quote()` call. With no quote available, a BUY outside CORE is rejected.
  6. **Re-price checks:** `risk.reprice_violation` runs in the marketable re-price and in the chase `_plan`.
  7. **Kill switch:** `proposals.py` blocks only opening proposals, so exit proposals pass under a kill.


### Live observations at the open (09:30-09:36 ET, app still on wave-1 code)
- **ADI:** the 348.45 stop filled at **328.00** (−$15.00), as the user chose.
- **NEW defect: churn / instant re-entry.** There is no re-entry cooldown after an exit. The only one is `FAST_LOOP_COOLDOWN_MINUTES=1` in `.env`, and it only covers fast-loop triggers.
  - **ADI:** stopped out at 328, then re-bought 1 min later (#54 @ 335.17 limit, filled 331.52).
  - **TXN:** a deterministic SELL #57 @ 275.28, then a fast-loop BUY #59 @ 276.34 about 90 s later.
  - **NXPI:** a deterministic SELL #56 (exit 243.11, −$8.79), then a BUY of 2 @ 244.40 about 90 s later.
  - The exit rules and the entry rules disagree, and each round trip pays the spread and slippage. This matches the backtest's very high turnover (§3.7).
  - **Fix needed:** a per-symbol re-entry cooldown after any exit, longer after a stop-loss. Plus a backtest of it.
- **NXPI also logged** `could not re-place stop 235.55: [OPENAPI_GENERATE_NEW_SHORT_POSITION]`. A working SELL #56 already reserved the shares, so verify should not try to re-place a stop while a SELL is working. Re-check this on the new code: `working_sell` should cover it, but the SELL may not have been listed yet.
- **#54 and #56 "placed (user)": not a bug.** The user confirmed they clicked approve.

### Restart (09:42 ET) and churn fix (restarted 09:50 ET)
- **Before the restart:**
  - DB backup: `data/backups/trader_pre_restart_2026-11-24_0940ET.db` (integrity ok, user_version 4).
  - `.env` backup: `data/backups/.env.pre_restart_2026-11-24`.
- **After the restart:**
  - Migrations: user_version is now **6**.
  - The status shows `min_news=3` and `sent_min=-0.3`.
  - Startup reconcile was clean.
  - The **ENTRY CHASE** fired (TXN re-sent as a marketable limit).
  - LLM scoring is working (60 headlines in 6 batches).
  - The event veto is firing: INSM was blocked for "acquisition target (deal pending)" (Sanofi), which is valid.
- **Churn root cause (fixed by the orchestrator, `app/engine/proposals.py` SELL loop):**
  - The high-water mark kv `hwm:<symbol>` (and `scaled:<symbol>`) was only reset when a deterministic cycle saw the symbol flat.
  - A fast-loop re-buy within about 70 s inherited the previous trade's peak. The give-back rule then sold the new trade at once ("gave back 105% of +5.4%") → re-buy → sell...
  - **Fix:** kv `profit_entry:<symbol>` = the entry time of the oldest open trade. When it changes, `hwm`/`scaled` are reset.
  - Tests: `tests/test_reentry_hwm.py` (2). The `-k "exit or profit or proposal or determin"` sweep: 60 passed.
  - In the first 4 min after the 09:50 restart there were no churn sells.
- **Still seen:**
  - Google News returned 429 on its first call after the restart, so the 20 min breaker is on. Watch whether it recovers.
  - Webull `search_news` returns 404, so its 30 min breaker is on.
  - `get_stock_bars`/`get_account_positions` 429s: about 10 per minute in the first minutes, then falling.
  - Many ETFs (SPYM/QQQM/IWM/XLK/XLE/IEFA/IAU) show `blocked` in the signal refresh. The reason hasn't been checked yet.
- **ML status:** the only trained model (#1) was **rejected**: walk-forward AUC was about 0.50, so it had no edge. The live gate runs "rules only".

---

## 4. Live positions and state (at ~08:30 ET, UAT sandbox, $2M paper account)

| Symbol | Qty | Avg cost | Last | Stop at Webull (CORE GTC) | Note |
|---|---|---|---|---|---|
| ADI | 1 | 343.00 | ~328 | **348.45 (above market!)** | Stale-price entry; see §3.6 |
| TXN | 1 | 280.41 | ~276 | 267.89 | Stale-price entry |
| SNPS | 1 | 426.00 | ~418 | 392.83 | |
| NXPI | 1 | 251.90 | ~255 | 235.55 | |
| XLF | 8 | 55.63 | ~55.5 | 53.64 | |
| XLY | 20 | 116.82 | ~116.5 | 111.78 | 5× duplicate buy from before wave 1. The user chose to keep it (risk ≈ $101 < $200 cap). |
| MSFT | 1 | 491.92 | ~488 | 458.56 | |
| AAPL | 1 | 348.85 | ~347 | 333.65 | |
| AMZN | **2** | 254.23 | ~251 | 236.12 | **Unexplained:** the ledger shows 1 BUY (#3) and 1 flatten SELL (#5). Investigate with Webull order history. |

- **Working BUY orders:** XLV 2 @ 179.42, TLT 6 @ 81.69, SWKS 6 @ 77.12. These are stale-priced premarket limits.
- **Day:** day P&L about −$38, 21 trades.

---

## 5. `.env` changes made (all with the user's explicit OK; backups were saved in the session scratchpad, which may be gone)

| Setting | Old | New | Why |
|---|---|---|---|
| `ELIGIBILITY_EARNINGS_BLACKOUT_DAYS` | 0 | **3** | 0 disabled the blackout; ADI reported earnings and was bought |
| `SENTIMENT_RSS_URL` | SG edition | US edition (`hl=en-US&gl=US&ceid=US:en`) | Relevance |
| `SENTIMENT_RSS_QUERY_SUFFIX` | ` stock` | removed | The code builds the query now |
| `CONSENSUS_MIN_NEWS` | 0 | 3 | `n` now counts relevant, scored headlines |
| `CONSENSUS_SENT_MIN` | −0.80 | −0.30 | −0.80 never blocked |
| `EXIT_ORDER_TYPE`, `ENTRY_ORDER_TYPE`, `MARKETABLE_OFFSET_BPS`, `ENTRY_CHASE_SECONDS` | (unset) | `market`, `marketable`, `10`, `45` | User asked for market orders in regular hours (appended at the end of `.env`) |

**None of these are live until the next restart.** `.env.example` is regenerated, and `gen_env_example.py --check` exits 0.

---

## 6. Test status at wrap-up

- **2026-11-24 ~09:05 ET (resume run): 773 passed, 0 failed** (12 min, full suite on the current disk state, including `tests/test_stop_reanchor.py`). Step 1 is done for the code that has tests. The swing settings in §3.7 still have no tests.
- `gen_env_example.py --check` exits 0.

---

## 7. Next steps (in order)

1. **Make the suite green.** Finish or roll back §3.6 (re-anchor) and §3.7 (swing settings and backtester) as needed; both were stopped mid-work.
2. **Opus verification of everything in §3** (read-only, adversarial, the same style as the wave-1 verifier). Focus on:
   - the re-anchor cancel/replace safety;
   - soft-stop single-print triggering;
   - the stale-price guard vs closing orders;
   - market-order and chase interactions with the working-entry guard and the kill switch;
   - the effect of `CONSENSUS_MIN_NEWS=3`;
   - migrations 0005/0006 on the live DB (back up `data/trader.db` first).
3. **Restart, then watch the feed.** Before 09:30 ET, decide what to do about ADI's 348.45 stop (§3.6/§4). After the restart, confirm:
   - migrations applied;
   - ExtHr prices used premarket;
   - no stale-price BUYs;
   - Google news arriving with no 429;
   - headlines scored;
   - the event veto/earnings blackout firing;
   - the first MARKET exit and marketable entry after 09:30 ET;
   - 429 levels on `get_stock_bars`.
4. **NEW, user's direction (09:45 ET): "It will be best if the ML can read candles and find opportunities to enter."**
   - The user prefers this over a simple re-entry cooldown.
   - Build an ML candle-entry model: multi-bar OHLCV/candle features, a triple-barrier label, and walk-forward evaluation net of realistic costs.
   - Compare it with the rules baseline, and show the user before enabling it.
   - **Started 09:58 ET** (Sonnet low, research only).
     - Files: `app/ml_candles.py`, `scripts/ml_candle_research.py` and `tests/test_ml_candles.py`.
     - Output: `data/ml_research/candles_2026-11-24/`.
   - **RESULT (done ~10:20 ET): no reliable edge.** Data: 86 symbols, 99.7k symbol-days, 5 expanding walk-forward folds (2022-12 to 2026-11) with an 11-day embargo. Picks are the top decile.
     - **AUC:** MLP mean 0.515-0.528 (per fold 0.48-0.56); logreg about the same.
     - **1.5×ATR target:** all entries +0.058R, random +0.04 to +0.06R, MLP +0.072R, rule (breakout + volume) +0.045R.
     - **3×ATR target:** all entries +0.121R, random +0.09 to +0.12R, **MLP +0.153R (5 of 5 folds positive, worst DD −15%)**, rule +0.127R.
     - The MLP's lead over random (~+0.035R) is below the noise (SE ≈ 0.04R across folds). Most of the positive expectancy is market drift.
     - **The useful takeaway is not the ML.** A **3×ATR target roughly doubles expectancy vs 1.5×ATR** for every selector, and the live setting is `TP_ATR_MULT=1.5`. Confirm this in the backtester (4a).
     - Caveats: survivorship bias (today's universe) and correlated samples.
     - Files: `app/ml_candles.py`, `scripts/ml_candle_research.py`, `tests/test_ml_candles.py` (6 pass), `data/ml_research/candles_2026-11-24/`. Nothing was wired into the app.
4a. **Backtester check DONE (~10:25 ET).**
   - **Why trade counts differed:** capital ruin, not a gate. With $1/order, equity fell from $25k to about $470 and `can_enter` refused entries ("insufficient cash", `backtest/engine.py:184-186`).
   - **Bug fixed:** same-day re-entry (`backtest/engine.py` ~745). A symbol exited intraday could be re-bought at that day's open, which live can't do.
   - **No live-exit fidelity defects.** The stop is checked first, gaps fill at the open, and per-trade state is fresh. Approximations: the peak uses the bar high (live uses the highest close), and a raised stop applies only from the next bar.
   - **Costs:** now Webull-realistic: commission 0, SEC fee 0.0000278×sell notional, TAF $0.000166/share (max $8.30), plus 5 bps slippage, ATR slippage and 5 bps impact. New CLI flags `--commission/--slippage-bps/--sec-fee/--taf/--cooldown`, `--stage v2`.
   - **Tests:** `tests/test_swing_strategy.py` 10 + `test_backtest.py`, 31 passed.
   - **v2 results** (87 symbols, $25k, full history; OOS = last 365 days; `data/backtests/swing_2026-11-24_v2/`):

     | variant | trades | win% | exp R | return | maxDD | OOS trades / exp R / return / DD |
     |---|---|---|---|---|---|---|
     | a baseline (live .env) | 16,740 | 59 | −0.029 | −35.8% | 42.1% | 4,585 / −0.052 / −21.2% / 33.6% |
     | b give-back off | 10,401 | 59 | +0.024 | +33.0% | 17.0% | 3,021 / −0.012 / −0.8% / 17.0% |
     | c b + no TP + trailing 2.5 ATR | 6,589 | 46 | +0.072 | +46.8% | 16.4% | 1,859 / −0.011 / +2.6% / 16.4% |
     | **d b + no TP, no scale-out, no break-even, no RSI exit; 2-ATR stop + 20-day time stop (+ trend-break exit)** | 5,234 | 45 | **+0.140** | **+81.4%** | 15.7% | 1,409 / **+0.148** / **+15.1%** / 15.7% |
     | e b + 3-day re-entry cooldown | 6,832 | 59 | +0.029 | +25.0% | 13.4% | 1,815 / −0.007 / +2.2% / 11.1% |

   - **Variant d is the only one positive out of sample.** It means "let winners run": stop, time stop and trend break only. Caveat: survivorship bias.
   - **APPLIED 10:06 ET with the user's OK.** `.env` was backed up first. The settings were confirmed loaded via `load_settings()`.
     - **Correction:** variant d has **no** trend-break exit, because live `.env` has `EXIT_ON_TREND_BREAK=false` and the backtester reads the same setting. Live matches the backtest.
     - Exits are now the 2-ATR stop, the 20-day time stop and the ML-drop exit (inactive: there is no promoted model).
     - Already-open positions keep their existing broker take-profit legs.
     - The first deterministic run after the restart generated 0 proposals, and the startup reconcile was clean.
   - **Live `.env` mapping for d:**
     - `PROFIT_GIVEBACK_PCT=0`
     - `BRACKET_TAKE_PROFIT=false`
     - `PROFIT_SCALE_OUT=false`
     - `PROFIT_BREAKEVEN_R=0`
     - `PROFIT_RSI_EXHAUSTION=0`
     - Unchanged: `SL_ATR_MULT=2.0` and `EXIT_MAX_HOLD_DAYS=20` (default).
4b. **Older checklist for 4a (superseded):**
   - Files: `app/backtest/*`, `scripts/swing_backtest.py` and `tests/test_swing_strategy.py`.
   - Output: `data/backtests/swing_2026-11-24_v2/`.
   - Covers:
     - why the trade count depends on costs;
     - `BACKTEST_LIVE_EXITS` fidelity;
     - realistic Webull costs (no commission);
     - variants with give-back off, no take-profit + trailing stop, a plain 2-ATR stop + 20-day time exit, and a 3-day re-entry cooldown.
   (original 4a items:)
   - confirm `BACKTEST_LIVE_EXITS` fidelity;
   - set realistic Webull costs;
   - re-run the variants with give-back off;
   - write `docs/BACKTEST_SWING_2026-11-24.md`;
   - show the user before any `.env` change.
4c. **ML improvement programme** (user's choice, 10:25 ET: "Harness + intraday timing"):
   - **Intraday data:** Webull bars support M1/M5/M15/M30/M60/M120/M240 (no M10; build it from M5). Yahoo intraday is only 60 days (5m/15m) or about 2 years (1h).
     - **Agent I: stopped by the user (~10:50 ET) before it reported.**
       - Its fetch process kept running and was causing `get_stock_bars` 429s on the shared account. The orchestrator stopped it (PIDs 13564/9220).
       - **Findings so far:** Webull M15 depth is about **6 months** (2026-05-28 → today, 3,244 bars/symbol, regular session).
       - **Saved:** 13 of 87 symbols in `data/bars_intraday/M15/`. The rest is unfetched. The script `scripts/fetch_intraday_bars.py` exists and is meant to be resumable (skips completed symbols).
       - **M5 depth:** not established.
       - **To resume:** `.venv\Scripts\python.exe scriptsetch_intraday_bars.py --timespan M15 --days 180`. Ideally run it outside market hours to avoid starving the live app.
     - **Original plan:** probes the M5/M15 depth and paging, then fetches M15 for the universe (and M5 for the top 20 if depth ≥ 60 days).
     - Files: `scripts/fetch_intraday_bars.py`. Output: `data/bars_intraday/<timespan>/`.
     - Rules: market data only, paced at ≥ 1.5 s per call (the live app shares the account), no auth flows.
   - **Experiment harness:**
     - **Agent H (Sonnet low):** started 10:27 ET. The user stopped it about 10:30 ET and resumed it from its transcript; it is running again. No files existed at the stop.
     - **Agent H DONE (~10:45 ET).** 7 tests pass. A full run takes about 120 s for 10 variants.
       - Entry families are added by swapping `engine.signal_at` (no backtester edits).
       - Research runs load bars truncated before the holdout cutoff.
       - The ML filter is an MLP trained per fold on rows that resolved before the test fold.
     - **Research-set leaderboard** (holdout NOT run yet; penalized = mean R − SE·sqrt(ln 10)):

       | # | variant | trades | expR | penalized | folds |
       |---|---|---|---|---|---|
       | 1 | consensus_time30 (d + 30-day time stop) | 2849 | +0.201 | +0.151 | +++-+ |
       | 2 | **consensus_ml_q80** (d + ML top-quintile filter) | 1772 | +0.170 | +0.112 | **+++++** |
       | 3 | consensus_trail3 | 4028 | +0.139 | +0.103 | +++-+ |
       | 4 | consensus_sl2.5 | 3637 | +0.135 | +0.101 | +++-+ |
       | 5 | **d_champion** | 3811 | +0.137 | +0.101 | +++-+ |
       | 6 | pullback_d | 2571 | +0.143 | +0.099 | +++-+ |
       | 7 | momentum_d | 1547 | +0.157 | +0.097 | +++-+ |
       | 8 | breakout20_vol_ml_q80 | 248 | +0.198 | +0.037 | -++-+ |
       | 9 | meanrev_d | 75 | +0.146 | −0.068 | +++-- |
       | 10 | breakout20_vol_d | 731 | +0.004 | −0.097 | -++-- |

       - Only `consensus_ml_q80` is promotable on the research folds: positive in all 5, max DD 10.7%, but its edge over the champion is small (+0.011).
       - `consensus_time30` has the best score but fails fold 4.
       - The champion's fold 4 is also negative (2024-25 period).
       - Caveats: the SE is optimistic (correlated trades), and the universe has survivorship bias.
     - **Next (needs the user):** spend the one-shot holdout on a short list (`consensus_ml_q80`, `consensus_time30` + the champion), then add the 15-min entry-timing variants once agent I's data is in.
     - **Design:** Files: `app/research/` (variants, harness, leaderboard), `scripts/experiments.py`, `tests/test_research_harness.py`. Output: `data/experiments/leaderboard.csv`.
     - **Variants:** a library of entry families (rules, pullback, breakout+volume, momentum, mean-reversion) × exit styles × an optional `ml_candles` MLP filter. Variant d is the champion.
     - **Testing:** walk-forward with embargo. **The last 365 days is a locked holdout** (needs `--holdout`; every peek is logged). The score is penalized for the number of variants tried.
     - **Promotion:** a challenger qualifies only if it beats the champion on the penalized score, is positive in every fold, and beats it on the holdout. The rule is reported, never auto-applied; the user approves.
   - **Then:** use 15-min candles to time daily-setup entries (an intraday trigger plus a tighter stop) as harness variants.
5. **Swing steps 3-5,** with backtests each time (pyramiding is now best done as a harness variant).
6. **Investigate AMZN 2 shares.**
7. **DONE (10:20 ET):** `strategy.md` was updated (backup: `data/backups/strategy.md.pre_2026-11-24`).
   - Goal: aggressive swing; let winners run.
   - Variant d exits.
   - Soft stop, re-anchoring, MARKET exits / marketable entries / chase.
   - Stale-price guard, event veto, news gate.
   - Enforcement table rows.
8. **Open question for the user:** the live account size and the risk % per trade. Until answered, the risk budget is `MAX_RISK_PER_TRADE_USD=200`.

---

## 8. Decisions log (this programme)
- **Aggressive swing trading is the priority.** DCA was rejected; pyramiding into winners replaces it.
- **XLY 20 shares is kept** as one full-size position.
- **The user declined to pause entries** while the stale-price fix was pending.
- **Market orders:** true MARKET is for exits only, in regular hours. Entries are marketable limits.
- **CORE GTC protective stops plus an app-side soft stop** cover extended hours.
- **The completed QA programme was moved out of this file** (at the user's request).
- **ADI (08:50 ET): let the 348.45 stop sell it at the 09:30 open** (user's choice; ~$3-8 loss on 1 share). No rushed restart with unverified code; restart after the §3 verification.
