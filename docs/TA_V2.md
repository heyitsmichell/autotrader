# TA scoring (v2 only)

The v2 score (grouped, volatility-normalised; see the docstring of `_score_v2` in `app/ta.py`) is
the only scoring model. The original six-rule v1 scorer was retired (QA L14).

`TA_MODEL=v1` in a saved `.env` still works: it is accepted and mapped to v2, with a one-time
WARNING in the log. Nothing breaks, but the score scale differs from v1 (v2 scores are more
compressed, with a smaller spread). If you used to run v1:

1. Run `./.venv/bin/python scripts/ta_evaluate.py` to look at the rank IC, the mean forward return
   by score quintile and the results above `CONSENSUS_TA_MIN` on your cached bars
   (`data/bars`, filled by the backtest).
2. Re-check `CONSENSUS_TA_MIN` against the quintile output instead of assuming 0.15 still fits.
3. Retrain the ML model; the TA score is one of its inputs.
4. Delete `TA_MODEL` from your `.env` to silence the warning.

Also: the Signals table says "N of M rules active" (rules with non-zero points).
