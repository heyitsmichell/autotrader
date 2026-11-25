"use client";

import { dateTime, money, secondsAgo, signTone } from "@/lib/format";
import type { EventRow, Portfolio, Status, SpyBenchmarkStatus } from "@/lib/types";
import Empty from "./ui/Empty";
import Mark from "./ui/Mark";
import Section from "./ui/Section";
import css from "./SidePanels.module.css";

export function AccountPanel({ portfolio, status, onRefresh }: {
  portfolio: Portfolio | null;
  status: Status | null;
  onRefresh: () => void;
}) {
  const bal = portfolio?.balance;
  const failed = portfolio?.errors ? Object.keys(portfolio.errors) : [];
  const tone = signTone(bal?.unrealized);
  return <Section
    title="Account balances"
    meta={!portfolio ? (status?.connected ? "Loading…" : "Broker not connected") : undefined}
    right={<button className="button" onClick={onRefresh}>Refresh</button>}
  >
    {failed.length > 0 && <p className={css["note"]}>Portfolio data incomplete: {failed.join(", ")}</p>}
    <dl className={css["kv"]}>
      <dt>Net liquidation</dt><dd className="num">{money(bal?.net_liq)}</dd>
      <dt>Cash</dt><dd className="num">{money(bal?.cash)}</dd>
      <dt>Buying power</dt><dd className="num">{money(bal?.buying_power)}</dd>
      <dt>Unrealized P&amp;L</dt><dd className={`num ${tone}`}>{money(bal?.unrealized)}</dd>
    </dl>
  </Section>;
}

export function OrdersPanel({ portfolio }: { portfolio: Portfolio | null }) {
  const openOrders = portfolio?.open_orders || [];
  return <Section title="Open orders" meta={openOrders.length ? `${openOrders.length} at the broker` : undefined}>
    {!openOrders.length
      ? <Empty>No open orders at the broker. Stops for open positions would show here.</Empty>
      : <div className="table-scroll"><table>
        <thead><tr><th>Symbol</th><th>Side</th><th className="num">Qty</th><th>Type</th><th>Status</th></tr></thead>
        <tbody>{openOrders.map((order, index) => <tr key={order.client_order_id || index}>
          <td>{order.symbol}</td>
          <td>{order.side}</td>
          <td className="num">{order.quantity}</td>
          <td className="num">{order.limit_price ? `Limit ${money(order.limit_price)}` : "Market"}</td>
          <td><Mark tone="neutral">{order.status || "working"}</Mark></td>
        </tr>)}</tbody>
      </table></div>}
  </Section>;
}

type State = "ok" | "warn" | "fail" | undefined;
interface Row { label: string; value: string; state: State }

export function HealthPanel({ status, pendingCount }: {
  status: Status | null;
  pendingCount: number;
  onTestAlert?: () => void;
  busy?: boolean;
}) {
  const channels = status?.alerts?.channels || [];
  const sup = status?.supervisor;
  const token = sup?.token;
  const signalAge = status?.signals?.last_refresh
    ? Math.max(0, Math.round(((status.server_time ? Date.parse(status.server_time) : Date.parse(status.signals.last_refresh))
      - Date.parse(status.signals.last_refresh)) / 60_000))
    : null;
  const supAge = secondsAgo(sup?.last_tick);
  const loginBroken = status?.broker_mode === "webull" && (!token || (token.status !== "NORMAL" && token.status !== "not_required"));
  const expiry = token?.expires_at ? dateTime(token.expires_at) : null;
  const st = (ok: boolean | undefined, bad: State = "fail"): State => ok ? "ok" : bad;

  const rows: Row[] = [
    { label: "Broker", value: status?.connected ? "Connected" : "Disconnected", state: st(status?.connected) },
    { label: "Price feed", value: `${status?.realtime?.mode || "—"}${status?.realtime?.mode === "poll" ? ` · ${status?.realtime?.poll_seconds}s` : ""}`, state: st(status?.connected) },
    { label: "Signal data", value: signalAge == null ? "Waiting" : `${signalAge}m old`, state: st(signalAge != null && signalAge < 30, "warn") },
    { label: "Risk engine", value: "Active · hard limits", state: "ok" },
    { label: "Trading mode", value: status?.trading_mode === "auto" ? "Auto-trade" : "Approval required", state: undefined },
    { label: "Supervisor", value: supAge == null ? "Starting" : `Checked ${supAge}s ago${sup?.auto_start ? " · auto-start" : ""}`, state: st(supAge != null && supAge < 180 && !sup?.error, "warn") },
    { label: "Alerts", value: channels.length ? channels.join(", ") : "None configured", state: st(channels.length > 0 && !status?.alerts?.last_error, "warn") },
  ];
  if (status?.broker_mode === "webull") {
    rows.push({
      label: "Webull login",
      value: !token ? "Checking" : token.status === "not_required"
        ? "No login token needed"
        : token.status !== "NORMAL"
          ? `${token.status}. Run ./auth.sh (or auth.ps1 on Windows) and approve in the Webull app`
          : `${Number(token.days_left).toFixed(1)} days left${token.estimated ? " (est.)" : ""}`,
      state: st(!!token && (token.status === "not_required" || (token.status === "NORMAL" && Number(token.days_left) > 3))),
    });
  }
  rows.push(
    {
      label: "Auto kill switch",
      value: `DD ${sup?.drawdown_pct == null ? "—" : `${sup.drawdown_pct}%`} / ${sup?.max_drawdown_pct ? `${sup.max_drawdown_pct}%` : "off"} · errors ${Math.max(sup?.run_failures ?? 0, sup?.order_failures ?? 0)} / ${sup?.max_errors || "off"} · ${sup?.kill_action || "pause"}`,
      state: st(!status?.kill_switch),
    },
    {
      label: "Market today",
      value: status?.session
        ? status.session.trading_day
          ? `${status.session.early_close ? "Early close" : "Closes"} ${status.session.close} ET`
          : status.session.holiday || "Weekend"
        : "—",
      state: undefined,
    },
  );
  const bench = status?.supervisor?.spy_benchmark;
  const benchBad = !!bench?.enabled && (!!bench.error || ((bench.trades_count || 0) >= (bench.min_trades || 10) && !bench.passing));
  const bad = [...rows.filter(r => r.state === "warn" || r.state === "fail").map(r => r.label), ...(benchBad ? ["SPY benchmark"] : [])];
  const attention = bad.length;
  const overall = !status ? "Checking…" : attention === 0 ? "All systems normal" : `${attention} need${attention === 1 ? "s" : ""} attention: ${bad.join(", ")}`;

  return <Section title="Health" meta={overall}>
    {loginBroken && <p className={css["alert"]} role="alert">
      <b>Webull login needs attention.</b> Run <code>./auth.sh</code> (macOS/Linux) or <code>auth.ps1</code> (Windows), then approve the login in the Webull app.
      {token?.status && token.status !== "NORMAL" ? ` Current status: ${token.status}.` : ""}
      {expiry ? ` Token expires ${expiry}.` : ""}
    </p>}

    <dl className={css["health-list"]}>
      {rows.map(r => <HealthRow key={r.label} {...r}/>)}
      {bench && <SpyBenchmarkDisplay benchmark={bench}/>}
    </dl>

    {pendingCount > 0 && status?.trading_mode !== "auto" && !channels.length && <p className={css["alert"]} role="alert">
      {pendingCount} proposal(s) need approval, but no alert channel is configured. This page is the only way to
      see them. Add ALERT_NTFY_TOPIC (or another channel) to .env or approve them here.
    </p>}
    {status?.broker_error && <p className={css["alert"]}>{status.broker_error}</p>}
    {sup?.error && <p className={css["alert"]}>Supervisor: {sup.error}</p>}
    {status?.alerts?.last_error && <p className={css["alert"]}>Alert delivery failed: {status.alerts.last_error}</p>}
    {status?.realtime?.error && <p className={css["alert"]}>{status.realtime.error}</p>}

    <h3 className={css["subhead"]}>Limits</h3>
    <dl className={css["kv"]}>
      <dt>Daily trade cap</dt><dd className="num">{status?.limits?.max_trades_per_day === 0 ? `${status?.trades_today ?? 0} today · no cap` : `${status?.trades_today ?? 0} / ${status?.limits?.max_trades_per_day ?? "—"}`}</dd>
      <dt>Daily loss stop</dt><dd className="num">{money(status?.limits?.max_daily_loss_usd)}</dd>
      <dt>Per-order cap</dt><dd className="num">{money(status?.limits?.max_order_usd)}</dd>
      <dt>Signal refresh</dt><dd className="num">{status?.signals?.refresh_minutes ?? "—"} min</dd>
    </dl>
    <p className={css["note"]}>
      The daily loss stop blocks new buys; it does not trip the kill switch. Only the drawdown and consecutive-error
      guards trip it.
    </p>
  </Section>;
}

export function ActivityFeed({ events }: { events: EventRow[] }) {
  return <Section title="Activity feed">
    {!events.length ? <Empty>No recent events.</Empty> : <div className={css["activity-list"]}>
      {events.slice(0, 12).map(event => <div className={css["activity-row"]} key={event.id}>
        <span className={`${css["dot"]} ${css[event.level === "error" ? "fail" : event.level === "warn" ? "warn" : "info"]}`} aria-label={event.level}/>
        <span>{event.message}</span>
        <time className="num" suppressHydrationWarning>{dateTime(event.ts)}</time>
      </div>)}
    </div>}
  </Section>;
}

function HealthRow({ label, value, state }: Row) {
  const word = state === "ok" ? "OK" : state === "warn" ? "needs attention" : state === "fail" ? "failing" : "";
  return <div className={css["health-row"]}>
    <dt>
      {state && <i className={`${css["dot"]} ${css[state]}`} aria-hidden="true"/>}
      {label}
      {word && <span className={css["sr-only"]}> {word}</span>}
    </dt>
    <dd>{value}</dd>
  </div>;
}

function SpyBenchmarkDisplay({ benchmark }: { benchmark: SpyBenchmarkStatus }) {
  if (!benchmark.enabled) return null;
  if (benchmark.error) return <HealthRow label="SPY benchmark" value={benchmark.error} state="fail"/>;
  const trades = benchmark.trades_count || 0;
  const minTrades = benchmark.min_trades || 10;
  if (trades < minTrades) {
    return <HealthRow label="SPY benchmark" value={`${trades}/${minTrades} trades · need more data`} state="warn"/>;
  }
  const outperformance = benchmark.outperformance_pct ?? 0;
  const target = benchmark.min_outperformance_pct || 0.5;
  return <HealthRow
    label="SPY benchmark"
    value={`${outperformance >= 0 ? "+" : ""}${outperformance.toFixed(2)}% vs SPY over ${benchmark.window_days || 5}d (need +${target}%) · ${trades} trades · action: ${benchmark.action || "warn"}`}
    state={benchmark.passing ? "ok" : "fail"}
  />;
}
