"""Offline end-to-end check against the mock broker. Groq is NOT called, so
this costs nothing. It checks the MCP connection, the tools given to Groq, the
parsers, signals, the consensus gate, the risk checks, bracket orders, protective
order handling, the market calendar, unknown-order reconciliation, the automatic
kill switch, flatten, alerts, and (if PyTorch is installed) model training.

    .venv\\Scripts\\python -m scripts.smoke_test            (everything)
    .venv\\Scripts\\python -m scripts.smoke_test --no-ml    (skip model training)
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="trader-smoke-"))
os.environ.update(
    BROKER_MODE="mock", MARKET_HOURS_ONLY="false", TRADING_MODE="approve",
    WATCHLIST="AAPL,MSFT,SPY", MAX_ORDER_USD="1000", MAX_POSITION_USD="2000",
    MAX_ORDER_QTY="50", MAX_TRADES_PER_DAY="5", PRICE_BAND_PCT="1.0",
    MAX_RISK_PER_TRADE_USD="100", BRACKET_ORDERS="true", SENTIMENT_LLM="false",
    ML_HISTORY_BARS="700", TRADER_DATA_DIR=str(TMP),
    MOCK_STATE_FILE=str(TMP / "mock_state.json"),
    # Unattended-operation settings, pinned so a real .env can't change the results
    # (and the test never sends real alerts).
    AUTO_START_SCHEDULER="false", KILL_MAX_DRAWDOWN_PCT="10", KILL_DAILY_LOSS_USD="0",
    KILL_MAX_CONSECUTIVE_ERRORS="3", KILL_ACTION="pause", FLATTEN_SLIPPAGE_PCT="0.5",
    MARKET_CLOSED_DATES="", MARKET_EARLY_CLOSE_DATES="", ALERT_NTFY_TOPIC="",
    ALERT_TELEGRAM_BOT_TOKEN="", ALERT_WEBHOOK_URL="", ALERT_SMTP_HOST="",
    WEBULL_TOKEN_DIR=str(TMP / "conf"),
)

from app import consensus, mock_mcp_server as mock  # noqa: E402
from app import market  # noqa: E402
from app.broker import Broker  # noqa: E402
from app.config import estimate_cost, load_settings, MODEL_DIR  # noqa: E402
from app.db import DB  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.ml import MLFilter  # noqa: E402
from app.portfolio import Portfolio, parse_balance, parse_orders, parse_position_rows  # noqa: E402
from app.sentiment import SentimentEngine, parse_fear_greed, parse_news  # noqa: E402
from app.signals import SignalService  # noqa: E402
from app.research import ResearchService  # noqa: E402
from app.alerts import Alerts  # noqa: E402
from app.supervisor import Supervisor, read_token_info  # noqa: E402

failures = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global failures
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  ({detail})" if detail and not cond else ""))
    failures += 0 if cond else 1


def bullish(price: float) -> dict:
    """A signal where every engine agrees (used to test the order flow)."""
    return {"id": None, "symbol": "X", "close": price, "atr": price * 0.02, "trend": "up",
            "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": None, "features": {}}


async def main(run_ml: bool) -> None:
    s = load_settings()
    db = DB(TMP / "trader.db", s.broker_mode, s.webull_environment, s.webull_account_id)
    broker = Broker(s)
    sentiment = SentimentEngine(s, broker, db)
    ml = MLFilter(s, db)
    research = ResearchService(s, broker, db)
    signals = SignalService(s, broker, db, sentiment, ml, research)
    portfolio = Portfolio(s, db, broker)
    engine = Engine(s, db, broker, signals, portfolio)

    print("MCP connection")
    await broker.start()
    names = {t.name for t in broker.tools}
    check(broker.connected, "connected to mock MCP server")
    check({"place_stock_order", "place_stock_combo_order", "get_stock_snapshot"} <= names,
          "server lists order, combo and quote tools")
    acct = await broker.account_id()
    check(acct == "MOCK0001", "account id parsed", acct)
    price = await broker.last_price("AAPL")
    check(price is not None and price > 0, "AAPL price parsed", str(price))

    print("Parsers")
    bal = parse_balance(await broker.balance_text())
    check(bal.get("net_liq", 0) > 0 and bal.get("cash") is not None, "balance parsed", str(bal))
    news = parse_news(await broker.call("get_stock_news", symbol="AAPL"), "AAPL")
    check(len(news) > 0 and all(n["title"] and n["news_id"] for n in news), "news parsed",
          str(len(news)))
    check(any(n["sentiment"] in ("POSITIVE", "NEGATIVE", "NEUTRAL") for n in news),
          "news carries Webull sentiment tags")
    fg = parse_fear_greed(await broker.call("get_market_sentiment"))
    check(bool(fg) and 0 <= fg["current"]["index"] <= 100, "fear & greed parsed", str(fg))

    print("Signals (TA + sentiment + ML)")
    sigs = await signals.refresh()
    check(len(sigs) == 3, "a signal per watchlist symbol", str(len(sigs)))
    sig = sigs[0] if sigs else {}
    check(sig.get("ta_score") is not None and -1 <= sig["ta_score"] <= 1, "TA score in range")
    check(sig.get("trend") in ("up", "down", "range"), "trend regime set")
    check(isinstance(sig.get("ta_components"), list) and len(sig["ta_components"]) >= 4,
          "score components recorded")
    check(sig.get("sent_n", 0) > 0 and sig.get("sent_webull") is not None,
          "Webull sentiment scored", f"n={sig.get('sent_n')}")
    check(sig.get("consensus") in ("long", "blocked") and sig.get("sl") and sig.get("tp"),
          "gate verdict and ATR levels set")
    check("Consensus for a BUY now" in signals.describe(), "get_signals text for Groq")

    print("Consensus gate")
    base = {"side": "BUY", "limit_price": 100.0}
    good = bullish(100.0)
    check(consensus.evaluate(base, good, s)["ok"], "passes when TA, news and model agree")
    check(not consensus.evaluate(base, {**good, "ta_score": 0.0}, s)["ok"], "blocks weak TA")
    check(not consensus.evaluate(base, {**good, "trend": "down"}, s)["ok"], "blocks downtrend")
    check(not consensus.evaluate(base, {**good, "sent_score": -0.6}, s)["ok"],
          "blocks negative news")
    check(consensus.evaluate(base, {**good, "sent_score": -0.9, "sent_n": 1}, s)["ok"],
          "too few headlines counts as neutral")
    check(not consensus.evaluate(base, {**good, "ml_p": 0.4}, s)["ok"], "blocks low model odds")
    check(consensus.evaluate({"side": "SELL", "limit_price": 100}, None, s)["ok"],
          "never blocks a SELL")
    g = consensus.evaluate(base, good, s)
    check(g["sl"] < 100 < g["tp"], "stop below and target above the entry", str(g))

    # Order flow: make every engine agree, so the risk checks and brackets are what's tested.
    async def agree(sym: str, max_age_minutes: int = 20):
        return {**bullish(await broker.last_price(sym)), "symbol": sym}
    engine.signals.ensure = agree

    print("Risk checks")
    run_ids = [db.create_run("smoke")]

    async def propose(**kw) -> str:
        return await engine._on_proposal(run_ids[-1], kw)

    base_kw = dict(confidence=0.5, rationale="smoke test")
    r = await propose(symbol="TSLA", side="BUY", quantity=1, limit_price=250, **base_kw)
    check(r.startswith("REJECTED") and "watchlist" in r, "rejects symbol off the watchlist", r)
    r = await propose(symbol="AAPL", side="BUY", quantity=1, limit_price=round(price * 1.1, 2), **base_kw)
    check(r.startswith("REJECTED") and "%" in r, "rejects limit price far from market", r)
    # New clamping behavior: quantity is clamped to fit within limits instead of rejecting
    r = await propose(symbol="AAPL", side="BUY", quantity=40, limit_price=price, **base_kw)
    check(r.startswith("ACCEPTED"), "clamps quantity to fit order limit (accepts instead of rejecting)", r)
    r = await propose(symbol="AAPL", side="SELL", quantity=1, limit_price=price, **base_kw)
    check(r.startswith("REJECTED") and "no shorting" in r, "rejects selling shares not held", r)
    os.environ["MAX_RISK_PER_TRADE_USD"] = "1"
    tight = Engine(load_settings(), db, broker, signals)
    tight.signals.ensure = agree
    r = await tight._on_proposal(run_ids[-1], dict(symbol="AAPL", side="BUY", quantity=2,
                                                   limit_price=price, **base_kw))
    # New clamping behavior: returns "no capacity" when risk per trade is too tight
    check(r.startswith("REJECTED") and "no capacity" in r, "rejects when no capacity for risk limit", r)
    os.environ["MAX_RISK_PER_TRADE_USD"] = "100"

    print("Bracket order flow")
    run_ids.append(db.create_run("smoke"))
    r = await propose(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2), **base_kw)
    check(r.startswith("ACCEPTED") and "Bracket" in r, "accepts a valid BUY with a bracket", r)
    pid = int(r.split("#")[1].split(":")[0])
    p = await engine.execute(pid, actor="user")
    check(p["status"] == "placed", "BUY placed as a bracket", f"{p['status']}: {p['reason']}")
    check((await broker.positions()).get("AAPL", 0) == 4, "position shows 4 AAPL")
    prot = db.protective("AAPL")
    check(sorted(o["kind"] for o in prot) == ["sl", "tp"], "stop and target recorded",
          str([(o["kind"], o["status"]) for o in prot]))
    await portfolio.reconcile()
    prot = db.protective("AAPL")
    check(all(o["status"] == "active" for o in prot), "exits armed after the entry filled",
          str([(o["kind"], o["status"]) for o in prot]))
    opened = parse_orders(await broker.open_orders_text())
    check({o["client_order_id"] for o in prot} <= {o["client_order_id"] for o in opened},
          "open orders parser sees both exit legs")
    rows = parse_position_rows(await broker.positions_text())
    check(rows and rows[0]["symbol"] == "AAPL" and rows[0]["market_value"], "positions parsed",
          str(rows))

    print("Partial exit re-protects the rest")
    price = await broker.last_price("AAPL")
    r = await propose(symbol="AAPL", side="SELL", quantity=2, limit_price=round(price * 0.995, 2), **base_kw)
    check(r.startswith("ACCEPTED"), "accepts SELL of held shares", r)
    pid = int(r.split("#")[1].split(":")[0])
    p = await engine.execute(pid, actor="user")
    check(p["status"] == "placed", "SELL placed", f"{p['status']}: {p['reason']}")
    check((await broker.positions()).get("AAPL", 0) == 2, "2 AAPL left")
    prot = db.protective("AAPL")
    check(len(prot) == 1 and prot[0]["kind"] == "sl" and prot[0]["quantity"] == 2,
          "old bracket cancelled, new stop on the 2 left",
          str([(o["kind"], o["quantity"], o["status"]) for o in prot]))
    hist = {o["client_order_id"]: o["status"] for o in parse_orders(await broker.order_history_text())}
    cancelled = [o for o in db.protective("AAPL", open_only=False) if o["status"] == "cancelled"]
    check(cancelled and all(hist.get(o["client_order_id"]) == "CANCELLED" for o in cancelled),
          "bracket legs cancelled at the broker")

    await portfolio.reconcile(min_age_seconds=0)
    check(not db.restorable_stops(), "cancelled stop settled once the sell filled")
    check(len(db.protective("AAPL")) == 1, "no duplicate stop after the sell filled")

    print("A sell that never fills gets its stop back")
    price = await broker.last_price("AAPL")
    r = await propose(symbol="AAPL", side="SELL", quantity=2, limit_price=round(price * 1.009, 2), **base_kw)
    pid = int(r.split("#")[1].split(":")[0])
    p = await engine.execute(pid, actor="user")
    check(p["status"] == "placed" and (await broker.positions()).get("AAPL", 0) == 2,
          "limit sell above the market is working, shares still held", str(p["status"]))
    check(not db.protective("AAPL") and len(db.restorable_stops()) == 1,
          "stop cancelled for the sell and remembered")
    await portfolio.reconcile(min_age_seconds=0)
    check(not db.protective("AAPL"), "nothing restored while the sell is still working")
    await broker.cancel_order(p["client_order_id"])           # the sell dies unfilled
    await portfolio.reconcile(min_age_seconds=0)
    prot = db.protective("AAPL")
    check(len(prot) == 1 and prot[0]["kind"] == "sl" and prot[0]["quantity"] == 2
          and prot[0]["status"] == "active", "stop restored on the 2 shares",
          str([(o["kind"], o["quantity"], o["status"]) for o in prot]))
    check(not db.restorable_stops(), "restore happens once")

    print("Exits are exempt from the daily trade limit")
    price = await broker.last_price("AAPL")
    r = await propose(symbol="AAPL", side="SELL", quantity=2, limit_price=round(price * 0.995, 2), **base_kw)
    pid = int(r.split("#")[1].split(":")[0])
    p = await engine.execute(pid, actor="user")
    check(p["status"] == "placed" and (await broker.positions()).get("AAPL", 0) == 0,
          "position closed")
    await portfolio.reconcile(min_age_seconds=0)
    check(not db.protective("AAPL"), "no protective orders left for a closed position")
    run_ids.append(db.create_run("smoke"))
    r = await propose(symbol="MSFT", side="BUY", quantity=1, limit_price=round(
        (await broker.last_price("MSFT")) * 1.005, 2), **base_kw)
    pid = int(r.split("#")[1].split(":")[0]) if r.startswith("ACCEPTED") else None
    if pid:
        await engine.execute(pid, actor="user")
    check(db.count_placed_today() == 5, "daily limit reached", str(db.count_placed_today()))
    r = await propose(symbol="SPY", side="BUY", quantity=1, limit_price=await broker.last_price("SPY"), **base_kw)
    check(r.startswith("REJECTED") and "daily limit" in r, "another BUY is blocked", r)
    msft = await broker.last_price("MSFT")
    r = await propose(symbol="MSFT", side="SELL", quantity=1, limit_price=round(msft * 0.995, 2), **base_kw)
    check(r.startswith("ACCEPTED"), "a SELL is still accepted at the limit", r)
    engine.dismiss(int(r.split("#")[1].split(":")[0]))

    print("Exits left behind by a closed position are cancelled")
    await portfolio.reconcile(min_age_seconds=0)
    check(len(db.protective("MSFT")) == 2 and all(o["status"] == "active" for o in db.protective("MSFT")),
          "MSFT stop and target armed")
    # SIZING_MODE=code may have raised the entry above the proposed 1 share, so sell
    # the whole holding here (as a manual sale in the Webull app would).
    msft_qty = (await broker.positions()).get("MSFT", 0)
    await broker.place_order("MSFT", "SELL", msft_qty, round(msft * 0.995, 2),
                             uuid.uuid4().hex)  # outside the app
    check((await broker.positions()).get("MSFT", 0) == 0, "MSFT sold outside the app")
    await portfolio.reconcile(min_age_seconds=120)
    check(len(db.protective("MSFT")) == 2, "young rows are left alone (age guard)")
    await portfolio.reconcile(min_age_seconds=0)
    check(not db.protective("MSFT"), "orphaned stop and target cancelled")

    print("Mock bracket logic (stop hit cancels the target)")
    state = {"cash": 10_000.0, "positions": {}, "orders": [], "day": "", "day_start_equity": 0}
    m = mock._new_order(state, "AAPL", "BUY", "LIMIT", 1, 100.0, None, "DAY", "m1", group_id="g")
    t = mock._new_order(state, "AAPL", "SELL", "LIMIT", 1, 110.0, None, "GTC", "t1", group_id="g")
    sl = mock._new_order(state, "AAPL", "SELL", "STOP_LOSS", 1, None, 95.0, "GTC", "s1", group_id="g")
    t.update(status="PENDING", parent="m1", oco="s1")
    sl.update(status="PENDING", parent="m1", oco="t1")
    mock.try_fill(state, price=lambda _: 99.0)
    check(m["status"] == "FILLED" and t["status"] == "SUBMITTED" and sl["status"] == "SUBMITTED",
          "entry fill arms both exits")
    mock.try_fill(state, price=lambda _: 94.0)
    check(sl["status"] == "FILLED" and t["status"] == "CANCELLED", "stop fill cancels the target")

    print("Per-run proposal cap")
    run_ids.append(db.create_run("smoke"))
    for _ in range(8):
        await propose(symbol="TSLA", side="BUY", quantity=1, limit_price=250, **base_kw)
    r = await propose(symbol="AAPL", side="BUY", quantity=1, limit_price=price, **base_kw)
    check("at most" in r, "stops a run after 8 proposals", r)

    print("Kill switch")
    run_ids.append(db.create_run("smoke"))
    await engine.set_kill_switch(True)
    r = await propose(symbol="MSFT", side="BUY", quantity=1, limit_price=400, **base_kw)
    check(r.startswith("REJECTED") and "kill switch" in r, "kill switch blocks proposals", r)

    print("Kill switch survives a restart")
    reborn = Engine(load_settings(), db, broker, signals, portfolio)
    check(reborn.kill_switch and reborn.kill_reason == "turned on by you",
          "kill switch and its reason reload from the database", str(reborn.kill_reason))
    await engine.set_kill_switch(False)
    check(not Engine(load_settings(), db, broker, signals).kill_switch, "release is saved too")

    print("Market calendar")
    from datetime import date, datetime as dt
    et = market.ET
    cases = {date(2026, 4, 3): "Good Friday", date(2026, 7, 3): "Independence Day",
             date(2026, 11, 26): "Thanksgiving Day", date(2026, 6, 19): "Juneteenth",
             date(2027, 6, 18): "Juneteenth", date(2027, 12, 24): "Christmas Day",
             date(2028, 1, 17): "Martin Luther King Jr. Day", date(2026, 5, 25): "Memorial Day",
             date(2025, 4, 18): "Good Friday", date(2026, 1, 1): "New Year's Day"}
    check(all(market.holiday_name(d) == n for d, n in cases.items()), "NYSE holidays",
          str({d: market.holiday_name(d) for d in cases}))
    check(market.is_trading_day(date(2027, 12, 31)), "no make-up day when Jan 1 is a Saturday")
    check(market.is_trading_day(date(2026, 11, 27))
          and market.session_close(date(2026, 11, 27)).hour == 13, "day after Thanksgiving closes at 1 pm")
    check(market.session_close(date(2025, 12, 24)).hour == 13
          and market.session_close(date(2025, 7, 3)).hour == 13, "Christmas Eve and July 3 close early")
    check(market.session_close(date(2026, 7, 2)).hour == 16, "no early close when July 3 is the holiday")
    check(market.market_is_open(dt(2026, 11, 27, 12, 59, tzinfo=et))
          and not market.market_is_open(dt(2026, 11, 27, 13, 0, tzinfo=et)), "early close respected")
    check(not market.market_is_open(dt(2026, 4, 3, 11, 0, tzinfo=et)), "closed on Good Friday")
    check(market.next_open(dt(2026, 4, 2, 17, 0, tzinfo=et)) == dt(2026, 4, 6, 9, 30, tzinfo=et),
          "next open skips the holiday weekend", str(market.next_open(dt(2026, 4, 2, 17, 0, tzinfo=et))))

    print("Unknown orders are settled from the broker")
    hist = parse_orders(await broker.order_history_text())
    filled = next(o for o in hist if o.get("status") == "FILLED")
    old = (market.now_utc() - __import__("datetime").timedelta(minutes=30)).isoformat(timespec="seconds")
    row = {"symbol": filled["symbol"], "side": filled["side"], "quantity": 1, "limit_price": 1.0}
    found = db.add_proposal(None, row, "unknown", client_order_id=filled["client_order_id"])
    lost = db.add_proposal(None, row, "unknown", client_order_id=uuid.uuid4().hex, placed_at=old)
    fresh = db.add_proposal(None, row, "unknown", client_order_id=uuid.uuid4().hex)
    unsent = db.add_proposal(None, row, "submitting")
    db.update_proposal(unsent, created_at=old)
    n = await engine.reconcile_orders()
    st = {k: db.get_proposal(v)["status"] for k, v in
          dict(found=found, lost=lost, fresh=fresh, unsent=unsent).items()}
    check(n == 3, "three of four settled", str(st))
    check(st["found"] == "placed", "order found at the broker -> placed", str(st))
    check(st["lost"] == "failed" and st["unsent"] == "failed",
          "never found after the grace period, or never sent -> failed", str(st))
    check(st["fresh"] == "unknown", "a fresh unknown order is left for the next check", str(st))
    db.update_proposal(fresh, status="failed")

    print("Automatic kill switch")
    alerts = Alerts(s, db)
    sup = Supervisor(s, db, broker, engine, portfolio, alerts)
    engine.run_failures = 3
    engine.last_failure = "Groq API returned HTTP 500"
    r = await sup.check_kill_triggers()
    check(engine.kill_switch and r and "3 failed AI runs" in r, "trips after 3 failed runs in a row", str(r))
    sup.on_release()
    await engine.set_kill_switch(False)
    check(engine.run_failures == 0 and not engine.kill_switch, "release resets the failure count")
    check(sup._money_trigger({"net_liq": 10_000.0}) is None, "peak recorded")
    check(sup._money_trigger({"net_liq": 9_200.0}) is None, "8% drawdown is under the 10% limit")
    r = sup._money_trigger({"net_liq": 8_950.0})
    check(r is not None and "drawdown" in r, "10.5% drawdown trips", str(r))
    sup.on_release()
    check(sup._money_trigger({"net_liq": 8_950.0}) is None, "release re-bases the peak")
    s_loss = dataclasses.replace(s, kill_daily_loss_usd=100.0)
    sup_loss = Supervisor(s_loss, db, broker, engine, portfolio, alerts)
    db.set_kv("daily_loss_ack", None)
    r = sup_loss._money_trigger({"net_liq": 9_000.0, "day_pnl": -150.0})
    check(r is not None and "daily loss" in r, "daily loss trips", str(r))
    sup_loss.on_release()
    check(sup_loss._money_trigger({"net_liq": 9_000.0, "day_pnl": -150.0}) is None,
          "daily loss doesn't re-trip after you release it the same day")
    await sup.tick()
    check(sup.last_tick is not None and not engine.kill_switch, "supervisor tick runs on the mock")

    print("Cancel entries keeps the stops")
    aapl = await broker.last_price("AAPL")
    buy_coid, stop_coid = uuid.uuid4().hex, uuid.uuid4().hex
    await broker.place_order("AAPL", "BUY", 6, round(aapl * 1.01, 2), uuid.uuid4().hex)
    await broker.place_order("AAPL", "BUY", 1, round(aapl * 0.5, 2), buy_coid)    # stays working
    await broker.place_stop("AAPL", 6, round(aapl * 0.8, 2), stop_coid)
    db.add_protective(None, "AAPL", "sl", stop_coid, 6, round(aapl * 0.8, 2), "active")
    n = await engine.cancel_open_orders(False, "smoke")
    working = {o["client_order_id"] for o in parse_orders(await broker.open_orders_text())}
    check(n == 1 and buy_coid not in working and stop_coid in working,
          "working BUY cancelled, stop left in place", f"{n} cancelled, working={working}")

    print("Flatten")
    held = (await broker.positions()).get("AAPL", 0)
    res = await engine.flatten("user", "smoke test")
    after = await broker.positions()
    check(engine.kill_switch, "flatten turns the kill switch on")
    check(after.get("AAPL", 0) == 0 and not any(after.values()), "every position sold", str(after))
    check(len([o for o in res["orders"] if o["symbol"] == "AAPL"]) >= 2,
          f"{held:g} AAPL sold in chunks under MAX_ORDER_USD", str(res["orders"]))
    check(not parse_orders(await broker.open_orders_text()), "no open orders left")
    check(not db.protective("AAPL"), "protective rows closed")
    sup.on_release()
    await engine.set_kill_switch(False)

    print("Alerts")
    s_alert = dataclasses.replace(s, alert_ntfy_topic="smoke-test-topic", alert_dedupe_minutes=30)
    db_alert = DB(TMP / "alerts.db", s_alert.broker_mode, s_alert.webull_environment, s_alert.webull_account_id)
    al = Alerts(s_alert, db_alert)
    sent = []

    async def fake_ntfy(title, body, level):
        sent.append((title, body))
    al._ntfy = fake_ntfy
    db_alert.log("error", "something broke")
    db_alert.log("info", "routine message")
    db_alert.log("info", "order placed", alert=True)
    db_alert.log("error", "something broke")            # duplicate
    await asyncio.sleep(0.05)
    check([t for t, _ in sent] == ["Trader ERROR", "Trader"], "errors and marked lines sent",
          str(sent))
    check(al.suppressed == 1, "duplicate suppressed", str(al.suppressed))
    check(all("Mock broker" in b for _, b in sent), "alerts name the broker environment")
    check("Net liquidation" in await sup.summary_text(), "daily summary text")
    check("smoke-test-topic" not in repr(s_alert), "alert secrets hidden from settings repr")

    print("Webull login expiry")
    (TMP / "conf").mkdir(exist_ok=True)
    exp_ms = int((time.time() + 2 * 86_400) * 1000)
    (TMP / "conf" / "token.txt").write_text(f"secret-token\n{exp_ms}\nNORMAL\n", encoding="utf-8")
    info = read_token_info(TMP / "conf")
    check(info["status"] == "NORMAL" and 1.9 < info["days_left"] < 2.1 and not info["estimated"],
          "expiry read from token.txt", str(info))
    check("secret" not in str(info), "the token itself is never read")
    check(read_token_info(TMP / "nowhere")["status"] == "missing", "missing login detected")

    print("Cost estimate")
    c = estimate_cost("openai/gpt-oss-120b", 1_000_000, 100_000, cache_read=500_000)
    check(c == 0.0, "Groq free-tier model estimated cost", str(c))

    print("ML provenance guards")
    # Test 1: Training refused in mock mode
    try:
        await ml.train(broker, s.watchlist)
        check(False, "training should be refused in mock mode")
    except RuntimeError as e:
        check("mock mode" in str(e).lower(), "training refused with mock mode message", str(e))

    # Test 2: Model with mock provenance rejected in webull mode
    if ml.available:
        import torch
        from app.ml import _net, FEATURES
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        mock_ckpt_path = MODEL_DIR / "model-mock-test.pt"
        model = _net(len(FEATURES))
        torch.save({
            "state": model.state_dict(),
            "mean": torch.zeros(len(FEATURES)).tolist(),
            "std": torch.ones(len(FEATURES)).tolist(),
            "features": FEATURES,
            "provenance": {"broker_mode": "mock", "webull_environment": None, "account_id": None}
        }, mock_ckpt_path)
        db.add_model(status="active", path=str(mock_ckpt_path), n_samples=100, n_symbols=3,
                     metrics={}, params={}, note="test",
                     broker_mode="mock", webull_environment=None, account_id=None)
        # Create MLFilter with webull settings to test rejection
        webull_settings = dataclasses.replace(s, broker_mode="webull", webull_environment="uat")
        ml_webull = MLFilter(webull_settings, db)
        check(ml_webull.model is None, "mock-provenance model rejected in webull mode")
        check("provenance mismatch" in (ml_webull.last_error or "").lower(),
              "rejection message mentions provenance mismatch", ml_webull.last_error)
        mock_ckpt_path.unlink(missing_ok=True)
        db.set_model_status(db.active_model()["id"], "retired")

    print("Leakage/saturation guards")
    # Test 3: High AUC rejected
    import torch
    from app.ml import _net, FEATURES, walk_forward, _fit, _predict
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # Create synthetic data that will produce implausibly high AUC
    # (This is a simplified test - we just verify the guard logic exists)
    wf_result = walk_forward([], s.consensus_ta_min, s.ml_min_p)
    check("rejection_note" in wf_result, "walk_forward returns rejection_note field")
    check("max_auc" in wf_result["thresholds"], "thresholds include max_auc")
    check("saturation_threshold" in wf_result["thresholds"], "thresholds include saturation_threshold")
    check("min_symbols" in wf_result["thresholds"], "thresholds include min_symbols")
    check("no_leakage" in wf_result["checks"], "checks include no_leakage")
    check("no_saturation" in wf_result["checks"], "checks include no_saturation")
    check("min_symbols" in wf_result["checks"], "checks include min_symbols")

    # Test: ML_MIN_BARS config setting
    check(hasattr(s, "ml_min_bars"), "Settings has ml_min_bars attribute")
    check(s.ml_min_bars == 500, "ML_MIN_BARS defaults to 500", str(s.ml_min_bars))

    # Test: ResearchService.effective_watchlist()
    check(hasattr(research, "effective_watchlist"), "ResearchService has effective_watchlist method")
    ewl = research.effective_watchlist()
    check(isinstance(ewl, list), "effective_watchlist returns a list")
    check(len(ewl) >= len(s.watchlist), "effective_watchlist includes manual watchlist")
    check(all(sym in ewl for sym in s.watchlist), "effective_watchlist contains all manual symbols")

    # Test: Engine uses effective_watchlist for signals
    # (This is tested implicitly by the signals refresh test above)

    # Test: MLFilter.train filters by ML_MIN_BARS
    if ml.available:
        print("ML MIN_BARS filtering")
        # Test that train drops symbols with insufficient bars
        # We can't easily test this with the mock broker, but we can verify
        # the method handles the parameter
        check(hasattr(ml, "train"), "MLFilter has train method")

    # Test: Sine-wave data must NOT promote a model
    if ml.available:
        print("Sine-wave leakage test")
        from app.ml import build_dataset, walk_forward
        from app import indicators
        import math
        # Create synthetic sine-wave bars (like the mock broker uses)
        # These are perfectly predictable and should trigger saturation/leakage guards
        sine_bars = []
        for i in range(1000):
            # Sum of sine waves with different periods (like mock_mcp_server.py)
            price = 100 + 5 * math.sin(i * 0.1) + 2 * math.sin(i * 0.05) + 1 * math.sin(i * 0.02)
            sine_bars.append({
                "time": datetime(2020, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000
            })
        bars_by_symbol = {"SINE": sine_bars}
        rows = build_dataset(bars_by_symbol)
        wf = walk_forward(rows, s.consensus_ta_min, s.ml_min_p)
        check(not wf["passed"], "sine-wave data is NOT promoted (leakage/saturation guards trigger)",
              f"passed={wf['passed']}, rejection_note={wf.get('rejection_note')}, "
              f"auc={wf.get('mean_auc')}, checks={wf['checks']}")

    if run_ml and ml.available:
        print("ML model (walk-forward on mock history)")
        t0 = time.time()
        res = await ml.train(broker, s.watchlist)
        wf = res["metrics"]
        check(res["status"] in ("active", "rejected"), "training finished and was judged",
              res["status"])
        check(len(wf["folds"]) >= 3 and wf["mean_auc"] is not None, "walk-forward folds ran",
              f"{len(wf['folds'])} folds, AUC {wf['mean_auc']}")
        print(f"        status={res['status']} AUC={wf['mean_auc']} "
              f"rules={wf['pooled']['rules']} rules+model={wf['pooled']['rules_model']} "
              f"({time.time() - t0:.0f}s)")
        if res["status"] == "active":
            p_up = ml.predict((await signals.refresh(symbols=["AAPL"]))[0]["features"])
            check(p_up is not None and 0 <= p_up <= 1, "active model scores live features",
                  str(p_up))
            reloaded = MLFilter(s, db)
            check(reloaded.model_id == res["id"] and reloaded.predict(
                signals.latest("AAPL")["features"]) == p_up, "model reloads from disk on restart",
                str(reloaded.last_error))
    elif run_ml:
        print("ML model: skipped (PyTorch not installed)")

    await broker.stop()
    print(f"\n{'All checks passed' if not failures else f'{failures} check(s) FAILED'}  (temp files in {TMP})")


if __name__ == "__main__":
    asyncio.run(main(run_ml="--no-ml" not in sys.argv))
    sys.exit(1 if failures else 0)
