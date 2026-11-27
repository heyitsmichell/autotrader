"""Shadow strategy definitions (docs/SHADOWS.md)."""
from __future__ import annotations

from dataclasses import dataclass, field

VAA_G4_OFFENSE = ["SPY", "EFA", "EEM", "AGG"]
VAA_G4_DEFENSE = ["LQD", "IEF", "SHY"]
# crypto trend: Yahoo signal symbol -> the spot ETF that is (virtually) traded
CRYPTO_ETF = {"BTC-USD": "IBIT", "ETH-USD": "ETHA"}


@dataclass(frozen=True)
class OptionSpec:
    """One-leg option shadow (app/shadows/options.py). Virtual fills from real Webull option quotes.

    side "short" is only allowed for PUT (cash-secured: contracts = cash / (strike x 100)); a naked short
    call is refused. side "long" spends ``budget_pct`` of equity on premium per cycle.
    gate: None (always open a new leg) or (yahoo_symbol, rule) from strategies.trend_signal; a new leg is
    only opened while the gate is >= 0.5. close_on_gate_off closes an open leg when the gate turns off.
    """
    underlying: str
    right: str = "PUT"               # PUT | CALL
    side: str = "short"              # short | long
    delta: float = 0.30              # target |delta|
    dte: int = 30
    dte_min: int = 21
    dte_max: int = 45
    est_vol: float = 0.25            # only to centre the strikes fetched before real deltas are known
    gate: tuple[str, str] | None = None
    close_on_gate_off: bool = False
    budget_pct: float = 0.05         # long legs only

    def __post_init__(self):
        if self.right not in ("PUT", "CALL") or self.side not in ("short", "long"):
            raise ValueError(f"bad option spec {self.right}/{self.side}")
        if self.side == "short" and self.right == "CALL":
            raise ValueError("naked short calls are not supported")


@dataclass(frozen=True)
class ShadowDef:
    id: str
    name: str
    description: str
    rule: str
    kind: str                       # "sp500" | "vaa_g4" | "swing_dynamic" | "amihud_quality" | "crypto_trend" | "option"
    expected: dict | None = field(default=None, hash=False, compare=False)
    band: float = 0.0               # target-weight shadows: skip a trade when |target - current| < band
    option: OptionSpec | None = None

    def symbols(self) -> list[str]:
        if self.kind == "sp500":
            from ..sleeves import data
            return data.sp500_symbols()
        if self.kind == "amihud_quality":
            from ..sleeves import data
            return data.sp500_symbols()
        if self.kind == "swing_dynamic":
            return []                   # replayed daily, no monthly targets (app/shadows/swing_dynamic.py)
        if self.kind == "crypto_trend":
            return list(CRYPTO_ETF)     # Yahoo signal symbols; the traded ETFs are CRYPTO_ETF values
        if self.kind == "option":
            return [self.option.gate[0]] if self.option and self.option.gate else []
        return list(dict.fromkeys(VAA_G4_OFFENSE + VAA_G4_DEFENSE))

    @property
    def mode(self) -> str:
        """"replay" (daily forward replay), "option" (one option leg) or "target" (target weights)."""
        return {"swing_dynamic": "replay", "option": "option"}.get(self.kind, "target")

    @property
    def monthly(self) -> bool:
        """Target-weight shadow (rebalanced monthly, every N days, or daily for crypto_trend)."""
        return self.mode == "target"

    @property
    def daily(self) -> bool:
        return self.kind in ("crypto_trend", "option")


SHADOWS: tuple[ShadowDef, ...] = (
    ShadowDef("mom_skip1w", "Momentum, skip 1 week",
              "Holds the 10 S&P 500 stocks with the strongest 12-month trend, ignoring the most recent week.",
              "Monthly: score = close 5 days ago / close 252 days ago - 1, top 10 equal weight.", "sp500"),
    ShadowDef("overnight_mom", "Overnight momentum",
              "Holds the 10 S&P 500 stocks whose price rose most between close and next open over a year.",
              "Monthly: score = sum of log(open / previous close) over the last 252 days, top 10 equal weight.",
              "sp500"),
    ShadowDef("vaa_g4", "VAA-G4",
              "Vigilant asset allocation with 4 offensive funds and 3 defensive bonds, holding one fund.",
              "Monthly: offense SPY EFA EEM AGG, defense LQD IEF SHY, B=1, top 1, 13612W momentum.", "vaa_g4"),
    ShadowDef("swing_dynamic", "Swing, dynamic top-100",
              "The live swing strategy, but it may only buy the 100 most traded S&P 500 and live-list stocks "
              "(by average dollar volume over 20 days, ranked from the previous day). Recomputed once a day "
              "after the close.",
              "Daily replay after 16:30 ET: live swing variant d (2 ATR stop, 20-day time stop, 0.5% risk per "
              "trade, max 10% per position) on Yahoo bars; entries only in the top 100 by 20-day average "
              "dollar volume of (S&P 500 + live universe), ranked on the previous day's data; no entries "
              "before the shadow started.", "swing_dynamic"),
)
AMIHUD_QUALITY = ShadowDef(
    "amihud_quality", "Amihud illiquidity + quality",
    "Holds the 20 least liquid (highest price impact per dollar traded) S&P 500 stocks among those with "
    "12-month return in the top 50%, rebalanced every 21 trading days. Forward test on live data, so it is "
    "survivorship-free; the backtest it came from used today's S&P 500 members and is survivorship-biased.",
    "Every 21 trading days: Amihud = mean(|daily return| / dollar volume) over 90 days; eligible = 12-month "
    "return percentile >= 0.5 (proxy for quality); top 20 by Amihud, equal weight. Signal at close, trade next open.",
    "amihud_quality")
CRYPTO_TREND = ShadowDef(
    "crypto_trend", "Crypto trend (IBIT/ETHA)",
    "Holds the Bitcoin and Ether spot ETFs only while their trend is up, scaled down when prices swing a lot; "
    "cash otherwise. Spot only, no leverage (the Botsfolio 'cycle trader' idea). Study: "
    "data/experiments/crypto_trend/REPORT.md.",
    "Daily after 09:45 ET on the last completed Yahoo close (NYSE days only): for BTC-USD and ETH-USD, "
    "signal = mean of 8 tests (price > its 50/100/150/200-day average; price > price 21/63/126/252 days ago); "
    "weight = 50% x signal x min(1, 40% / 21-day volatility) in IBIT / ETHA; rest cash. "
    "Trades only when a weight moves by 10 points or goes to 0.",
    "crypto_trend", band=0.10,
    expected={"cagr": 36.6, "sharpe": 1.21, "max_dd": -34.3, "window": "2018-07..2025-11, BTC/ETH proxy, model"})
IBIT_PUT_TREND = ShadowDef(
    "ibit_put_trend", "IBIT put-write, trend gated",
    "Sells a cash-secured 30-delta IBIT put about 30 days out, but only while the Bitcoin trend is up; buys it "
    "back when the trend turns. Virtual fills on real Webull IBIT option quotes.",
    "Open when the BTC trend ensemble (same 8 tests as crypto_trend) >= 0.5: sell the 21-45 DTE IBIT put "
    "nearest 30 DTE and 0.30 delta at mid - 25% of the spread, contracts = cash / (strike x 100). Close at "
    "mid + 25% of the spread when the gate turns off; otherwise hold to expiry (cash-settled at intrinsic).",
    "option", option=OptionSpec("IBIT", "PUT", "short", 0.30, 30, est_vol=0.50, gate=("BTC-USD", "ens_all"),
                                close_on_gate_off=True),
    expected={"cagr": 18.9, "sharpe": 0.97, "max_dd": -39.1, "window": "2016..2025-11, Black-Scholes + DVOL"})
IBIT_PUT_ALWAYS = ShadowDef(
    "ibit_put_always", "IBIT put-write, always on",
    "Control for the gated version: sells a cash-secured 30-delta IBIT put about 30 days out every cycle, "
    "regardless of trend.",
    "Every cycle: sell the 21-45 DTE IBIT put nearest 30 DTE and 0.30 delta at mid - 25% of the spread, "
    "hold to expiry (cash-settled at intrinsic), reopen the next trading day.",
    "option", option=OptionSpec("IBIT", "PUT", "short", 0.30, 30, est_vol=0.50),
    expected={"cagr": 13.6, "sharpe": 0.58, "max_dd": -63.4, "window": "2016..2025-11, Black-Scholes + DVOL"})
SHADOWS = SHADOWS + (AMIHUD_QUALITY, CRYPTO_TREND, IBIT_PUT_TREND, IBIT_PUT_ALWAYS)
IDS = [d.id for d in SHADOWS]
_BY_ID = {d.id: d for d in SHADOWS}


def get(shadow_id: str) -> ShadowDef:
    try:
        return _BY_ID[shadow_id]
    except KeyError:
        raise KeyError(f"unknown shadow {shadow_id!r}") from None
