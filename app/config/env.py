"""Environment helpers, paths and the env-var registry used by the settings loader and
scripts/gen_env_example.py."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from dotenv import load_dotenv

_log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent.parent   # app/config/env.py -> repo root
if os.environ.get("TRADER_SKIP_DOTENV", "").strip().lower() not in ("1", "true", "yes"):
    load_dotenv(ROOT / ".env", override=False)   # tests set TRADER_SKIP_DOTENV=1

# TRADER_DATA_DIR lets tests and demos use a separate database and model folder,
# so they never mix into your real trading history.
DATA_DIR = Path(os.environ.get("TRADER_DATA_DIR") or ROOT / "data").resolve()
MODEL_DIR = DATA_DIR / "models"
# TRADER_LOG_DIR does the same for log files (the test suite points both at a temp dir).
LOG_DIR = Path(os.environ.get("TRADER_LOG_DIR") or ROOT / "logs").resolve()
WEB_DIR = ROOT / "web" / "out"          # Next.js static export (npm run build in web/)
STRATEGY_FILE = ROOT / "strategy.md"
LESSONS_FILE = ROOT / "lessons.md"

# Every env var the loader reads, with the default it passed: name -> default.
# scripts/gen_env_example.py builds .env.example from this, so a setting added to the
# loader can never be missing from the example file.
REGISTRY: dict[str, object] = {}


def _reg(name: str, default: object) -> None:
    REGISTRY.setdefault(name, default)


def _raw(name: str, default: str = "") -> str:
    """Unstripped-default string read (secrets and free text)."""
    _reg(name, default)
    return os.environ.get(name, default)

def _str(name: str, default: str) -> str:
    _reg(name, default)
    value = os.environ.get(name, "").strip()
    return value or default


def _bool(name: str, default: bool) -> bool:
    _reg(name, default)
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    _log.warning("Invalid boolean for %s=%r; using default %r", name, value, default)
    return default


def _float(name: str, default: float) -> float:
    _reg(name, default)
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        out = float(raw)
    except ValueError:
        _log.warning("Invalid float for %s=%r; using default %r", name, raw, default)
        return default
    if not math.isfinite(out):
        _log.warning("Non-finite float for %s=%r; using default %r", name, raw, default)
        return default
    return out


def _int(name: str, default: int) -> int:
    _reg(name, default)
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        out = float(raw)
        if math.isfinite(out) and out == int(out):
            return int(out)
    except (ValueError, OverflowError):
        pass
    _log.warning("Invalid integer for %s=%r; using default %r", name, raw, default)
    return default


def _symbols(name: str, default: str) -> list[str]:
    raw = _str(name, default)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _build_watchlist() -> list[str]:
    """WATCHLIST plus any named universes in WATCHLIST_UNIVERSES (e.g. tech_semis).

    Manual WATCHLIST symbols always come first and are never trimmed; WATCHLIST_MAX only
    caps how many universe symbols are appended (0 = no cap). WATCHLIST_EXCLUDE removes
    symbols (useful for names that cost more than one order or that you never want).
    """
    manual = _symbols("WATCHLIST", "AAPL,MSFT,NVDA,SPY,QQQ")
    names = [n.strip().lower() for n in _str("WATCHLIST_UNIVERSES", "").split(",") if n.strip()]
    extra: list[str] = []
    if names:
        from ..universe import get_predefined_universe   # lazy: universe imports config
        for n in names:
            extra.extend(get_predefined_universe(n))
    exclude = set(_symbols("WATCHLIST_EXCLUDE", ""))
    cap = max(0, _int("WATCHLIST_MAX", 0))
    merged = list(dict.fromkeys(manual))
    added = 0
    for sym in extra:
        if sym in merged or sym in exclude:
            continue
        if cap and added >= cap:
            break
        merged.append(sym)
        added += 1
    return [s for s in merged if s not in exclude or s in manual]


def _sector_map(name: str, default: str) -> dict[str, str]:
    """Parse SYMBOL:SECTOR pairs from env var. Format: AAPL:TECH,MSFT:TECH,JPM:FINANCIAL"""
    raw = _str(name, default)
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            sym, sector = pair.split(":", 1)
            result[sym.strip().upper()] = sector.strip().upper()
    return result
