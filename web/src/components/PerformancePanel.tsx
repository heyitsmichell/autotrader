"use client";

import { money, pct } from "@/lib/format";
import type { Performance } from "@/lib/types";

/** Live account-vs-benchmark report. All numbers come from stored daily closes. */
export default function PerformancePanel({ performance }: { performance: Performance | null }) {
  if (!performance) {
    return <section className="panel" aria-label="Performance versus SPY">
      <div className="panel-head">
        <div>
          <div className="eyebrow">PERFORMANCE</div>
          <h2>Account vs benchmark</h2>
        </div>
        <span className="eyebrow">LIVE</span>
      </div>
      <p className="muted">Waiting for the first performance response…</p>
    </section>;
  }

  const symbol = performance.benchmark_symbol || "SPY";
  const sig = performance.significance;
  const acct = performance.account || {};
  const bench = performance.benchmark || {};

  return <section className="panel" aria-label={`Performance versus ${symbol}`}>
    <div className="panel-head">
      <div>
        <div className="eyebrow">PERFORMANCE · LIVE DAILY CLOSES</div>
        <h2>Account vs {symbol}</h2>
        <p className="muted">
          {performance.end
            ? `${performance.start} → ${performance.end} · ${performance.days ?? 0} trading day(s)`
            : "One row is stored after each completed session."}
        </p>
      </div>
      <span className="eyebrow">NOT A FORECAST</span>
    </div>

    {performance.state === "no_data" ? <div className="empty-state">
      <span>◷</span>
      <p>{performance.message || "Not enough data yet."}</p>
      <p className="muted">
        The benchmark job stores net liquidation, cash, {symbol} close and an estimated
        cash flow once per session. Deposits and withdrawals are removed from returns.
      </p>
    </div> : <>
      <div className="learning-health">
        <div className="learning-stat">
          <span>ACCOUNT</span>
          <b className={tone(acct.total_return_pct)}>{pct(acct.total_return_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>{symbol}</span>
          <b className={tone(bench.total_return_pct)}>{pct(bench.total_return_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>EXCESS</span>
          <b className={tone(performance.excess_return_pct)}>{pct(performance.excess_return_pct)}</b>
        </div>
      </div>

      <div className="learning-health">
        <div className="learning-stat">
          <span>ACCOUNT MAX DRAWDOWN</span>
          <b className="negative">{pct(acct.max_drawdown_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>{symbol} MAX DRAWDOWN</span>
          <b className="negative">{pct(bench.max_drawdown_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>EST. EXTERNAL FLOW</span>
          <b>{money(performance.flows_total)}</b>
        </div>
      </div>

      <div className="inline-warning" role="note">
        {sig?.state === "insufficient_data" ? <>
          <b>Not enough data yet.</b>{" "}
          {sig.message || `${sig.n ?? 0} of ${sig.min_days ?? 60} trading days.`}
        </> : sig?.state === "ok" ? <>
          <b>Excess return significance:</b>{" "}
          {pct(sig.mean_annualized_pct)} annualized, p = {sig.p_value?.toFixed(4) ?? "—"}
          {" "}({sig.significant ? "distinguishable from zero" : "not distinguishable from zero"}).
          {" "}95% CI {pct(sig.ci_low_annualized_pct)} to {pct(sig.ci_high_annualized_pct)}.
          {" "}Newey-West lag {sig.lags ?? "—"}; {sig.n} days.
        </> : <>
          <b>Significance unavailable.</b> {sig?.message || "Not enough variance yet."}
        </>}
      </div>

      <div className="learning-health">
        <div className="learning-stat">
          <span>SHARPE (ACCOUNT)</span>
          <b>{num(acct.sharpe)}</b>
        </div>
        <div className="learning-stat">
          <span>INFORMATION RATIO</span>
          <b>{num(performance.information_ratio)}</b>
        </div>
        <div className="learning-stat">
          <span>BETA</span>
          <b>{num(acct.beta)}</b>
        </div>
        <div className="learning-stat">
          <span>ALPHA (ANNUALIZED)</span>
          <b className={tone(acct.alpha_pct)}>{pct(acct.alpha_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>TRACKING ERROR</span>
          <b>{pct(performance.tracking_error_pct)}</b>
        </div>
        <div className="learning-stat">
          <span>DAILY HIT RATE</span>
          <b>{performance.hit_rate == null ? "—" : `${(performance.hit_rate * 100).toFixed(1)}%`}</b>
        </div>
        <div className="learning-stat">
          <span>ANNUALIZED TURNOVER</span>
          <b>{performance.turnover == null ? "—" : `${performance.turnover.toFixed(1)}×`}</b>
        </div>
        <div className="learning-stat">
          <span>VOLATILITY (ACCOUNT)</span>
          <b>{pct(acct.volatility_pct)}</b>
        </div>
      </div>

      <p className="muted">
        {performance.method}. Net liquidation {money(performance.first_net_liq)} →{" "}
        {money(performance.last_net_liq)}. Deposits and withdrawals are excluded from
        returns where the daily flow estimate can see them; a large transfer made while
        the app was off can still distort one day.
      </p>
    </>}
  </section>;
}

function tone(value: number | null | undefined) {
  if (value == null) return "";
  return value > 0 ? "positive" : value < 0 ? "negative" : "";
}

function num(value: number | null | undefined) {
  return value == null ? "—" : value.toFixed(2);
}
