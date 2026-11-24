#!/usr/bin/env python3
"""Run baseline backtest with current watchlist/universe.

Live parity (QA H7): ML on by default (walk-forward retrained); pass --no-ml for rules-only.
"""

import argparse
import asyncio
import sys
import os

# Set all backtest env vars BEFORE loading settings
os.environ['BACKTEST_DATA_DIR'] = '/tmp/bars'
os.environ['BACKTEST_RESULTS_DIR'] = '/tmp/backtests'
os.environ['BACKTEST_UNIVERSE'] = ''
os.environ['BACKTEST_MIN_BARS'] = '60'
os.environ['BACKTEST_INITIAL_CASH'] = '25000'
os.environ['BACKTEST_BENCHMARK_SYMBOL'] = 'SPY'
os.environ['BACKTEST_COMMISSION_PER_ORDER'] = '1.0'
os.environ['BACKTEST_SLIPPAGE_BPS'] = '5.0'
os.environ['BACKTEST_ATR_SLIPPAGE_MULT'] = '0.03'
os.environ['BACKTEST_IMPACT_BPS'] = '5.0'
os.environ['BACKTEST_MAX_IMPACT_BPS'] = '100.0'
os.environ['BACKTEST_MAX_GROSS_EXPOSURE_PCT'] = '100.0'
os.environ['BACKTEST_MAX_HOLD_DAYS'] = '20'
os.environ['BACKTEST_EXIT_ON_TREND_BREAK'] = 'true'
os.environ['BACKTEST_FETCH_COUNT'] = '1000'
os.environ['BACKTEST_FETCH_DELAY_SECONDS'] = '1.0'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import load_settings
from app.backtest.engine import run_backtest
from app.mock_mcp_server import price_of
from datetime import datetime, timedelta, timezone
import time
import random

class MockBrokerForBacktest:
    def __init__(self):
        self.base_prices = {'AAPL': 230.0, 'MSFT': 430.0, 'NVDA': 140.0, 'SPY': 590.0, 'QQQ': 510.0,
                           'AMZN': 200.0, 'GOOGL': 175.0, 'META': 580.0, 'TSLA': 250.0}

    async def daily_bars(self, symbol: str, count: int) -> list[dict]:
        bars = []
        base_time = time.time() - count * 86400 * 1.5
        for i in range(count):
            bar_time = base_time + i * 86400
            opens = []
            highs = []
            lows = []
            closes = []
            for j in range(20):
                t = bar_time + j * 4320
                p = price_of(symbol, t)
                opens.append(p)
                highs.append(p)
                lows.append(p)
                closes.append(p)
            random.seed(hash(symbol + str(i)) % 2**32)
            open_ = opens[0] * (1 + random.gauss(0, 0.002))
            close = closes[-1] * (1 + random.gauss(0, 0.002))
            high = max(highs) * (1 + abs(random.gauss(0, 0.005)))
            low = min(lows) * (1 - abs(random.gauss(0, 0.005)))
            high = max(high, open_, close)
            low = min(low, open_, close)
            volume = random.uniform(1_000_000, 10_000_000)
            bar_date = datetime.fromtimestamp(bar_time, tz=timezone.utc).date()
            bars.append({
                'time': datetime.combine(bar_date, datetime.min.time()).replace(tzinfo=timezone.utc),
                'open': round(open_, 2),
                'high': round(high, 2),
                'low': round(low, 2),
                'close': round(close, 2),
                'volume': volume,
            })
        return bars


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--no-ml', action='store_true', help='rules-only (not live parity)')
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.no_ml:
        import dataclasses
        settings = dataclasses.replace(settings, backtest_ml_enabled=False)

    mock = MockBrokerForBacktest()

    def progress(msg):
        print(msg)

    result = run_backtest(
        settings,
        universe=None,
        broker=mock,
        progress_callback=progress,
        auto_ml=True,
    )
    print(result.limitations)

    print('=== BACKTEST RESULT ===')
    print(f'Universe: {result.universe}')
    print(f'Start: {result.start_date}, End: {result.end_date}')
    print(f'Trades: {len(result.trades)}')
    from app.metrics import flatten_summary
    m = flatten_summary(result.metrics, result.trades)
    print(f'Total Return: {m.get("total_return", 0)*100:.2f}%')
    print(f'CAGR: {m.get("annualized_return", 0)*100:.2f}%')
    print(f'Sharpe: {m.get("sharpe", 0):.2f}')
    print(f'Max Drawdown: {m.get("max_drawdown", 0)*100:.2f}%')
    print(f'Win Rate: {m.get("backtest", {}).get("win_rate", 0)*100:.2f}%')
    print(f'Profit Factor: {m.get("profit_factor", 0):.2f}')
    print(f'Avg Trade: {m.get("avg_trade", 0)*100:.2f}%')
    print(f'Number of Trades: {m.get("backtest", {}).get("n_trades", 0)}')
    print(f'Turnover: {m.get("turnover", 0):.2f}')
    print(f'Return vs SPY: {m.get("excess_return", 0)*100:.2f}%')
    print(f'Alpha: {m.get("alpha", 0)*100:.2f}%')
    print(f'Beta: {m.get("beta", 0):.2f}')
    print(f'Information Ratio: {m.get("information_ratio", 0):.2f}')
    print(f'Excess Hit Rate: {m.get("excess_hit_rate", 0)*100:.2f}%')
    print(f'Significance: {m.get("significance", {})}')

    # Print trade details
    print('\n=== TRADES ===')
    for t in result.trades:
        print(f'{t.symbol} {t.entry_day}->{t.exit_day} entry={t.entry_fill_price:.2f} exit={t.exit_fill_price:.2f} qty={t.quantity} pnl={t.pnl:.2f} ({t.pnl_pct*100:.2f}%) reason={t.exit_reason} hold={t.hold_days}d')

    # Also return the result for potential further processing
    return result


if __name__ == "__main__":
    result = asyncio.run(main())