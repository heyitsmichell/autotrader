"""Data loading, point-in-time features, labels, walk-forward splits, metrics, registry."""
from __future__ import annotations
import csv, hashlib, io, math, time
from pathlib import Path
from statistics import NormalDist
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "data" / "bars_yahoo_ext"
OUT = ROOT / "data" / "experiments" / "ml"
REGISTRY = ROOT / "data" / "experiments" / "trials_registry.csv"
RESEARCH_END = pd.Timestamp("2025-11-23")   # locked holdout starts after this
EVAL_START = pd.Timestamp("2011-01-03")
PRIOR_TRIALS = 304
ROT_SR_VAR = 1.728e-4
ANN = 252
FEATURES = ["r1m", "r3m", "r6m", "r12m", "mom121", "rev1m", "vol20", "vol60", "beta", "dist52h",
            "rsi14", "voltrend", "secrel"]


def _read(p: Path) -> pd.DataFrame:
    d = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    d = d[~d.index.duplicated()].sort_index()
    return d[d.index <= RESEARCH_END]          # holdout never kept in memory


class Panel:
    def __init__(self, ext: Path = EXT):
        files = [f for f in sorted(ext.glob("*.csv")) if not f.name.startswith("sp500_list")]
        raw = {f.stem: _read(f) for f in files}
        idx = raw["SPY"].index
        self.index = idx
        self.syms = sorted(raw)
        g = lambda c: pd.DataFrame({s: raw[s][c].reindex(idx) for s in self.syms})
        self.open, self.close, self.volume = g("open"), g("close"), g("volume")
        lst = pd.read_csv(sorted(ext.glob("sp500_list_*.csv"))[-1])
        lst["Symbol"] = lst["Symbol"].astype(str).str.replace(".", "-", regex=False)
        lst = lst[lst["Symbol"].isin(raw)]
        self.stocks = sorted(lst["Symbol"])
        self.sector = dict(zip(lst["Symbol"], lst["GICS Sector"]))


def compute_features(close: pd.DataFrame, volume: pd.DataFrame, spy: pd.Series, sector: dict) -> dict:
    """Daily T x M features; row t uses data <= t only."""
    c = close
    r1 = c.pct_change(fill_method=None)
    f = {}
    for k, n in (("r1m", 21), ("r3m", 63), ("r6m", 126), ("r12m", 252)):
        f[k] = c / c.shift(n) - 1
    f["mom121"] = c.shift(21) / c.shift(252) - 1
    f["rev1m"] = -f["r1m"]
    f["vol20"] = r1.rolling(20, min_periods=20).std()
    f["vol60"] = r1.rolling(60, min_periods=60).std()
    sr = spy.pct_change(fill_method=None)
    ex, ey = r1.rolling(126, min_periods=126).mean(), sr.rolling(126, min_periods=126).mean()
    exy = r1.mul(sr, axis=0).rolling(126, min_periods=126).mean()
    f["beta"] = (exy.sub(ex.mul(ey, axis=0))).div(sr.rolling(126, min_periods=126).var(ddof=0), axis=0)
    f["dist52h"] = c / c.rolling(252, min_periods=252).max() - 1
    up = r1.clip(lower=0).rolling(14, min_periods=14).mean()
    dn = (-r1.clip(upper=0)).rolling(14, min_periods=14).mean()
    f["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    v20, v60 = volume.rolling(20, min_periods=20).mean(), volume.rolling(60, min_periods=60).mean()
    f["voltrend"] = v20 / v60.replace(0, np.nan) - 1
    m = f["mom121"]
    rel = m.copy()
    secs = pd.Series({s: sector.get(s) for s in c.columns})
    for sc in secs.dropna().unique():
        cols = secs.index[secs == sc]
        rel[cols] = m[cols].sub(m[cols].median(axis=1), axis=0)
    f["secrel"] = rel
    return f


def month_decisions(index: pd.DatetimeIndex) -> np.ndarray:
    """Positions of last trading day of each month (final row excluded): decide at close, trade next open."""
    key = np.asarray(index.year * 100 + index.month)
    return np.where(key[:-1] != key[1:])[0]


def week_decisions(index: pd.DatetimeIndex) -> np.ndarray:
    iso = index.isocalendar()
    key = np.asarray(iso["year"].astype(int) * 100 + iso["week"].astype(int))
    return np.where(key[:-1] != key[1:])[0]


def forward_labels(open_: pd.DataFrame, dec: np.ndarray, step: int = 1) -> np.ndarray:
    """Label for decision i at position p: return open[p+1] -> open[dec[i+step]+1] (executable, t -> next period).
    Decisions without a full next period are NaN. Returns (n_dec, M)."""
    O = open_.values
    out = np.full((len(dec), O.shape[1]), np.nan)
    for i in range(len(dec) - step):
        a, b = dec[i] + 1, dec[i + step] + 1
        with np.errstate(all="ignore"):
            out[i] = O[b] / O[a] - 1
    return out


def label_end_pos(dec: np.ndarray, step: int = 1) -> np.ndarray:
    e = np.full(len(dec), -1)
    e[:-step] = dec[step:] + 1
    return e


def walk_forward_splits(dec: np.ndarray, index: pd.DatetimeIndex, first_test_year: int, last_year: int,
                        embargo: int = 21, step: int = 1):
    """Expanding window, retrain each calendar year. A train decision is allowed only if its label end
    position + embargo trading days <= first test position. Yields (year, train_idx, test_idx)."""
    ends = label_end_pos(dec, step)
    yrs = np.asarray(index.year)[dec]
    for y in range(first_test_year, last_year + 1):
        te = np.where(yrs == y)[0]
        if len(te) == 0:
            continue
        first_pos = dec[te[0]] + 1
        tr = np.where((ends >= 0) & (ends + embargo <= first_pos))[0]
        yield y, tr, te


def xs_rank(a: np.ndarray) -> np.ndarray:
    """Per-row rank to [-0.5, 0.5]; NaN stays NaN. a: (n, M)."""
    return pd.DataFrame(a).rank(axis=1, pct=True).values - 0.5


# ---------------- metrics / registry ----------------
def metrics(r: pd.Series, turn: pd.Series | None = None, start=None) -> dict:
    if start is not None:
        r = r[r.index >= start]
        turn = turn[turn.index >= start] if turn is not None else None
    yrs = len(r) / ANN
    eq = (1 + r).cumprod()
    sd = r.std()
    return dict(cagr=float(eq.iloc[-1] ** (1 / yrs) - 1),
                sharpe=float(r.mean() / sd * math.sqrt(ANN)) if sd > 0 else float("nan"),
                maxdd=float((eq / eq.cummax() - 1).min()),
                turnover=float(turn.sum() / yrs) if turn is not None else float("nan"),
                daily_sr=float(r.mean() / sd) if sd > 0 else float("nan"))


def deflated_sharpe(r: pd.Series, n_trials: int, sr_var: float) -> float:
    N = NormalDist()
    x = r.dropna().values
    T = len(x)
    if T < 50 or x.std() == 0:
        return float("nan")
    sr = x.mean() / x.std()
    g = 0.5772156649
    sr0 = math.sqrt(max(sr_var, 1e-12)) * ((1 - g) * N.inv_cdf(1 - 1 / n_trials) + g * N.inv_cdf(1 - 1 / (n_trials * math.e)))
    z = (x - x.mean()) / x.std()
    sk, ku = float((z ** 3).mean()), float((z ** 4).mean())
    den = math.sqrt(max(1 - sk * sr + (ku - 1) / 4 * sr ** 2, 1e-9))
    return float(N.cdf((sr - sr0) * math.sqrt(T - 1) / den))


HEADER = "ts,lab,strategy,config_hash,universe,start,end,cagr,sharpe,maxdd,turnover,notes".split(",")


def log_trial(strategy: str, config: str, universe: str, start, end, m: dict, notes: str = "") -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    new = not REGISTRY.exists() or REGISTRY.stat().st_size == 0
    row = [time.strftime("%Y-%m-%dT%H:%M:%S"), "ml", strategy, hashlib.md5(config.encode()).hexdigest()[:10], universe,
           str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date()), f"{m['cagr']:.4f}", f"{m['sharpe']:.3f}",
           f"{m['maxdd']:.4f}", f"{m['turnover']:.2f}", notes]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(HEADER)
    w.writerow(row)
    with open(REGISTRY, "a", newline="") as fh:       # single append write, never rewrite
        fh.write(buf.getvalue())


def n_trials() -> int:
    if not REGISTRY.exists():
        return PRIOR_TRIALS
    with open(REGISTRY) as fh:
        return PRIOR_TRIALS + max(0, sum(1 for _ in fh) - 1)
