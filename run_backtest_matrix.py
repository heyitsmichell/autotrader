#!/usr/bin/env python3
"""Run backtest matrix with various feature combinations."""

import asyncio
import sys
import os
import subprocess
from pathlib import Path
import json
from datetime import datetime

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Define test configurations
CONFIGS = [
    # Name, env_overrides dict
    ("baseline", {}),
    ("regime_aware_entries", {"REGIME_AWARE_ENTRIES": "true"}),
    ("relative_strength", {"RELATIVE_STRENGTH_ENABLED": "true"}),
    ("volatility_scaling", {"VOL_SCALING_ENABLED": "true"}),
    ("market_regime_filter", {"MARKET_REGIME_FILTER": "true"}),
    ("trailing_stops", {"TRAILING_STOP_ENABLED": "true"}),
    ("all_features", {
        "REGIME_AWARE_ENTRIES": "true",
        "RELATIVE_STRENGTH_ENABLED": "true",
        "VOL_SCALING_ENABLED": "true",
        "MARKET_REGIME_FILTER": "true",
        "TRAILING_STOP_ENABLED": "true",
    }),
]

# Base environment (always set)
BASE_ENV = {
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
    'BACKTEST_ML_ENABLED': 'false',
    'BACKTEST_FETCH_COUNT': '1000',
    'BACKTEST_FETCH_DELAY_SECONDS': '1.0',
    # Task 3 universe settings
    'UNIVERSE_NAME': 'expanded',
    'UNIVERSE_TIER1_SIZE': '50',
    'UNIVERSE_TIER2_SIZE': '100',
    'UNIVERSE_TIER3_SIZE': '200',
    'UNIVERSE_SCAN_ALL_TIERS': 'true',
    'UNIVERSE_MIN_PRICE': '5.0',
    'UNIVERSE_MIN_AVG_DOLLAR_VOLUME': '20000000.0',
    'UNIVERSE_MIN_HISTORY_DAYS': '250',
    # Core-satellite
    'CORE_SYMBOL': 'SPY',
    'CORE_TARGET_PCT': '80.0',
    'SATELLITE_MAX_PCT': '20.0',
    # Risk
    'MAX_ORDER_USD': '5000.0',
    'MAX_POSITION_USD': '10000.0',
    'MAX_RISK_PER_TRADE_USD': '200.0',
    'MAX_DAILY_LOSS_USD': '500.0',
    # Consensus defaults
    'CONSENSUS_TA_MIN': '0.10',
    'CONSENSUS_TREND_FLOOR': 'neutral',
    'CONSENSUS_SENTIMENT_FLOOR': '-0.20',
    'CONSENSUS_ML_MIN_PROB': '0.55',
}

async def run_backtest(name: str, extra_env: dict) -> dict | None:
    """Run a single backtest configuration."""
    env = {**os.environ, **BASE_ENV, **extra_env}

    print(f"\n{'='*60}")
    print(f"Running: {name}")
    print(f"Extra env: {extra_env}")
    print(f"{'='*60}")

    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / 'run_backtest_manual.py')],
            env=env,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            print(f"ERROR: {result.stderr}")
            return None

        # Parse output
        output = result.stdout
        metrics = parse_output(output)
        metrics['config_name'] = name
        metrics['raw_output'] = output
        return metrics

    except subprocess.TimeoutExpired:
        print(f"TIMEOUT for {name}")
        return None
    except Exception as e:
        print(f"EXCEPTION for {name}: {e}")
        return None


def parse_output(output: str) -> dict:
    """Parse backtest output for key metrics."""
    metrics = {}

    lines = output.strip().split('\n')
    in_backtest = False
    in_gate = False
    in_exits = False

    for line in lines:
        line = line.strip()

        if line.startswith('=== BACKTEST RESULT ==='):
            in_backtest = True
            in_gate = False
            in_exits = False
            continue
        elif line.startswith('=== GATE STATISTICS ==='):
            in_backtest = False
            in_gate = True
            in_exits = False
            continue
        elif line.startswith('=== EXIT REASONS ==='):
            in_backtest = False
            in_gate = False
            in_exits = True
            continue

        if in_backtest and ':' in line:
            key, val = line.split(':', 1)
            key = key.strip().lower().replace(' ', '_').replace('%', 'pct').replace('/', '_per_')
            val = val.strip()

            # Convert numeric values
            if val.endswith('%'):
                try:
                    metrics[key] = float(val.rstrip('%')) / 100
                except:
                    metrics[key] = val
            else:
                try:
                    if '.' in val:
                        metrics[key] = float(val)
                    else:
                        metrics[key] = int(val)
                except:
                    metrics[key] = val

        elif in_gate and ':' in line:
            key, val = line.split(':', 1)
            key = f"gate_{key.strip().lower().replace(' ', '_')}"
            try:
                metrics[key] = int(val.strip())
            except:
                metrics[key] = val.strip()

        elif in_exits and ':' in line:
            key, val = line.split(':', 1)
            key = f"exit_{key.strip().lower().replace(' ', '_')}"
            try:
                metrics[key] = int(val.strip())
            except:
                metrics[key] = val.strip()

    return metrics


async def main():
    print("Starting backtest matrix...")
    print(f"Testing {len(CONFIGS)} configurations")

    all_results = []

    for name, extra_env in CONFIGS:
        result = await run_backtest(name, extra_env)
        if result:
            all_results.append(result)
            # Print key metrics
            print(f"\nKey metrics for {name}:")
            for k in ['total_return', 'cagr', 'sharpe', 'sortino', 'max_drawdown',
                      'win_rate', 'profit_factor', 'avg_trade', 'number_of_trades',
                      'exposure', 'turnover', 'return_vs_spy', 'alpha', 'beta',
                      'information_ratio', 'excess_hit_rate']:
                if k in result:
                    print(f"  {k}: {result[k]}")
        else:
            print(f"FAILED: {name}")

    # Save results
    output_file = f'/tmp/backtest_matrix_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(output_file, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nSaved results to {output_file}")

    # Print comparison table
    print("\n\n=== COMPARISON TABLE ===")
    print_comparison_table(all_results)


def print_comparison_table(results: list):
    """Print formatted comparison table."""
    if not results:
        print("No results to compare")
        return

    # Key metrics to compare
    metrics_order = [
        'total_return', 'cagr', 'sharpe', 'sortino', 'max_drawdown',
        'win_rate', 'profit_factor', 'avg_trade', 'number_of_trades',
        'exposure', 'turnover', 'return_vs_spy', 'alpha', 'beta',
        'information_ratio', 'excess_hit_rate',
        'gate_total_evaluated', 'gate_passed', 'gate_no_signal',
        'gate_ta_blocked', 'gate_regime_blocked', 'gate_market_regime_blocked',
        'gate_relative_strength_blocked', 'gate_ml_blocked', 'gate_cost_edge_blocked',
    ]

    # Header
    header = f"{'Config':<25}"
    for m in metrics_order:
        header += f" {m[:12]:>12}"
    print(header)
    print('-' * (25 + 13 * len(metrics_order)))

    for r in results:
        row = f"{r.get('config_name', 'unknown'):<25}"
        for m in metrics_order:
            val = r.get(m, 'N/A')
            if isinstance(val, float):
                if abs(val) < 0.01 and val != 0:
                    row += f" {val:>12.4f}"
                elif abs(val) < 1:
                    row += f" {val:>12.2%}"
                else:
                    row += f" {val:>12.2f}"
            elif isinstance(val, int):
                row += f" {val:>12d}"
            else:
                row += f" {str(val):>12}"
        print(row)


if __name__ == "__main__":
    asyncio.run(main())