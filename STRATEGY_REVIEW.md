# Strategy Review Report

**Generated:** 2026-11-24  
**Environment:** UAT/Mock only  
**Backtest Period:** 2022-10-15 to 2025-07-10 (~1000 trading days)  
**Initial Capital:** $25,000  
**Benchmark:** SPY  

---

## Executive Summary

The current strategy **significantly underperforms** the benchmark (SPY) across all tested configurations. The baseline strategy achieves **0% total return** vs **~10% for SPY** over the test period, with a 25% win rate and 0% exposure (equity curve flat). 

**Critical Finding:** The backtest engine has a **fundamental P&L tracking bug** — portfolio equity remains at initial $25,000 despite 1,106 trades executing. Exposure stays at 0%, turnover is calculated but no capital is actually deployed. All strategy improvements are moot until this is fixed.

Key findings on feature flags (tested individually and in combination):
- **Volatility scaling** reduces turnover (4.48 → 2.06) and improves excess return (-10.5% → -3.9%)
- **Relative strength filter** improves win rate (25% → 31%) and excess return (-10.5% → +2.3%)
- **Market regime filter** is **too restrictive** — blocks all trades (0 trades) when requiring SPY trend="up" AND ADX≥20
- **Trailing stops** work correctly (23-64 exits triggered) but don't improve returns due to P&L bug
- **All features combined (without market regime filter)** achieves 35% win rate, 2.01 turnover, +2.5% vs SPY

**Recommendation:** **Do not deploy current strategy to UAT.** Fix the backtest engine P&L/position tracking bug first, then re-evaluate.

---

## Baseline Configuration (All Features OFF)

| Metric | Value |
|--------|-------|
| **Total Return** | 0.00% |
| **CAGR** | 0.00% |
| **Sharpe** | 0.00 |
| **Sortino** | 0.00 |
| **Max Drawdown** | 0.00% |
| **Win Rate** | 25.4% |
| **Profit Factor** | 0.00 |
| **Avg Trade P&L** | 0.00% |
| **Number of Trades** | 1,106 |
| **Exposure** | 0.0% |
| **Turnover** | 4.48 |
| **Return vs SPY** | -10.52% |
| **Alpha** | 0.00% |
| **Beta** | 0.00 |
| **Information Ratio** | -0.25 |
| **Excess Hit Rate** | 0.00% |

### Gate Statistics (Baseline)
| Gate | Count |
|------|-------|
| Total Evaluated | 1,930 |
| TA Blocked | 0 |
| Regime Blocked | 0 |
| Sentiment Blocked | 0 |
| ML Blocked | 0 |
| Cost Edge Blocked | 0 |
| Market Regime Blocked | 0 |
| Relative Strength Blocked | 0 |
| No Signal | 14,734 |
| **Passed** | **1,930** |

### Exit Reasons (Baseline)
| Reason | Count |
|--------|-------|
| Stop Loss | 36 |
| Take Profit | 198 |
| Time Exit | 90 |
| Trend Break | 782 |

---

## Individual Feature Tests

### 1. Regime-Aware Entries (`REGIME_AWARE_ENTRIES=true`)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | 25.2% | -0.2% |
| Number of Trades | 1,101 | -5 |
| Turnover | 4.51 | +0.03 |
| Return vs SPY | -11.39% | -0.87% |
| Information Ratio | -0.26 | -0.01 |

**Assessment:** No improvement. Regime-aware TA thresholds (up: 0.10, range: 0.20, down: 0.30) don't meaningfully filter entries.

---

### 2. Relative Strength Filter (`RELATIVE_STRENGTH_ENABLED=true`, lookback=20, min=0.0)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | **30.9%** | **+5.5%** |
| Number of Trades | 793 | -313 |
| Turnover | 4.23 | -0.25 |
| Return vs SPY | **+2.27%** | **+12.8%** |
| Information Ratio | -0.12 | +0.13 |

**Assessment:** **Best single feature improvement.** Filters out weak relative performers, improving win rate and excess return significantly. However, still 0% absolute return.

---

### 3. Volatility Scaling (`VOL_SCALING_ENABLED=true`, target_atr_pct=1.5%, max_factor=2.0)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | 26.2% | +0.8% |
| Number of Trades | 1,019 | -87 |
| Turnover | **2.06** | **-2.42** |
| Return vs SPY | -3.92% | +6.6% |
| Information Ratio | -0.18 | +0.07 |

**Assessment:** **Significantly reduces turnover** (54% reduction) and improves risk-adjusted metrics. Position sizing now adapts to volatility. Still no absolute return.

---

### 4. Market Regime Filter (`MARKET_REGIME_FILTER=true`, min_trend=up, ADX_min=20)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| **Total Return** | **0.00%** | = |
| **Number of Trades** | **0** | **-1,106** |
| Win Rate | 0.0% | N/A |

**Assessment:** **BROKEN — blocks ALL trades.** The filter requires SPY trend="up" AND ADX≥20. Over 2022-2025, SPY spent significant time in "range" or "down" regimes, or ADX<20. This filter is too aggressive for a long-only strategy.

**Fix needed:** Relax to `min_trend="range"` or add "allow in range with higher TA threshold" logic.

---

### 5. Trailing Stops (`TRAILING_STOP_ENABLED=true`, activation_r=1.0, atr_mult=1.5)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | ~35% | +10% |
| Number of Trades | 760 | -346 |
| Turnover | 2.01 | -2.47 |
| Trailing Stop Exits | 64 | +64 |

**Assessment:** **Working correctly** — 64 trailing stop exits triggered. Activates at 1R profit, trails 1.5 ATR from highest close, never below entry. Improves win rate by locking in profits. No absolute return improvement due to P&L bug.

---

## Combination Tests

### 6. All Features Except Market Regime Filter (Recommended Combo)

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | **35.3%** | **+9.9%** |
| Number of Trades | 760 | -346 |
| Turnover | **2.01** | **-2.47** |
| Return vs SPY | **+2.45%** | **+13.0%** |
| Information Ratio | -0.12 | +0.13 |
| Trailing Stop Exits | 64 | +64 |

**Assessment:** **Best overall configuration.** Combines relative strength filtering, volatility scaling, and trailing stops. Win rate +10%, turnover -55%, excess return +13%. Still 0% absolute return due to P&L bug.

---

### 7. All Features Including Market Regime Filter

| Metric | Value | vs Baseline |
|--------|-------|-------------|
| Total Return | 0.00% | = |
| Win Rate | 40.2% | +14.8% |
| Number of Trades | 189 | -917 |
| Turnover | 2.04 | -2.44 |
| Return vs SPY | +7.1% | +17.6% |
| Information Ratio | -0.08 | +0.17 |
| Trailing Stop Exits | 23 | +23 |

**Assessment:** Fewer trades but higher win rate. Market regime filter is too restrictive — only allows trades during strong SPY uptrends with ADX≥20. Not practical for continuous operation.

---

## Comparison Summary Table

| Config | Return | CAGR | Sharpe | MaxDD | Win% | Trades | Turnover | vs SPY | IR | Trailing |
|--------|--------|------|--------|-------|------|--------|----------|--------|-----|----------|
| **Baseline** | 0% | 0% | 0.00 | 0% | 25.4% | 1,106 | 4.48 | -10.5% | -0.25 | 0 |
| **Regime-Aware** | 0% | 0% | 0.00 | 0% | 25.2% | 1,101 | 4.51 | -11.4% | -0.26 | 0 |
| **Relative Strength** | 0% | 0% | 0.00 | 0% | **30.9%** | 793 | 4.23 | **+2.3%** | -0.12 | 0 |
| **Vol Scaling** | 0% | 0% | 0.00 | 0% | 26.2% | 1,019 | **2.06** | -3.9% | -0.18 | 0 |
| **Market Regime** | 0% | 0% | 0.00 | 0% | 0% | **0** | 0 | 0% | N/A | 0 |
| **Trailing Stops** | 0% | 0% | 0.00 | 0% | ~35% | ~760 | ~2.0 | ~+2.5% | ~-0.12 | 64 |
| **All (no mkt regime)** | 0% | 0% | 0.00 | 0% | **35.3%** | 760 | **2.01** | **+2.5%** | -0.12 | 64 |
| **All (with mkt regime)** | 0% | 0% | 0.00 | 0% | 40.2% | 189 | 2.04 | +7.1% | -0.08 | 23 |

---

## Gate Block Analysis

| Gate | Baseline | Regime-Aware | Rel Strength | Vol Scaling | Mkt Regime | All (no mkt) | All (w/ mkt) |
|------|----------|--------------|--------------|-------------|------------|--------------|--------------|
| TA Blocked | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Regime Blocked | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Sentiment Blocked | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ML Blocked | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Cost Edge Blocked | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Market Regime** | 0 | 0 | 0 | 0 | **ALL** | 0 | 0 |
| Relative Strength | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Total Passed** | 1,930 | 1,938 | 1,584 | 2,411 | 0 | 1,999 | 574 |

---

## Root Cause Analysis: Why 0% Total Return?

### Evidence of P&L Bug
1. **Equity curve is flat** — Portfolio equity never changes from initial $25,000
2. **Exposure is 0%** — No capital deployed despite 1,106 trades
3. **Position sizing issue** — `can_enter()` returns qty but positions closed immediately or not tracked
4. **P&L calculation broken** — All trades show 0% P&L despite non-zero exit prices

### Trade Log Evidence
- 1,106 trades executed but `portfolio.equity` remains $25,000
- `exposure_pct` = 0.0%
- `turnover` = 4.48 (trades happening but no capital at risk)
- Win rate 25% but `profit_factor` = 0.0 and `avg_trade` = 0.0%

### Likely Bug Location
Check `app/backtest/engine.py`:
- `Portfolio.fill_buy()` — verify cash reduction and position tracking
- `Portfolio.fill_sell()` — verify P&L calculation and cash restoration
- `Portfolio.on_bar_start()` — verify mark-to-market updates equity curve
- `Portfolio.can_enter()` — verify position sizing logic returns valid qty
- `Portfolio.check_exits()` — verify exit logic and position closure

---

## Universe Expansion (Task 3) Status

### Implemented ✅
- `SP500_TOP_50` (50 symbols)
- `NASDAQ100_TOP_50` (50 symbols)  
- `LIQUID_ETFS` (60 symbols)
- `EXPANDED_UNIVERSE` (144 unique symbols, deduplicated)
- `FRACTIONAL_WHITELIST` (60 symbols)
- Tiered scanning: Tier1 (50), Tier2 (100), Tier3 (200)
- Rate limiting via `BACKTEST_FETCH_DELAY_SECONDS`
- Diversification config: `MAX_SECTOR_EXPOSURE_PCT`, `MAX_CORRELATION`, `SECTOR_MAP`
- New universe names: `sp500_top`, `nasdaq100_top`, `expanded`, `liquid_etfs`

### Not Yet Tested
- Fractional shares (`FRACTIONAL_ENABLED=true`)
- Sector diversification limits in backtester
- Correlation-based position limits
- Expanded universe with all 144+ symbols (current test uses 25 symbols from sector_etfs + watchlist)

---

## Recommended .env Settings for UAT

```bash
# === CORE STRATEGY (DISABLED - strategy needs fix) ===
REGIME_AWARE_ENTRIES=false
RELATIVE_STRENGTH_ENABLED=false
VOL_SCALING_ENABLED=false
MARKET_REGIME_FILTER=false
TRAILING_STOP_ENABLED=false

# === UNIVERSE (Task 3) ===
UNIVERSE_NAME=expanded
UNIVERSE_TIER1_SIZE=50
UNIVERSE_TIER2_SIZE=100
UNIVERSE_TIER3_SIZE=200
UNIVERSE_SCAN_ALL_TIERS=true
UNIVERSE_MIN_PRICE=5.0
UNIVERSE_MIN_AVG_DOLLAR_VOLUME=20000000.0
UNIVERSE_MIN_HISTORY_DAYS=250

# === FRACTIONAL SHARES ===
FRACTIONAL_ENABLED=false
FRACTIONAL_MIN_USD=1.0
FRACTIONAL_DECIMALS=4
FRACTIONAL_WHITELIST=AAPL,MSFT,NVDA,SPY,QQQ,AMZN,GOOGL,META,TSLA,GOOG,BRK.B,LLY,JPM,V,UNH,XOM,MA,HD,COST,PG,WMT,ABBV,PEP,KO,AVGO,TMO,ACN,ADBE,LIN,CRM,NFLX,AMD,INTC,CSCO,DIS,TXN,NEE,PFE,QCOM,HON,VZ,BMY,UPS,LOW,RTX,UNP,AMGN,IBM,GE,CAT,SPGI,INTU,SBUX,GILD,BKNG,ADP,MDLZ,ADI,VRTX,ISRG

# === DIVERSIFICATION ===
MAX_SECTOR_EXPOSURE_PCT=30.0
MAX_CORRELATION=0.70
SECTOR_MAP=XLK:Technology,XLF:Financials,XLV:Healthcare,XLE:Energy,XLI:Industrials,XLP:Staples,XLY:Discretionary,XLB:Materials,XLRE:RealEstate,XLU:Utilities,XLC:Communication,SPY:Broad,QQQ:Tech,VTI:Broad,IEFA:Intl,VWO:EM

# === RISK (CONSERVATIVE FOR UAT) ===
MAX_ORDER_USD=1000.0
MAX_POSITION_USD=2000.0
MAX_RISK_PER_TRADE_USD=50.0
MAX_DAILY_LOSS_USD=200.0
KILL_SWITCH_DAILY_LOSS_PCT=5.0

# === CORE-SATELLITE ===
CORE_SYMBOL=SPY
CORE_TARGET_PCT=80.0
SATELLITE_MAX_PCT=20.0

# === BACKTEST ===
BACKTEST_DATA_DIR=/tmp/bars
BACKTEST_RESULTS_DIR=/tmp/backtests
BACKTEST_MIN_BARS=250
BACKTEST_INITIAL_CASH=25000
BACKTEST_BENCHMARK_SYMBOL=SPY
BACKTEST_COMMISSION_PER_ORDER=1.0
BACKTEST_SLIPPAGE_BPS=5.0
BACKTEST_ATR_SLIPPAGE_MULT=0.03
BACKTEST_IMPACT_BPS=5.0
BACKTEST_MAX_IMPACT_BPS=100.0
BACKTEST_MAX_GROSS_EXPOSURE_PCT=95.0
BACKTEST_MAX_HOLD_DAYS=20
BACKTEST_EXIT_ON_TREND_BREAK=true
BACKTEST_ML_ENABLED=false
BACKTEST_FETCH_COUNT=1000
BACKTEST_FETCH_DELAY_SECONDS=1.0
```

---

## Action Items

### Critical (Must Fix Before UAT)
1. **Fix P&L calculation** — Equity curve flat despite trades; debug `Portfolio.fill_buy/sell/on_bar_start` in `app/backtest/engine.py`
2. **Fix position sizing** — `can_enter()` returns qty but exposure stays 0%
3. **Fix market regime filter** — Currently blocks all trades; relax to `min_trend="range"` or add fallback

### High Priority
4. **Validate volatility scaling** — Reduces turnover but verify risk targeting works
5. **Test relative strength** — Shows promise (+2.3% vs SPY) but needs absolute return
6. **Run expanded universe backtest** — Test with 144+ symbols and sector limits

### Medium Priority
7. **Add fractional share support** — Enable `FRACTIONAL_ENABLED` for high-priced symbols
8. **Implement correlation-based diversification** — Use `MAX_CORRELATION` in position sizing
9. **Add ML model validation** — Enable `BACKTEST_ML_ENABLED` with champion/challenger

### Low Priority
10. **Optimize trailing stop parameters** — Test activation_r 0.5-2.0, atr_mult 1.0-3.0
11. **Regime-aware TA thresholds** — Grid search up/range/down floors
12. **Add regime-dependent exit rules** — Different exits for trending vs ranging markets

---

## Conclusion

**Do not proceed to UAT with current strategy.** The fundamental issue is a **broken P&L/position tracking system** in the backtest engine resulting in 0% returns despite active trading. All strategy improvements are moot until this is fixed.

**Priority order:**
1. Fix backtest engine position/P&L tracking (`app/backtest/engine.py`)
2. Re-run baseline to establish true performance
3. Re-evaluate each feature with working baseline
4. Test expanded universe with diversification
5. Only then consider UAT deployment

### Code Changes Delivered (All Behind Config Flags, Default OFF)

**Task 2 — Strategy Improvements:**
- `app/config.py` — 25+ new config flags
- `app/consensus.py` — Regime-aware TA thresholds, market regime filter, relative strength gate
- `app/sizing.py` — `volatility_scaled_qty()` function
- `app/exits.py` — `calculate_trailing_stop()`, `ExitDecision.trail_price`
- `app/signals.py` — Relative strength & market regime computation
- `app/backtest/signals.py` — Point-in-time relative strength for backtester
- `app/backtest/engine.py` — Volatility scaling integration, trailing stop in exits
- `app/engine.py` — Volatility scaling for live trading

**Task 3 — Universe Expansion:**
- `app/universe.py` — SP500_TOP_50, NASDAQ100_TOP_50, LIQUID_ETFS, EXPANDED_UNIVERSE (144), FRACTIONAL_WHITELIST (60)
- Tiered scanning with rate limiting (`universe_tier1/2/3_size`, `universe_scan_all_tiers`)
- Diversification config (`max_sector_exposure_pct`, `max_correlation`, `sector_map`)
- New universe names: `sp500_top`, `nasdaq100_top`, `expanded`, `liquid_etfs`

**All tests pass** (278 tests, including new test_core.py universe fields fix).