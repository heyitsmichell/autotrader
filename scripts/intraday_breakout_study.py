"""Option A study: daily signal decides WHAT to buy, 5-min candles decide WHEN.

For each daily signal (close of day D), simulate entry variants on day D+1 using
data/bars_intraday/M5, then hold with the live variant-d exits (2-ATR stop from the
entry, 20-trading-day time stop), all on 5-min bars. Research only; no orders.

Signal sets:
  v2     the live v2 consensus gate (rules-only, like the backtester; ML off)
  trend  daily uptrend days: close > SMA50 > SMA200 (a broader swing setup set)
  all    every symbol-day (how each entry rule behaves unconditionally)

NOTE: the 5-min history (2026-05-29 ->) lies inside the locked holdout period
(after 2025-11-23). The user chose this as an exploratory study (2026-11-25).

  python scripts/intraday_breakout_study.py [--sets v2,trend,all] [--slip-bps 5]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.data import read_bars_csv  # noqa: E402
from app.backtest.signals import build_features, signal_at  # noqa: E402
from app.config.loader import load_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
M5 = ROOT / "data" / "bars_intraday" / "M5"
DAILY = ROOT / "data" / "bars_yahoo"
OUT = ROOT / "data" / "experiments" / "intraday_breakout"
ETFS = {"SPY", "SPYM", "QQQ", "QQQM", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLI",
        "XLP", "XLU", "XLB", "XLC", "XLRE", "TLT", "IEF", "IAU", "GLD", "IEFA", "SMH", "XSD",
        "SOXX", "XBI", "ARKK", "EFA", "EEM", "VTI", "VOO"}
CUTOFF_SLOT = 66          # last 5-min slot allowed to trigger an entry (15:00 ET)
STOP_ATR, HOLD_DAYS = 2.0, 20


# ------------------------------------------------------------------ data
def load_m5(sym: str) -> dict[date, list[dict]]:
    """5-min regular-session bars grouped by ET trading day, each with its slot index."""
    days: dict[date, list[dict]] = defaultdict(list)
    p = M5 / f"{sym}.csv"
    if not p.exists():
        return {}
    with p.open() as fh:
        for r in csv.DictReader(fh):
            t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ET)
            slot = ((t.hour * 60 + t.minute) - 570) // 5
            if 0 <= slot < 78:
                days[t.date()].append({"slot": slot, "o": float(r["open"]), "h": float(r["high"]),
                                       "l": float(r["low"]), "c": float(r["close"]),
                                       "v": float(r["volume"])})
    for d in days:
        days[d].sort(key=lambda b: b["slot"])
    # drop half days / broken days (need the open bar and most of the session)
    return {d: b for d, b in days.items() if b and b[0]["slot"] == 0 and len(b) >= 70}


# ------------------------------------------------------------------ entry rules
def entries(day: list[dict], prev: list[dict], atr: float, vol_med: list[float] | None,
            slip: float) -> dict[str, tuple[int, float, float] | None]:
    """Variant -> (index of first bar AFTER the fill moment, fill price, structural stop) or None.

    The fill bar itself is included in exit checks from the next bar on; a breakout fill
    inside bar j is treated conservatively: its low after the fill is not known, so exit
    checks start at bar j+1 (and the stop is far enough that this rarely matters)."""
    o = day[0]["o"]
    up = 1 + slip
    res: dict[str, tuple[int, float, float] | None] = {}
    res["OPEN"] = (0, o * up, o - STOP_ATR * atr)
    for name, k in (("T15", 3), ("T30", 6)):
        res[name] = (k, day[k]["o"] * up, day[k]["o"] - STOP_ATR * atr) if len(day) > k else None

    def stop_break(level: float, start: int, tag_stop: float, confirm: bool = False, vol: bool = False):
        for j in range(start, len(day)):
            b = day[j]
            if b["slot"] > CUTOFF_SLOT:
                return None
            if confirm:
                if b["c"] > level and (not vol or _vol_ok(b, vol_med)):
                    if j + 1 < len(day):
                        return (j + 1, day[j + 1]["o"] * up, tag_stop)
                    return None
            elif b["h"] > level and (not vol or _vol_ok(b, vol_med)):
                fill = max(level, b["o"]) * (up + slip)  # stop-entry: extra slippage
                return (j + 1, fill, tag_stop)
        return None

    for name, k in (("ORB5", 1), ("ORB15", 3), ("ORB30", 6)):
        orh = max(b["h"] for b in day[:k])
        orl = min(b["l"] for b in day[:k])
        res[name] = stop_break(orh, k, orl)
        if k == 3:
            res["ORB15_close"] = stop_break(orh, k, orl, confirm=True)
            res["ORB15_vol"] = stop_break(orh, k, orl, vol=True) if vol_med else None
    pdh = max(b["h"] for b in prev)
    res["PDH"] = stop_break(pdh, 0, min(b["l"] for b in day[:1]))
    # gap-and-go guard: if the open is already above the prior-day high, PDH fills at the open
    # VWAP reclaim: after 9:45, first 5-min close back above session VWAP having traded below it
    pv = vv = 0.0
    below = False
    res["VWAP"] = None
    for j, b in enumerate(day):
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vv += b["v"]
        vwap = pv / vv if vv else b["c"]
        if b["slot"] > CUTOFF_SLOT:
            break
        if b["c"] < vwap:
            below = True
        elif below and j >= 3 and j + 1 < len(day):
            lo = min(x["l"] for x in day[:j + 1])
            res["VWAP"] = (j + 1, day[j + 1]["o"] * up, lo)
            break
    # dip limit: resting buy at open - 0.3 ATR (no slippage on a limit)
    lim = o - 0.3 * atr
    res["DIP03"] = None
    for j in range(1, len(day)):
        if day[j]["slot"] > CUTOFF_SLOT:
            break
        if day[j]["l"] <= lim:
            res["DIP03"] = (j + 1, min(lim, day[j]["o"]), lim - STOP_ATR * atr)
            break
    return res


def _vol_ok(b: dict, med: list[float] | None) -> bool:
    return bool(med) and med[b["slot"]] > 0 and b["v"] >= 1.5 * med[b["slot"]]


# ------------------------------------------------------------------ exit simulation
def hold(days_seq: list[list[dict]], start_idx: int, entry: float, stop: float, slip: float):
    """Walk 5-min bars from days_seq[0][start_idx:] onward. Variant-d exits.

    Returns (return_pct, days_held, reason, ret_close_d0, ret_close_d5, mae_d0_pct)."""
    r0 = r5 = None
    mae0 = 0.0
    for di, bars in enumerate(days_seq):
        rng = bars[start_idx:] if di == 0 else bars
        for b in rng:
            if di == 0:
                mae0 = min(mae0, b["l"] / entry - 1)
            if b["l"] <= stop:
                px = min(stop, b["o"]) * (1 - 2 * slip)
                ret = px / entry - 1
                return ret, di, "stop", (r0 if r0 is not None else ret), (r5 if r5 is not None else ret), mae0
        c = bars[-1]["c"]
        if di == 0:
            r0 = c / entry - 1
        if di == 5:
            r5 = c / entry - 1
        if di == HOLD_DAYS:
            return c * (1 - slip) / entry - 1, di, "time", r0, r5, mae0
    c = days_seq[-1][-1]["c"]
    return c / entry - 1, len(days_seq) - 1, "open", r0, (r5 if r5 is not None else c / entry - 1), mae0


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="v2,trend,all")
    ap.add_argument("--slip-bps", type=float, default=5.0, help="per side, stocks (ETFs use 2)")
    a = ap.parse_args()
    import dataclasses
    settings = dataclasses.replace(load_settings(), backtest_ml_enabled=False)
    sets = a.sets.split(",")
    syms = sorted(p.stem for p in M5.glob("*.csv"))
    spy_daily = read_bars_csv(DAILY / "SPY.csv")
    spy_feat = build_features(spy_daily)
    spy_by_day = {b["time"].date(): (b, f) for b, f in zip(spy_daily, spy_feat)}
    rows: list[dict] = []
    for sym in syms:
        m5 = load_m5(sym)
        dp = DAILY / f"{sym}.csv"
        if not m5 or not dp.exists():
            continue
        slip = (2.0 if sym in ETFS else a.slip_bps) / 1e4
        daily = read_bars_csv(dp)
        feats = build_features(daily)
        # benchmark aligned BY DATE (engine.py aligns by index; wrong for short histories)
        bb, bf = [], []
        for b in daily:
            x = spy_by_day.get(b["time"].date())
            bb.append(x[0] if x else b)
            bf.append(x[1] if x else {})
        mdays = sorted(m5)
        vols = {d: [0.0] * 78 for d in mdays}
        for d in mdays:
            for b in m5[d]:
                vols[d][b["slot"]] = b["v"]
        didx = {b["time"].date(): i for i, b in enumerate(daily)}
        for k in range(1, len(mdays)):
            d_sig, d_ent = mdays[k - 1], mdays[k]
            i = didx.get(d_sig)
            if i is None or i < 250:
                continue
            f = feats[i]
            tags = []
            if "all" in sets:
                tags.append("all")
            if "trend" in sets and f.get("sma50") and f.get("sma200") and \
                    f["close"] > f["sma50"] > f["sma200"]:
                tags.append("trend")
            if "v2" in sets:
                s = signal_at(daily, feats, i, settings, benchmark_bars=bb, benchmark_features=bf)
                if s.get("ok"):
                    tags.append("v2")
            if not tags:
                continue
            atr_pct = (f.get("atr14") or 0) / f["close"] if f.get("close") else 0
            if atr_pct <= 0:
                continue
            day, prev = m5[d_ent], m5[d_sig]
            atr = atr_pct * prev[-1]["c"]
            prior = mdays[max(0, k - 20):k]
            med = [st.median(vols[d][s] for d in prior) for s in range(78)] if len(prior) >= 10 else None
            seq = [m5[d] for d in mdays[k:k + HOLD_DAYS + 1]]
            for var, e in entries(day, prev, atr, med, slip).items():
                base = {"sym": sym, "sig_day": str(d_sig), "day": str(d_ent), "var": var,
                        "tags": "|".join(tags), "atr_pct": atr_pct,
                        "gap_pct": day[0]["o"] / prev[-1]["c"] - 1}
                if e is None:
                    rows.append({**base, "filled": 0})
                    continue
                j, px, sstop = e
                stop = px * (1 - STOP_ATR * atr_pct)  # live variant d: 2 ATR below the fill
                ret, held, why, r0, r5, mae0 = hold(seq, j, px, stop, slip)
                # structural stop (OR low / session low), kept between 0.25 and 2 ATR below the fill
                risk_struct = min(STOP_ATR * atr_pct, max(0.25 * atr_pct, 1 - sstop / px))
                ret_s, _, why_s, _, _, _ = hold(seq, j, px, px * (1 - risk_struct), slip)
                rows.append({**base, "filled": 1, "fill_slot": j, "entry": px,
                             "vs_open_bps": (px / day[0]["o"] - 1) * 1e4, "ret": ret, "held": held,
                             "exit": why, "r_d0": r0, "r_d5": r5, "mae_d0": mae0,
                             "R_2atr": ret / (STOP_ATR * atr_pct), "struct_risk_atr": risk_struct / atr_pct,
                             "ret_struct": ret_s, "exit_struct": why_s, "R_struct": ret_s / risk_struct})
        print(f"{sym}: {len(rows)} rows", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    keys = sorted({k for r in rows for k in r})
    with (OUT / "rows.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    summarize(rows, sets)


def boot_ci(diffs_by_day: dict[str, list[float]], n: int = 2000, seed: int = 7):
    """Mean diff with a 90% CI, bootstrapping whole entry days (signals on a day are correlated)."""
    days = list(diffs_by_day)
    if len(days) < 5:
        return None
    rnd = random.Random(seed)
    means = []
    for _ in range(n):
        pick = [x for _ in days for x in diffs_by_day[rnd.choice(days)]]
        if pick:
            means.append(sum(pick) / len(pick))
    means.sort()
    return means[int(0.05 * len(means))], means[int(0.95 * len(means))]


def summarize(rows: list[dict], sets: list[str]) -> None:
    out = {}
    lines = []
    for tag in sets:
        sub = [r for r in rows if tag in r["tags"].split("|")]
        by_var = defaultdict(list)
        for r in sub:
            by_var[r["var"]].append(r)
        open_ret = {(r["sym"], r["day"]): r["ret"] for r in by_var["OPEN"] if r.get("filled")}
        n_sig = len(open_ret)
        lines.append(f"\n=== set {tag}: {n_sig} signals, "
                     f"{len({r['day'] for r in by_var['OPEN']})} entry days ===")
        lines.append("  A) 2-ATR stop (live variant d).  entry effect = same signal, this entry minus OPEN.")
        lines.append("     selection = OPEN return of the signals this rule took vs the ones it skipped.")
        lines.append("  B) structural stop (OR low etc., 0.25-2 ATR), in R = return / stop distance.")
        lines.append(f"{'variant':<12}{'fill%':>5}{'n':>5}{'vsOpen':>7}{'mean%':>7}{'win%':>5}"
                     f"{'entry eff% [90% CI]':>22}{'took':>7}{'skip':>7} |{'stopATR':>8}{'R_d':>6}"
                     f"{'R_s':>6}{'R_s-R_open [90% CI]':>22}{'stop%':>6} | net% vs random skip")
        open_R = {(r["sym"], r["day"]): r["R_2atr"] for r in by_var["OPEN"] if r.get("filled")}
        res = {}
        for var, rs in by_var.items():
            f = [r for r in rs if r.get("filled")]
            if not f:
                continue
            rets = [r["ret"] for r in f]
            dd, dr = defaultdict(list), defaultdict(list)
            for r in f:
                k = (r["sym"], r["day"])
                if k in open_ret:
                    dd[r["day"]].append(r["ret"] - open_ret[k])
                    dr[r["day"]].append(r["R_struct"] - open_R[k])
            diffs = [x for v in dd.values() for x in v]
            rdiffs = [x for v in dr.values() for x in v]
            ci, rci = boot_ci(dd), boot_ci(dr)
            # net value per signal vs skipping the same share of signals at random:
            # (this rule's return, 0 when unfilled) - fill_rate * OPEN return
            fr = len(f) / max(1, len(rs))
            got = {(r["sym"], r["day"]): r["ret"] for r in f}
            nv = defaultdict(list)
            for k, v in open_ret.items():
                nv[k[1]].append(got.get(k, 0.0) - fr * v)
            net = [x for v in nv.values() for x in v]
            nci = boot_ci(nv)
            filled_keys = {(r["sym"], r["day"]) for r in f}
            took = [v for k, v in open_ret.items() if k in filled_keys]
            missed = [v for k, v in open_ret.items() if k not in filled_keys]
            res[var] = {
                "fill_rate": len(f) / max(1, len(rs)), "n": len(f),
                "vs_open_bps": st.mean(r["vs_open_bps"] for r in f),
                "mean": st.mean(rets), "median": st.median(rets),
                "win": sum(x > 0 for x in rets) / len(rets),
                "stop_rate": sum(r["exit"] == "stop" for r in f) / len(f),
                "d0": st.mean(r["r_d0"] for r in f),
                "ev_per_signal": sum(rets) / max(1, n_sig),
                "entry_effect": st.mean(diffs) if diffs else None, "entry_ci90": ci,
                "took_open_mean": st.mean(took) if took else None,
                "skipped_open_mean": st.mean(missed) if missed else None, "n_skipped": len(missed),
                "stop_dist_atr": st.mean(r["struct_risk_atr"] for r in f),
                "R_2atr": st.mean(r["R_2atr"] for r in f),
                "R_struct": st.mean(r["R_struct"] for r in f),
                "R_struct_vs_open": st.mean(rdiffs) if rdiffs else None, "R_ci90": rci,
                "stop_rate_struct": sum(r["exit_struct"] == "stop" for r in f) / len(f),
                "net_vs_random": st.mean(net) if net else None, "net_ci90": nci,
            }
            x = res[var]

            def fmt(v, c, scale=100.0):
                cs = f"[{c[0]*scale:+.2f},{c[1]*scale:+.2f}]" if c else "[n/a]"
                return f"{(v or 0)*scale:+6.2f} {cs:<15}"
            sk = f"{x['skipped_open_mean']*100:+7.2f}" if x["skipped_open_mean"] is not None else "      -"
            lines.append(
                f"{var:<12}{x['fill_rate']*100:5.0f}{x['n']:5d}{x['vs_open_bps']:+7.0f}"
                f"{x['mean']*100:+7.2f}{x['win']*100:5.0f} {fmt(x['entry_effect'], ci)}"
                f"{(x['took_open_mean'] or 0)*100:+7.2f}{sk} |{x['stop_dist_atr']:8.2f}"
                f"{x['R_2atr']:+6.2f}{x['R_struct']:+6.2f} {fmt(x['R_struct_vs_open'], rci, 1.0)}"
                f"{x['stop_rate_struct']*100:6.0f} | {fmt(x['net_vs_random'], nci)}")
        out[tag] = res
    txt = "\n".join(lines)
    print(txt)
    (OUT / "summary.txt").write_text(txt)
    (OUT / "summary.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
