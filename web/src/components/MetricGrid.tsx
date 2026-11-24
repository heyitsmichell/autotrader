"use client";

import { useMemo } from "react";
import { money, signTone } from "@/lib/format";
import type { Portfolio, Signal, Status } from "@/lib/types";
import css from "./MetricGrid.module.css";

export function MetricGrid({ status, portfolio, pendingCount }: {
  status: Status | null;
  portfolio: Portfolio | null;
  pendingCount: number;
}) {
  const positions = portfolio?.positions || [];
  const dayPnl = portfolio?.balance?.day_pnl ?? status?.day_pnl;
  const lossLimit = status?.limits?.max_daily_loss_usd;
  const dayPnlKnown = dayPnl != null && Number.isFinite(Number(dayPnl));
  const stale = Boolean(portfolio?.balance?.stale);

  return <section className={`${css["metric-grid"]}`} aria-label="Account summary">
    <Metric
      label="NET LIQUIDATION"
      value={money(portfolio?.balance?.net_liq)}
      sub={`Cash ${money(portfolio?.balance?.cash)} · BP ${money(portfolio?.balance?.buying_power)}`}
      icon="◈"
    />
    <Metric
      label="DAY P&L"
      value={dayPnlKnown ? money(dayPnl) : "—"}
      sub={stale ? "Broker returned an empty balance · showing last good values"
        : lossLimit != null ? `Broker reported · daily loss stop ${money(lossLimit)}` : "Broker reported · US session"}
      tone={dayPnlKnown ? signTone(dayPnl) : ""}
      icon="↗"
    />
    <Metric
      label="OPEN POSITIONS"
      value={String(positions.length)}
      sub={`${positions.length ? money(positions.reduce((total, p) => total + Number(p.market_value || 0), 0)) : money(0)} market value`}
      icon="▤"
    />
    <Metric
      label="PENDING PROPOSALS"
      value={String(pendingCount)}
      sub={status?.limits?.max_trades_per_day === 0
        ? `${status?.trades_today ?? 0} trades today · no daily cap`
        : `${status?.trades_today ?? 0} / ${status?.limits?.max_trades_per_day ?? "—"} trades used`}
      icon="⌁"
      tone={pendingCount ? "warning" : ""}
    />
  </section>;
}

/** Human answer to "why is nothing trading?", computed from the gate output. */
export function GateSummary({ signals, status }: { signals: Signal[]; status: Status | null }) {
  const summary = useMemo(() => {
    const passed = signals.filter(s => s.consensus === "long").length;
    const failed = signals.filter(s => s.consensus !== "long");
    const reasons = new Map<string, number>();
    for (const signal of failed) {
      const checks = (signal.consensus_checks || []).filter(check => !check.ok);
      if (!checks.length) {
        reasons.set(signal.consensus == null ? "Awaiting data" : "Unknown block", (reasons.get(signal.consensus == null ? "Awaiting data" : "Unknown block") || 0) + 1);
        continue;
      }
      for (const check of checks) reasons.set(check.name, (reasons.get(check.name) || 0) + 1);
    }
    return {
      passed,
      total: signals.length,
      reasons: [...reasons.entries()].sort((a, b) => b[1] - a[1]),
    };
  }, [signals]);

  const aboveCap = useMemo(() => {
    const cap = status?.limits?.max_order_usd;
    if (!cap) return [];
    return signals.filter(s => s.close != null && Number(s.close) > cap).map(s => s.symbol);
  }, [signals, status]);

  return <section className={`panel ${css["gate-summary"]}`} aria-label="Why nothing is trading">
    <div>
      <div className="eyebrow">BUY GATE SUMMARY</div>
      <h2>
        {summary.total === 0
          ? "Waiting for the first signal snapshot"
          : `${summary.passed} of ${summary.total} symbols pass the buy gate`}
      </h2>
      <p className="muted">
        {summary.reasons.length
          ? `Main reasons: ${summary.reasons.slice(0, 5).map(([name, count]) => `${name} ×${count}`).join(", ")}`
          : "No failed checks recorded. Sells to close held shares bypass this gate."}
      </p>
    </div>
    <div className={`${css["gate-summary-facts"]}`}>
      <span><b>{summary.passed}</b> pass</span>
      <span><b>{Math.max(0, summary.total - summary.passed)}</b> blocked</span>
      <span><b>{money(status?.limits?.max_order_usd)}</b> per order</span>
    </div>
    {aboveCap.length > 0 && <div className="inline-warning" role="note">
      {aboveCap.join(", ")} {aboveCap.length === 1 ? "is" : "are"} priced above the {money(status?.limits?.max_order_usd)} per-order cap.
      At one-share granularity {aboveCap.length === 1 ? "it" : "they"} cannot be bought.
    </div>}
  </section>;
}

export function RoiNote({ status }: { status: Status | null }) {
  if (status?.cost_today_usd == null) return null;
  return <span className="muted">Est. LLM spend today {money(status.cost_today_usd)}</span>;
}

function Metric({ label, value, sub, icon, tone = "" }: {
  label: string;
  value: string;
  sub: string;
  icon: string;
  tone?: string;
}) {
  return <article className="metric-card">
    <div className={`${css["metric-top"]}`}>
      <span className="eyebrow">{label}</span>
      <span className={`${css["metric-icon"]}`} aria-hidden="true">{icon}</span>
    </div>
    <b className={`metric-value ${tone}`}>{value}</b>
    <span className={`${css["metric-sub"]}`}>{sub}</span>
  </article>;
}
