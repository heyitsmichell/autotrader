"""Sleeve registry: static definitions and client-order-id helpers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SleeveDef:
    id: str
    name: str
    code: str
    color_slot: int
    description: str
    rebalance_rule: str
    expected: dict = field(default_factory=dict)


SLEEVES: tuple[SleeveDef, ...] = (
    SleeveDef("swing", "Swing", "w", 1,
              "Short-term swing trades on strong stocks, sized to risk a small slice of the account on each idea.",
              "The live engine trades daily; each position exits on a 2-ATR stop or after 20 trading days.",
              {"cagr": 0.239, "sharpe": 1.05, "max_dd": -0.248,
               "window": "2022-11..2025-11 backtest, $500k, 0.5% risk"}),
    SleeveDef("momentum", "Momentum", "m", 2,
              "Holds the 10 stocks with the strongest 12-month-minus-1-month price trend, equally weighted.",
              "Re-ranks monthly and trades to equal weights, selling names that drop out of the top 10.",
              {"cagr": 0.268, "sharpe": 0.92, "max_dd": -0.38,
               "window": "2011-2025 backtest (hindsight-biased universe)"}),
    SleeveDef("putwrite", "Put-write", "p", 3,
              "Sells cash-secured put options about 30 days out to collect premium while waiting to buy at a discount.",
              "Opens a new put about monthly once the last one expires; closes early if it is in the money on expiry day.",
              {"cagr": 0.107, "sharpe": 0.96, "max_dd": -0.186,
               "window": "2011-2025 modelled (Black-Scholes)"}),
    SleeveDef("vaa", "Defensive (VAA)", "v", 4,
              "Rotates among global ETFs, moving into bonds when too many risk assets show falling momentum.",
              "Recomputes at month-end and trades to the new target mix of ETFs.",
              {"cagr": 0.064, "sharpe": 0.78, "max_dd": -0.239, "window": "2011-2025 backtest"}),
)

NON_SWING = ("momentum", "putwrite", "vaa")


def get(sleeve_id: str) -> SleeveDef:
    for s in SLEEVES:
        if s.id == sleeve_id:
            return s
    raise KeyError(sleeve_id)


def by_code(code: str) -> SleeveDef | None:
    return next((s for s in SLEEVES if s.code == code), None)


def coid_for(sleeve_id: str) -> str:
    """Client order id: 's' + sleeve code + 30 hex chars (32 total)."""
    return "s" + get(sleeve_id).code + uuid.uuid4().hex[:30]


def sleeve_from_coid(coid: str) -> str | None:
    if not coid or len(coid) != 32 or coid[0] != "s":
        return None
    s = by_code(coid[1])
    return s.id if s else None
