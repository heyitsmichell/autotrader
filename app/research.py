"""Read-only cross-market discovery and provenance packets.

Research candidates are deliberately separate from executable stock proposals.
They require manual review and are never marked live-eligible by this scanner.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone

from .broker import Broker, BrokerError
from .config import Settings
from .db import DB
from .market import is_trading_day, now_et, now_utc

log = logging.getLogger(__name__)

SOURCE_TIMEOUT_SECONDS = 30.0

SYMBOL_RE = re.compile(r"\b(?:Symbol|Ticker|Underlying)\s*:\s*([A-Z][A-Z0-9.\-]{0,14})\b", re.I)
SCREENER_SYMBOL_RE = re.compile(r"^\s{0,8}([A-Z][A-Z0-9.\-]{0,14})\s+-\s+", re.M)


def _tool_args(tool, category: str, watchlist: list[str]) -> dict:
    """Build conservative arguments from an MCP tool's declared schema."""
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    args: dict = {}
    for name in props:
        if name == "category":
            args[name] = category
        elif name in {"underlying_symbols", "symbols"}:
            args[name] = ",".join(watchlist) if props[name].get("type") == "string" else watchlist
        elif name == "page_size":
            args[name] = min(50, max(1, int(props[name].get("maximum", 50))))
        elif name == "status":
            if category == "US_OPTION":
                args[name] = "LISTING"
            elif props[name].get("type") == "array":
                args[name] = ["OC", "CO", "NT"]
        elif name in required:
            enum = props[name].get("enum")
            if enum:
                args[name] = category if category in enum else enum[0]
            elif props[name].get("type") == "array":
                args[name] = watchlist
            elif props[name].get("type") == "string":
                args[name] = category
            elif props[name].get("type") == "integer":
                args[name] = 50
    return args


def _metric_value(raw: str, label: str) -> float | None:
    m = re.search(rf"\b{re.escape(label)}:\s*\$?([\d,.]+)\s*([KMBT]?)", raw, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2).upper()]


def _screener_records(raw: str) -> dict[str, dict]:
    """Read the documented Webull screener formatter without guessing missing fields."""
    heads = list(SCREENER_SYMBOL_RE.finditer(raw))
    records = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        block = raw[head.start():end]
        records[head.group(1).upper()] = {
            "price": _metric_value(block, "Price"),
            "volume": _metric_value(block, "Volume"),
        }
    return records


class ResearchService:
    def __init__(self, settings: Settings, broker: Broker, db: DB) -> None:
        self.settings, self.broker, self.db = settings, broker, db
        self.manual_symbols = list(settings.watchlist)
        self.auto_symbols = [s for s in db.get_kv("research_auto_watchlist", [])
                             if s not in self.manual_symbols][:10]
        db.set_kv("research_auto_watchlist", self.auto_symbols)
        self.refreshing = False
        self.last_refresh: str | None = db.get_kv("research_last_refresh")
        self.errors: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.settings.research_scan_minutes > 0 and self._task is None:
            self._task = asyncio.create_task(self._scan_loop(), name="research-scan")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if not self.broker.connected or not self._in_scan_window():
                continue
            try:
                await self.refresh_if_due()
            except Exception:  # noqa: BLE001 - scan failures must not stop unattended service
                log.exception("automatic research scan failed")

    @staticmethod
    def _in_scan_window() -> bool:
        t = now_et()
        return is_trading_day(t.date()) and time(4, 0) <= t.time() < time(20, 0)

    async def refresh_if_due(self, *, force: bool = False) -> bool:
        if self.settings.research_scan_minutes <= 0 and not force:
            return False
        if not force and (not self.broker.connected or not self._in_scan_window()):
            return False
        if not force and self.last_refresh:
            try:
                previous = datetime.fromisoformat(self.last_refresh)
                if now_utc() - previous < timedelta(minutes=self.settings.research_scan_minutes):
                    return False
            except ValueError:
                pass
        if self.refreshing:
            return False
        await self.refresh()
        return True

    async def refresh(self) -> dict:
        if self.refreshing:
            return {"refreshing": True, "packets": self.db.research_packets(100)}
        self.refreshing = True
        self.errors = {}
        self.db.clear_research_packets()
        try:
            by_name = {t.name: t for t in self.broker.tools}
            sources = [
                ("equity", "get_gainers_losers", "US_STOCK", "equity_movers"),
                ("equity", "get_most_active", "US_STOCK", "equity_activity"),
                ("crypto_spot", "get_crypto_instruments", "US_CRYPTO", "crypto_instruments"),
                ("option", "get_option_contracts", "US_OPTION", "option_chain_inventory"),
            ]
            raw_results: list[tuple[str, str, str]] = []
            for asset, name, category, kind in sources:
                tool = by_name.get(name)
                if tool is None:
                    self.errors[name] = "Tool unavailable in this broker region/package."
                    continue
                async def _fetch(name=name, tool=tool, category=category, kind=kind) -> str:
                    if kind == "crypto_instruments" and hasattr(self.broker, "crypto_instruments"):
                        return await self.broker.crypto_instruments()
                    if kind == "option_chain_inventory" and hasattr(self.broker, "option_contracts"):
                        return await self.broker.option_contracts(self.manual_symbols)
                    args = _tool_args(tool, category, self.effective_watchlist())
                    return await self.broker.call(name, **args)

                try:
                    # Every network call is bounded: one hung MCP request must not stall the scan.
                    raw = await asyncio.wait_for(_fetch(), SOURCE_TIMEOUT_SECONDS)
                    raw_results.append((asset, kind, raw[:12000]))
                except asyncio.TimeoutError:
                    self.errors[name] = f"Timed out after {SOURCE_TIMEOUT_SECONDS:g}s."
                except (BrokerError, ValueError, TypeError) as exc:
                    self.errors[name] = str(exc)[:500]

            candidates: dict[str, tuple[str, list[dict]]] = {}
            asset_counts: dict[str, int] = {}
            qualifying_movers: set[str] = set()
            qualifying_active: set[str] = set()
            for asset, kind, raw in raw_results:
                if asset == "equity" and kind in {"equity_movers", "equity_activity"}:
                    records = _screener_records(raw)
                    qualified = {symbol for symbol, values in records.items()
                                 if values["price"] is not None and values["price"] >= 5
                                 and values["volume"] is not None and values["volume"] >= 250_000}
                    if kind == "equity_movers":
                        qualifying_movers = qualified
                    else:
                        qualifying_active = qualified
                matches = list(dict.fromkeys(
                    [m.group(1).upper() for m in SYMBOL_RE.finditer(raw)]
                    + [m.group(1).upper() for m in SCREENER_SYMBOL_RE.finditer(raw)]))
                if asset == "option":
                    # Chain inventory belongs to the underlyings; never treat contracts as
                    # independently approved candidates.
                    matches = list(dict.fromkeys(self.manual_symbols))
                for symbol in matches:
                    if asset == "equity" and symbol in self.manual_symbols:
                        continue
                    key = f"{asset}:{symbol}"
                    class_cap = 50 if asset == "equity" else (
                        10 if asset == "crypto_spot" else len(self.manual_symbols))
                    if key not in candidates and asset_counts.get(asset, 0) >= class_cap:
                        continue
                    if key not in candidates:
                        asset_counts[asset] = asset_counts.get(asset, 0) + 1
                    candidates.setdefault(key, (asset, []))[1].append({
                        "source": kind, "observed_at": datetime.now(timezone.utc).isoformat(),
                        "excerpt": _excerpt(raw, symbol),
                    })

            # Only overlap-qualified, non-configured equities enter the AI candidate
            # list. Keep the list bounded and refresh it from current scan evidence.
            eligible_symbols = sorted((qualifying_movers & qualifying_active)
                                      - set(self.manual_symbols))[:10]
            self.auto_symbols = eligible_symbols

            for key, (asset, evidence) in candidates.items():
                symbol = key.split(":", 1)[1]
                auto_eligible = asset == "equity" and symbol in eligible_symbols
                caveats = [
                    "Discovery candidate only; not a trade recommendation.",
                    "This packet does not establish liquidity, valuation, or expected return.",
                ]
                if auto_eligible:
                    caveats.append("AI may evaluate this screened equity; proposals still require "
                                   "signal consensus and deterministic risk checks.")
                else:
                    caveats.append("Manual review required before adding to the watchlist.")
                if asset == "option":
                    caveats.append("Option quotes and live collateral impact were not verified.")
                if asset == "crypto_spot":
                    caveats.append("Spot instrument inventory only; no perpetuals or leverage.")
                self.db.add_research_packet(asset_class=asset, symbol=symbol,
                                            packet_type="candidate", evidence=evidence,
                                            caveats=caveats,
                                            approval_only=not (auto_eligible and
                                                self.settings.auto_trade_research_candidates))

            self.db.set_kv("research_auto_watchlist", self.auto_symbols)

            # Also retain a market-level provenance packet when a source returned data but
            # did not expose a symbol in the formatter output.
            for asset, kind, raw in raw_results:
                if not SYMBOL_RE.search(raw):
                    self.db.add_research_packet(
                        asset_class=asset, symbol="MARKET", packet_type=kind,
                        evidence=[{"source": kind, "excerpt": raw[:2500]}],
                        caveats=["Raw broker output retained for manual review."],
                        approval_only=True)
            self.last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.db.set_kv("research_last_refresh", self.last_refresh)
            return {"refreshing": False, "last_refresh": self.last_refresh,
                    "candidate_count": len(candidates), "errors": self.errors,
                    "manual_watchlist": self.manual_symbols,
                    "auto_watchlist": self.auto_symbols,
                    "effective_watchlist": self.effective_watchlist(),
                    "auto_eligibility": {"required_screener_overlap": True,
                                         "minimum_price_usd": 5,
                                         "minimum_daily_volume": 250_000},
                    "packets": self.db.research_packets(100)}
        finally:
            self.refreshing = False

    def effective_watchlist(self) -> list[str]:
        """Return the combined watchlist: manual symbols first, then up to 10 auto-discovered."""
        return list(dict.fromkeys(
            [*self.manual_symbols, *[s for s in self.auto_symbols
                                     if s not in self.manual_symbols][:10]]))

    def analyst_context(self) -> str:
        """Compact evidence for equities that passed the automated overlap screen."""
        if not self.auto_symbols:
            return "No equities passed the automated mover/activity screen on the latest scan."
        packets = {p["symbol"]: p for p in self.db.research_packets(100)
                   if p["asset_class"] == "equity" and p["packet_type"] == "candidate"}
        lines = [f"Automated screen timestamp (UTC): {self.last_refresh or 'unknown'}."]
        for symbol in self.auto_symbols:
            packet = packets.get(symbol)
            evidence = packet.get("evidence", []) if packet else []
            snippets = [f"{item.get('source')}: {item.get('excerpt', '')[:220]}"
                        for item in evidence[:2]]
            lines.append(f"{symbol}: " + (" | ".join(snippets) if snippets else
                                           "passed both mover and activity screens; fetch current evidence"))
        return "\n".join(lines)


def _excerpt(raw: str, symbol: str) -> str:
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if re.search(rf"\b{re.escape(symbol)}\b", line, re.I):
            return "\n".join(lines[max(0, i - 1):min(len(lines), i + 3)])[:1200]
    return raw[:500]
