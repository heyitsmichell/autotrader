"""Strategy variants as data: entry family + exit style + Settings overrides + optional ML filter."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

CHAMPION = "d_champion"

# exit style "d": live variant d (give-back off, no TP, no scale-out, no BE, no RSI exit, 2 ATR stop, 20d time stop)
EXIT_STYLES: dict[str, dict] = {
    "d": {"profit_giveback_pct": 0.0, "bracket_take_profit": False, "profit_scale_out_enabled": False,
          "profit_breakeven_after_r": 0.0, "profit_rsi_exhaustion": 0.0, "sl_atr_mult": 2.0,
          "backtest_max_hold_days": 20, "exit_max_hold_days": 20,
          "exit_on_trend_break_enabled": False, "backtest_exit_on_trend_break": False},
}
EXIT_STYLES["d_trail3"] = {**EXIT_STYLES["d"], "trailing_stop_enabled": True, "trailing_stop_atr_mult": 3.0,
                           "trailing_stop_activation_r": 1.0}
EXIT_STYLES["d_time30"] = {**EXIT_STYLES["d"], "backtest_max_hold_days": 30, "exit_max_hold_days": 30}
EXIT_STYLES["d_sl2.5"] = {**EXIT_STYLES["d"], "sl_atr_mult": 2.5}
EXIT_STYLES["d_sl1.5"] = {**EXIT_STYLES["d"], "sl_atr_mult": 1.5}
EXIT_STYLES["d_sl3"] = {**EXIT_STYLES["d"], "sl_atr_mult": 3.0}
EXIT_STYLES["d_time10"] = {**EXIT_STYLES["d"], "backtest_max_hold_days": 10, "exit_max_hold_days": 10}
EXIT_STYLES["d_time40"] = {**EXIT_STYLES["d"], "backtest_max_hold_days": 40, "exit_max_hold_days": 40}
EXIT_STYLES["chand3"] = dict(EXIT_STYLES["d"])     # + close < 22d highest close - 3 ATR, next open (harness)
EXIT_STYLES["native"] = dict(EXIT_STYLES["d"])     # family-native exit (harness); hold days per family below
NATIVE_HOLD_DAYS = {"connors": 14, "donchian55": 150, "dualma": 150}   # calendar days (engine counts calendar)

ENTRY_FAMILIES: dict[str, dict] = {
    "consensus": {},                                   # current live consensus rules
    "pullback": {"entry_pullback_gate": True},         # consensus + pullback-in-uptrend gate
    "breakout20_vol": {},                              # custom hook
    "momentum": {},                                    # custom hook: top-quintile 20d return
    "meanrev": {},                                     # custom hook: RSI<30 in uptrend
}
from .families import NEW_FAMILIES  # noqa: E402

for _f in NEW_FAMILIES:
    ENTRY_FAMILIES[_f] = {}
CUSTOM_ENTRIES = {"breakout20_vol", "momentum", "meanrev", *NEW_FAMILIES}


@dataclass(frozen=True)
class Variant:
    name: str
    entry: str
    exit_style: str
    ml_filter: bool = False
    extra_overrides: dict = field(default_factory=dict)
    description: str = ""
    regime: str = "none"      # none | spy200 | spy50
    max_pos: int = 0          # 0 = unlimited (live champion behaviour)

    def overrides(self) -> dict:
        ex = dict(EXIT_STYLES[self.exit_style])
        if self.exit_style == "native" and self.entry in NATIVE_HOLD_DAYS:
            ex.update(backtest_max_hold_days=NATIVE_HOLD_DAYS[self.entry],
                      exit_max_hold_days=NATIVE_HOLD_DAYS[self.entry])
        return {**ex, **ENTRY_FAMILIES[self.entry], **self.extra_overrides}

    def config_hash(self) -> str:
        blob = json.dumps({"entry": self.entry, "exit": self.exit_style, "ml": self.ml_filter,
                           "reg": self.regime, "mp": self.max_pos, "ov": self.overrides()}, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


def make(entry, exit_style="d", regime="none", max_pos=10, ml=False) -> Variant:
    """Programmatic variant for staged search; name encodes every knob."""
    name = f"{entry}|{regime}|{exit_style}|p{max_pos}|{'ml' if ml else 'noml'}"
    return Variant(name, entry, exit_style, ml, {}, "", regime, max_pos)


def _v(name, entry, exit_style="d", ml=False, desc=""):
    return Variant(name, entry, exit_style, ml, {}, desc)


REGISTRY: dict[str, Variant] = {v.name: v for v in [
    _v(CHAMPION, "consensus", desc="LIVE variant d: consensus entry, plain 2ATR stop + 20d time stop"),
    _v("pullback_d", "pullback", desc="ENTRY_PULLBACK_GATE on"),
    _v("breakout20_vol_d", "breakout20_vol", desc="close > prior 20d high, volume >= 1.5x avg, above SMA50"),
    _v("momentum_d", "momentum", desc="top-quintile 20d return across universe, above SMA50"),
    _v("meanrev_d", "meanrev", desc="RSI14<30 while above 200d mean"),
    _v("consensus_trail3", "consensus", "d_trail3", desc="d + 3 ATR trail after 1R"),
    _v("consensus_time30", "consensus", "d_time30", desc="d + 30-day time stop"),
    _v("consensus_sl2.5", "consensus", "d_sl2.5", desc="d + 2.5 ATR stop"),
    _v("consensus_ml_q80", "consensus", "d", True, desc="champion + MLP prob >= top quintile (walk-forward)"),
    _v("breakout20_vol_ml_q80", "breakout20_vol", "d", True, desc="breakout + MLP top quintile"),
]}


def get(names: str | list[str]) -> list[Variant]:
    if isinstance(names, str):
        names = list(REGISTRY) if names == "all" else [n for n in names.split(",") if n]
    out = []
    for n in names:
        if n not in REGISTRY:
            raise KeyError(f"unknown variant {n!r}; known: {', '.join(REGISTRY)}")
        out.append(REGISTRY[n])
    return out
