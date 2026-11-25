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
    kind: str                       # "sp500" | "vaa_g4": which universe
    expected: dict | None = field(default=None)

    def symbols(self) -> list[str]:
        if self.kind == "sp500":
            from ..sleeves import data
            return data.sp500_symbols()
        return list(dict.fromkeys(VAA_G4_OFFENSE + VAA_G4_DEFENSE))


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
)
IDS = [d.id for d in SHADOWS]
_BY_ID = {d.id: d for d in SHADOWS}


def get(shadow_id: str) -> ShadowDef:
    try:
        return _BY_ID[shadow_id]
    except KeyError:
        raise KeyError(f"unknown shadow {shadow_id!r}") from None
