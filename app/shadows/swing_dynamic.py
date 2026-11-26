"""swing_dynamic shadow: "forward replay" of live swing variant d on a dynamic top-100 liquidity universe.

Once per trading day after the close the whole history since the shadow's start date is replayed with the
backtest engine on Yahoo bars (virtual money, no broker call of any kind). The result REPLACES the shadow's
equity / holdings / trades rows, so a rerun on the same day gives the same rows. Blocking work: call
``replay`` through ``asyncio.to_thread``. See docs/SHADOWS.md.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import tempfile
import threading
from datetime import date
from pathlib import Path

import pandas as pd

from ..sleeves import data

log = logging.getLogger("app.shadows")

SHADOW_ID = "swing_dynamic"
TOP_N = 100                  # universe size (tests shrink it)
WARMUP_DAYS = 300            # trading days replayed before the shadow start (no entries then)
MIN_ROWS = 260               # symbols with fewer bars in the window are dropped (engine needs 250)
LIQ_DAYS = 20
POSITION_FRACTION = 0.10
# live variant d (scripts/swing_backtest.py V2["d_gb0_plain_sl2_time20"]) + sizing at 0.5% risk
VARIANT_D = {"profit_giveback_pct": 0.0, "bracket_take_profit": False, "profit_scale_out_enabled": False,
             "profit_breakeven_after_r": 0.0, "profit_rsi_exhaustion": 0.0, "sl_atr_mult": 2.0,
             "backtest_max_hold_days": 20}
_LOCK = threading.Lock()


def pool_symbols(settings) -> list[str]:
    """S&P 500 list plus the live swing universe (without the SPY benchmark)."""
    from ..universe import get_predefined_universe
    live = get_predefined_universe("tech_semis") + [x.upper() for x in settings.watchlist]
    return sorted(set(data.sp500_symbols()) | set(live))


def history_period(start: date, today: date) -> str:
    need = (today - start).days + int(WARMUP_DAYS * 1.5) + 30
    return "2y" if need <= 720 else "5y"


def make_entry_hook(close: pd.DataFrame, volume: pd.DataFrame, start: date, top_n: int | None = None):
    """entry_allowed(day, symbol): top ``top_n`` by 20d average dollar volume as of the PREVIOUS trading day,
    and never before ``start``."""
    n = TOP_N if top_n is None else top_n
    dv = (close * volume).rolling(LIQ_DAYS, min_periods=LIQ_DAYS).mean()
    idx = close.index
    cache: dict[int, frozenset] = {}

    def allowed(day, symbol) -> bool:
        if day < start:
            return False
        pos = int(idx.searchsorted(pd.Timestamp(day))) - 1      # last row strictly before `day`
        if pos < 0:
            return False
        if pos not in cache:
            row = dv.iloc[pos].dropna().sort_values(ascending=False, kind="stable")
            cache[pos] = frozenset(row.index[:n])
        return symbol in cache[pos]
    return allowed


def _write_csvs(frames: dict[str, pd.DataFrame], symbols: list[str], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for s in symbols:
        df = pd.DataFrame({k: frames[k][s] for k in ("open", "high", "low", "close", "volume")}).dropna(
            subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0)
        df.index = df.index.strftime("%Y-%m-%d")
        df.index.name = "date"
        df.to_csv(out / f"{s}.csv")


def replay(settings, start: date, today: date, cap: float, *, top_n: int | None = None) -> dict:
    """Run the replay (blocking). Returns equity rows, open positions, trades, cash, peak, last prices.
    Raises on any failure (the caller keeps the old rows)."""
    pool = pool_symbols(settings)
    ohlcv = data.daily_ohlcv(pool + ["SPY"], history_period(start, today))
    close = ohlcv.get("close")
    if close is None or close.empty or "SPY" not in close.columns:
        raise RuntimeError("Yahoo download returned no usable data")
    cut = pd.Timestamp(today)
    frames = {k: v[v.index <= cut] for k, v in ohlcv.items()}       # never a bar after today
    spy_idx = frames["close"]["SPY"].dropna().index
    if len(spy_idx) < MIN_ROWS:
        raise RuntimeError("not enough SPY history")
    before = spy_idx[spy_idx < pd.Timestamp(start)]
    first = before[max(0, len(before) - WARMUP_DAYS)] if len(before) else spy_idx[0]
    frames = {k: v[v.index >= first] for k, v in frames.items()}
    ok = [s for s in frames["close"].columns if frames["close"][s].notna().sum() >= MIN_ROWS]
    if "SPY" not in ok:
        raise RuntimeError("SPY history too short")
    frames = {k: v[ok] for k, v in frames.items()}
    uni = [s for s in ok if s in set(pool)]
    hook = make_entry_hook(frames["close"][uni], frames["volume"][uni].fillna(0), start, top_n)
    tmp = Path(tempfile.mkdtemp(prefix="swing_dyn_"))
    try:
        _write_csvs(frames, ok, tmp)
        from ..backtest import engine
        s = dataclasses.replace(
            settings, backtest_data_dir=tmp, backtest_results_dir=tmp / "res", backtest_live_exits=True,
            backtest_ml_enabled=False, backtest_min_bars=250, backtest_max_gross_exposure_pct=100.0,
            backtest_initial_cash=float(cap), size_from_risk=True, risk_per_trade_pct=0.5,
            max_position_usd=cap * POSITION_FRACTION, max_order_usd=cap * POSITION_FRACTION,
            max_order_qty=1_000_000, backtest_commission_per_order=0.0, **VARIANT_D)
        grabbed: list = []
        def _capture(settings_, cash):    # capture the Portfolio (open positions are not in the result)
            p = engine.Portfolio(settings_, cash=cash)
            grabbed.append(p)
            return p
        res = engine.run_backtest(s, universe=uni, benchmark_symbol="SPY", csv_dir=tmp,
                                  sec_fee_rate=2.78e-05, taf_per_share=0.000166, entry_allowed=hook,
                                  portfolio_factory=_capture)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = min(len(res.equity_dates), len(res.equity_curve))
    eq = [(d, float(v)) for d, v in zip(res.equity_dates[:n], res.equity_curve[:n]) if d >= start]
    if not eq:
        raise RuntimeError("replay produced no equity rows")
    pos_raw = grabbed[0].positions if grabbed else {}
    positions = {sym: (float(p["qty"]), float(p["entry_price"])) for sym, p in pos_raw.items()}
    last = {sym: float(frames["close"][sym].dropna().iloc[-1]) for sym in positions if sym in frames["close"]}
    trades = []
    for t in res.trades:
        if t.entry_day < start:
            continue
        trades.append((t.entry_day, t.symbol, "BUY", t.quantity, t.entry_fill_price, "entry"))
        if t.exit_day:
            trades.append((t.exit_day, t.symbol, "SELL", t.quantity, t.exit_fill_price, t.exit_reason))
    for sym, p in pos_raw.items():       # positions still open: their BUY
        if p["entry_day"] >= start:
            trades.append((p["entry_day"], sym, "BUY", p.get("qty0", p["qty"]), p["entry_price"], "entry"))
    trades.sort(key=lambda x: (x[0], x[2] != "BUY", x[1]))
    cash = float(grabbed[0].cash) if grabbed else eq[-1][1] - sum(
        q * last.get(s, c) for s, (q, c) in positions.items())
    return {"equity": eq, "positions": positions, "trades": trades, "cash": cash,
            "peak": max([cap] + [v for _, v in eq]), "last_prices": last}


def apply(db, shadow: str, out: dict) -> None:
    """Replace this shadow's equity, holdings and trades with the replay result (own tables only)."""
    ns = db._shadow_ns
    c = db.conn
    for t in ("shadow_equity", "shadow_holdings", "shadow_trades"):
        c.execute(f"DELETE FROM {t} WHERE shadow=? AND ns=?", (shadow, ns))
    for d, v in out["equity"]:
        db.record_shadow_equity(_iso(d), shadow, round(v, 2))
    for sym, (q, cost) in out["positions"].items():
        db.set_shadow_holding(shadow, sym, q, round(cost, 4))
    for d, sym, side, q, px, reason in out["trades"]:
        c.execute("INSERT INTO shadow_trades (ns, ts, shadow, symbol, side, qty, price, reason) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (ns, f"{_iso(d)}T{'09:30:00' if side == 'BUY' else '16:00:00'}", shadow, sym, side,
                   float(q), round(float(px), 4), reason))


def _iso(d) -> str:
    """Dates arrive as date objects (in-process) or ISO strings (from the worker's JSON)."""
    return d if isinstance(d, str) else d.isoformat()


# ---------------------------------------------------------------------------------------------
# Worker process. The server has ONE CPU core: a ~60 s CPU-bound replay inside the trading process
# (even in a thread) competes for the GIL with the 1-second risk loop and the after-hours soft
# stops at 16:30 ET. So the replay runs in a separate, low-priority process (`nice -n 15` on Linux)
# and only the row writes happen in the trading process. The temporary engine.Portfolio swap in
# replay() is then also confined to that worker.
WORKER_TIMEOUT_SECONDS = 900


def _under_pytest() -> bool:
    import sys
    return "pytest" in sys.modules


async def run_replay(settings, start: date, today: date, cap: float) -> dict:
    """Replay in a worker process (in-process thread under tests, which patch replay())."""
    import asyncio
    if _under_pytest():
        return await asyncio.to_thread(replay, settings, start, today, cap)
    import json
    import os
    import sys
    import tempfile
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="shadow_replay_") as td:
        out_path = Path(td) / "out.json"
        cmd = [sys.executable, "-m", "app.shadows.swing_dynamic", start.isoformat(), today.isoformat(),
               repr(float(cap)), str(out_path)]
        if os.name != "nt":
            cmd = ["nice", "-n", "15"] + cmd
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(root), stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), WORKER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"replay worker timed out after {WORKER_TIMEOUT_SECONDS}s")
        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            raise RuntimeError(f"replay worker exit {proc.returncode}: {' | '.join(tail)[:300]}")
        return json.loads(out_path.read_text(encoding="utf-8"))


def _worker_main(argv: list[str]) -> int:
    import json
    from ..config import load_settings
    start, today, cap, out_path = date.fromisoformat(argv[0]), date.fromisoformat(argv[1]), float(argv[2]), argv[3]
    out = replay(load_settings(), start, today, cap)
    Path(out_path).write_text(json.dumps(out, default=_iso), encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_worker_main(sys.argv[1:]))
