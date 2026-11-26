"""MAE / MFE analytics for closed trades (mixed into Portfolio)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("app.portfolio")


class MaeMfeMixin:
    async def _compute_mae_mfe(self, symbol: str, entry_price: float,
                               entry_time: datetime, exit_time: datetime
                               ) -> tuple[float, float] | tuple[None, None]:
        """MAE / MFE (percent vs entry) over the hold, or (None, None) when it can't be measured.

        Uses 5-minute bars when they cover the hold window, else daily high/low bars over the
        hold dates. Never returns a fake 0.0.
        """
        if entry_price <= 0:
            return None, None
        entry_ts, exit_ts = entry_time.timestamp(), exit_time.timestamp()

        def _ts(b) -> float | None:
            t = b.get("time")
            try:
                if isinstance(t, str):
                    t = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                return t.timestamp()
            except Exception as exc:
                log.debug("timestamp parse failed: %s", exc)
                return None

        def _result(sel: list[dict]):
            if not sel:
                return None, None
            mae = min(0.0, (min(b["low"] for b in sel) - entry_price) / entry_price * 100)
            mfe = max(0.0, (max(b["high"] for b in sel) - entry_price) / entry_price * 100)
            return round(mae, 2), round(mfe, 2)

        try:
            bars = await self.broker.intraday_bars(symbol, timespan="5m", count=390)
        except Exception as exc:
            log.debug("intraday bars unavailable for MAE/MFE: %s", exc)
            bars = []
        stamped = [(t, b) for b in bars or [] if (t := _ts(b)) is not None]
        # intraday bars must reach back to the entry to cover the hold window
        if stamped and min(t for t, _ in stamped) <= entry_ts + 300:
            sel = [b for t, b in stamped if entry_ts <= t <= exit_ts]
            if sel:
                return _result(sel)

        try:
            daily = await self.broker.daily_bars(symbol, 60)
        except Exception as exc:
            log.debug("daily bars unavailable for MAE/MFE: %s", exc)
            return None, None
        d0, d1 = entry_time.astimezone(timezone.utc).date(), exit_time.astimezone(timezone.utc).date()
        sel = []
        for b in daily or []:
            t = _ts(b)
            if t is not None and d0 <= datetime.fromtimestamp(t, timezone.utc).date() <= d1:
                sel.append(b)
        return _result(sel)

