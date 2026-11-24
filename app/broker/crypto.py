"""Read-only crypto market data (mixin for ``Broker``). No crypto order placement exists; if one
is added it must call ``self._guard({... "kind": "crypto"})`` and ``self._submit``."""

from __future__ import annotations

from .errors import BrokerError


class CryptoMixin:
    async def crypto_instruments(self, symbols: list[str] | None = None) -> str:
        args = {"category": "US_CRYPTO", "page_size": 50}
        if symbols:
            args["symbols"] = ",".join(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        return await self.call("get_crypto_instruments", **args)

    async def crypto_snapshot(self, symbols: list[str]) -> str:
        cleaned = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        if not cleaned:
            raise BrokerError("At least one crypto spot symbol is required")
        return await self.call("get_crypto_snapshot", symbols=",".join(cleaned),
                               category="US_CRYPTO")

    async def crypto_bars(self, symbols: list[str], *, timespan: str = "D",
                          count: int = 200) -> str:
        cleaned = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        if not cleaned or count < 1 or count > 1000:
            raise BrokerError("Crypto bars require symbols and a count between 1 and 1000")
        return await self.call("get_crypto_bars", symbols=",".join(cleaned),
                               category="US_CRYPTO", timespan=timespan, count=count)
