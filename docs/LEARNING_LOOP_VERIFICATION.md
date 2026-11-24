# Learning Loop: what it does and what it can change

This document states, as of the QA fix programme (findings M9, M22, H6), what each part of the
learning loop is allowed to influence. The short version: **nothing the loop produces can change
a live setting or an order without a human approving it, and risk limits are never touchable.**

## Architecture

```
Learner background loop (every 3600 s, app/learning.py:_loop)
  1. evaluate_outcomes()    score proposals and signal snapshots 1 and 5 trading days later
                            (ret_1d, ret_5d, spy_5d). Forward-only, no AI.
  2. _review_due() -> run_review("auto")
                            one LLM call -> VALIDATED DRAFT (see below). Never lessons.md.
  3. _retrain_due() -> ml.train()
                            walk-forward validated model, challenger/champion promotion.
Nightly job (run_nightly_job): outcomes, review if due, apply active lessons, retrain if due,
                            propose parameter lessons from autopsies.
```

## Who can change what

| Source | Can it change settings or orders? | Gate |
|---|---|---|
| LLM review text (`lessons`, `strategy_suggestions`) | **No.** Stored in the `reviews` table and written to `lessons.proposed.md`, a draft file nothing reads. It is never written to `lessons.md`. | Human copies text over, if wanted. |
| LLM `parameter_suggestions` (optional) | **No, not on its own.** Each item is validated against the learnable-parameter registry and its bounds (`app/lessons.py`, risk limits refused). Valid items are stored as lessons with status `proposed`, `created_by="review"`, `sample_size=0`, `p_value=None`. | A human must approve. Even when active, the engine skips a lesson with `sample_size < 30` or `p_value` missing or `>= 0.05`, so an LLM suggestion cannot auto-apply. |
| Autopsy -> `propose_lessons_from_autopsies` | Proposals only (`status='proposed'`). Chronological train/val/test split plus a Mann-Whitney test, minimum 30 closed trades. | Human approval, then the engine's auto-apply guardrails (n >= 30, p < 0.05, bounds). |
| Autopsy -> sizing, gates, exits | **No.** `app.autopsy.influence()` states this in code. Wiring it into sizing would need its own guard, so it is not wired. | n/a |
| ML model | Yes, as a gate (`ml_p`), only after promotion rules pass (below). | Walk-forward guardrails. |
| Active lessons (human approved) | Yes, 7 whitelisted settings only. | Engine `_apply_active_lessons`: n >= 30, p < 0.05, bounds. Risk limits are on a deny list. |

`strategy.md` and `lessons.md` are human notes. Since the LLM analyst (`app/agent.py`) was removed,
**no code reads them to make a decision.** "Save strategy" in Settings does not change trading.

## LLM review output validation (`learning.validate_review`)

- Required keys and types: `lessons` (str), `strategy_suggestions` (str), `evidence_strength`
  (`weak` | `moderate` | `strong`). A missing or mistyped key rejects the whole review (stored as an
  error review, logged, nothing written).
- Text is stripped of control characters and capped (lessons 3000 chars and 8 lines,
  suggestions 2000 chars). Trimming is logged.
- `parameter_suggestions` items: `param_name` must be a learnable setting, `value` a finite
  number inside the bounds of `lessons.LEARNABLE_PARAMS`. Anything else is skipped with a logged
  reason (`Learning review item skipped/trimmed: ...`).
- Prompt inputs from third parties (proposal rationales, which can quote headlines) go through
  `sentiment.sanitize_headline`. `strategy.md` and `lessons.md` are the owner's own text.

## ML retraining (`ml.train`)

Walk-forward validation guardrails (all must pass):

| Check | Threshold | Purpose |
|-------|-----------|---------|
| `auc` | mean_auc >= 0.52 | Model has predictive power |
| `hit_vs_rules` | hit rate above rules-only | Model improves hit rate |
| `return_vs_rules` | average return above rules-only | Model improves returns |
| `sample` | n_selected >= 30 | Statistical significance |
| `no_leakage` | mean_auc <= 0.70 | Detects data leakage |
| `no_saturation` | under 80% of predictions at the extremes | Detects overconfidence |
| `min_symbols` | at least 3 distinct symbols | Prevents symbol-specific overfit |

Also: training is blocked in mock mode (synthetic data), the checkpoint carries provenance
(broker mode, environment, account) and is refused in a different mode, and a changed feature list
rejects the checkpoint.

## Backtest parity (H7)

The backtest defaults to the live decision rule: `BACKTEST_ML_ENABLED=true` with a **walk-forward
retrained** model (`ml.make_walkforward_predictor`): each prediction uses a model fit only on rows
dated before the start of its 126-day window minus 30 days (more than the 20-bar label horizon).
The promoted checkpoint is not used in a backtest on purpose: it was trained on data that includes
the test period. `--no-ml` gives the rules-only variant and `--compare` prints both. Without
PyTorch the ML variant degrades to rules-only and says so.

## What the backtest cannot reproduce

Printed in every backtest report: sentiment is always neutral, the first-15-minutes rule and the
earnings blackout are not simulated, and the live add rules (max adds, never add to a loser) never
trigger because the backtester holds one position per symbol.

## Dashboard endpoints

```
GET  /api/learning/stats     learner.stats() (scorecard)
GET  /api/learning/analytics learner.analytics() (calibration bins)
POST /api/learning/evaluate  learner.evaluate_outcomes()
POST /api/learning/review    learner.run_review("manual")
```

## Tests

`tests/test_learning_review.py` (schema validation, proposed status, draft file, sanitised
prompt), `tests/test_ml_provenance.py`, `tests/test_spy_benchmark.py`, `tests/test_lessons.py`,
`tests/test_parity_backtest.py`.
