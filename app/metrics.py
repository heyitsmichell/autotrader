"""Pure performance and significance math (no I/O, no broker, no DB).

All return/volatility/drawdown values are fractions (0.12 = 12%), not percent,
so callers can format them consistently. ``significance`` works on daily excess
returns and reports both daily and annualized figures.

The only external dependency is the standard library, so these functions are
cheap to unit-test and safe to reuse from the live API, the daily benchmark job
and (later) the backtester.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

PERIODS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
def daily_returns(values: Sequence[float], flows: Sequence[float] | None = None) -> list[float]:
    """Time-weighted daily returns from a value series.

    ``flows[i]`` is the net external cash flow *during* period ``i`` (a deposit
    is positive, a withdrawal negative). A flow is removed before the return is
    measured, so deposits and withdrawals do not count as performance.

        r_i = (V_i - flow_i) / V_{i-1} - 1
    """
    if flows is None:
        flows = [0.0] * len(values)
    out: list[float] = []
    for i in range(1, len(values)):
        prev, cur = values[i - 1], values[i]
        if prev is None or cur is None or prev <= 0:
            continue
        flow = flows[i] if i < len(flows) and flows[i] is not None else 0.0
        out.append((cur - flow) / prev - 1.0)
    return out


def returns_from_prices(prices: Sequence[float]) -> list[float]:
    """Simple close-to-close returns (used for a benchmark such as SPY)."""
    return [prices[i] / prices[i - 1] - 1.0
            for i in range(1, len(prices))
            if prices[i - 1] and prices[i - 1] > 0]


def total_return(values: Sequence[float], flows: Sequence[float] | None = None) -> float:
    """Compounded (time-weighted) return over the whole series."""
    return _compound(daily_returns(values, flows))


def annualized_return(returns: Sequence[float],
                      periods: int = PERIODS_PER_YEAR) -> float:
    """Geometric annualized return from a sequence of periodic returns."""
    if not returns:
        return 0.0
    growth = _compound(returns)
    if growth <= -1.0:
        return -1.0
    return (1.0 + growth) ** (periods / len(returns)) - 1.0


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
def volatility(returns: Sequence[float], periods: int = PERIODS_PER_YEAR) -> float | None:
    """Annualized sample standard deviation of returns."""
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(periods)


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a negative fraction (0 = no drawdown)."""
    peak = None
    worst = 0.0
    for value in values:
        if value is None:
            continue
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def drawdown_series(values: Sequence[float]) -> list[float]:
    """Running drawdown for each value (negative fractions), for charts."""
    peak = None
    out: list[float] = []
    for value in values:
        if value is None:
            out.append(0.0)
            continue
        if peak is None or value > peak:
            peak = value
        out.append(value / peak - 1.0 if peak else 0.0)
    return out


def sharpe(returns: Sequence[float], risk_free: float = 0.0,
           periods: int = PERIODS_PER_YEAR) -> float | None:
    """Annualized Sharpe ratio. ``risk_free`` is an annual rate (0.04 = 4%)."""
    if len(returns) < 2:
        return None
    vol = statistics.stdev(returns)
    if vol == 0:
        return None
    excess = statistics.fmean(returns) - risk_free / periods
    return excess / vol * math.sqrt(periods)


# ---------------------------------------------------------------------------
# Benchmark-relative
# ---------------------------------------------------------------------------
def beta(asset_returns: Sequence[float],
         benchmark_returns: Sequence[float]) -> float | None:
    n = min(len(asset_returns), len(benchmark_returns))
    if n < 2:
        return None
    a, b = asset_returns[-n:], benchmark_returns[-n:]
    var = statistics.variance(b)
    if var == 0:
        return None
    return statistics.covariance(a, b) / var


def alpha(asset_returns: Sequence[float], benchmark_returns: Sequence[float],
          risk_free: float = 0.0, periods: int = PERIODS_PER_YEAR) -> float | None:
    """Annualized Jensen's alpha: mean excess return minus beta times benchmark excess."""
    n = min(len(asset_returns), len(benchmark_returns))
    if n < 2:
        return None
    a, b = asset_returns[-n:], benchmark_returns[-n:]
    b_asset = beta(a, b)
    if b_asset is None:
        return None
    rf = risk_free / periods
    return ((statistics.fmean(a) - rf) - b_asset * (statistics.fmean(b) - rf)) * periods


def tracking_error(excess_returns: Sequence[float],
                   periods: int = PERIODS_PER_YEAR) -> float | None:
    if len(excess_returns) < 2:
        return None
    return statistics.stdev(excess_returns) * math.sqrt(periods)


def information_ratio(excess_returns: Sequence[float],
                      periods: int = PERIODS_PER_YEAR) -> float | None:
    """Annualized active return divided by tracking error."""
    if len(excess_returns) < 2:
        return None
    te = statistics.stdev(excess_returns)
    if te == 0:
        return None
    return statistics.fmean(excess_returns) / te * math.sqrt(periods)


def hit_rate(excess_returns: Sequence[float]) -> float | None:
    """Share of days whose excess return is strictly positive."""
    if not excess_returns:
        return None
    return sum(1 for r in excess_returns if r > 0) / len(excess_returns)


def turnover(traded_notional: Sequence[float], equity: float | Sequence[float],
             periods: int = PERIODS_PER_YEAR) -> float | None:
    """Annualized turnover: traded notional per period divided by average equity.

    ``traded_notional`` is one value per period (for example the absolute value
    of buys plus sells each day). ``equity`` may be a single average value or a
    per-period series.
    """
    if not traded_notional:
        return None
    if isinstance(equity, (int, float)):
        avg = float(equity)
    else:
        if not equity:
            return None
        avg = statistics.fmean(equity)
    if avg <= 0:
        return None
    return sum(abs(x) for x in traded_notional) / avg / len(traded_notional) * periods


# ---------------------------------------------------------------------------
# Significance of excess returns
# ---------------------------------------------------------------------------
def newey_west_lags(n: int) -> int:
    """Standard automatic bandwidth (Newey-West 1994), at least one lag."""
    return max(1, int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))))


def newey_west_se(values: Sequence[float], lags: int | None = None) -> float | None:
    """Standard error of the mean with a Newey-West autocorrelation correction."""
    n = len(values)
    if n < 2:
        return None
    mean = statistics.fmean(values)
    e = [v - mean for v in values]
    lag = newey_west_lags(n) if lags is None else max(0, int(lags))
    variance = sum(x * x for x in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - k / (lag + 1.0)
        cov = sum(e[i] * e[i - k] for i in range(k, n)) / n
        variance += 2.0 * weight * cov
    variance = max(variance, 0.0)
    if variance == 0:
        return None
    return math.sqrt(variance / n)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    # Acklam's inverse normal CDF approximation; plenty accurate for a CI.
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def significance(excess_returns: Sequence[float], min_days: int = 60,
                 confidence: float = 0.95,
                 periods: int = PERIODS_PER_YEAR) -> dict:
    """Is the mean daily excess return over the benchmark distinguishable from zero?

    Uses a Newey-West corrected standard error of the mean, which accounts for
    autocorrelated daily excess returns (overlapping holding periods, regime
    persistence). Below ``min_days`` observations the answer is explicitly
    "not enough data yet" rather than a misleading p-value.
    """
    n = len(excess_returns)
    result = {
        "state": "insufficient_data",
        "n": n,
        "min_days": max(2, int(min_days)),
        "confidence": confidence,
        "lags": newey_west_lags(n) if n >= 2 else 0,
    }
    if n < result["min_days"]:
        result["days_needed"] = result["min_days"] - n
        result["message"] = (f"Not enough data yet: {n} of {result['min_days']} "
                             f"trading days. {result['days_needed']} more needed.")
        return result

    mean = statistics.fmean(excess_returns)
    se = newey_west_se(excess_returns)
    if se is None or se <= 0:
        result.update(state="no_variance", mean_daily=mean,
                      mean_annualized=mean * periods,
                      message="Excess return has no measurable variance yet.")
        return result

    t_stat = mean / se
    p_value = 2.0 * (1.0 - _norm_cdf(abs(t_stat)))
    z = _norm_ppf(0.5 + confidence / 2.0)
    lo, hi = mean - z * se, mean + z * se
    result.update(
        state="ok",
        mean_daily=mean,
        mean_annualized=mean * periods,
        se_daily=se,
        ci_low_daily=lo,
        ci_high_daily=hi,
        ci_low_annualized=lo * periods,
        ci_high_annualized=hi * periods,
        t_stat=t_stat,
        p_value=p_value,
        significant=p_value < (1.0 - confidence),
        message=("Excess return is not distinguishable from zero at the chosen "
                 f"{confidence:.0%} confidence." if p_value >= (1.0 - confidence)
                 else f"Excess return is positive/negative with p={p_value:.4f}."),
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _compound(returns: Sequence[float]) -> float:
    growth = 1.0
    for r in returns:
        growth *= 1.0 + r
    return growth - 1.0


def summarize(account_values: Sequence[float], account_flows: Sequence[float] | None,
              benchmark_values: Sequence[float], *,
              turnover_notional: Sequence[float] | None = None,
              risk_free: float = 0.0,
              min_days: int = 60) -> dict:
    """One account-vs-benchmark summary as fractions (percent conversion is the UI's job)."""
    account_returns = daily_returns(account_values, account_flows)
    benchmark_returns = returns_from_prices(benchmark_values)
    n = min(len(account_returns), len(benchmark_returns))
    account_returns = account_returns[-n:] if n else []
    benchmark_returns = benchmark_returns[-n:] if n else []
    excess = [a - b for a, b in zip(account_returns, benchmark_returns)]

    acct_total = _compound(account_returns)
    spy_total = _compound(benchmark_returns)
    avg_equity = statistics.fmean(account_values) if account_values else 0.0
    turn = None
    if turnover_notional is not None and avg_equity > 0:
        turn = turnover(turnover_notional, avg_equity)

    return {
        "days": n,
        "account": {
            "total_return": acct_total,
            "annualized_return": annualized_return(account_returns),
            "volatility": volatility(account_returns),
            "max_drawdown": max_drawdown(account_values),
            "sharpe": sharpe(account_returns, risk_free),
            "beta": beta(account_returns, benchmark_returns),
            "alpha": alpha(account_returns, benchmark_returns, risk_free),
        },
        "benchmark": {
            "total_return": spy_total,
            "annualized_return": annualized_return(benchmark_returns),
            "volatility": volatility(benchmark_returns),
            "max_drawdown": max_drawdown(benchmark_values),
            "sharpe": sharpe(benchmark_returns, risk_free),
        },
        "excess_return": acct_total - spy_total,
        "tracking_error": tracking_error(excess),
        "information_ratio": information_ratio(excess),
        "hit_rate": hit_rate(excess),
        "turnover": turn,
        "significance": significance(excess, min_days=min_days),
    }


def flatten_summary(summary: dict, trades: Sequence | None = None) -> dict:
    """Flat view of summarize() for scripts and reports.

    summarize() nests results under "account" and "benchmark", but the backtest scripts read
    top-level keys such as total_return/sharpe/max_drawdown/alpha/beta. Those lookups silently
    returned 0, which is why STRATEGY_REVIEW.md reported "0% return, 0% exposure" for every
    configuration. Use this helper instead of m.get("total_return", 0).
    """
    acct = summary.get("account", {}) or {}
    bench = summary.get("benchmark", {}) or {}
    flat = {
        "total_return": acct.get("total_return", 0.0),
        "cagr": acct.get("annualized_return", 0.0),
        "annualized_return": acct.get("annualized_return", 0.0),
        "volatility": acct.get("volatility", 0.0),
        "max_drawdown": acct.get("max_drawdown", 0.0),
        "sharpe": acct.get("sharpe", 0.0),
        "alpha": acct.get("alpha", 0.0),
        "beta": acct.get("beta", 0.0),
        "benchmark_total_return": bench.get("total_return", 0.0),
        "benchmark_max_drawdown": bench.get("max_drawdown", 0.0),
        "excess_return": summary.get("excess_return", 0.0),
        "information_ratio": summary.get("information_ratio", 0.0),
        "excess_hit_rate": summary.get("hit_rate", 0.0),
        "turnover": summary.get("turnover") or 0.0,
        "significance": summary.get("significance", {}),
        "backtest": summary.get("backtest", {}),
    }
    if trades:
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [-t.pnl for t in trades if t.pnl < 0]
        flat["profit_factor"] = (sum(wins) / sum(losses)) if losses else (float("inf") if wins else 0.0)
        flat["avg_trade"] = statistics.fmean(t.pnl_pct for t in trades)
    else:
        flat["profit_factor"], flat["avg_trade"] = 0.0, 0.0
    return flat
