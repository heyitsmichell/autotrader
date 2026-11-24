"""Pessimistic, configurable cost model for the backtester.

Costs are charged on both sides:
  - a flat commission per order,
  - a price-bps slippage,
  - an ATR-based dollar slippage (scales with volatility),
  - a market-impact term that grows with the order's share of the bar's dollar
    volume, capped by BACKTEST_MAX_IMPACT_BPS.

Defaults are deliberately harsher than a zero-commission retail account.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings


@dataclass(frozen=True)
class CostModel:
    commission_per_order: float = 1.0
    slippage_bps: float = 5.0
    atr_slippage_mult: float = 0.03
    impact_bps: float = 5.0
    max_impact_bps: float = 100.0
    # Regulatory sell-side fees (Webull US stocks: no commission, but SEC fee + FINRA TAF apply).
    # Not Settings fields: swing_backtest.py passes them explicitly.
    sec_fee_rate: float = 0.0       # fraction of sell notional (about 0.0000278)
    taf_per_share: float = 0.0      # FINRA TAF per share sold (about 0.000166)
    taf_max: float = 8.30           # FINRA TAF cap per sell order

    @classmethod
    def from_settings(cls, s: Settings) -> "CostModel":
        return cls(
            commission_per_order=s.backtest_commission_per_order,
            slippage_bps=s.backtest_slippage_bps,
            atr_slippage_mult=s.backtest_atr_slippage_mult,
            impact_bps=s.backtest_impact_bps,
            max_impact_bps=s.backtest_max_impact_bps,
        )

    def commission(self) -> float:
        return max(0.0, self.commission_per_order)

    def sell_fees(self, quantity: float, price: float) -> float:
        """SEC fee + FINRA TAF for one sell order (zero unless configured)."""
        if quantity <= 0 or price <= 0:
            return 0.0
        sec = self.sec_fee_rate * quantity * price
        taf = min(self.taf_max, self.taf_per_share * quantity) if self.taf_per_share > 0 else 0.0
        return max(0.0, sec) + taf

    def slippage_per_share(self, price: float, *, atr: float | None = None,
                           order_notional: float = 0.0,
                           dollar_volume: float = 0.0) -> float:
        """Dollar slippage per share for one fill."""
        if price <= 0:
            return 0.0
        participation_bps = 0.0
        if dollar_volume > 0 and order_notional > 0:
            participation_bps = min(self.max_impact_bps,
                                    order_notional / dollar_volume * 10_000.0)
        impact = min(self.max_impact_bps, self.impact_bps + participation_bps)
        per_share = price * (self.slippage_bps + impact) / 10_000.0
        if atr and atr > 0:
            per_share += atr * self.atr_slippage_mult
        return max(0.0, per_share)


def buy_fill(bar: dict, limit_price: float, model: CostModel, *,
             atr: float | None, quantity: int,
             dollar_volume: float) -> float | None:
    """Fill price for a BUY limit order on the next bar, or None if it did not fill.

    A buy limit fills when the bar trades at or below the limit. A gap down
    fills at the open (better); otherwise the fill is at the limit.
    """
    if limit_price <= 0 or bar.get("low") is None:
        return None
    if bar["low"] > limit_price:
        return None
    base = min(bar["open"], limit_price)
    slip = model.slippage_per_share(base, atr=atr,
                                   order_notional=quantity * base,
                                   dollar_volume=dollar_volume)
    return min(limit_price, base + slip)


def sell_fill(bar: dict, base_price: float, model: CostModel, *,
              atr: float | None, quantity: int, dollar_volume: float,
              limit_floor: float | None = None) -> float:
    """Sell fill after slippage. ``limit_floor`` keeps a take-profit limit honest."""
    slip = model.slippage_per_share(base_price, atr=atr,
                                   order_notional=quantity * base_price,
                                   dollar_volume=dollar_volume)
    price = base_price - slip
    if limit_floor is not None:
        price = max(limit_floor, price)
    return max(0.01, price)


def stop_exit_price(bar: dict, stop: float, model: CostModel, *,
                    atr: float | None, quantity: int, dollar_volume: float) -> float:
    """Stop-market fill: gap through the stop fills at the open, never at the stop."""
    base = bar["open"] if bar["open"] <= stop else stop
    return sell_fill(bar, base, model, atr=atr, quantity=quantity,
                     dollar_volume=dollar_volume)


def target_exit_price(bar: dict, target: float, model: CostModel, *,
                      atr: float | None, quantity: int, dollar_volume: float) -> float:
    """Take-profit limit: a gap above the target fills at the open, otherwise at target."""
    base = bar["open"] if bar["open"] >= target else target
    return sell_fill(bar, base, model, atr=atr, quantity=quantity,
                     dollar_volume=dollar_volume, limit_floor=target)


def round_trip_cost_pct(model: CostModel, price: float, *, atr: float | None,
                        notional: float, dollar_volume: float = 0.0) -> float:
    """Estimated cost of one full buy-then-sell, as a percent of the entry price.

    Slippage is charged on both fills and the commission on both orders, using the same
    numbers as the backtester. ``notional`` is the typical order size in dollars (it sets how
    much the flat commission weighs per share); ``dollar_volume`` adds market impact if known.
    """
    if price <= 0:
        return 0.0
    qty = max(1.0, notional / price) if notional > 0 else 1.0
    slip = model.slippage_per_share(price, atr=atr, order_notional=notional,
                                    dollar_volume=dollar_volume)
    return (2.0 * slip + 2.0 * model.commission() / qty) / price * 100.0
