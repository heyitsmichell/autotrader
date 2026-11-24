#!/usr/bin/env python3
"""
UAT Order End-to-End Test

Exercises the real order path on Webull sandbox (UAT only):
- preview_order -> place_bracket -> wait for status -> cancel
- Prints each Webull response for debugging

Refuses to run unless WEBULL_ENVIRONMENT=uat
"""

import asyncio
import os
import sys
import uuid

# Windows consoles and redirected output default to cp1252, which cannot print the emoji below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Add project root to path
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.broker import Broker, BrokerError
from app.config import load_settings
from app.db import DB
from app.market import market_is_open
from app.signals import SignalService


async def main():
    # Check for --mock flag
    mock_mode = "--mock" in sys.argv

    # CRITICAL: Refuse to run unless in UAT mode (unless --mock)
    env = os.environ.get("WEBULL_ENVIRONMENT", "").lower()
    if not mock_mode and env != "uat":
        print("❌ REFUSING TO RUN: This script only runs against Webull UAT (sandbox).")
        print(f"   Current WEBULL_ENVIRONMENT={env!r}")
        print("   Set WEBULL_ENVIRONMENT=uat in your .env or environment.")
        print("   Or use --mock flag to test with mock broker.")
        sys.exit(1)

    broker_mode = os.environ.get("BROKER_MODE", "").lower()
    if not mock_mode and broker_mode != "webull":
        print("❌ REFUSING TO RUN: This script requires BROKER_MODE=webull")
        print(f"   Current BROKER_MODE={broker_mode!r}")
        sys.exit(1)

    if mock_mode:
        os.environ["BROKER_MODE"] = "mock"
        broker_mode = "mock"
        print("🔧 Running in MOCK mode for order flow testing")

    print("=" * 60)
    print("UAT ORDER END-TO-END TEST")
    print("=" * 60)
    print(f"Broker mode: {broker_mode}")
    print(f"Environment: {env}")
    print(f"Region: {os.environ.get('WEBULL_REGION_ID', 'us')}")
    print(f"Order execution: BROKER_EXECUTION={os.environ.get('BROKER_EXECUTION', 'mcp')}")
    print()

    # Check market hours
    if not market_is_open():
        print("⚠️  WARNING: Market is closed. Orders may not fill.")
        print("   Continuing anyway for bracket placement test...")
        print()

    # Load settings
    settings = load_settings()
    print(f"Watchlist: {settings.watchlist}")
    print(f"Max order USD: ${settings.max_order_usd:,.2f}")
    print(f"Max order qty: {settings.max_order_qty}")
    print(f"Bracket orders: {settings.bracket_orders}")
    print(f"SL ATR mult: {settings.sl_atr_mult}")
    print(f"TP ATR mult: {settings.tp_atr_mult}")
    print()

    # Initialize components
    from app.config import DATA_DIR
    from app.db import DB
    from app.signals import SignalService
    from app.sentiment import SentimentEngine
    from app.ml import MLFilter
    from app.research import ResearchService
    db = DB(DATA_DIR / "trader.db", settings.broker_mode, settings.webull_environment, settings.webull_account_id)
    broker = Broker(settings)
    sentiment = SentimentEngine(settings, broker, db)
    ml = MLFilter(settings, db)
    research = ResearchService(settings, broker, db)
    signals = SignalService(settings, broker, db, sentiment, ml, research)

    try:
        print("Starting broker connection...")
        await broker.start()
        print(f"✅ Broker connected (orders via {broker.router.transport_name})")
        print()

        # Get account ID
        account_id = await broker.account_id()
        print(f"Account ID: {account_id}")
        print()

        # Pick a symbol from watchlist (prefer one with reasonable price)
        symbol = settings.watchlist[0]  # AAPL typically
        print(f"Testing with symbol: {symbol}")
        print()

        # Get current price
        print("Fetching current price...")
        last_price = await broker.last_price(symbol)
        if not last_price or last_price <= 0:
            print(f"❌ Could not get price for {symbol}")
            return 1
        print(f"Last price: ${last_price:.2f}")
        print()

        # Calculate limit price (marketable - slightly above for BUY)
        limit_price = round(last_price * 1.005, 2)  # 0.5% above market
        quantity = 1
        print(f"Order: BUY {quantity} {symbol} @ ${limit_price:.2f} (LIMIT DAY)")
        print()

        # Preview order
        print("--- PREVIEW ORDER ---")
        try:
            preview = await broker.preview_order(symbol, "BUY", quantity, limit_price)
            print(f"Preview response:\n{preview}")
            print()
        except BrokerError as e:
            print(f"❌ Preview failed: {e}")
            return 1

        # Place bracket order
        print("--- PLACE BRACKET ORDER ---")
        coids = {
            "entry": uuid.uuid4().hex,
            "tp": uuid.uuid4().hex,
            "sl": uuid.uuid4().hex,
        }
        combo_id = uuid.uuid4().hex

        # Calculate stop loss and take profit using same logic as engine
        from app import ta
        # We need a signal for ATR - let's fetch one
        sig = await signals.ensure(symbol)
        if sig and sig.get("atr"):
            sl, tp = ta.levels(limit_price, sig["atr"], settings)
        else:
            # Fallback: 2% stop, 3% target
            sl = round(limit_price * 0.98, 2)
            tp = round(limit_price * 1.03, 2)

        print(f"Bracket: entry=${limit_price:.2f}, stop=${sl:.2f}, target=${tp:.2f}")
        print(f"Client order IDs: entry={coids['entry'][:8]}..., tp={coids['tp'][:8]}..., sl={coids['sl'][:8]}...")
        print(f"Combo ID: {combo_id[:8]}...")
        print()

        try:
            response = await broker.place_bracket(
                symbol, quantity, limit_price, tp, sl, coids, combo_id
            )
            print(f"Place bracket response:\n{response}")
            print()
        except BrokerError as e:
            print(f"❌ Place bracket failed: {e}")
            return 1

        # Wait and check order status
        print("--- CHECK ORDER STATUS ---")
        print("Waiting 3 seconds for order to process...")
        await asyncio.sleep(3)

        try:
            open_orders = await broker.open_orders_text()
            print(f"Open orders:\n{open_orders}")
            print()
        except BrokerError as e:
            print(f"❌ Could not fetch open orders: {e}")

        # Check order history
        print("--- ORDER HISTORY ---")
        try:
            from app.portfolio import parse_orders
            hist_text = await broker.call("get_order_history", account_id=account_id)
            orders = parse_orders(hist_text)
            print(f"Recent orders ({len(orders)}):")
            for o in orders:
                if o.get("client_order_id") in coids.values() or o.get("combo_id") == combo_id:
                    print(f"  {o.get('symbol')} {o.get('side')} {o.get('quantity')} @ {o.get('limit_price')} "
                          f"status={o.get('status')} coid={o.get('client_order_id')[:8]}...")
            print()
        except Exception as e:
            print(f"❌ Could not fetch order history: {e}")

        # Cancel everything this test created: the entry first (its stop/target are attached to it),
        # then any leg that is still working. Nothing is left open on the sandbox account.
        print("--- CANCEL ORDERS ---")
        from app.portfolio import parse_orders
        for leg in ("entry", "tp", "sl"):
            try:
                open_now = {o.get("client_order_id") for o in parse_orders(await broker.open_orders_text())}
            except BrokerError as e:
                print(f"❌ Could not fetch open orders: {e}")
                open_now = set(coids.values())
            if coids[leg] not in open_now:
                print(f"{leg}: not open (nothing to cancel)")
                continue
            try:
                cancel_response = await broker.cancel_order(coids[leg])
                print(f"{leg}: cancel sent ({broker.router.transport_name}):")
                print(cancel_response)
            except BrokerError as e:
                print(f"❌ {leg}: cancel failed: {e}")
            await asyncio.sleep(2)

        # Verify cancellation
        print("--- VERIFY CANCELLATION ---")
        await asyncio.sleep(2)
        try:
            remaining = [o for o in parse_orders(await broker.open_orders_text())
                         if o.get("client_order_id") in coids.values()]
            if remaining:
                print("❌ Still open:")
                for o in remaining:
                    print(f"  {o.get('side')} {o.get('symbol')} {o.get('order_type')} status={o.get('status')} "
                          f"coid={o.get('client_order_id')}")
            else:
                print("✅ No test orders left open")
        except BrokerError as e:
            print(f"❌ Could not fetch open orders: {e}")

        # Position check: an entry that filled before the cancel leaves 1 share on the sandbox account
        try:
            held = (await broker.positions()).get(symbol, 0)
            print(f"Sandbox position in {symbol} after the test: {held:g}")
        except BrokerError as e:
            print(f"❌ Could not fetch positions: {e}")
        print()
        print("=" * 60)
        print("UAT ORDER TEST COMPLETE")
        print("=" * 60)
        return 0

    finally:
        await broker.stop()
        print("Broker connection closed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))