"""Data loading, metrics, deflated Sharpe, config sampling and the sweep driver."""
from __future__ import annotations
import itertools, math, random
from pathlib import Path
import numpy as np
import pandas as pd
from statistics import NormalDist
_N = NormalDist()
from .engine import (Spec, features, regime_series, rebal_flags, build_targets, run, apply_voltarget, ANN)

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "data" / "bars_yahoo_ext"
APP = ROOT / "data" / "bars_yahoo"
RESEARCH_END = pd.Timestamp("2025-11-23")   # holdout (2025-11-24..) is never loaded
EVAL_START = pd.Timestamp("2011-01-03")      # first date every 12-1 signal can exist
SECTORS = "XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY".split()


def read_csv(p: Path) -> pd.DataFrame:
    d = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    return d[~d.index.duplicated()].sort_index()


class Data:
    def __init__(self, ext: Path = EXT):
        files = [f for f in sorted(ext.glob("*.csv")) if not f.name.startswith("sp500_list")]
        raw = {f.stem: read_csv(f) for f in files}
        idx = raw["SPY"].index
        idx = idx[idx <= RESEARCH_END]
        self.index = idx
        self.syms = sorted(raw)
        self.open = pd.DataFrame({s: raw[s]["open"].reindex(idx) for s in self.syms})
        self.close = pd.DataFrame({s: raw[s]["close"].reindex(idx) for s in self.syms})
        sp = sorted(ext.glob("sp500_list_*.csv"))
        self.sp500 = []
        self.survivorship_biased = True  # M17: sp500 universe = today's list, not point-in-time
        if sp:
            lst = pd.read_csv(sp[-1])["Symbol"].astype(str).str.replace(".", "-", regex=False)
            self.sp500 = [s for s in lst if s in raw]
        self.app87 = sorted(p.stem for p in APP.glob("*.csv") if p.stem in raw)
        self.univ = {"sector": [s for s in SECTORS if s in raw], "sp500": self.sp500, "app87": self.app87}
        self.feat = features(self.close)
        self.col = {s: i for i, s in enumerate(self.syms)}
        self.reb = {k: rebal_flags(idx, k) for k in ("weekly", "monthly", "daily")}
        self.reg = {k: regime_series(self.close["SPY"], k) for k in ("none", "200", "100")}

    def sub(self, universe: str, ro: str):
        ix = np.array([self.col[s] for s in self.univ[universe]])
        r = self.col[ro]
        O = np.column_stack([self.open.values[:, ix], self.open.values[:, r]])
        C = np.column_stack([self.close.values[:, ix], self.close.values[:, r]])
        return ix, O, C


def run_spec(d: Data, spec: Spec):
    ix, O, C = d.sub(spec.universe, spec.riskoff)
    F = d.feat
    score = F[spec.score][:, ix]
    elig = ~np.isnan(score)
    absok = F["absok"][:, ix]
    if spec.absmom:
        elig = elig & absok
    vol = F["vol"][:, ix]
    W, flag = build_targets(score, elig, vol, d.reg[spec.regime], d.reb[spec.rebal], spec, absok)
    if spec.voltarget > 0:
        r0, _ = run(O, C, W, flag, costs=False)
        W, flag = apply_voltarget(W, flag, r0, spec.voltarget)
    r, turn = run(O, C, W, flag)
    return pd.Series(r, index=d.index), pd.Series(turn, index=d.index)


def run_static(d: Data, weights, rebal="monthly", ro="BIL"):
    """Benchmarks: dict -> fixed weights, list -> equal weight over those available that day."""
    syms = list(weights)
    ix = np.array([d.col[s] for s in syms])
    O = np.column_stack([d.open.values[:, ix], d.open[ro].values])
    C = np.column_stack([d.close.values[:, ix], d.close[ro].values])
    T, M = len(d.index), len(syms)
    av = ~np.isnan(d.close.values[:, ix])
    W = np.zeros((T, M + 1)); flag = np.zeros(T, bool)
    reb = d.reb[rebal]; cur = np.zeros(M + 1); cur[M] = 1
    for t in range(T):
        if reb[t] or t == 0:
            new = np.zeros(M + 1)
            if isinstance(weights, dict):
                new[:M] = [weights[s] for s in syms]
            elif av[t].any():
                new[:M] = av[t] / av[t].sum()
            new[M] = 1 - new[:M].sum()
            if not np.array_equal(new, cur):
                flag[t] = True; cur = new
        W[t] = cur
    r, turn = run(O, C, W, flag)
    return pd.Series(r, index=d.index), pd.Series(turn, index=d.index)


def deflated_sharpe(r: pd.Series, n_trials: int, sr_var: float) -> float:
    x = r.dropna().values
    T = len(x)
    if T < 50 or x.std() == 0:
        return float("nan")
    sr = x.mean() / x.std()
    g = 0.5772156649
    sr0 = math.sqrt(max(sr_var, 1e-12)) * ((1 - g) * _N.inv_cdf(1 - 1 / n_trials) + g * _N.inv_cdf(1 - 1 / (n_trials * math.e)))
    z = (x - x.mean()) / x.std(); sk, ku = float((z**3).mean()), float((z**4).mean())
    den = math.sqrt(max(1 - sk * sr + (ku - 1) / 4 * sr ** 2, 1e-9))
    return float(_N.cdf((sr - sr0) * math.sqrt(T - 1) / den))


BLOCKS = [(2011, 2013), (2014, 2016), (2017, 2019), (2020, 2022), (2023, 2025)]


def _sr(x):
    return x.mean() / x.std() * math.sqrt(ANN) if len(x) > 5 and x.std() > 0 else float("nan")


def metrics(r: pd.Series, turn: pd.Series, spy: pd.Series) -> dict:
    r = r[r.index >= EVAL_START]; s = spy[spy.index >= EVAL_START]; turn = turn[turn.index >= EVAL_START]
    yrs = len(r) / ANN
    eq = (1 + r).cumprod()
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * math.sqrt(ANN)
    dd = math.sqrt((np.minimum(r, 0) ** 2).mean()) * math.sqrt(ANN)
    sortino = r.mean() * ANN / dd if dd > 0 else float("nan")
    mdd = (eq / eq.cummax() - 1).min()
    ry = (1 + r).groupby(r.index.year).prod() - 1
    sy = (1 + s).groupby(s.index.year).prod() - 1
    beat = float((ry > sy.reindex(ry.index)).mean())
    bl_sr, bl_beat = [], 0
    for a, b in BLOCKS:
        m = (r.index.year >= a) & (r.index.year <= b)
        x, y = r[m], s[m]
        bl_sr.append(_sr(x))
        bl_beat += int(_sr(x) > _sr(y))
    return dict(cagr=cagr, vol=vol, sharpe=_sr(r), sortino=sortino, maxdd=mdd, mar=cagr / abs(mdd) if mdd else np.nan,
                turnover_yr=turn.sum() / yrs, pct_years_beat_spy=beat, blocks_beating_spy_sr=bl_beat,
                block_sharpes=";".join(f"{v:.2f}" for v in bl_sr), n_days=len(r))


def sample_specs(n: int, seed: int = 7) -> list[Spec]:
    grid = list(itertools.product(["sector", "sp500", "app87"], ["mom121", "mom_vol", "m36", "r126", "r63", "ytd"],
                                  [3, 5, 10, 25], ["weekly", "monthly", "daily"], ["none", "200", "100"],
                                  ["BIL", "SPY", "TLT", "IEF"], [False, True], [0.0, 0.15]))
    random.Random(seed).shuffle(grid)
    out = []
    for u, s, n_, rb, rg, ro, am, vt in grid:
        if u == "sector" and n_ == 25:
            continue
        out.append(Spec(name=f"{u}|{s}|N{n_}|{rb}|rg{rg}|ro{ro}|am{int(am)}|vt{int(vt > 0)}", universe=u, score=s,
                        topn=n_, rebal=rb, regime=rg, riskoff=ro, absmom=am, voltarget=vt))
        if len(out) >= n:
            break
    return out


SOURCE = [
    Spec("A_claude", "sp500", "mom121", 25, "daily", "200", "SPY", False, 0.0, "invvol"),
    Spec("B_chatgpt", "sector", "m36", 3, "weekly", "none", "BIL", True, 0.0, "equal", True),
    Spec("C_gemini", "sp500", "ytd", 5, "daily", "none", "BIL", False, 0.0, "equal"),
    Spec("D_grok", "sp500", "mom_vol", 10, "monthly", "none", "BIL", False, 0.0, "equal"),
]
