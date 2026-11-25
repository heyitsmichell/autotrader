"""Price history for the monthly sleeves (momentum, VAA).

Daily adjusted closes from Yahoo Finance in one batched download (blocking; call it through
``asyncio.to_thread``). Orders still go to Webull; Yahoo is only used to rank.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("app.sleeves")
SP500_FILE = Path(__file__).with_name("sp500.txt")


def sp500_symbols() -> list[str]:
    return [ln.strip().upper() for ln in SP500_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def daily_closes(symbols: list[str], period: str = "18mo") -> pd.DataFrame:
    """Adjusted daily closes, index ascending, one column per symbol (missing ones dropped)."""
    import yfinance as yf
    syms = sorted(set(symbols))
    if not syms:
        return pd.DataFrame()
    raw = yf.download(syms, period=period, interval="1d", auto_adjust=True, progress=False,
                      threads=True, group_by="column")
    if raw is None or raw.empty:
        return pd.DataFrame()
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(
        columns={"Close": syms[0]})
    close = close.dropna(how="all").sort_index()
    close.columns = [str(c).upper() for c in close.columns]
    missing = sorted(set(syms) - set(close.columns[close.notna().any()]))
    if missing:
        log.info("sleeve data: no Yahoo history for %d symbol(s): %s", len(missing), ", ".join(missing[:20]))
    return close.dropna(axis=1, how="all")


def daily_ohlc(symbols: list[str], period: str = "18mo") -> dict[str, pd.DataFrame]:
    """Adjusted daily opens and closes: {"open": DataFrame, "close": DataFrame}, same layout as daily_closes."""
    import yfinance as yf
    syms = sorted(set(symbols))
    empty = {"open": pd.DataFrame(), "close": pd.DataFrame()}
    if not syms:
        return empty
    raw = yf.download(syms, period=period, interval="1d", auto_adjust=True, progress=False,
                      threads=True, group_by="column")
    if raw is None or raw.empty:
        return empty
    out = {}
    for key, col in (("open", "Open"), ("close", "Close")):
        df = raw[col] if isinstance(raw.columns, pd.MultiIndex) else raw[[col]].rename(columns={col: syms[0]})
        df = df.dropna(how="all").sort_index()
        df.columns = [str(c).upper() for c in df.columns]
        out[key] = df.dropna(axis=1, how="all")
    return out


def daily_ohlcv(symbols: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """Adjusted daily bars: {"open","high","low","close","volume"} DataFrames, same layout as daily_closes."""
    import yfinance as yf
    syms = sorted(set(symbols))
    keys = (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume"))
    empty = {k: pd.DataFrame() for k, _ in keys}
    if not syms:
        return empty
    raw = yf.download(syms, period=period, interval="1d", auto_adjust=True, progress=False,
                      threads=True, group_by="column")
    if raw is None or raw.empty:
        return empty
    out = {}
    for key, col in keys:
        df = raw[col] if isinstance(raw.columns, pd.MultiIndex) else raw[[col]].rename(columns={col: syms[0]})
        df = df.dropna(how="all").sort_index()
        df.columns = [str(c).upper() for c in df.columns]
        out[key] = df.dropna(axis=1, how="all")
    return out
