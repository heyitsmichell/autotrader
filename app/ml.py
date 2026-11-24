"""PyTorch trade filter, trained on daily bars and promoted only if it helps.

What it predicts (triple-barrier): the probability that a trade hits its take-profit
before its stop-loss, within the maximum hold time. Labels come from a
triple-barrier simulation over every symbol's daily bars (TP hit = 1, SL hit or time
expiry = 0; returns are net of the backtester's round-trip costs), with real ledger outcomes overriding the simulated label where they exist.

How it is judged (walk-forward, no peeking):
  - Data is split by date into 4 folds. Each fold trains only on earlier dates,
    leaves a gap (max hold time, so no label overlaps the test), and
    tests on the next block of dates.
  - In the consensus gate the model filters setups the rules already like, so the
    test that matters is: among rows where the TA score passes, do the rows the
    model also approves do better than all rule-approved rows?
  - Promotion needs: mean AUC >= 0.52, a higher hit rate AND a higher average
    return than the rules alone, on at least 30 approved test rows.
    Otherwise the model is saved as "rejected" and the gate keeps using rules only.

Unlike the LLM analyst, a numeric model has not memorized price history, so
this historical test is meaningful. It still can't promise future results.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import time

from . import indicators, ta
from .backtest.walkforward import WalkForwardConfig, WalkForwardResult, walk_forward as wf_walk_forward, create_ml_walkforward_config
from .backtest.costs import CostModel, round_trip_cost_pct
from .broker import Broker, BrokerError
from .config import MODEL_DIR, Settings
from .db import DB, decode
from .universe import get_universe, build_universe_config

log = logging.getLogger(__name__)

try:
    import torch
    from torch import nn
    TORCH_ERROR: str | None = None
except Exception as e:  # noqa: BLE001 - torch is optional; the gate runs rules-only without it
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    TORCH_ERROR = f"PyTorch is not installed ({type(e).__name__}: {e})"

FEATURES = ["pct_vs_sma20", "pct_vs_sma50", "sma20_slope_pct", "rsi14", "adx14", "atr_pct",
            "atr_pct_rank", "rv20", "pct_from_high10", "chg_5d_pct", "chg_20d_pct",
            "days_below_sma20", "vol_vs_20d_avg"]
HORIZON = 5  # Kept for backward compatibility with feature computation (5-day forward returns)
# For bar-based triple-barrier simulation, use longer max hold time to match real GTC bracket orders
# 5 days is too short; many trades take 2-4 weeks to hit SL/TP
SIM_MAX_HOLD_BARS = 20
HIDDEN = (32, 16)
MIN_AUC = 0.52              # Fallback default if settings.ml_min_auc not available
MIN_SELECTED = 30
MAX_AUC = 0.70              # Implausibly high AUC suggests leakage
SATURATION_THRESHOLD = 0.80  # >80% predictions saturated (<0.05 or >0.95)
MIN_SYMBOLS = 3             # Minimum distinct symbols in test set

# Triple-barrier constants
TRIPLE_BARRIER_LABELS = {"tp": 1, "sl": 0, "time": 0}  # time expiry = label 0 (barrier not hit), not dropped


def vector(f: dict) -> list[float] | None:
    vals = [f.get(k) for k in FEATURES]
    if any(v is None for v in vals):
        return None
    return [float(v) for v in vals]


def build_dataset(bars_by_symbol: dict[str, list[dict]], db: DB | None = None,
                   settings: Settings | None = None) -> list[dict]:
    """Build the training dataset using triple-barrier labels.

    Every symbol and day in the universe is labelled by simulating the take-profit / stop-loss
    race on the daily bars, so the model learns from all setups and not only the ones the
    rules happened to approve. If a db is given, real closed-trade outcomes (TP/SL) override
    the simulated label for the same symbol and entry day, which is how it learns from
    its own bad trades.
    """
    rows = []

    # Real closed trades from the ledger. These used to replace the dataset once there were
    # 400 of them, but they only cover setups the rules already approved (selection bias).
    # They are now merged on top of the universe-wide simulation (see the end of this function).
    ledger_rows: list[dict] = []
    if db is not None:
        for trade in db.closed_trades(limit=5000):
            exit_reason = trade.get("exit_reason", "")
            if exit_reason not in ("stop", "target"):
                continue

            entry_features = trade.get("entry_features")
            if not entry_features:
                continue

            try:
                features = json.loads(entry_features) if isinstance(entry_features, str) else entry_features
            except (json.JSONDecodeError, TypeError):
                continue

            x = vector(features)
            entry_day = str(trade.get("entry_fill_time") or "")[:10]  # same YYYY-MM-DD form as bar rows
            if x is None or not entry_day:
                continue

            ledger_rows.append({
                "date": entry_day,
                "symbol": trade["symbol"],
                "x": x,
                "y": 1.0 if exit_reason == "target" else 0.0,
                "fwd": trade.get("realized_pnl_pct") or 0,
                "ta": trade.get("entry_ta_score") or 0,
                "exit_reason": exit_reason,
                "hold_hours": (trade.get("hold_seconds") or 0) / 3600.0,
                "source": "ledger",
            })

    # Universe-wide triple-barrier simulation on the daily bars (the base of the dataset).
    # Uses ATR-based stop/target from settings (or defaults) and simulates barrier hits
    if settings is None:
        # Import here to avoid circular dependency
        from .config import load_settings
        settings = load_settings()

    max_hold_bars = SIM_MAX_HOLD_BARS  # Maximum bars to hold (time barrier)
    cost_model = CostModel.from_settings(settings)  # same costs as the backtester

    for sym, bars in bars_by_symbol.items():
        if len(bars) < 30:  # Need enough bars for ATR
            continue

        feats = indicators.feature_series(bars)
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        for i in range(len(bars) - max_hold_bars):
            f = feats[i]
            if f.get("atr14") is None or f.get("close") is None:
                continue

            x = vector(f)
            if x is None:
                continue

            entry = f["close"]
            atr = f["atr14"]

            # Compute stop loss and take profit using same logic as engine
            dist = settings.sl_atr_mult * atr
            dist = max(entry * settings.min_sl_pct / 100, min(entry * settings.max_sl_pct / 100, dist))
            reward = dist * (settings.tp_atr_mult / settings.sl_atr_mult)
            sl = entry - dist
            tp = entry + reward

            # Simulate intraday path to find which barrier hits first
            label = -1  # time expiry (dropped)
            fwd_ret = 0.0
            j = max_hold_bars

            for j in range(1, max_hold_bars + 1):
                if i + j >= len(bars):
                    break
                bar_high = highs[i + j]
                bar_low = lows[i + j]

                if bar_low <= sl:
                    label = 0.0  # SL hit
                    fwd_ret = (sl / entry - 1) * 100
                    break
                if bar_high >= tp:
                    label = 1.0  # TP hit
                    fwd_ret = (tp / entry - 1) * 100
                    break

            # Time expiry: no barrier hit, label as 0 (barrier not hit) - keep for training
            if label == -1:
                label = 0.0  # time expiry = 0
                fwd_ret = (closes[i + j] / entry - 1) * 100 if i + j < len(bars) else 0.0

            # Net of costs: simulated fills are exact, so charge the round-trip slippage and
            # commission here. A take-profit that does not clear the cost counts as a miss.
            fwd_ret -= round_trip_cost_pct(cost_model, entry, atr=atr, notional=settings.max_order_usd,
                                           dollar_volume=(closes[i] * (bars[i].get("volume") or 0)))
            if label == 1.0 and fwd_ret <= 0:
                label = 0.0

            ta_score = ta.score(f)[0]

            rows.append({
                "date": f["date"],
                "symbol": sym,
                "x": x,
                "y": label,
                "fwd": fwd_ret,
                "ta": ta_score,
                "exit_reason": "target" if label == 1.0 else "stop",
                "hold_hours": j * 24.0,  # trading days held, as hours (approximate)
                "source": "bars",
            })

    # Real outcomes beat simulated ones: a ledger trade replaces the simulated row for the same
    # symbol and entry day; ledger trades with no matching simulated row are added as they are.
    if ledger_rows:
        real = {(r["symbol"], r["date"]): r for r in ledger_rows}
        rows = [r for r in rows if (r["symbol"], r["date"]) not in real]
        rows.extend(real.values())

    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    return rows


def _net(d: int):
    return nn.Sequential(nn.Linear(d, HIDDEN[0]), nn.ReLU(), nn.Dropout(0.2),
                         nn.Linear(HIDDEN[0], HIDDEN[1]), nn.ReLU(), nn.Linear(HIDDEN[1], 1))


def _fit(rows: list[dict], seed: int = 7, epochs: int = 300, patience: int = 20):
    torch.manual_seed(seed)
    x = torch.tensor([r["x"] for r in rows], dtype=torch.float32)
    y = torch.tensor([[r["y"]] for r in rows], dtype=torch.float32)
    mean, std = x.mean(0), x.std(0).clamp_min(1e-6)
    xn = (x - mean) / std
    cut = max(1, int(len(rows) * 0.85))            # last 15% (by date) for early stopping
    model = _net(len(FEATURES))
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-3)
    loss_fn = nn.BCEWithLogitsLoss()
    best, best_state, bad = math.inf, copy.deepcopy(model.state_dict()), 0
    for _ in range(epochs):
        model.train()
        perm = torch.randperm(cut)
        for b in range(0, cut, 256):
            idx = perm[b:b + 256]
            opt.zero_grad()
            loss_fn(model(xn[idx]), y[idx]).backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            val = loss_fn(model(xn[cut:]), y[cut:]).item() if cut < len(rows) else 0.0
        if val < best - 1e-4:
            best, best_state, bad = val, copy.deepcopy(model.state_dict()), 0
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    return model, mean, std


def _predict(model, mean, std, xs: list[list[float]]) -> list[float]:
    with torch.no_grad():
        x = (torch.tensor(xs, dtype=torch.float32) - mean) / std
        return torch.sigmoid(model(x)).squeeze(1).tolist()


class MLUnavailable(RuntimeError):
    """PyTorch (or enough data) is missing, so a model cannot be built."""


def make_walkforward_predictor(settings: Settings, bars_by_symbol: dict[str, list[dict]], *,
                               refit_days: int = 126, purge_days: int = 30,
                               min_rows: int = 300):
    """Point-in-time ML predictor for the backtester (QA H7 live parity).

    Same features, architecture and training recipe as the live MLFilter (``build_dataset`` +
    ``_fit``), but it never sees the future: a prediction for day D uses a model that was fit
    only on rows dated before ``bucket_start - purge_days``, where ``bucket_start`` is the start
    of D's ``refit_days`` window. ``purge_days`` (30 calendar days) exceeds the 20-bar triple
    barrier horizon, so no label overlaps the prediction day. Models are fit lazily and cached
    per window. A promoted checkpoint is not used here on purpose: it was trained on data that
    includes the backtest period, which would leak. Raises MLUnavailable without torch.
    Returns ``predictor(features: dict) -> float | None`` (None = no model for that window yet).
    """
    if torch is None:
        raise MLUnavailable(TORCH_ERROR or "PyTorch is not installed")
    from datetime import date, timedelta

    rows = build_dataset(bars_by_symbol, None, settings)
    if len(rows) < min_rows:
        raise MLUnavailable(f"only {len(rows)} labelled rows in the history (need {min_rows})")
    cache: dict[int, tuple | None] = {}

    def predictor(f: dict) -> float | None:
        day_s = f.get("date")
        x = vector(f)
        if not day_s or x is None:
            return None
        try:
            bucket = date.fromisoformat(str(day_s)[:10]).toordinal() // refit_days
        except ValueError:
            return None
        if bucket not in cache:
            cutoff = (date.fromordinal(bucket * refit_days) - timedelta(days=purge_days)).isoformat()
            train = [r for r in rows if r["date"] < cutoff]
            cache[bucket] = _fit(train) if len(train) >= min_rows else None
        model = cache[bucket]
        if model is None:
            return None
        return round(_predict(model[0], model[1], model[2], [x])[0], 4)

    predictor.refit_days = refit_days            # type: ignore[attr-defined]
    predictor.n_rows = len(rows)                 # type: ignore[attr-defined]
    return predictor


def _auc(scores: list[float], labels: list[float]) -> float | None:
    """Probability a random positive outranks a random negative (ties count half)."""
    pairs = sorted(zip(scores, labels))
    n_pos = sum(1 for _, y in pairs if y > 0.5)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2
        rank_sum += avg_rank * sum(1 for k in range(i, j) if pairs[k][1] > 0.5)
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def _group(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "hit": None, "avg_fwd": None}
    return {"n": len(rows), "hit": round(sum(r["y"] for r in rows) / len(rows), 4),
            "avg_fwd": round(sum(r["fwd"] for r in rows) / len(rows), 4)}


# Backward compatibility wrapper - delegates to shared walkforward module
def walk_forward(rows: list[dict], ta_min: float, p_min: float, n_folds: int = 4, min_auc: float = 0.51) -> dict:
    """Legacy wrapper - use app.backtest.walkforward.walk_forward for new code."""
    config = WalkForwardConfig(n_folds=n_folds, embargo_bars=SIM_MAX_HOLD_BARS, purge_bars=SIM_MAX_HOLD_BARS)

    def model_factory(train_rows):
        model, mean, std = _fit(train_rows)
        return (model, mean, std)

    def predict_fn(model_tuple, test_rows):
        model, mean, std = model_tuple
        return _predict(model, mean, std, [r["x"] for r in test_rows])

    result: WalkForwardResult = wf_walk_forward(rows, config, ta_min, p_min,
                                                 model_factory=model_factory, predict_fn=predict_fn)

    # Convert to legacy format
    return {
        "folds": [
            {"train": f.n_train, "test": f.n_test,
             "from": str(f.train_start), "to": str(f.train_end),
             "auc": f.auc, "all": f.metrics["all"], "rules": f.metrics["rules"],
             "rules_model": f.metrics["rules_model"]}
            for f in result.folds
        ],
        "mean_auc": result.mean_auc,
        "pooled": result.details.get("pooled_rules_model", {}),
        "checks": result.details.get("checks", {}),
        "passed": result.passed,
        "rejection_note": result.rejection_reason,
        "thresholds": {"ta_min": ta_min, "p_min": p_min, "min_auc": min_auc,
                       "max_auc": MAX_AUC, "min_selected": MIN_SELECTED,
                       "saturation_threshold": SATURATION_THRESHOLD,
                       "min_symbols": MIN_SYMBOLS}
    }


class MLFilter:
    def __init__(self, settings: Settings, db: DB) -> None:
        self.s, self.db = settings, db
        self.model = None
        self.mean = self.std = None
        self.model_id: int | None = None
        self.training = False
        self.last_error: str | None = None
        self._load_champion()

    @property
    def available(self) -> bool:
        return torch is not None

    def _load_champion(self) -> None:
        """Load the champion model for predictions. Falls back to 'active' for backward compatibility."""
        row = self.db.champion_model()
        if row is None:
            # Fallback to legacy 'active' status for backward compatibility
            row = self.db.active_model()
        if row is None or torch is None or not row.get("path"):
            return
        try:
            # Refuse legacy/invalid models: no provenance or stored metrics that trip the leakage guard.
            if row.get("broker_mode") is None:
                raise ValueError("model has no provenance (trained before provenance tracking); retrain it")
            stored = decode(row).get("metrics") or {}
            stored_auc = stored.get("mean_auc") if isinstance(stored, dict) else None
            if stored_auc is not None and stored_auc > MAX_AUC:
                raise ValueError(f"stored mean AUC {stored_auc} exceeds {MAX_AUC} (possible leakage)")
            ckpt = torch.load(row["path"], weights_only=True)

            # Verify provenance matches current settings
            ckpt_provenance = ckpt.get("provenance", {})
            if ckpt_provenance:
                if ckpt_provenance.get("broker_mode") != self.s.broker_mode:
                    raise ValueError(f"Model provenance mismatch: trained in {ckpt_provenance.get('broker_mode')} mode, "
                                     f"but current mode is {self.s.broker_mode}. Refusing to load.")
                if self.s.broker_mode == "webull":
                    if ckpt_provenance.get("webull_environment") != self.s.webull_environment:
                        raise ValueError(f"Model provenance mismatch: trained in {ckpt_provenance.get('webull_environment')} environment, "
                                         f"but current is {self.s.webull_environment}. Refusing to load.")
                    if ckpt_provenance.get("account_id") and ckpt_provenance.get("account_id") != self.s.webull_account_id:
                        log.warning(f"Model was trained on account {ckpt_provenance.get('account_id')}, "
                                    f"current account is {self.s.webull_account_id}")

            model = _net(len(ckpt["features"]))
            model.load_state_dict(ckpt["state"])
            model.eval()
            if list(ckpt["features"]) != FEATURES:
                raise ValueError("feature list changed since this model was trained")
            self.model, self.model_id = model, row["id"]
            self.mean = torch.tensor(ckpt["mean"])
            self.std = torch.tensor(ckpt["std"])
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Could not load champion model #{row['id']}: {e}"
            log.warning(self.last_error)

    def _load_challenger(self, mid: int) -> tuple | None:
        """Load a challenger model by ID for shadow evaluation."""
        row = self.db.get_model_by_id(mid)
        if row is None or torch is None or not row.get("path"):
            return None
        try:
            ckpt = torch.load(row["path"], weights_only=True)
            model = _net(len(ckpt["features"]))
            model.load_state_dict(ckpt["state"])
            model.eval()
            return model, torch.tensor(ckpt["mean"]), torch.tensor(ckpt["std"])
        except Exception as e:
            log.warning(f"Could not load challenger model #{mid}: {e}")
            return None

    def predict(self, f: dict) -> float | None:
        """Predict using the champion model."""
        if self.model is None or not self.s.ml_enabled:
            return None
        x = vector(f)
        return None if x is None else round(_predict(self.model, self.mean, self.std, [x])[0], 4)

    def predict_challenger(self, f: dict, mid: int) -> float | None:
        """Predict using a challenger model (for shadow evaluation)."""
        loaded = self._load_challenger(mid)
        if loaded is None:
            return None
        model, mean, std = loaded
        x = vector(f)
        return None if x is None else round(_predict(model, mean, std, [x])[0], 4)

    def status(self) -> dict:
        champion = decode(self.db.champion_model())
        challenger = decode(self.db.challenger_model())

        # Add provenance info if available
        champion_provenance = {}
        if champion and champion.get("path") and torch is not None:
            try:
                ckpt = torch.load(champion["path"], weights_only=True)
                champion_provenance = ckpt.get("provenance", {})
            except Exception:
                pass

        challenger_provenance = {}
        if challenger and challenger.get("path") and torch is not None:
            try:
                ckpt = torch.load(challenger["path"], weights_only=True)
                challenger_provenance = ckpt.get("provenance", {})
            except Exception:
                pass

        return {
            "available": self.available,
            "error": TORCH_ERROR or self.last_error,
            "enabled": self.s.ml_enabled,
            "training": self.training,
            "min_p": self.s.ml_min_p,
            "champion": champion,
            "challenger": challenger,
            "features": FEATURES,
            "horizon_days": HORIZON,
            "provenance": champion_provenance,
            "challenger_provenance": challenger_provenance,
        }

    async def train(self, broker: Broker, symbols: list[str] | None = None) -> dict:
        """Train a new challenger model using the universe (or provided symbols).

        The model is saved as 'challenger' and enters shadow evaluation.
        It does not immediately replace the champion.

        Args:
            broker: Broker for fetching bar data
            symbols: Optional explicit symbol list. If None, uses the universe
                     from settings (with WATCHLIST added).
        """
        if torch is None:
            raise RuntimeError(TORCH_ERROR)
        if self.training:
            raise ValueError("Training is already running")
        # Refuse to train in mock mode - synthetic data produces misleading models
        if self.s.broker_mode == "mock":
            raise RuntimeError("Training is disabled in mock mode. Synthetic data produces "
                               "misleading models that don't generalize to real markets.")
        # Check if there's already a challenger in progress
        existing_challenger = self.db.challenger_model()
        if existing_challenger:
            raise ValueError(f"A challenger model #{existing_challenger['id']} is already in shadow evaluation. "
                           f"Wait for it to complete or reject it first.")

        self.training = True
        try:
            # Get universe from settings (with WATCHLIST as extra symbols)
            if symbols is None:
                universe_symbols = await get_universe(broker, self.s, extra_symbols=self.s.watchlist)
            else:
                universe_symbols = symbols

            if not universe_symbols:
                raise ValueError("No symbols in universe after filtering")

            log.info("ML training on universe: %s", ", ".join(universe_symbols))

            bars = {}
            for sym in universe_symbols:
                try:
                    bars[sym] = await broker.daily_bars(sym, self.s.ml_history_bars)
                except BrokerError as e:
                    log.warning("training bars for %s failed: %s", sym, e)
            # Filter symbols by minimum bars requirement
            min_bars = self.s.ml_min_bars
            filtered_bars = {sym: b for sym, b in bars.items() if len(b) >= min_bars}
            if len(filtered_bars) < len(bars):
                dropped = [sym for sym in bars if sym not in filtered_bars]
                log.info("Dropped %d symbol(s) with fewer than %d daily bars: %s",
                         len(dropped), min_bars, ", ".join(dropped))
            if not filtered_bars:
                raise ValueError(f"No symbols have at least {min_bars} daily bars of history")
            started = time.time()
            result = await asyncio.to_thread(self._train_sync, filtered_bars)
            result["seconds"] = round(time.time() - started, 1)
            result["symbols_dropped"] = [sym for sym in bars if sym not in filtered_bars]
            result["universe"] = universe_symbols
            return result
        except Exception as e:
            self.db.add_model(status="failed", note=f"{type(e).__name__}: {e}")
            self.db.log("error", f"Model training failed: {e}")
            raise
        finally:
            self.training = False

    def _train_sync(self, bars: dict[str, list[dict]]) -> dict:
        torch.set_num_threads(2)
        # Use trade ledger for triple-barrier labels if available, else simulate from bars
        rows = build_dataset(bars, db=self.db, settings=self.s)
        if len(rows) < 400:
            raise ValueError(f"Only {len(rows)} usable rows; need at least 400 "
                             "(more daily history or more symbols)")

        # Create walk-forward config from settings
        wf_config = create_ml_walkforward_config(self.s)
        import dataclasses
        wf_config = dataclasses.replace(
            wf_config, min_total_test_trades=MIN_SELECTED, min_independent_test_trades=20)

        # Define model factory and predict functions for the new walkforward
        def model_factory(train_rows):
            model, mean, std = _fit(train_rows)
            return (model, mean, std)

        def predict_fn(model_tuple, test_rows):
            model, mean, std = model_tuple
            return _predict(model, mean, std, [r["x"] for r in test_rows])

        # Run purged/embargoed walk-forward
        wf_result: WalkForwardResult = wf_walk_forward(
            rows, wf_config, self.s.consensus_ta_min, self.s.ml_min_p,
            model_factory=model_factory, predict_fn=predict_fn
        )

        params = {"features": FEATURES, "hidden": HIDDEN, "horizon": HORIZON,
                  "symbols": sorted(bars), "bars_per_symbol": self.s.ml_history_bars,
                  "label_type": "triple_barrier",
                  "walkforward": {
                      "n_folds": wf_config.n_folds,
                      "embargo_bars": wf_config.embargo_bars,
                      "purge_bars": wf_config.purge_bars,
                      "min_folds": wf_config.min_folds,
                      "min_total_test_trades": wf_config.min_total_test_trades,
                      "min_independent_test_trades": wf_config.min_independent_test_trades,
                      "multiple_testing_method": wf_config.multiple_testing_method,
                  }}

        # Provenance info for the model
        provenance = {
            "broker_mode": self.s.broker_mode,
            "webull_environment": self.s.webull_environment if self.s.broker_mode == "webull" else None,
            "account_id": self.s.webull_account_id if self.s.broker_mode == "webull" else None,
        }

        # Convert WalkForwardResult to dict for storage
        wf_dict = {
            "folds": [
                {
                    "fold": f.fold,
                    "train_start": str(f.train_start),
                    "train_end": str(f.train_end),
                    "test_start": str(f.test_start),
                    "test_end": str(f.test_end),
                    "n_train": f.n_train,
                    "n_test": f.n_test,
                    "n_test_independent": f.n_test_independent,
                    "n_test_purged": f.n_test_purged,
                    "auc": f.auc,
                    "hit_rate": f.hit_rate,
                    "avg_return": f.avg_return,
                    "metrics": f.metrics,
                }
                for f in wf_result.folds
            ],
            "mean_auc": wf_result.mean_auc,
            "mean_hit_rate": wf_result.mean_hit_rate,
            "mean_avg_return": wf_result.mean_avg_return,
            "total_test_trades": wf_result.total_test_trades,
            "total_independent_test_trades": wf_result.total_independent_test_trades,
            "effective_sample_size": wf_result.effective_sample_size,
            "passed": wf_result.passed,
            "rejection_reason": wf_result.rejection_reason,
            "multiple_testing_adjusted_p": wf_result.multiple_testing_adjusted_p,
            "deflated_sharpe": wf_result.deflated_sharpe,
            "details": wf_result.details,
            "checks": wf_result.details.get("checks", {}),
            "pooled": wf_result.details.get("pooled_rules_model", {}),
            "thresholds": wf_result.details.get("config", {}),
        }

        if not wf_result.passed:
            base_note = "Did not beat the rules out-of-sample (purged walk-forward); not used"
            rejection_note = wf_result.rejection_reason
            note = f"{base_note}" + (f" ({rejection_note})" if rejection_note else "")
            mid = self.db.add_model(status="rejected", n_samples=len(rows), n_symbols=len(bars),
                                    metrics=wf_dict, params=params,
                                    note=note,
                                    broker_mode=provenance["broker_mode"],
                                    webull_environment=provenance["webull_environment"],
                                    account_id=provenance["account_id"])
            self.db.log("info", f"Model #{mid} trained on {len(rows)} rows but not promoted "
                                f"(AUC {wf_result.mean_auc}); the gate keeps using rules only"
                                + (f" [{rejection_note}]" if rejection_note else ""))
            return {"id": mid, "status": "rejected", "metrics": wf_dict}

        # Final model on all labelled history
        model, mean, std = _fit(rows)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = MODEL_DIR / f"model-{int(time.time())}.pt"
        torch.save({"state": model.state_dict(), "mean": mean.tolist(), "std": std.tolist(),
                    "features": FEATURES, "provenance": provenance}, path)

        # Save as challenger (not active yet)
        from datetime import datetime
        mid = self.db.add_model(
            status="challenger",
            path=str(path),
            n_samples=len(rows),
            n_symbols=len(bars),
            metrics=wf_dict,
            params=params,
            note="Beat the rules out-of-sample (purged walk-forward); entered shadow evaluation as challenger",
            broker_mode=provenance["broker_mode"],
            webull_environment=provenance["webull_environment"],
            account_id=provenance["account_id"],
            shadow_started_at=datetime.utcnow().isoformat(timespec="seconds"),
        )
        self.db.log("info", f"Model #{mid} trained (AUC {wf_result.mean_auc}, "
                            f"hit {wf_result.mean_hit_rate} vs rules "
                            f"{wf_result.details.get('pooled_rules', {}).get('hit', 'N/A')}, "
                            f"ESS={wf_result.effective_sample_size:.1f}) "
                            f"entered shadow evaluation as challenger")
        # Nothing else in the app drives the shadow evaluation, so without this a model that
        # passed the purged walk-forward test would sit as a challenger forever and never be used.
        if self.s.ml_auto_promote:
            promoted = self.promote_challenger(mid)
            return {"id": mid, "status": promoted["status"], "metrics": wf_dict,
                    "replaced_id": promoted.get("replaced_id")}
        return {"id": mid, "status": "challenger", "metrics": wf_dict}

    def promote_challenger(self, mid: int | None = None) -> dict:
        """Promote the current challenger to champion.

        Args:
            mid: Optional challenger model ID. If None, uses the current challenger.

        Returns:
            Dict with promotion result.
        """
        challenger = self.db.challenger_model() if mid is None else self.db.get_model_by_id(mid)
        if not challenger or challenger["status"] != "challenger":
            raise ValueError("No valid challenger to promote")

        champion = self.db.champion_model()
        replaced_id = champion["id"] if champion else None

        # Promote challenger to champion
        self.db.promote_challenger(challenger["id"], replaced_id)

        # Reload the champion model for predictions
        self._load_champion()

        self.db.log("info", f"Model #{challenger['id']} promoted to champion "
                        f"(replaced #{replaced_id})")
        return {"id": challenger["id"], "status": "champion", "replaced_id": replaced_id}

    def reject_challenger(self, mid: int | None = None) -> dict:
        """Reject the current challenger.

        Args:
            mid: Optional challenger model ID. If None, uses the current challenger.

        Returns:
            Dict with rejection result.
        """
        challenger = self.db.challenger_model() if mid is None else self.db.get_model_by_id(mid)
        if not challenger or challenger["status"] != "challenger":
            raise ValueError("No valid challenger to reject")

        self.db.reject_challenger(challenger["id"])
        self.db.log("info", f"Model #{challenger['id']} rejected after shadow evaluation")
        return {"id": challenger["id"], "status": "rejected"}

    def rollback_champion(self) -> dict:
        """Rollback to the previous champion (if any).

        Returns:
            Dict with rollback result.
        """
        champion = self.db.champion_model()
        if not champion:
            raise ValueError("No current champion to rollback")

        replaced_id = champion.get("champion_replaced_id")
        if not replaced_id:
            raise ValueError("No previous champion to rollback to")

        # Rollback to previous champion
        self.db.rollback_to_champion(replaced_id)

        # Reload the champion model
        self._load_champion()

        self.db.log("info", f"Rolled back to champion #{replaced_id}")
        return {"id": replaced_id, "status": "champion", "rolled_back_from": champion["id"]}

    def log_challenger_outcome(self, mid: int, hit: bool, return_pct: float) -> None:
        """Log a trade outcome for challenger shadow evaluation."""
        self.db.log_challenger_outcome(mid, hit, return_pct)

    def evaluate_challenger(self, mid: int | None = None) -> dict:
        """Evaluate challenger performance and decide promotion/rejection.

        Uses settings:
        - ml_challenger_min_trades: minimum trades before evaluation (default 20)
        - ml_challenger_min_hit_rate_improvement: minimum hit rate improvement (default 0.02 = 2%)
        - ml_challenger_min_return_improvement: minimum return improvement (default 0.1 = 10bps)
        - ml_challenger_max_days: max days in shadow before forced decision (default 14)

        Returns:
            Dict with evaluation result and recommended action.
        """
        challenger = self.db.challenger_model() if mid is None else self.db.get_model_by_id(mid)
        if not challenger or challenger["status"] != "challenger":
            return {"action": "none", "reason": "No challenger to evaluate"}

        trades = challenger.get("shadow_trades", 0)
        hit_rate = challenger.get("shadow_hit_rate", 0.0)
        avg_return = challenger.get("shadow_avg_return", 0.0)

        min_trades = getattr(self.s, "ml_challenger_min_trades", 20)
        min_hit_improvement = getattr(self.s, "ml_challenger_min_hit_rate_improvement", 0.02)
        min_ret_improvement = getattr(self.s, "ml_challenger_min_return_improvement", 0.1)
        max_days = getattr(self.s, "ml_challenger_max_days", 14)

        # Get champion baseline metrics
        champion = self.db.champion_model()
        champ_hit_rate = 0.0
        champ_avg_return = 0.0
        if champion and champion.get("metrics"):
            champ_metrics = decode(champion).get("metrics", {})
            pooled = champ_metrics.get("pooled", {})
            champ_hit_rate = pooled.get("rules_model", {}).get("hit", 0.0) or pooled.get("rules", {}).get("hit", 0.0)
            champ_avg_return = pooled.get("rules_model", {}).get("avg_fwd", 0.0) or pooled.get("rules", {}).get("avg_fwd", 0.0)

        hit_improvement = hit_rate - champ_hit_rate
        ret_improvement = avg_return - champ_avg_return

        # Check if enough trades for evaluation
        if trades < min_trades:
            return {
                "action": "continue",
                "reason": f"Only {trades}/{min_trades} trades in shadow evaluation",
                "trades": trades,
                "hit_rate": hit_rate,
                "avg_return": avg_return,
                "champion_hit_rate": champ_hit_rate,
                "champion_avg_return": champ_avg_return,
                "hit_improvement": hit_improvement,
                "ret_improvement": ret_improvement,
            }

        # Check max days
        if challenger.get("shadow_started_at"):
            from datetime import datetime
            started = datetime.fromisoformat(challenger["shadow_started_at"])
            days = (datetime.utcnow() - started).days
            if days >= max_days:
                # Force evaluation at max days
                pass  # fall through to evaluation

        # Evaluate
        hit_ok = hit_improvement >= min_hit_improvement
        ret_ok = ret_improvement >= min_ret_improvement

        if hit_ok and ret_ok:
            return {"action": "promote", "reason": "Challenger outperforms champion",
                    "trades": trades, "hit_improvement": hit_improvement, "ret_improvement": ret_improvement}
        elif not hit_ok and not ret_ok:
            return {"action": "reject", "reason": "Challenger underperforms champion on both metrics",
                    "trades": trades, "hit_improvement": hit_improvement, "ret_improvement": ret_improvement}
        else:
            # Mixed results - continue shadow evaluation
            return {"action": "continue", "reason": "Mixed results, continuing evaluation",
                    "trades": trades, "hit_improvement": hit_improvement, "ret_improvement": ret_improvement}

    def deactivate(self) -> None:
        """Retire the current champion model."""
        champion = self.db.champion_model()
        if champion:
            self.db.set_model_status(champion["id"], "retired")
            self.db.log("info", f"Champion model #{champion['id']} retired by you; rules only")
        self.model = self.mean = self.std = None
        self.model_id = None
