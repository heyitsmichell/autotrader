"""Universe management for Phase 3: named lists, filters, and point-in-time screening."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .broker import Broker, BrokerError
from .config import Settings

log = logging.getLogger(__name__)

# Predefined universe: sector ETFs (11 sectors + broad market)
SECTOR_ETFS = [
    "SPY",   # S&P 500 (benchmark)
    "QQQ",   # Nasdaq 100
    "DIA",   # Dow Jones Industrial Average
    "IWM",   # Russell 2000
    "XLF",   # Financial Select Sector SPDR
    "XLK",   # Technology Select Sector SPDR
    "XLV",   # Health Care Select Sector SPDR
    "XLE",   # Energy Select Sector SPDR
    "XLI",   # Industrial Select Sector SPDR
    "XLP",   # Consumer Staples Select Sector SPDR
    "XLY",   # Consumer Discretionary Select Sector SPDR
    "XLB",   # Materials Select Sector SPDR
    "XLRE",  # Real Estate Select Sector SPDR
    "XLU",   # Utilities Select Sector SPDR
    "XLC",   # Communication Services Select Sector SPDR
]

# Broad ETFs: major equity ETFs across regions, styles, factors
BROAD_ETFS = [
    "SPY",   # S&P 500
    "QQQ",   # Nasdaq 100
    "DIA",   # Dow 30
    "IWM",   # Russell 2000
    "EFA",   # MSCI EAFE (developed international)
    "EEM",   # MSCI Emerging Markets
    "VTI",   # Total US Stock Market
    "VEA",   # Developed Markets ex-US
    "VWO",   # Emerging Markets
    "SCHB",  # Broad US Market (Schwab)
    "SCHF",  # International Developed (Schwab)
    "SCHE",  # Emerging Markets (Schwab)
    "RSP",   # S&P 500 Equal Weight
    "QQQE",  # Nasdaq 100 Equal Weight
    "IWF",   # Russell 1000 Growth
    "IWD",   # Russell 1000 Value
    "IWO",   # Russell 2000 Growth
    "IWN",   # Russell 2000 Value
    "MTUM",  # MSCI USA Momentum Factor
    "USMV",  # MSCI USA Minimum Volatility
    "QUAL",  # MSCI USA Quality Factor
    "VLUE",  # MSCI USA Value Factor
    "SIZE",  # MSCI USA Size Factor
]

# S&P 500 top 50 by market cap (approximate, for universe expansion)
SP500_TOP_50 = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "BRK.B", "TSLA", "AVGO",
    "LLY", "JPM", "V", "UNH", "XOM", "MA", "HD", "COST", "PG", "WMT",
    "MRK", "ABBV", "PEP", "KO", "TMO", "ACN", "ADBE", "LIN", "CRM", "NFLX",
    "AMD", "INTC", "CSCO", "DIS", "TXN", "NEE", "PFE", "QCOM", "HON", "VZ",
    "BMY", "UPS", "LOW", "RTX", "UNP", "AMGN", "IBM", "GE", "CAT", "SPGI"
]

# Nasdaq-100 top 50 by market cap (approximate)
NASDAQ100_TOP_50 = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO", "PEP",
    "COST", "ADBE", "CSCO", "NFLX", "AMD", "INTC", "QCOM", "TXN", "AMGN", "HON",
    "INTU", "SBUX", "GILD", "BKNG", "ADP", "MDLZ", "ADI", "VRTX", "ISRG", "REGN",
    "PDD", "MU", "LRCX", "KLAC", "SNPS", "CDNS", "MRVL", "FTNT", "MCHP", "CTAS",
    "KDP", "PAYX", "MELI", "CEG", "CSX", "ORLY", "ASML", "NXPI", "ROST", "ODFL"
]

# Liquid ETFs for core-satellite and hedging
LIQUID_ETFS = [
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VEA", "VWO", "IEFA", "IEMG", "SCHB",
    "SCHF", "SCHE", "EFA", "EEM", "RSP", "QQQE", "MTUM", "USMV", "QUAL", "VLUE",
    "SIZE", "XLF", "XLK", "XLV", "XLE", "XLI", "XLP", "XLY", "XLB", "XLRE",
    "XLU", "XLC", "TLT", "IEF", "SHY", "LQD", "HYG", "JNK", "TIP", "GLD",
    "SLV", "USO", "UNG", "XOP", "XBI", "KBE", "KRE", "XHB", "XME", "XRT",
    "SPYM", "QQQM", "IAU", "DBC", "DBA", "FXI", "EWJ", "EWZ", "EWA", "EWH",
]

# Technology + semiconductor focus list (large/mid-cap, liquid, optionable). Used by the live
# trader through WATCHLIST_UNIVERSES=tech_semis. Prices vary widely, so high-priced names
# (AVGO, ASML, LRCX, KLAC, MPWR...) need FRACTIONAL_ENABLED=true or a higher MAX_ORDER_USD.
SEMICONDUCTORS = [
    "NVDA", "AMD", "AVGO", "TSM", "ASML", "QCOM", "TXN", "INTC", "MU", "AMAT",
    "LRCX", "KLAC", "ADI", "MRVL", "NXPI", "MCHP", "ON", "MPWR", "SWKS",  # QRVO removed 2026-11-24: no longer trades (Webull INVALID_SYMBOL, no Yahoo data)
    "ARM", "SMCI", "ALAB", "CRDO", "COHR", "ENTG", "TER", "ONTO", "FORM", "LSCC",
    "RMBS", "GFS", "STM", "UMC", "ASX", "SNPS", "CDNS",
]
TECH_SOFTWARE_INTERNET = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "ORCL", "CRM", "ADBE", "NOW",
    "INTU", "PANW", "CRWD", "FTNT", "ZS", "DDOG", "SNOW", "NET", "PLTR", "SHOP",
    "UBER", "ANET", "DELL", "HPE", "CSCO", "IBM", "ACN", "MDB", "TEAM", "WDAY",
]
TECH_ETFS = ["SMH", "SOXX", "SOXQ", "XLK", "QQQM", "IGV", "SKYY", "CIBR", "BOTZ", "XSD"]
TECH_SEMIS = list(dict.fromkeys(SEMICONDUCTORS + TECH_SOFTWARE_INTERNET + TECH_ETFS))

# All major symbols for expanded universe (S&P 500 + Nasdaq-100 + ETFs, deduplicated)
def _build_expanded_universe() -> list[str]:
    """Build the expanded universe combining all lists, deduplicated."""
    all_symbols = set()
    all_symbols.update(SECTOR_ETFS)
    all_symbols.update(BROAD_ETFS)
    all_symbols.update(SP500_TOP_50)
    all_symbols.update(NASDAQ100_TOP_50)
    all_symbols.update(LIQUID_ETFS)
    all_symbols.update(TECH_SEMIS)
    return sorted(all_symbols)

EXPANDED_UNIVERSE = _build_expanded_universe()

# Fractional whitelist (high-liquidity symbols that support fractional shares)
FRACTIONAL_WHITELIST = [
    "AAPL", "MSFT", "NVDA", "SPY", "QQQ", "AMZN", "GOOGL", "META", "TSLA",
    "GOOG", "BRK.B", "LLY", "JPM", "V", "UNH", "XOM", "MA", "HD", "COST",
    "PG", "WMT", "ABBV", "PEP", "KO", "AVGO", "TMO", "ACN", "ADBE", "LIN",
    "CRM", "NFLX", "AMD", "INTC", "CSCO", "DIS", "TXN", "NEE", "PFE", "QCOM",
    "HON", "VZ", "BMY", "UPS", "LOW", "RTX", "UNP", "AMGN", "IBM", "GE", "CAT",
    "SPGI", "INTU", "SBUX", "GILD", "BKNG", "ADP", "MDLZ", "ADI", "VRTX", "ISRG",
]


@dataclass
class UniverseConfig:
    """Configuration for universe construction."""
    name: str                    # "sector_etfs" | "sp500_top" | "broad_etfs" | "custom"
    custom_symbols: list[str]    # used when name == "custom"
    top_n: int                   # for "sp500_top"
    min_price: float
    min_avg_dollar_volume: float
    min_history_days: int


@dataclass
class SymbolInfo:
    """Information about a symbol for universe filtering."""
    symbol: str
    price: float | None = None
    avg_dollar_volume: float | None = None
    history_days: int | None = None


def get_predefined_universe(name: str) -> list[str]:
    """Get a predefined universe by name."""
    name = name.lower()
    if name == "sector_etfs":
        return SECTOR_ETFS.copy()
    elif name == "broad_etfs":
        return BROAD_ETFS.copy()
    elif name == "sp500_top":
        return SP500_TOP_50.copy()
    elif name == "nasdaq100_top":
        return NASDAQ100_TOP_50.copy()
    elif name == "expanded":
        return EXPANDED_UNIVERSE.copy()
    elif name == "liquid_etfs":
        return LIQUID_ETFS.copy()
    elif name == "semiconductors":
        return SEMICONDUCTORS.copy()
    elif name in ("tech_semis", "tech"):
        return TECH_SEMIS.copy()
    elif name == "custom":
        return []
    else:
        log.warning("Unknown universe name %s, using sector_etfs", name)
        return SECTOR_ETFS.copy()


async def fetch_symbol_info(
    broker: Broker,
    symbols: list[str],
    min_history_days: int,
    settings: Settings,
) -> dict[str, SymbolInfo]:
    """Fetch price, volume, and history info for symbols.

    One failure doesn't stop the run. Fetches are batched with delays.
    """
    info = {}
    delay = getattr(settings, 'backtest_fetch_delay_seconds', 1.0)

    from .broker.market_data import fetch_bars_many
    fetch_errors: dict = {}
    all_bars = await fetch_bars_many(broker, symbols, min_history_days + 10, errors=fetch_errors)

    for i, sym in enumerate(symbols):
        try:
            if sym.upper() in fetch_errors:
                raise fetch_errors[sym.upper()]
            bars = all_bars.get(sym.upper(), [])
            if len(bars) < min_history_days:
                log.debug("Symbol %s has only %d bars (need %d)", sym, len(bars), min_history_days)
                info[sym] = SymbolInfo(symbol=sym, history_days=len(bars))
                continue

            # Compute average dollar volume from available bars
            volumes = [b["volume"] for b in bars if b.get("volume", 0) > 0]
            closes = [b["close"] for b in bars if b.get("close", 0) > 0]

            avg_dollar_volume = None
            if volumes and closes:
                avg_vol = sum(volumes) / len(volumes)
                avg_close = sum(closes) / len(closes)
                avg_dollar_volume = avg_vol * avg_close

            latest_price = closes[-1] if closes else None

            info[sym] = SymbolInfo(
                symbol=sym,
                price=latest_price,
                avg_dollar_volume=avg_dollar_volume,
                history_days=len(bars),
            )
        except BrokerError as e:
            log.warning("Failed to fetch bars for %s: %s", sym, e)
            info[sym] = SymbolInfo(symbol=sym)
        except Exception as e:  # noqa: BLE001
            log.warning("Unexpected error fetching %s: %s", sym, e)
            info[sym] = SymbolInfo(symbol=sym)

    return info


def filter_universe(
    symbols: list[str],
    info: dict[str, SymbolInfo],
    config: UniverseConfig,
) -> list[str]:
    """Apply universe filters: price, volume, history."""
    if not symbols:
        return []

    filtered = []
    for sym in symbols:
        sym_info = info.get(sym)
        if not sym_info:
            # No info available - keep if it's a predefined ETF (assumed liquid)
            if sym in SECTOR_ETFS or sym in BROAD_ETFS:
                filtered.append(sym)
            continue

        # Price filter
        if sym_info.price is not None and sym_info.price < config.min_price:
            log.debug("Dropping %s: price %.2f < %.2f", sym, sym_info.price, config.min_price)
            continue

        # Dollar volume filter
        if sym_info.avg_dollar_volume is not None and sym_info.avg_dollar_volume < config.min_avg_dollar_volume:
            log.debug("Dropping %s: avg dollar volume %.0f < %.0f", sym, sym_info.avg_dollar_volume, config.min_avg_dollar_volume)
            continue

        # History filter
        if sym_info.history_days is not None and sym_info.history_days < config.min_history_days:
            log.debug("Dropping %s: history %d days < %d", sym, sym_info.history_days, config.min_history_days)
            continue

        filtered.append(sym)

    return filtered


def build_universe_config(settings: Settings) -> UniverseConfig:
    """Build UniverseConfig from Settings."""
    return UniverseConfig(
        name=settings.universe_name,
        custom_symbols=settings.universe_custom,
        top_n=settings.universe_top_n,
        min_price=settings.universe_min_price,
        min_avg_dollar_volume=settings.universe_min_avg_dollar_volume,
        min_history_days=settings.universe_min_history_days,
    )


async def get_universe(
    broker: Broker,
    settings: Settings,
    extra_symbols: list[str] | None = None,
) -> list[str]:
    """Get the full filtered universe for research/backtest/ML.

    Args:
        broker: Broker for fetching bar data
        settings: App settings
        extra_symbols: Additional symbols to include (e.g., WATCHLIST)

    Returns:
        Filtered list of symbols
    """
    config = build_universe_config(settings)

    # Start with predefined universe
    if config.name == "custom":
        base_symbols = config.custom_symbols
    else:
        base_symbols = get_predefined_universe(config.name)

    # Add extra symbols (WATCHLIST, etc.)
    if extra_symbols:
        base_symbols = list(dict.fromkeys(base_symbols + extra_symbols))

    if not base_symbols:
        log.warning("No base symbols for universe")
        return []

    # Tiered scanning with rate limiting for large universes
    # Tier 1: Core symbols (high priority, always scanned)
    # Tier 2: Satellite symbols (scanned with lower frequency)
    # Tier 3: Extended universe (scanned on demand)
    tier1_size = getattr(settings, 'universe_tier1_size', 50)
    tier2_size = getattr(settings, 'universe_tier2_size', 100)
    tier3_size = getattr(settings, 'universe_tier3_size', 200)

    # Split symbols into tiers
    tier1_symbols = base_symbols[:tier1_size]
    tier2_symbols = base_symbols[tier1_size:tier1_size + tier2_size]
    tier3_symbols = base_symbols[tier1_size + tier2_size:tier1_size + tier2_size + tier3_size]

    # For backtesting/research, scan all tiers but with rate limiting
    # In live mode, only scan tier 1 by default
    scan_all_tiers = getattr(settings, 'universe_scan_all_tiers', False)
    if scan_all_tiers:
        symbols_to_scan = base_symbols
    else:
        symbols_to_scan = tier1_symbols + tier2_symbols

    # Fetch symbol info and filter (tiered)
    info = await fetch_symbol_info(broker, symbols_to_scan, config.min_history_days, settings)
    filtered = filter_universe(symbols_to_scan, info, config)

    log.info("Universe %s: %d -> %d symbols after filtering (tiered: %d tier1, %d tier2, %d tier3)",
             config.name, len(symbols_to_scan), len(filtered), len(tier1_symbols), len(tier2_symbols), len(tier3_symbols))
    return filtered


# Synchronous wrapper for backtester/ML which run in thread pool
def get_universe_sync(
    broker: Broker,
    settings: Settings,
    extra_symbols: list[str] | None = None,
) -> list[str]:
    """Synchronous wrapper for get_universe."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(get_universe(broker, settings, extra_symbols))