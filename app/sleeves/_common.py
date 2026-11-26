"""Shared imports, constants and helpers for the SleeveManager mixins."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from statistics import NormalDist

from ..broker import BrokerError
from ..market import is_trading_day, market_is_open, now_et, now_utc
from ..portfolio import ACTIVE, parse_orders, parse_position_rows
from ..engine.util import parse_occ
from . import analytics, registry
from .strategies import momentum, putwrite, rebalance, vaa

PUT_SETTLE_MAX_TRIES_PER_DAY = 3
log = logging.getLogger("app.sleeves")
_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")
OPEN_STATUSES = ("submitting", "working", "partial")
TICK_SECONDS = 60.0
CANCEL_AFTER_SECONDS = 180.0       # an unfilled marketable limit is cancelled and re-tried next tick
GIVE_UP_SECONDS = 600.0            # never seen at the broker -> failed
OPTION_FEE_PER_CONTRACT = 0.65     # estimate: per-contract option fee (broker reports no fee field)
STOCK_SELL_FEE_RATE = 0.0000278    # estimate: SEC + TAF regulatory fees on stock sells, x notional
OFFSET_BPS = 10.0                 # marketable limit offset beyond the bid/ask
DD_CONFIRM_TICKS = 3               # drawdown pause needs this many consecutive breaching ticks
MAX_PASSES_PER_MONTH = 12          # rebalance passes before a month is closed with a warning
VAA_SYMBOLS = list(dict.fromkeys(vaa.G12 + vaa.DEFENSIVE))


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _meta(row: dict | None) -> dict:
    try:
        return json.loads((row or {}).get("meta_json") or "{}")
    except ValueError:
        return {}


def next_rebalance_day(after: date) -> date:
    """First trading day of the month after ``after``."""
    d = (after.replace(day=1) + timedelta(days=32)).replace(day=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d

__all__ = [n for n in dir() if not n.startswith("__")]
