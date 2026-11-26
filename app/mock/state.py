"""Persisted mock account state (data/mock_state.json, overridable via MOCK_STATE_FILE)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .options import option_price
from .quotes import price_of

log = logging.getLogger(__name__)


def _state_file() -> Path:
    """Get the state file path, evaluating env vars at call time (not import time)."""
    return Path(os.environ.get("MOCK_STATE_FILE")
                or Path(os.environ.get("TRADER_DATA_DIR")
                        or Path(__file__).resolve().parent.parent.parent / "data") / "mock_state.json")


# For backward compatibility - STATE_FILE as a property-like object
class _StateFileProxy:
    def __str__(self) -> str:
        return str(_state_file())

    def __fspath__(self):
        return _state_file()

    def __getattr__(self, name):
        # Delegate to the actual Path object
        return getattr(_state_file(), name)


STATE_FILE = _StateFileProxy()
ACCOUNT_ID = "MOCK0001"
START_CASH = 25_000.0
FRACTIONAL_ELIGIBLE = {"AAPL", "MSFT", "NVDA", "SPY", "QQQ", "AMZN", "GOOGL", "META", "TSLA"}
DISCLAIMER = ("⚠️ Disclaimer: Simulated data from the mock broker. "
              "Not real market data.\n\n")
READ_ONLY = {"readOnlyHint": True}
OPEN_STATUSES = {"SUBMITTED", "PENDING"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")



# -- state ---------------------------------------------------------------------
def load() -> dict:
    sf = _state_file()
    state = None
    if sf.exists():
        try:
            state = json.loads(sf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bak = sf.with_name(sf.name + ".bak")
            try:
                state = json.loads(bak.read_text(encoding="utf-8"))
                log.warning("mock state %s corrupt; restored from %s", sf, bak)
            except (OSError, json.JSONDecodeError):
                log.warning("mock state %s corrupt and no usable .bak; starting fresh", sf)
    if state is None:
        state = {"cash": START_CASH, "positions": {}, "orders": [], "day": "",
                 "day_start_equity": START_CASH, "option_positions": {}}
    today = datetime.now(timezone.utc).date().isoformat()
    if state["day"] != today:
        state["day"] = today
        state["day_start_equity"] = equity(state)
    # Backward compat
    if "option_positions" not in state:
        state["option_positions"] = {}
    return state


def save(state: dict) -> None:
    sf = _state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_name(sf.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if sf.exists():
        try:
            json.loads(sf.read_text(encoding="utf-8"))      # never overwrite .bak with a corrupt file
            shutil.copyfile(sf, sf.with_name(sf.name + ".bak"))
        except (OSError, ValueError):
            pass
    os.replace(tmp, sf)


def equity(state: dict) -> float:
    eq = state["cash"]
    for s, p in state["positions"].items():
        eq += p["qty"] * price_of(s)
    for sym, legs in state.get("option_positions", {}).items():
        for leg in legs:
            # Option value = qty * 100 * mark (mid of bid/ask)
            bid, ask = option_price(leg["underlying"], leg["strike"], leg["expiration"], leg["opt_type"])
            mark = (bid + ask) / 2
            eq += leg["qty"] * 100 * mark
    return eq


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _set(order: dict, status: str) -> None:
    order["status"] = status
    order["updated_at"] = _now_iso()


def _children(state: dict, order: dict) -> list[dict]:
    return [o for o in state["orders"] if o.get("parent") == order["client_order_id"]]
