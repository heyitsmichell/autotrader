"""Technical indicators computed in code, so the analyst gets exact numbers instead
of doing arithmetic over hundreds of bars (FinAgent-style tool augmentation).
Definitions follow StockCharts ChartSchool (Wilder smoothing for RSI, ATR, ADX).

`feature_series` is the single source of truth: live signals use its last row and
the ML model trains on every row, so training and live features always match.
"""

from __future__ import annotations

import math
import statistics

from .broker import bar_day


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2 / (n + 1)
    prev = None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        prev = sum(values[i - n + 1:i + 1]) / n if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(bars: list[dict], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    if len(trs) < n:
        return out
    value = sum(trs[:n]) / n
    out[n - 1] = value
    for i in range(n, len(trs)):
        value = (value * (n - 1) + trs[i]) / n
        out[i] = value
    return out


def adx(bars: list[dict], n: int = 14) -> list[float | None]:
    """Average Directional Index (trend strength, 0-100), Wilder smoothing."""
    size = len(bars)
    out: list[float | None] = [None] * size
    if size < 2 * n + 1:
        return out
    trs, pdm, mdm = [], [], []
    for i in range(1, size):
        h, low, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        up, down = h - bars[i - 1]["high"], bars[i - 1]["low"] - low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))

    def dx(tr_s: float, p_s: float, m_s: float) -> float:
        if tr_s <= 0:
            return 0.0
        pdi, mdi = 100 * p_s / tr_s, 100 * m_s / tr_s
        return 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)

    tr_s, p_s, m_s = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = [dx(tr_s, p_s, m_s)]                     # dxs[j] belongs to bar n + j
    for k in range(n, len(trs)):
        tr_s += trs[k] - tr_s / n
        p_s += pdm[k] - p_s / n
        m_s += mdm[k] - m_s / n
        dxs.append(dx(tr_s, p_s, m_s))
    if len(dxs) < n:
        return out
    value = sum(dxs[:n]) / n
    out[2 * n - 1] = value
    for j in range(n, len(dxs)):
        value = (value * (n - 1) + dxs[j]) / n
        out[n + j] = value
    return out


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else (a / b - 1) * 100


def feature_series(bars: list[dict]) -> list[dict]:
    """Setup features for every bar (index i uses bars[0..i] only, no look-ahead)."""
    size = len(bars)
    if size < 2:
        return []
    closes = [b["close"] for b in bars]
    s20, s50, e20 = sma(closes, 20), sma(closes, 50), ema(closes, 20)
    r14, a14, x14 = rsi(closes, 14), atr(bars, 14), adx(bars, 14)
    logret = [0.0] + [math.log(closes[i] / closes[i - 1])
                      if closes[i] > 0 and closes[i - 1] > 0 else 0.0 for i in range(1, size)]
    atr_pct = [a / c * 100 if a and c else None for a, c in zip(a14, closes)]

    rows: list[dict] = []
    below = 0
    for i in range(size):
        c = closes[i]
        below = below + 1 if s20[i] is not None and c < s20[i] else 0
        win = bars[max(0, i - 9):i + 1]
        high10, low10 = max(b["high"] for b in win), min(b["low"] for b in win)
        vols = [b["volume"] for b in bars[max(0, i - 20):i] if b["volume"]]
        hist = [v for v in atr_pct[max(0, i - 99):i + 1] if v is not None]
        rets = logret[i - 19:i + 1] if i >= 20 else []
        f = {
            "close": c,
            "date": bar_day(bars[i]["time"]).isoformat(),
            "sma20": s20[i], "sma50": s50[i], "ema20": e20[i],
            "rsi14": r14[i], "adx14": x14[i],
            "atr14": a14[i], "atr_pct": atr_pct[i],
            "atr_pct_rank": (sum(v <= atr_pct[i] for v in hist) / len(hist))
            if atr_pct[i] is not None and len(hist) >= 20 else None,
            "rv20": statistics.pstdev(rets) * math.sqrt(252) * 100 if len(rets) == 20 else None,
            "pct_vs_sma20": _pct(c, s20[i]), "pct_vs_sma50": _pct(c, s50[i]),
            "sma20_slope_pct": _pct(s20[i], s20[i - 5]) if i >= 5 else None,
            "high10": high10, "low10": low10, "pct_from_high10": _pct(c, high10),
            "chg_5d_pct": _pct(c, closes[i - 5]) if i >= 5 else None,
            "chg_20d_pct": _pct(c, closes[i - 20]) if i >= 20 else None,
            "days_below_sma20": below,
            "vol_vs_20d_avg": (bars[i]["volume"] / (sum(vols) / len(vols))) if vols else None,
        }
        rows.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()})
    return rows


def features(bars: list[dict]) -> dict:
    """Numbers that describe the setup on the latest daily bar."""
    rows = feature_series(bars)
    return rows[-1] if rows else {}


def summarize(symbol: str, bars: list[dict]) -> str:
    if len(bars) < 21:
        return f"Not enough daily bars for {symbol} ({len(bars)} found, need 21+)."
    f = features(bars)

    def n(v, fmt="{:.2f}"):
        return "n/a" if v is None else fmt.format(v)

    trend = []
    if f["sma20"] is not None:
        trend.append(f"close {'above' if f['close'] > f['sma20'] else 'below'} SMA20")
    if f["sma20"] is not None and f["sma50"] is not None:
        trend.append(f"SMA20 {'above' if f['sma20'] > f['sma50'] else 'below'} SMA50")
    return "\n".join([
        f"{symbol} daily indicators ({len(bars)} bars, latest {f['date']})",
        f"Close {n(f['close'])} | SMA20 {n(f['sma20'])} ({n(f['pct_vs_sma20'], '{:+.2f}')}%) | "
        f"SMA50 {n(f['sma50'])} ({n(f['pct_vs_sma50'], '{:+.2f}')}%) | EMA20 {n(f['ema20'])}",
        f"RSI14 {n(f['rsi14'], '{:.1f}')} | ADX14 {n(f['adx14'], '{:.1f}')} | "
        f"ATR14 {n(f['atr14'])} ({n(f['atr_pct'])}% of price, "
        f"{n(f['atr_pct_rank'], '{:.0%}')} percentile of 100 days)",
        f"10-day high {n(f['high10'])} ({n(f['pct_from_high10'], '{:+.2f}')}% from it) | "
        f"10-day low {n(f['low10'])}",
        f"Change: 5d {n(f['chg_5d_pct'], '{:+.2f}')}% | 20d {n(f['chg_20d_pct'], '{:+.2f}')}% | "
        f"SMA20 slope 5d {n(f['sma20_slope_pct'], '{:+.2f}')}% | "
        f"realized vol 20d {n(f['rv20'], '{:.1f}')}%",
        f"Trend: {', '.join(trend) or 'n/a'} | closes below SMA20 in a row: {f['days_below_sma20']}",
        f"Volume today vs 20-day average: {n(f['vol_vs_20d_avg'], '{:.2f}')}x",
    ])
