#!/usr/bin/env python3
"""Manual backtest on synthetic (mock) bars.

Live parity (QA H7): the default mirrors the live decision rule, with the ML filter ON and
retrained walk-forward (each prediction uses a model fit on past data only). Use:

  python run_backtest_manual.py              # ML on (settings.backtest_ml_enabled, default True)
  python run_backtest_manual.py --no-ml      # rules-only (consensus_ta_min threshold)
  python run_backtest_manual.py --compare    # both, side by side, on the same bars

If PyTorch is not installed the ML variant degrades to rules-only with a clear message.
The report always prints what a daily-bar backtest cannot reproduce (sentiment, first 15
minutes, earnings).
"""

import argparse
import asyncio
import sys
import os

# Backtest env defaults, applied BEFORE loading settings. Anything already set in the
# environment (for example by run_backtest_matrix.py) wins, including BACKTEST_ML_ENABLED.
for _k, _v in {
    'BACKTEST_DATA_DIR': '/tmp/bars',
    'BACKTEST_RESULTS_DIR': '/tmp/backtests',
    'BACKTEST_UNIVERSE': '',
    'BACKTEST_MIN_BARS': '60',
    'BACKTEST_INITIAL_CASH': '25000',
    'BACKTEST_BENCHMARK_SYMBOL': 'SPY',
    'BACKTEST_COMMISSION_PER_ORDER': '1.0',
    'BACKTEST_SLIPPAGE_BPS': '5.0',
    'BACKTEST_ATR_SLIPPAGE_MULT': '0.03',
    'BACKTEST_IMPACT_BPS': '5.0',
    'BACKTEST_MAX_IMPACT_BPS': '100.0',
    'BACKTEST_MAX_GROSS_EXPOSURE_PCT': '100.0',
    'BACKTEST_MAX_HOLD_DAYS': '20',
    'BACKTEST_EXIT_ON_TREND_BREAK': 'true',
    'BACKTEST_FETCH_COUNT': '1000',
    'BACKTEST_FETCH_DELAY_SECONDS': '1.0',
}.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import load_settings
from app.mock_mcp_server import price_of
from datetime import datetime, timedelta, timezone
import time
import random


class MockBrokerForBacktest:
    def __init__(self):
        self.base_prices = {'AAPL': 230.0, 'MSFT': 430.0, 'NVDA': 140.0, 'SPY': 590.0, 'QQQ': 510.0,
                           'AMZN': 200.0, 'GOOGL': 175.0, 'META': 580.0, 'TSLA': 250.0}

    async def daily_bars(self, symbol: str, count: int):
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


async def run_backtest_manual(settings, broker, *, ml=None, loaded=None):
    """``ml``: True/False forces the ML filter on/off (default: settings.backtest_ml_enabled).
    ``loaded``: optional (bars_dict, errors) so --compare reuses the same bars."""
    import dataclasses
    if ml is not None and ml != settings.backtest_ml_enabled:
        settings = dataclasses.replace(settings, backtest_ml_enabled=bool(ml))
    from app.universe import get_universe_sync, build_universe_config, get_predefined_universe

    u_config = build_universe_config(settings)
    if u_config.name == 'custom':
        universe = u_config.custom_symbols
    else:
        universe = get_predefined_universe(u_config.name)
        if not universe and u_config.name == 'sp500_top':
            universe = get_predefined_universe('sector_etfs')
    universe = list(dict.fromkeys(universe + [s.upper() for s in settings.watchlist]))

    benchmark_symbol = settings.backtest_benchmark_symbol.upper()
    all_symbols = list(set(universe + [benchmark_symbol]))

    print(f"Loading bars for {len(all_symbols)} symbols...")

    from app.backtest.data import load_bars
    if loaded is not None:
        bars_dict, errors = loaded
    else:
        bars_dict, errors = await load_bars(settings, all_symbols, broker=broker, force=True, csv_dir=None)

    print("Loaded bars: " + ", ".join(f"{s}({len(b)})" for s, b in bars_dict.items()))

    min_bars = settings.backtest_min_bars
    valid_symbols = {s: b for s, b in bars_dict.items() if len(b) >= min_bars}
    dropped = set(all_symbols) - set(valid_symbols.keys())
    if dropped:
        print("Dropped (insufficient bars): " + ", ".join(sorted(dropped)))

    print("Building features...")
    from app.backtest.signals import build_features
    all_features = {}
    for sym, bars in valid_symbols.items():
        all_features[sym] = build_features(bars)

    all_dates = set()
    for sym, bars in valid_symbols.items():
        for bar in bars:
            all_dates.add(bar['time'].date())
    common_dates = sorted(all_dates)

    price_maps = {}
    for sym, bars in valid_symbols.items():
        price_maps[sym] = {bar['time'].date(): bar for bar in bars}

    if benchmark_symbol not in valid_symbols:
        raise ValueError(f"Benchmark {benchmark_symbol} has insufficient data")
    bench_bars = valid_symbols[benchmark_symbol]
    bench_price_map = {bar['time'].date(): bar for bar in bench_bars}

    from app.backtest.engine import Portfolio
    from app.backtest.costs import CostModel
    portfolio = Portfolio(settings, cash=settings.backtest_initial_cash)
    cost_model = CostModel.from_settings(settings)

    print(f"Running backtest over {len(common_dates)} days...")

    from app.backtest.signals import signal_at, build_ml_predictor
    ml_predictor, ml_status = build_ml_predictor(settings, valid_symbols)
    print(f"ML filter: {ml_status}")

    gate_stats = {
        'total_evaluated': 0,
        'ta_blocked': 0,
        'regime_blocked': 0,
        'sentiment_blocked': 0,
        'ml_blocked': 0,
        'cost_edge_blocked': 0,
        'market_regime_blocked': 0,
        'relative_strength_blocked': 0,
        'no_signal': 0,
        'passed': 0,
    }

    from app.consensus import evaluate
    from app import ta

    for i in range(len(common_dates)):
        day = common_dates[i]
        if i == 0:
            current_prices = {sym: price_maps[sym][day]['close'] for sym in valid_symbols if day in price_maps[sym]}
            portfolio.on_bar_start(day, current_prices)
            continue

        prev_day = common_dates[i - 1]

        current_prices = {sym: price_maps[sym][day]['open'] for sym in valid_symbols if day in price_maps[sym]}
        portfolio.on_bar_start(day, current_prices)

        # 1. Check exits
        for symbol in list(portfolio.positions.keys()):
            if symbol not in valid_symbols:
                continue
            bar = price_maps[symbol].get(day)
            if not bar:
                continue
            dollar_volume = bar['close'] * (bar.get('volume') or 0)
            exit_result = portfolio.check_exits(symbol, bar, day, cost_model, dollar_volume)
            if exit_result:
                exited, fill_price, reason = exit_result
                if exited:
                    portfolio.fill_sell(symbol, fill_price, day, reason, cost_model.commission(), 0.0)

        # 2. Evaluate new entries
        for symbol in universe:
            if symbol not in valid_symbols:
                continue
            if symbol in portfolio.positions:
                continue
            prev_bar = price_maps[symbol].get(prev_day)
            if not prev_bar:
                continue

            features = all_features[symbol]
            bar_list = valid_symbols[symbol]
            prev_idx = None
            for idx, b in enumerate(bar_list):
                if b['time'].date() == prev_day:
                    prev_idx = idx
                    break
            if prev_idx is None:
                continue

            benchmark_bars = valid_symbols.get(benchmark_symbol)
            benchmark_features = all_features.get(benchmark_symbol)
            sig_result = signal_at(bar_list, features, prev_idx, settings, ml_predictor=ml_predictor,
                                   benchmark_bars=benchmark_bars, benchmark_features=benchmark_features)
            if not sig_result.get('ok'):
                gate_stats['no_signal'] += 1
                continue

            sig = sig_result['signal']
            sl = sig_result['sl']
            tp = sig_result['tp']
            limit_price = sig.get('close') or prev_bar['close']
            atr = sig.get('atr')

            if atr is None or sl is None:
                continue

            p = {'side': 'BUY', 'limit_price': limit_price}
            gate_result = evaluate(p, sig, settings)
            gate_stats['total_evaluated'] += 1

            if not gate_result['ok']:
                for check in gate_result['checks']:
                    if not check['ok']:
                        if check['name'] == 'TA direction':
                            gate_stats['ta_blocked'] += 1
                        elif check['name'] == 'TA regime':
                            gate_stats['regime_blocked'] += 1
                        elif check['name'] == 'Sentiment':
                            gate_stats['sentiment_blocked'] += 1
                        elif check['name'] == 'ML filter':
                            gate_stats['ml_blocked'] += 1
                        elif check['name'] == 'Cost edge':
                            gate_stats['cost_edge_blocked'] += 1
                        elif check['name'] == 'Market regime':
                            gate_stats['market_regime_blocked'] += 1
                        elif check['name'] == 'Relative strength':
                            gate_stats['relative_strength_blocked'] += 1
                continue

            gate_stats['passed'] += 1

            qty, reason = portfolio.can_enter(symbol, limit_price, atr, sl)
            if qty <= 0:
                continue

            bar = price_maps[symbol].get(day)
            if not bar:
                continue
            dollar_volume = bar['close'] * (bar.get('volume') or 0)
            from app.backtest.costs import buy_fill
            fill_price = buy_fill(bar, limit_price, cost_model, atr=atr, quantity=qty, dollar_volume=dollar_volume)
            if fill_price is None:
                continue

            commission = cost_model.commission()
            slippage = cost_model.slippage_per_share(fill_price, atr=atr, order_notional=qty * fill_price, dollar_volume=dollar_volume)

            portfolio.fill_buy(symbol, qty, fill_price, day, atr, sl, tp,
                               sig.get('trend'), sig.get('ta_score'), sig.get('ml_p'),
                               commission, slippage)

    final_prices = {sym: price_maps[sym][common_dates[-1]]['close'] for sym in valid_symbols if common_dates[-1] in price_maps[sym]}
    portfolio.on_bar_start(common_dates[-1], final_prices)

    bench_equity = []
    if len(bench_bars) >= 2:
        first_close = bench_bars[0]['close']
        for bar in bench_bars:
            bench_equity.append(settings.backtest_initial_cash * (bar['close'] / first_close))
    else:
        bench_equity = [settings.backtest_initial_cash] * len(portfolio.equity_curve)

    account_values = portfolio.equity_curve
    bench_values = bench_equity[:len(account_values)]
    if len(bench_values) < len(account_values):
        bench_values = bench_values + [bench_values[-1]] * (len(account_values) - len(bench_values))

    traded_notional = []
    for trade in portfolio.trades:
        traded_notional.append(trade.quantity * trade.entry_fill_price)
        if trade.exit_fill_price:
            traded_notional.append(trade.quantity * trade.exit_fill_price)

    from app.metrics import summarize
    import statistics
    metrics = summarize(
        account_values, None, bench_values,
        turnover_notional=traded_notional,
        risk_free=settings.performance_risk_free_rate,
        min_days=settings.performance_min_days,
    )

    metrics['backtest'] = {
        'initial_cash': settings.backtest_initial_cash,
        'final_equity': portfolio.equity,
        'n_trades': len(portfolio.trades),
        'win_rate': sum(1 for t in portfolio.trades if t.pnl > 0) / len(portfolio.trades) if portfolio.trades else 0,
        'avg_hold_days': statistics.mean([t.hold_days for t in portfolio.trades]) if portfolio.trades else 0,
        'total_commission': sum(t.commission for t in portfolio.trades),
        'total_slippage': sum(t.slippage for t in portfolio.trades),
    }

    from app.backtest.engine import BacktestResult
    from app.backtest.signals import limitations_text
    return BacktestResult(
        equity_curve=portfolio.equity_curve,
        equity_dates=common_dates[:len(portfolio.equity_curve)],
        trades=portfolio.trades,
        benchmark_equity=bench_equity[:len(portfolio.equity_curve)],
        metrics=metrics,
        settings=settings,
        universe=universe,
        start_date=common_dates[0],
        end_date=common_dates[-1],
        errors=errors,
        ml_status=ml_status,
        limitations=limitations_text(settings, ml_status),
        bars=bars_dict,
    ), gate_stats


def _print_result(result, gate_stats, title='BACKTEST RESULT'):
    print(f'=== {title} ===')
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

    print()
    print('=== GATE STATISTICS ===')
    for k, v in gate_stats.items():
        print(f'{k}: {v}')

    exit_reasons = {}
    for t in result.trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
    print()
    print('=== EXIT REASONS ===')
    for reason, count in sorted(exit_reasons.items()):
        print(f'{reason}: {count}')
    print()
    print(result.limitations)
    return m


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument('--no-ml', action='store_true',
                     help='rules-only: ML filter off (the old default; not live parity)')
    grp.add_argument('--compare', action='store_true',
                     help='run rules-only and ML variants on the same bars and print both')
    args = ap.parse_args(argv)

    settings = load_settings()
    mock = MockBrokerForBacktest()

    if args.compare:
        loaded = None
        rows = {}
        for label, ml in (('RULES-ONLY (--no-ml)', False), ('ML (live parity)', True)):
            result, gate_stats = await run_backtest_manual(settings, mock, ml=ml, loaded=loaded)
            loaded = (result.bars, result.errors)
            m = _print_result(result, gate_stats, f'BACKTEST RESULT - {label}')
            rows[label] = (m, result)
            print()
        print('=== SIDE BY SIDE ===')
        keys = [('total_return', 'Total Return', 100, '%'), ('sharpe', 'Sharpe', 1, ''),
                ('max_drawdown', 'Max Drawdown', 100, '%'), ('excess_return', 'Return vs SPY', 100, '%')]
        print(f'{"":20s}' + ''.join(f'{k:>26s}' for k in rows))
        for key, name, mult, unit in keys:
            print(f'{name:20s}' + ''.join(f'{r[0].get(key, 0) * mult:>25.2f}{unit or " "}' for r in rows.values()))
        print(f'{"Trades":20s}' + ''.join(f'{len(r[1].trades):>26d}' for r in rows.values()))
        print()
        print(rows['ML (live parity)'][1].ml_status)
        return

    result, gate_stats = await run_backtest_manual(settings, mock, ml=False if args.no_ml else None)
    _print_result(result, gate_stats)


if __name__ == "__main__":
    asyncio.run(main())
