"""Run ML lab experiments A and B, write data/experiments/ml/REPORT.md. Research only."""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from app.ml_lab import core, exp


def fmt(res, dsr):
    rows = []
    for k, m in res.items():
        rows.append(f"{k:18s} cagr {m['cagr']:6.3f} sharpe {m['sharpe']:5.2f} maxdd {m['maxdd']:7.3f} "
                    f"turn {m['turnover']:6.1f} dsr {dsr.get(k, float('nan')):5.3f}")
    return "\n".join(rows)


def main():
    t0 = time.time()
    p = core.Panel()
    assert p.index.max() <= core.RESEARCH_END
    a = exp.run_a(p)
    b = exp.run_b(p)
    allres = {"A:" + k: v for k, v in a["res"].items()}
    allres.update({"B:" + k: v for k, v in b["res"].items()})
    allr = {"A:" + k: v for k, v in a["rets"].items()}
    allr.update({"B:" + k: v for k, v in b["rets"].items()})
    dsr, nt, srv = exp.dsr_table(allr, allres)
    dA = {k[2:]: v for k, v in dsr.items() if k.startswith("A:")}
    dB = {k[2:]: v for k, v in dsr.items() if k.startswith("B:")}
    out = [f"# ML lab report", "",
           f"Eval window: A {a['start'].date()}..{a['end'].date()} (first test year 2013: 2011-12 is the minimum training history), "
           f"B {b['start'].date()}..{b['end'].date()}. Data capped at 2025-11-23; holdout never loaded.",
           f"Walk-forward: expanding window, retrain every calendar year, embargo = label end + 21 trading days. "
           f"Trades at next open; 5 bps + SEC + TAF. DSR uses n_trials={nt} (registry + 304 prior), daily-SR variance {srv:.3e}.",
           "SURVIVORSHIP: universe is today's S&P 500 (2026-11-25 list); losers/delisted names absent. Inflates all stock "
           "strategies equally, ML and baseline alike; relative comparison is the meaningful one.", "",
           "## A. Cross-sectional ranking (monthly)", "", "```", fmt(a["res"], dA), "```", "",
           f"Full-window (2011+) plain 12-1 top10 baseline: {a['full_base']}", "",
           "IC mean / t-stat (monthly Spearman, test months):", "```",
           pd.DataFrame({"ic": a["ic_mean"], "t": a["ic_t"]}).round(4).to_string(), "```",
           "Per-year mean IC:", "```", a["ic_year"].round(3).to_string(), "```",
           "Per-year IC hit rate (share of months IC>0):", "```", a["hit_year"].round(2).to_string(), "```"]
    yr = pd.DataFrame({k: exp.yearly(a["rets"][k]) for k in ("mom121|N10|ew", "ridge|N10|ew", "lgbm|N10|ew", "ens|N10|ew", "SPY_BH")})
    out += ["Per-year returns (N10 equal weight):", "```", yr.round(3).to_string(), "```", ""]
    out += ["## B. SPY market timing (weekly)", "", f"Test base rate P(SPY>BIL over 4w) = {b['base_rate']:.3f}", "```",
            fmt(b["res"], dB), "```", "AUC per year:", "```", pd.DataFrame(b["auc"]).round(3).to_string(), "```", "",
            "Credit proxy (HYG/LQD) not in dataset; used IEF-BIL (curve) and TLT-IEF (term) proxies only.", "",
            "## C. Intraday entry timing", "",
            "NOT RUN. data/bars_intraday/M15 spans 2026-05-28..2026-11-24, which lies entirely inside the locked holdout "
            "(after 2025-11-23). The hard rule forbids loading it, so C is blocked pending explicit owner permission.", "",
            f"Total runtime {time.time() - t0:.0f}s (A {a['runtime']:.0f}s, B {b['runtime']:.0f}s)."]
    (core.OUT / "REPORT.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
