"""Shadow strategy definitions (docs/SHADOWS.md)."""
from __future__ import annotations

from dataclasses import dataclass, field

VAA_G4_OFFENSE = ["SPY", "EFA", "EEM", "AGG"]
VAA_G4_DEFENSE = ["LQD", "IEF", "SHY"]


@dataclass(frozen=True)
class ShadowDef:
    id: str
    name: str
    description: str
    rule: str
    kind: str                       # "sp500" | "vaa_g4" | "swing_dynamic": which universe / engine
    expected: dict | None = field(default=None)

    def symbols(self) -> list[str]:
        if self.kind == "sp500":
            from ..sleeves import data
            return data.sp500_symbols()
        if self.kind == "amihud_quality":
            from ..sleeves import data
            return data.sp500_symbols()
        if self.kind == "swing_dynamic":
            return []                   # replayed daily, no monthly targets (app/shadows/swing_dynamic.py)
        return list(dict.fromkeys(VAA_G4_OFFENSE + VAA_G4_DEFENSE))

    @property
    def monthly(self) -> bool:
        return self.kind != "swing_dynamic"


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
SHADOWS = SHADOWS + (AMIHUD_QUALITY,)
IDS = [d.id for d in SHADOWS]
_BY_ID = {d.id: d for d in SHADOWS}


def get(shadow_id: str) -> ShadowDef:
    try:
        return _BY_ID[shadow_id]
    except KeyError:
        raise KeyError(f"unknown shadow {shadow_id!r}") from None
