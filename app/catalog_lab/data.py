"""Panel loading (<= 2025-11-21 only), simulation and metrics. Timing follows app/rotation_lab/engine.py:
weights decided at close t are traded at open t+1; day k return = open[k] -> open[k+1]."""
from __future__ import annotations
import csv, hashlib, io, math, time
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "data" / "bars_yahoo_ext"
SOCIAL = EXT / "social"
RAWOPT = ROOT / "data" / "experiments" / "options_research" / "raw"
OUT = ROOT / "data" / "experiments" / "catalog"
REGISTRY = ROOT / "data" / "experiments" / "trials_registry.csv"
END = pd.Timestamp("2025-11-21")          # locked holdout: nothing later is ever kept
START = pd.Timestamp("2011-01-03")
L3Y = pd.Timestamp("2022-11-21")
ANN = 252
SLIP, SEC_FEE = 0.0005, 0.0000278         # same as rotation_lab.engine (5 bps/side, SEC fee on sells)


def _read(p: Path) -> pd.DataFrame:
    d = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    d = d[~d.index.duplicated()].sort_index()
    return d[d.index <= END]


class Panel:
    def __init__(self):
        lst = pd.read_csv(sorted(EXT.glob("sp500_list_*.csv"))[-1])
        lst["Symbol"] = lst["Symbol"].astype(str).str.replace(".", "-", regex=False)
        lst = lst[lst["Symbol"].apply(lambda s: (EXT / f"{s}.csv").exists())].drop_duplicates("Symbol")
        self.stocks = sorted(lst["Symbol"])
        self.sector = dict(zip(lst["Symbol"], lst["GICS Sector"]))
        self.industry = dict(zip(lst["Symbol"], lst["GICS Sub-Industry"]))
        spy = _read(EXT / "SPY.csv")
        self.index = spy.index
        raw = {s: _read(EXT / f"{s}.csv") for s in self.stocks}
        g = lambda c: pd.DataFrame({s: raw[s][c].reindex(self.index) for s in self.stocks})
        self.O, self.H, self.L, self.C, self.V = g("open"), g("high"), g("low"), g("close"), g("volume")
        self.spy = spy
        del raw
        self.vix = self._macro(RAWOPT / "VIX.csv")
        self.vix3m = self._macro(RAWOPT / "VIX3M.csv")

    def _macro(self, p: Path) -> pd.Series:
        d = pd.read_csv(p, parse_dates=["Date"]).set_index("Date")["Close"]
        return d[~d.index.duplicated()].sort_index().reindex(self.index).ffill()

    def etf(self, syms, folder=EXT):
        d = {s: _read(folder / f"{s}.csv") for s in syms}
        f = lambda c: pd.DataFrame({s: d[s][c].reindex(self.index) for s in syms})
        return f("open"), f("high"), f("low"), f("close")


def sim(W, O: np.ndarray):
    """W: T x M target weights set at close t. Returns (gross, net, turnover) daily arrays."""
    W = np.nan_to_num(np.asarray(W, float))
    with np.errstate(all="ignore"):
        R = np.zeros(O.shape, dtype=float)
        R[:-1] = O[1:] / O[:-1] - 1.0
    R = np.nan_to_num(R, nan=0.0, posinf=0.0, neginf=0.0)
    R = np.clip(R, -0.9, 3.0)
    Wp = np.zeros_like(W); Wp[1:] = W[:-1]
    gross = (Wp * R).sum(1)
    d = np.zeros_like(W); d[1:] = Wp[1:] - Wp[:-1]
    turn = np.abs(d).sum(1)
    sells = (-d).clip(min=0).sum(1)
    net = gross - SLIP * turn - SEC_FEE * sells
    return gross, net, turn


def metrics(r: pd.Series, turn: pd.Series | None = None) -> dict:
    r = r.dropna()
    if len(r) < 30:
        return dict(cagr=np.nan, sharpe=np.nan, maxdd=np.nan, turnover=np.nan)
    yrs = len(r) / ANN
    eq = (1 + r).cumprod()
    sd = r.std()
    return dict(cagr=float(eq.iloc[-1] ** (1 / yrs) - 1) if eq.iloc[-1] > 0 else -1.0,
                sharpe=float(r.mean() / sd * math.sqrt(ANN)) if sd > 0 else float("nan"),
                maxdd=float((eq / eq.cummax() - 1).min()),
                turnover=float(turn.reindex(r.index).sum() / yrs) if turn is not None else float("nan"))


HEADER = "ts,lab,strategy,config_hash,universe,start,end,cagr,sharpe,maxdd,turnover,notes".split(",")


def log_trial(strategy, config, universe, start, end, m, notes=""):
    """Same format as app.ml_lab.core.log_trial but lab='catalog'. Single append write."""
    new = not REGISTRY.exists() or REGISTRY.stat().st_size == 0
    row = [time.strftime("%Y-%m-%dT%H:%M:%S"), "catalog", strategy, hashlib.md5(config.encode()).hexdigest()[:10],
           universe, str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date()), f"{m['cagr']:.4f}",
           f"{m['sharpe']:.3f}", f"{m['maxdd']:.4f}", f"{m['turnover']:.2f}", notes]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(HEADER)
    w.writerow(row)
    with open(REGISTRY, "a", newline="") as fh:
        fh.write(buf.getvalue())


def month_end_flags(index) -> np.ndarray:
    key = np.asarray(index.year * 100 + index.month)
    f = np.zeros(len(index), bool)
    f[:-1] = key[:-1] != key[1:]
    return f


def rsi(c: pd.DataFrame, n: int) -> pd.DataFrame:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(H, L, C, n=14):
    pc = C.shift()
    tr = np.maximum(np.maximum((H - L).values, (H - pc).abs().values), (L - pc).abs().values)
    return pd.DataFrame(tr, index=C.index, columns=C.columns).rolling(n, min_periods=n).mean()


def topn_portfolio(score, elig, N, reb, scale=None, sector=None, sec_cap=None, weights=None):
    """Hold top-N by score (higher better) among elig on rebalance days. score/elig: T x M arrays.
    scale: optional T-vector exposure multiplier. weights: optional T x M raw weights."""
    T, M = score.shape
    W = np.zeros((T, M))
    cur = np.zeros(M)
    secs = list(sector) if sector is not None else None
    for t in range(T):
        if reb[t]:
            s = np.where(elig[t] & np.isfinite(score[t]), score[t], -np.inf)
            order = np.argsort(-s)
            sel, cnt = [], {}
            for i in order:
                if not np.isfinite(s[i]) or len(sel) >= N:
                    break
                if secs is not None and sec_cap is not None:
                    k = secs[i]
                    cp = sec_cap.get(k) if isinstance(sec_cap, dict) else sec_cap
                    if cp is not None:
                        if cnt.get(k, 0) >= max(1, int(math.floor(cp * N))):
                            continue
                        cnt[k] = cnt.get(k, 0) + 1
                sel.append(i)
            cur = np.zeros(M)
            if sel:
                if weights is not None:
                    w = np.nan_to_num(weights[t, sel])
                    w = w / w.sum() if w.sum() > 0 else np.full(len(sel), 1 / len(sel))
                    cur[sel] = w * len(sel) / N
                else:
                    cur[sel] = 1.0 / N
        W[t] = cur * (scale[t] if scale is not None else 1.0)
    return W


def run_events(cand, rank, exit_sig, max_hold, max_pos, C, atr_, stop_mult=None, group=None, group_cap=None):
    """Generic long event engine (T x M arrays). At close t: process exits, then fill free slots from cand ordered
    by rank ascending. Positions are traded at open t+1 by sim()."""
    T, M = cand.shape
    W = np.zeros((T, M))
    held = np.zeros(M, bool); days = np.zeros(M, int); epx = np.zeros(M); eatr = np.zeros(M)
    for t in range(T):
        if held.any():
            days[held] += 1
            ex = exit_sig[t] | (days >= max_hold) | ~np.isfinite(C[t])
            if stop_mult is not None:
                ex = ex | (C[t] < epx - stop_mult * eatr)
            held &= ~ex
        free = max_pos - int(held.sum())
        if free > 0:
            idx = np.where(cand[t] & ~held)[0]
            if len(idx):
                idx = idx[np.argsort(rank[t, idx])]
                gc = {}
                if group is not None and group_cap is not None:
                    for h in np.where(held)[0]:
                        gc[group[h]] = gc.get(group[h], 0) + 1
                for i in idx:
                    if free <= 0:
                        break
                    if group is not None and group_cap is not None:
                        if gc.get(group[i], 0) >= group_cap:
                            continue
                        gc[group[i]] = gc.get(group[i], 0) + 1
                    held[i] = True; days[i] = 0; epx[i] = C[t, i]
                    eatr[i] = atr_[t, i] if atr_ is not None else 0.0
                    free -= 1
        W[t] = held / max_pos
    return W
