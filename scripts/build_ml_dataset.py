"""Rebuild data/ml_dataset/trades.csv.gz from scratch (idempotent). Never starts the app; DB opened read-only.

Usage: python scripts/build_ml_dataset.py [--no-backtest]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ml_data import (ALL_COLS, DATASET_PATH, HOLDOUT_LAST_BACKTEST_ENTRY, BarStore, append_trades,  # noqa: E402
                         hold_excursions, is_holdout, load, signal_date_for)

EXP = ROOT / "data" / "experiments"
BT_START, BT_END = "2022-01-01", HOLDOUT_LAST_BACKTEST_ENTRY
BT_VARIANTS = ["d_champion", "consensus_time30"]
STORE = BarStore()


def _nn(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else x


def truncated_bars(src: Path, last: str) -> Path:
    out = EXP / "_cache" / f"ml_dataset_bars_{last}"
    if (out / ".done").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.csv"):
        df = pd.read_csv(f)
        df[df["date"].astype(str).str[:10] <= last].to_csv(out / f.name, index=False)
    (out / ".done").write_text("ok")
    return out


def backtest_rows(variant_name: str, bars_dir: Path) -> list[dict]:
    from app.backtest.engine import run_backtest
    from app.strategy_lab import harness
    from app.strategy_lab.variants import REGISTRY
    v = REGISTRY[variant_name]
    s = harness.build_settings(v.overrides(), bars_dir)          # load_settings + live-style overrides, ML off
    s = dataclasses.replace(s, backtest_ml_enabled=False)
    os.chdir(ROOT)
    universe = sorted(p.stem for p in bars_dir.glob("*.csv"))
    res = run_backtest(s, universe=universe, benchmark_symbol="SPY", **harness.FEES)
    rows = []
    for t in res.trades:
        ed = str(t.entry_day)[:10]
        if not t.exit_day or ed < BT_START or ed > BT_END:
            continue
        ep = t.entry_fill_price
        rows.append({
            "strategy": "live_consensus_v2", "variant": variant_name, "symbol": t.symbol, "asset": "stock",
            "signal_date": signal_date_for(ed, STORE), "entry_date": ed, "entry_price": ep,
            "exit_date": str(t.exit_day)[:10], "exit_price": t.exit_fill_price, "exit_reason": t.exit_reason,
            "hold_days": t.hold_days, "ret": t.pnl_pct,
            "r_multiple": t.r_multiple if t.risk_usd > 0 else None,
            "mfe": t.mfe, "mae": t.mae, "stop": t.sl, "ta_score": t.entry_ta_score, "ml_p": t.entry_ml_p,
            "consensus": "long", "config_hash": v.config_hash()})
    return rows


def intraday_rows() -> list[dict]:
    d = pd.read_csv(EXP / "intraday_breakout" / "rows.csv")
    d = d[d["filled"] == 1]
    rows = []
    for r in d.itertuples(index=False):
        ed = str(r.day)[:10]
        rows.append({
            "strategy": "intraday_breakout", "variant": r.var, "symbol": r.sym, "asset": "stock",
            "signal_date": str(r.sig_day)[:10], "entry_date": ed, "entry_price": r.entry, "exit_reason": _nn(r.exit),
            "hold_days": r.held, "ret": r.ret, "r_multiple": r.R_2atr, "mae": r.mae_d0, "config_hash": "",
            "intr_gap_pct": r.gap_pct, "intr_vs_open_bps": r.vs_open_bps, "intr_fill_slot": r.fill_slot,
            "intr_tags": r.tags, "intr_r_struct": r.R_struct, "intr_ret_struct": r.ret_struct})
    return rows


def options_rows() -> list[dict]:
    d = pd.read_csv(EXP / "options_entry" / "trades.csv")
    rows = []
    for r in d.itertuples(index=False):
        rows.append({
            "strategy": "options_entry_model", "variant": r.variant, "symbol": r.sym, "asset": "call",
            "signal_date": signal_date_for(r.entry, STORE), "entry_date": r.entry, "exit_date": r.exit,
            "entry_price": r.prem0, "exit_reason": r.opt_reason, "ret": r.opt_ret, "config_hash": "",
            "opt_strike": r.strike, "opt_iv": r.iv, "opt_prem0": r.prem0, "opt_stock_ret": r.stock_ret,
            "opt_stock_reason": r.stock_reason})
        rows[-1]["hold_days"] = (pd.Timestamp(r.exit) - pd.Timestamp(r.entry)).days
    return rows


def live_rows() -> list[dict]:
    con = sqlite3.connect("file:" + (ROOT / "data" / "trader.db").as_posix() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = """select l.*, p.ml_p as p_ml, p.consensus as p_cons, p.signal_id as p_sig, p.asset_class as p_asset,
                  p.option_type as p_opt, p.strike_price as p_strike, p.features as p_feat, s.ta_score as s_ta,
                  s.ml_p as s_ml, s.consensus as s_cons, s.features as s_feat
           from trade_ledger l left join proposals p on p.id = l.proposal_id left join signals s on s.id = p.signal_id
           where l.side = 'BUY' and l.entry_fill_price is not null"""
    rows = []
    for r in con.execute(q):
        r = dict(r)
        ed = (r["entry_fill_time"] or r["created_at"])[:10]
        xd = (r["exit_fill_time"] or "")[:10] or None
        ep, xp = r["entry_fill_price"], r["exit_fill_price"]
        feat = {}
        for k in ("entry_features", "p_feat", "s_feat"):
            try:
                x = json.loads(r[k]) if r[k] else None
                x = json.loads(x) if isinstance(x, str) else x
                if isinstance(x, dict):
                    feat = x
                    break
            except (ValueError, TypeError):
                pass
        sd = str(feat.get("date") or "")[:10] or signal_date_for(ed, STORE)
        row = {"strategy": "live_paper", "variant": f"{r['broker_mode'] or 'na'}:{r['webull_environment'] or 'na'}",
               "symbol": r["symbol"], "asset": "call" if (r["p_asset"] or "") == "option" else "stock",
               "signal_date": sd, "entry_date": ed, "entry_price": ep, "exit_date": xd, "exit_price": xp,
               "exit_reason": r["exit_reason"], "stop": r["stop_price"],
               "ret": (xp / ep - 1) if xp and ep else None,
               "ta_score": r["entry_ta_score"] if r["entry_ta_score"] is not None else r["s_ta"],
               "ml_p": r["entry_ml_p"] if r["entry_ml_p"] is not None else (r["p_ml"] if r["p_ml"] is not None else r["s_ml"]),
               "consensus": "long" if (r["p_cons"] or r["s_cons"]) else None, "holdout": 1, "config_hash": "",
               "live_quantity": r["quantity_filled"], "live_proposal_id": r["proposal_id"],
               "live_broker_mode": r["broker_mode"], "opt_strike": r["p_strike"]}
        if xd:
            row["hold_days"] = (pd.Timestamp(xd) - pd.Timestamp(ed)).days
        if ep and r["mfe"] is not None and r["mae"] is not None:
            row["mfe"], row["mae"] = r["mfe"] / ep, r["mae"] / ep   # ledger mfe/mae are price-points
        if ep and r["stop_price"] and xp and ep > r["stop_price"]:
            row["r_multiple"] = (xp - ep) / (ep - r["stop_price"])
        if feat:   # recomputed features fill gaps; stored ones (what the live gate saw) win
            from app.ml_data import features_at
            full = features_at(r["symbol"], sd, STORE) or {}
            row.update({"f_" + k: v for k, v in {**full, **feat}.items() if k != "date"})
        rows.append(row)
    con.close()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-backtest", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    if DATASET_PATH.exists():
        DATASET_PATH.unlink()
    if not a.no_backtest:
        bars_dir = truncated_bars(ROOT / "data" / "bars_yahoo", BT_END)
        for name in BT_VARIANTS:
            t1 = time.time()
            n = append_trades("backtest_live", backtest_rows(name, bars_dir), store=STORE)
            print(f"backtest {name}: file rows {n} ({time.time() - t1:.0f}s)", flush=True)
    append_trades("intraday_breakout", intraday_rows(), store=STORE)
    append_trades("options_entry", options_rows(), store=STORE)
    append_trades("live_paper", live_rows(), store=STORE)
    df = load()
    print(f"\nTotal rows {len(df)}, columns {len(df.columns)}, build {time.time() - t0:.0f}s")
    fc = [c for c in df.columns if c.startswith("f_")]
    df["_complete"] = df[fc].notna().all(axis=1)
    g = df.groupby(["source", "variant"]).agg(rows=("symbol", "size"), first=("entry_date", "min"),
                                              last=("entry_date", "max"), holdout=("holdout", "sum"),
                                              complete_feat=("_complete", "mean"))
    g["complete_feat"] = (g["complete_feat"] * 100).round(1)
    print(g.to_string())


if __name__ == "__main__":
    main()
