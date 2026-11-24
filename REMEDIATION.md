# Remediation status: QA_REPORT.md findings

**Date:** 2026-11-24 · **Programme:** waves 1A, 1B, 1C, 2A-1, 2A-2, 2B and 3 (see `HANDOFF.md` for the history and `docs/ARCHITECTURE_CONTRACTS.md` for the contracts).

Status legend: **Fixed** = code changed and covered by tests (or, for the frontend, `tsc` + `eslint` + `next build` + a mock-broker run) · **Mitigated** = the risk is reduced, but a limitation remains (stated) · **Open** = not done.

## High

| ID | Status | What changed | Evidence |
|---|---|---|---|
| H1 | Fixed | `OptionExitManager` (`app/engine/option_exits.py`): premium stop −50%, take profit +50%, DTE ≤ 2, expiry-day close at 15:45 ET. It sends SELL_TO_CLOSE immediately, in approve mode and under the kill switch, with one exit in flight per contract. It is driven by the `RiskLoop` (every `RISK_LOOP_SECONDS`) and armed by `startup_reconcile`. | `tests/test_engine_runtime.py` (exit triggers, frozen clock, kill/approve, one-in-flight) |
| H2 | Fixed | The option gate fails closed: no snapshot, missing `as_of`, missing or zero bid/ask, a crossed or locked market, the spread, and missing IV, delta, volume or OI each reject. | `test_engine_runtime.py::test_gate_fails_closed` (12 cases) |
| H3 | Fixed | True IV rank and percentile over `atm_iv_series(…, 252)`, recorded daily per underlying. Below `OPTIONS_IV_RANK_MIN_HISTORY` it falls back to raw IV, labelled "IV (raw, insufficient history N/60)". | `test_iv_*` |
| H4 | Fixed | The log redaction filter covers the Telegram token, api_key, Bearer and secrets; httpx/httpcore are silenced. | `test_logging_redaction.py` |
| H5 | Fixed | The daily-loss guard fails closed when day P&L is unavailable. | `test_pnl_guard.py`, `test_strategy_rules.py` |
| H6 | Fixed | The dead `app/agent.py` was deleted. Max 1 add and no-add-to-loser are on; the pullback gate is opt-in (`ENTRY_PULLBACK_GATE`). The Settings text is labelled "Reviewer notes (does not change trading)". | `test_strategy_rules.py` |
| H7 | Fixed | Backtest ML is on by default (`BACKTEST_ML_ENABLED=true`, walk-forward). `run_backtest_manual.py --no-ml` and `--compare` exist, and the report lists what a daily backtest cannot reproduce. | `test_parity_backtest.py`; `--compare` run 2026-11-24 |
| H8 | Fixed | `web/src/app/error.tsx`, `global-error.tsx`, and a per-panel `<ErrorBoundary>` around every panel. The TopBar (kill/flatten) sits outside every boundary. | `next build` |
| H9 | Fixed | `tests/test_ml_provenance.py` uses `pytest.importorskip("torch")`. | the suite collects without torch |
| H10 | Fixed | The API control plane and the feed have tests: auth, routes, stream, lifespan and feed bus. | `test_api_*.py`, `test_feed_bus.py` |

## Medium

| ID | Status | What changed | Evidence |
|---|---|---|---|
| M1 | Fixed | The kill switch is re-checked in `execute()` for every asset type before any broker call. It blocks opening orders only; exits always pass. | `test_kill_blocks_opening_order_at_execution_before_the_broker`; mock E2E |
| M2 | Fixed | The broker's last-line `OrderGuard` checks the kill switch, notional/qty caps (options ×100) and the whitelist on opening orders. Flatten uses `flatten_mode()`. | `test_broker_guard.py` |
| M3 | Fixed | `_proposal_lock` serialises `_on_proposal` (slow cycle + fast loop). The DB partial unique index `ux_proposals_open` raises `DuplicateProposalError`, which is handled everywhere (flatten settles the blocking row and is never blocked). The fast loop re-checks the kill switch after its awaits (TOCTOU). | `test_on_proposal_is_serialised`, `test_fastloop_rechecks_kill_after_awaits`, `test_db_core.py` |
| M4 | Fixed | Cold-path workers (signals, research, maintenance) feed a TTL `SignalStore`; `run_cycle` holds `_run_lock` only for proposal generation. The fast loop reads the store and fails closed when it is stale. Daily `db.prune`. | `test_signal_store_ttl`, `test_fastloop_reads_signal_store_fail_closed`, `test_maintenance_prunes_once_per_day` |
| M5 | Fixed | SQLite: WAL, busy_timeout, an RLock per statement, numbered migrations, `DB.close()`. | `test_db_core.py` |
| M6 | Fixed | yfinance timeouts, a negative cache and a circuit breaker. | `test_yf_service.py` |
| M7 | Fixed | Quote/IV/Greeks age check (`OPTIONS_QUOTE_MAX_AGE_SECONDS`). | `test_gate_stale_cutoff_follows_setting` |
| M8 | Mitigated | Earnings check fails closed live (open in mock mode). The backtest cannot reproduce sentiment, the first 15 minutes or earnings; every report now says so. | `test_eligibility.py`, backtest parity notes |
| M9 | Fixed | Lesson auto-apply records `applied`/`skipped` with a reason, and run params are saved per run. **Also fixed:** auto-apply could never apply anything, because `setattr` on the frozen `Settings` raised; it now swaps in a new instance via `dataclasses.replace`. | `test_lessons_apply_and_skip_with_reasons`, `test_resume_fixes.py` |
| M10 | Fixed | LearningView: approve (with `approved_by`), reject, activate, retire and rollback (with `rolled_back_by`) call the API through confirm dialogs, and show apply-state badges. | `tsc`/`eslint`/build |
| M11 | Fixed | Every approval goes through an always-on review sheet (`ApproveDrawer`): size in shares or contracts ×100, notional, max loss, SL/TP, buying-power impact, re-quote deltas and environment. It is disabled when data is stale, the broker is offline, a duplicate exists or the kill switch blocks the order. A live account also needs the symbol typed in. | build; mock E2E |
| M12 | Fixed | Every failed request counts toward the banner, the portfolio included (labelled "broker offline" on 409). | `useDashboardData.ts` |
| M13 | Fixed | `proposalStatus.ts` maps every backend status (`PROPOSAL_STATUSES`, including `orphan`). | — |
| M14 | Fixed | Data comes from the SSE mux (`/api/stream`), with tiered polling as a fallback (10 s while the stream is down, else a 60 s resync; slow tier 30 s). Every setState is structural-equality guarded, and the `/api/ml` fetch is deduplicated. | — |
| M15 | Fixed | One `<Dialog>` primitive (focus trap, Escape, focus return, Cancel focused on danger dialogs) replaces ConfirmModal internals, DecisionModal and the three div overlays. | — |
| M16 | Fixed | A large red KILL button (Shift+K), separate from Flatten, in a sticky header. Release needs RELEASE typed. | — |
| M17–M20 | Fixed | `trader.py stop`, Windows process scan, venv auto-detect, package excludes. | `test_ops_*.py` |
| M21 | Fixed | Per-launch API token (`X-Trader-Token`), injected as a meta tag. The dashboard sends it on every mutating call (with a dev-mode fallback and one retry after a backend restart). | `test_api_auth.py`; mock E2E (403 without the token) |
| M22 | Fixed | Headline sanitiser, LLM JSON validation and clamps; LLM lessons go to `lessons.proposed.md` only. | `test_llm_sentiment.py`, `test_learning_review.py` |
| T1 | Fixed | DBs are closed in fixtures. **Also:** the whole test session now runs against temp `data/` and `logs/` dirs (`TRADER_DATA_DIR`, new `TRADER_LOG_DIR`), so tests no longer write `data/alert.flag`, `data/models` or `logs/mcp_server.log`. | `tests/conftest.py` |

## Low

| ID | Status | Notes |
|---|---|---|
| L1–L3 | Fixed | Negative cache excludes mutating tools; LLM deadlines; `broker.mode_warning` shown in the banner rail. |
| L4–L9 | Fixed | Indexes, migrations, backup checks, portable paths, pinned deps, generated `.env.example` (`--check`). |
| L10 | Fixed | OptionsStatus auto-refreshes every 30 s (visible tab only). |
| L11 | Fixed | `/api/trades` returns one row shape for every filter. |
| L12 | Fixed | A `<Countdown>` leaf ticks; the proposal list re-renders only on data changes. |
| L13 | Fixed | Global CSS is now just `styles/tokens.css` and `styles/base.css` (shared primitives: `.panel`, `.button`, status chips…). 152 component-specific classes moved into 20 `components/*.module.css` files, and everything is formatted by Prettier (no minified blocks). 12 dead classes were removed. A computed-style comparison of 1,098 elements before and after shows no visual change; two cascade-order differences found on the way were fixed. Nothing is below 11 px, `--faint` is at least 4.98:1, and coloured P&L gets ▲/▼ (zero is neutral). |
| L14 | Fixed | TA v1 maps to v2 with a one-time warning. |
| L15 | Fixed | `Start_Trader.bat` polls `/api/status`. |
| L16 | Fixed | ESLint 10 flat config (`eslint-config-next` 16.4.6) and `npm run lint`; clean. Three bundled plugins (`react`, `import`, `jsx-a11y`) do not yet list ESLint 10 in their peer ranges, so npm prints a peer warning. Their rules were verified to fire under 10. |

## Other defects found and fixed during the programme

- `DB.add_review()` dropped `trigger`/`status` (NOT NULL), so every learning review failed to save.
- `/api/ml/{id}/scorecard` only looked at the newest model and did not decode `metrics`.
- The SSE publisher and the DB both published `proposals` (a duplicate event per change). The hub now drops a re-publish of an identical snapshot.
- `OptionRiskManager` measured DTE against the host's local date; it now uses the ET date.
- The status endpoint and the fast loop read the boot-time settings, so applied lessons never showed there; both now read `engine.s`.
- Undefined CSS variables (`--color-proposed`, `--color-active`, …) left lesson and autopsy badges without a background.

## Known limitations (deliberate or out of scope)

- `SdkTransport` (Webull SDK execution) has not been validated against a live account. Keep `BROKER_EXECUTION=mcp` until it is tested on UAT.
- Options stay **off** by default (`OPTIONS_ENABLED=false`). New options strategies (debit spreads, covered calls) are the next phase.
- A real-browser click-through in Chrome on the mock broker passed. It covered approve via the review sheet → filled; Shift+K kill (Cancel focused); release (blocked until RELEASE is typed); auto/approve; resume/pause; Flatten (FLATTEN typed); lesson approval with a name; and the decision/lesson/autopsy drawers, Escape and focus return. There were no console errors.
- Found and fixed during that pass: Resume engine sent *pause* (an inverted flag in the header); stale chart limit lines (overlays now refresh every 30 s); ▲ shown on a $0.00 P&L.
- **Not done (needs your credentials):** validating `SdkTransport` on Webull UAT. There is no `.env` or token in this workspace. Run `scripts/uat_order_test.py` as described in HANDOFF.md.
