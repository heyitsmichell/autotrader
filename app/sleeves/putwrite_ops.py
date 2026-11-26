"""SleeveManager mixin: put-write sleeve steps, settlement, option orders."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


class PutWriteMixin:
    # ------------------------------------------------------------------ put-write
    async def _putwrite_step(self, values: dict, snap: dict) -> None:
        sid, s = "putwrite", self.s
        today, now = now_et().date(), now_et()
        self.__dict__["_putwrite_prev_cash"] = self.__dict__.get("_putwrite_cash_last")
        self.__dict__["_putwrite_cash_last"] = snap.get("cash")
        under = s.sleeve_putwrite_underlying
        holds = self.db.sleeve_holdings(sid)
        # 1. assigned or leftover shares are sold (≈ the cash settlement the backtest models)
        for h in [h for h in holds if h["kind"] == "stock" and float(h["qty"]) > 0]:
            px = await self._price(h["symbol"], snap) or float(h.get("avg_cost") or 0)
            await self._send_chunks(sid, h["symbol"], "SELL", int(float(h["qty"])), px, "assigned shares")
            return
        puts = [h for h in holds if h["kind"] == "option" and float(h["qty"]) < 0]
        spot = await self._price(under, snap)
        for h in puts:
            occ = parse_occ(h["symbol"]) or {}
            if not occ:
                continue
            put = {"strike": occ["strike"], "expiration": date.fromisoformat(occ["expiration"])}
            if h["symbol"].upper() not in snap["options"] and putwrite.is_expired(put, today, now):
                try:    # broker order history is the primary assignment evidence (item 4a)
                    snap = {**snap, "fills_text": await self.broker.order_history_text()}
                except Exception as exc:  # noqa: BLE001
                    log.warning("order history unavailable for put settlement: %s", exc)
                self._settle_expired(h, occ, snap)
                return
            if spot and putwrite.should_close(put, spot, today, now):
                await self._close_put(h, occ)
                return
        if puts:
            return
        st = self.db.sleeve_state(sid) or {}
        if st.get("paused") or self.eng.kill_switch or _meta(st).get("cycle_end_day") == today.isoformat():
            return
        await self._open_put(under, spot, today)

    def _settle_expired(self, h: dict, occ: dict, snap: dict) -> None:
        """The put left the account: expired worthless, or assigned (shares appeared)."""
        sid, under, n = "putwrite", occ["underlying"], abs(float(h["qty"]))
        if not snap.get("raw_qty") and not snap.get("options"):
            self.db.log("warn", f"Sleeve put-write: positions snapshot empty/failed; not settling "
                                f"{h['symbol']}, retrying next tick")
            return
        raw = float(snap["raw_qty"].get(under, 0.0))
        reserved = self.db.sleeve_reserved().get(under, 0.0)
        swing_open = sum(float(t.get("quantity_filled_real") or t.get("quantity_filled") or 0)
                         for t in self.db.open_trades() if t.get("symbol") == under and t.get("side") == "BUY")
        extra = raw - reserved - swing_open
        shares = n * 100
        verdict = self._assignment_verdict(h, occ, snap, extra, shares)
        if verdict is None:
            day = now_et().date().isoformat()
            tries = self.__dict__.setdefault("_settle_tries", {})
            k = (h["symbol"], day)
            tries[k] = tries.get(k, 0) + 1
            cap = tries[k] >= PUT_SETTLE_MAX_TRIES_PER_DAY
            self.db.log("warn", f"Sleeve put-write: AMBIGUOUS settlement of {h['symbol']} (extra shares "
                                f"{extra:g}, expected {shares:g} if assigned); not settling, "
                                + ("retry cap reached: MANUAL REVIEW needed" if cap else "retrying next tick"),
                        alert=True)
            return
        assigned = verdict
        with self.db.transaction():   # holding, cycle meta and assignment cash change together
            self.db.set_sleeve_holding(sid, h["symbol"], "option", 0, None)
            meta = _meta(self.db.sleeve_state(sid))
            meta["cycle_end_day"] = now_et().date().isoformat()
            self.db.upsert_sleeve_state(sid, meta=meta)
            if assigned:
                self.db.set_sleeve_holding(sid, under, "stock", shares, occ["strike"])
                self._add_cash(sid, -shares * occ["strike"])
        if assigned:
            self.db.log("warn", f"Sleeve put-write: {h['symbol']} was ASSIGNED; {shares:g} {under} booked "
                                f"at {occ['strike']} and will be sold", alert=True)
            self._event(sid, "assigned", f"Put {h['symbol']} assigned: {shares:g} {under} at {occ['strike']}")
        else:
            self.db.log("info", f"Sleeve put-write: {h['symbol']} expired; premium kept")
            self._event(sid, "put_expired", f"Put {h['symbol']} expired worthless; premium kept")

    def _assignment_verdict(self, h: dict, occ: dict, snap: dict, extra: float, shares: float) -> bool | None:
        """True assigned, False expired worthless, None ambiguous (do not settle).
        Share increase must match n*100; broker fill text and the cash drop (vs the previous
        snapshot) are cross-checks when available."""
        under, strike = occ["underlying"], float(occ["strike"])
        text = str(snap.get("fills_text") or "").upper()
        sym = h["symbol"].upper()
        evidence = bool(text) and sym in text and any(w in text for w in ("ASSIGN", "EXERCIS"))
        none = abs(extra) <= shares * 0.05
        match = shares * 0.99 <= extra <= shares * 1.01
        if match:
            cash_now, prev = snap.get("cash"), self.__dict__.get("_putwrite_prev_cash")
            if cash_now is not None and prev is not None:
                drop = float(prev) - float(cash_now)
                if abs(drop - strike * shares) > 0.05 * strike * shares:
                    return None
            return True
        if none:
            return None if evidence else False
        return None

    async def _close_put(self, h: dict, occ: dict) -> None:
        sym, n = h["symbol"], int(abs(float(h["qty"])))
        q = await self._option_quote(sym)
        ask = (q or {}).get("ask") or (q or {}).get("last")
        if not ask:
            self.db.log("warn", f"Sleeve put-write: no quote to buy back {sym}; retrying")
            return
        px = putwrite.round_tick(float(ask) * 1.02 + 0.01, "up", (q or {}).get("tick"))
        await self._send_option(sym, occ, "BUY", "BUY_TO_CLOSE", n, px, "in the money at expiry: buy back")
        meta = _meta(self.db.sleeve_state("putwrite"))
        meta["cycle_end_day"] = now_et().date().isoformat()
        self.db.upsert_sleeve_state("putwrite", meta=meta)

    async def _option_quote(self, sym: str) -> dict | None:
        try:
            return (await self.broker.parsed_option_snapshots([sym])).get(sym)
        except BrokerError:
            return None

    async def _open_put(self, under: str, spot: float | None, today: date) -> None:
        s = self.s
        if not spot:
            return
        text = await self.broker.option_contracts([under])
        rows = []
        for tok in dict.fromkeys(_OCC_TOKEN.findall(text or "")):
            occ = parse_occ(tok)
            if not occ or occ["underlying"] != under or occ["option_type"] != "PUT":
                continue
            exp = date.fromisoformat(occ["expiration"])
            # standard strikes only: adjusted contracts (after a corporate action) carry odd
            # strikes such as 579.78 and trade thinly (2026-11-25 the picker chose them)
            if abs(occ["strike"] * 2 - round(occ["strike"] * 2)) > 1e-6:
                continue
            if putwrite.DTE_MIN <= (exp - today).days <= putwrite.DTE_MAX:
                rows.append({**occ, "symbol": tok, "expiration": exp})
        if not rows:
            self.db.log("warn", f"Sleeve put-write: no {under} put expiring in "
                                f"{putwrite.DTE_MIN}-{putwrite.DTE_MAX} days")
            return
        target = min({r["expiration"] for r in rows}, key=lambda e: abs((e - today).days - s.sleeve_putwrite_dte))
        dte = (target - today).days
        z = NormalDist().inv_cdf(1 - s.sleeve_putwrite_delta)
        est = spot * math.exp(-z * putwrite.EST_VOL * math.sqrt(max(dte, 1) / 365))
        near = sorted((r for r in rows if r["expiration"] == target), key=lambda r: abs(r["strike"] - est))[:10]
        snaps = await self.broker.parsed_option_snapshots([r["symbol"] for r in near])
        chain = []
        for r in near:
            d = snaps.get(r["symbol"]) or {}
            chain.append({**r, "delta": d.get("delta"), "bid": d.get("bid"), "ask": d.get("ask")})
        pick = putwrite.pick_contract(chain, spot, today, s.sleeve_putwrite_delta, s.sleeve_putwrite_dte)
        if not pick:
            self.db.log("warn", f"Sleeve put-write: no {under} {target} put with a two-sided quote")
            return
        n = putwrite.contracts_for(self._cash("putwrite"), pick["strike"])
        if n < 1:
            self.db.log("warn", "Sleeve put-write: cash does not secure one contract")
            return
        bid, ask = float(pick["bid"]), float(pick.get("ask") or pick["bid"])
        px = putwrite.sell_open_limit(bid, ask, pick.get("tick"))
        occ = parse_occ(pick["symbol"])
        await self._send_option(pick["symbol"], occ, "SELL", "SELL_TO_OPEN", n, px,
                                f"sell {n} x {pick['symbol']} (delta {pick.get('delta')}, {pick['dte']} DTE)")

    async def _send_option(self, sym: str, occ: dict, side: str, intent: str, n: int, px: float, why: str) -> None:
        s = self.s
        cap_usd = min([c for c in (s.max_order_usd, getattr(s, "max_order_notional", 0)) if c] or [0])
        closing = intent.endswith("_TO_CLOSE")
        if not closing and cap_usd and px * 100 > cap_usd:
            self.db.log("warn", f"Sleeve put-write: one contract (${px * 100:,.0f}) exceeds MAX_ORDER_USD="
                                f"{cap_usd:,.0f}; raise it before enabling sleeves", alert=True)
            return
        pieces = rebalance.chunks(n, px * 100, cap_usd, int(s.options_max_contracts_per_order or 0))
        for q in pieces:
            coid = registry.coid_for("putwrite")
            oid = self.db.add_sleeve_order("putwrite", sym, "option", side, q, px, coid,
                                           position_intent=intent, reason=why)
            try:
                await self.broker.place_option_single_order(
                    sym, side, int(q), occ["option_type"], occ["strike"], occ["expiration"], px, coid,
                    position_intent=intent, allow_when_killed=closing)
            except BrokerError as e:
                if e.transport:
                    self.db.update_sleeve_order(oid, status="working", reason=f"{why}; no reply: {e}")
                else:
                    self.db.update_sleeve_order(oid, status="rejected", reason=f"{why}; rejected: {e}")
                    self.db.log("warn", f"Sleeve put-write: {intent} {q} {sym} rejected: {e}", alert=True)
                return
            self.db.update_sleeve_order(oid, status="working")
            self.db.log("info", f"Sleeve put-write: {intent} {q} {sym} @ {px:.2f} ({why})")
            if intent in ("SELL_TO_OPEN", "BUY_TO_CLOSE"):
                self._event("putwrite", "put_open" if intent == "SELL_TO_OPEN" else "put_close",
                            f"{'Sold' if intent == 'SELL_TO_OPEN' else 'Bought back'} {q} x {sym} @ {px:.2f}")

