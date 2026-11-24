"use client";

import type { ReactNode } from "react";
import { clockTime, money, pct, signTone } from "@/lib/format";
import type { EventRow, Performance, Portfolio, Status } from "@/lib/types";
import EquityChart from "./EquityChart";
import css from "./Today.module.css";

const DEAD = new Set(["cancelled", "canceled", "filled", "rejected", "expired"]);

/** Plain-language summary of the account, built from status, portfolio and pending data. */
export function statusSentence(status: Status | null, portfolio: Portfolio | null, pendingCount: number): string {
  if (!status) return "Connecting to the trading engine.";
  const parts: string[] = [];
  const env = status.is_live ? "the live account" : "paper";
  if (!status.connected) parts.push("Broker offline.");
  if (status.kill_switch) {
    parts.push("Kill switch on: no new entries. Exits still run.");
  } else {
    parts.push(status.trading_mode === "auto" ? `Auto-trading on ${env}.` : `Approve mode on ${env}.`);
    if (!status.scheduler_on) parts.push("Engine paused.");
  }
  if (status.kill_switch && status.is_live) parts.push("Live account.");
  const positions = portfolio?.positions || [];
  if (portfolio) {
    if (!positions.length) parts.push("No open positions.");
    else {
      const plural = positions.length === 1 ? "position" : "positions";
      if (portfolio.protective) {
        const stopped = new Set(portfolio.protective
          .filter(p => p.kind === "sl" && !DEAD.has(String(p.status).toLowerCase())).map(p => p.symbol));
        const covered = positions.filter(p => stopped.has(p.symbol)).length;
        parts.push(covered === positions.length ? `${positions.length} ${plural}, all with stops.`
          : `${positions.length} ${plural}, ${positions.length - covered} without a stop.`);
      } else parts.push(`${positions.length} ${plural}.`);
    }
  }
  parts.push(pendingCount > 0
    ? `${pendingCount} ${pendingCount === 1 ? "trade needs" : "trades need"} your decision.`
    : "Nothing needs you.");
  return parts.join(" ");
}

const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "-" : ""}${money(Math.abs(n))}`;

export default function Today({ status, portfolio, performance, events, pendingCount, needsYou, controls, onSymbol }: {
  status: Status | null;
  portfolio: Portfolio | null;
  performance: Performance | null;
  events: EventRow[];
  pendingCount: number;
  needsYou: ReactNode;
  controls: ReactNode;
  onSymbol?: (symbol: string) => void;
}) {
  const positions = portfolio?.positions || [];
  const dayPnl = portfolio?.balance?.day_pnl ?? status?.day_pnl;
  const hasDay = dayPnl != null && Number.isFinite(Number(dayPnl));
  const since = performance?.account?.total_return_pct;
  const excess = performance?.excess_return_pct;
  const bench = performance?.benchmark_symbol || "SPY";
  const stops = new Map<string, number>();
  for (const p of portfolio?.protective || []) {
    if (p.kind === "sl" && !DEAD.has(String(p.status).toLowerCase())) stops.set(p.symbol, p.price);
  }
  const hasStopData = !!portfolio?.protective;

  return <div className={css["today"]}>
    <div className={css["head"]}>
      <h1 className={css["sentence"]}>{statusSentence(status, portfolio, pendingCount)}</h1>
      <p className={css["figures"]}>
        <span>Day <b className={hasDay ? signTone(dayPnl) : ""}>{hasDay ? signed(Number(dayPnl)) : "—"}</b></span>
        <span>Since start <b className={signTone(since)}>{pct(since)}</b></span>
        {excess != null && <span>vs {bench} <b className={signTone(excess)}>{pct(excess)}</b></span>}
        <span>Net value <b>{money(portfolio?.balance?.net_liq)}</b></span>
      </p>
      {portfolio?.balance?.stale && <p className="muted">The broker returned an empty balance; showing the last good values.</p>}
    </div>

    <div className={css["needs"]}>
      <h2>Needs you</h2>
      {pendingCount > 0 ? needsYou : <p className="muted">Nothing needs you.</p>}
    </div>

    <div className={css["chart-area"]}><EquityChart performance={performance}/></div>

    <section className={css["positions"]} aria-label="Positions">
      <h2>Positions</h2>
      {positions.length ? <div className="table-scroll">
        <table className={css["table"]}>
          <thead><tr>
            <th scope="col">Symbol</th><th scope="col" className={css["num"]}>Qty</th>
            <th scope="col" className={css["num"]}>Cost</th><th scope="col" className={css["num"]}>Last</th>
            <th scope="col" className={css["num"]}>P&amp;L</th>
            {hasStopData && <th scope="col" className={css["num"]}>Stop</th>}
          </tr></thead>
          <tbody>{positions.map(p => {
            const qty = Number(p.qty);
            const last = qty ? Number(p.market_value) / qty : NaN;
            const stop = stops.get(p.symbol);
            return <tr key={p.symbol}>
              <th scope="row">{onSymbol
                ? <button className={css["sym"]} onClick={() => onSymbol(p.symbol)}>{p.symbol}</button> : p.symbol}</th>
              <td className={css["num"]}>{qty}</td>
              <td className={css["num"]}>{money(p.cost)}</td>
              <td className={css["num"]}>{Number.isFinite(last) ? money(last) : "—"}</td>
              <td className={`${css["num"]} ${signTone(p.unrealized)}`}>
                {p.unrealized == null ? "—" : signed(Number(p.unrealized))}
                {p.unrealized_pct != null && <small> {pct(p.unrealized_pct)}</small>}
              </td>
              {hasStopData && <td className={css["num"]}>{stop != null ? money(stop) : <span className="muted">none</span>}</td>}
            </tr>;
          })}</tbody>
        </table>
      </div> : <p className="muted">No open positions.</p>}
    </section>

    <section className={css["activity"]} aria-label="Activity">
      <h2>Activity</h2>
      {events.length ? <ul className={css["events"]}>
        {events.slice(0, 10).map(e => <li key={e.id}>
          <time suppressHydrationWarning>{clockTime(e.ts)}</time>
          <span className={e.level === "error" || e.level === "critical" ? "negative" : ""}>{e.message}</span>
        </li>)}
      </ul> : <p className="muted">No recent events.</p>}
    </section>

    <div className={css["secondary"]}>{controls}</div>
  </div>;
}
