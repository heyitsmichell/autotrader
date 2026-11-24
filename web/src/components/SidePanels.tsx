"use client";

import { dateTime, money, pct, secondsAgo, signTone } from "@/lib/format";
import type { EventRow, Portfolio, Status, SpyBenchmarkStatus } from "@/lib/types";
import css from "./SidePanels.module.css";

export function AccountPanel({ portfolio, status, onRefresh }: {
  portfolio: Portfolio | null;
  status: Status | null;
  onRefresh: () => void;
}) {
  const positions = portfolio?.positions || [];
  return <section className={`panel positions-panel`}>
    <div className="panel-head">
      <div><div className="eyebrow">ACCOUNT</div><h2>Open positions</h2></div>
      <button className="icon-button" aria-label="Refresh portfolio" onClick={onRefresh}>↻</button>
    </div>
    {!positions.length
      ? <Empty icon="◌" text={portfolio ? "No open positions" : status?.connected ? "Loading account…" : "Connect the broker to view account"}/>
      : <div className={`${css["position-list"]}`}>
        {positions.map(p => <div className={`${css["position-row"]}`} key={p.symbol}>
          <div className={`${css["position-head"]}`}>
            <div><b>{p.symbol}</b><small>{p.qty} shares · avg {money(p.cost)}</small></div>
            <div className={`${css["position-right"]}`}>
              <b>{money(p.market_value)}</b>
              <small>{money(p.unrealized)} ({pct(p.unrealized_pct)})</small>
            </div>
          </div>
          <div className={`${css["position-track"]}`}>
            <span
              style={{ width: `${Math.min(100, Math.max(8, Math.abs(Number(p.unrealized_pct || 0)) * 5))}%` }}
              className={Number(p.unrealized) >= 0 ? `${css["gain"]}` : "loss"}
            />
          </div>
        </div>)}
      </div>}
    {portfolio?.errors && Object.keys(portfolio.errors).length > 0 && <div className="inline-warning">
      Portfolio data incomplete: {Object.keys(portfolio.errors).join(", ")}
    </div>}
    <div className={`${css["account-meta"]}`}>
      <span>Net liquidation</span><b>{money(portfolio?.balance?.net_liq)}</b>
      <span>Unrealized P&amp;L</span><b className={signTone(portfolio?.balance?.unrealized)}>{money(portfolio?.balance?.unrealized)}</b>
      <span>Buying power</span><b>{money(portfolio?.balance?.buying_power)}</b>
    </div>
  </section>;
}

export function OrdersPanel({ portfolio }: { portfolio: Portfolio | null }) {
  const openOrders = portfolio?.open_orders || [];
  return <section className="panel">
    <div className="panel-head">
      <div><div className="eyebrow">BROKER ORDERS</div><h2>Open orders <span className="count-pill">{openOrders.length}</span></h2></div>
    </div>
    {!openOrders.length
      ? <Empty icon="⌁" text="No open orders"/>
      : <div className={`${css["order-list"]}`}>
        {openOrders.map((order, index) => <div className={`${css["order-row"]}`} key={order.client_order_id || index}>
          <div><b>{order.side} {order.symbol}</b><small>{order.quantity} shares{order.limit_price ? ` · limit ${money(order.limit_price)}` : ""}</small></div>
          <span className="status-chip working">{order.status || "working"}</span>
        </div>)}
      </div>}
    <div className={`${css["protective-list"]}`}>
      {(portfolio?.protective || []).slice(0, 6).map(item => <div className={`${css["protective-row"]}`} key={item.id}>
        <span className={`${css["protect-icon"]}`} aria-hidden="true">{item.kind === "sl" ? "↓" : "↑"}</span>
        <span>{item.symbol} {item.kind === "sl" ? "stop" : "target"}<small>{item.status}{item.status === "waiting" ? " · armed on fill" : ""}</small></span>
        <b>{money(item.price)}</b>
      </div>)}
    </div>
  </section>;
}

export function HealthPanel({ status, pendingCount, onTestAlert, busy }: {
  status: Status | null;
  pendingCount: number;
  onTestAlert: () => void;
  busy: boolean;
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

  return <section className="panel">
    <div className="panel-head">
      <div><div className="eyebrow">SYSTEM HEALTH</div><h2>Operations</h2></div>
      <span className={`${css["health-pill"]} ${status?.connected ? "healthy" : `${css["unhealthy"]}`}`}>{status?.connected ? "HEALTHY" : "DEGRADED"}</span>
    </div>

    {loginBroken && <div className="inline-warning" role="alert">
      <b>Webull login needs attention.</b> Run <code>./auth.sh</code> (macOS/Linux) or <code>auth.ps1</code> (Windows), then approve the login in the Webull app.
      {token?.status && token.status !== "NORMAL" ? ` Current status: ${token.status}.` : ""}
      {expiry ? ` Token expires ${expiry}.` : ""}
    </div>}

    <div className={`${css["health-list"]}`}>
      <Health label="Broker" value={status?.connected ? "Connected" : "Disconnected"} ok={status?.connected}/>
      <Health label="Price feed" value={`${status?.realtime?.mode || "—"}${status?.realtime?.mode === "poll" ? ` · ${status?.realtime?.poll_seconds}s` : ""}`} ok={status?.connected}/>
      <Health label="Signal data" value={signalAge == null ? "Waiting" : `${signalAge}m old`} ok={signalAge != null && signalAge < 30}/>
      <Health label="Risk engine" value="Active · hard limits" ok/>
      <Health label="Trading mode" value={status?.trading_mode === "auto" ? "Auto-trade" : "Approval required"} ok={status?.trading_mode !== "auto"}/>
      <Health
        label="Supervisor"
        value={supAge == null ? "Starting" : `Checked ${supAge}s ago${sup?.auto_start ? " · auto-start" : ""}`}
        ok={supAge != null && supAge < 180 && !sup?.error}
      />
      <Health
        label="Alerts"
        value={channels.length ? channels.join(", ") : "None configured"}
        ok={channels.length > 0 && !status?.alerts?.last_error}
      />
      {status?.broker_mode === "webull" && <Health
        label="Webull login"
        value={!token ? "Checking" : token.status === "not_required"
          ? "No login token needed"
          : token.status !== "NORMAL"
          ? `${token.status} — run ./auth.sh (or auth.ps1 on Windows) and approve in the Webull app`
          : `${Number(token.days_left).toFixed(1)} days left${token.estimated ? " (est.)" : ""}`}
        ok={!!token && (token.status === "not_required" || (token.status === "NORMAL" && Number(token.days_left) > 3))}
      />}

      {status?.supervisor?.spy_benchmark && (
        <SpyBenchmarkDisplay benchmark={status.supervisor.spy_benchmark} />
      )}
      <Health
        label="Auto kill switch"
        value={`DD ${sup?.drawdown_pct == null ? "—" : `${sup.drawdown_pct}%`} / ${sup?.max_drawdown_pct ? `${sup.max_drawdown_pct}%` : "off"} · errors ${Math.max(sup?.run_failures ?? 0, sup?.order_failures ?? 0)} / ${sup?.max_errors || "off"} · ${sup?.kill_action || "pause"}`}
        ok={!status?.kill_switch}
      />
      <Health
        label="Market today"
        value={status?.session
          ? status.session.trading_day
            ? `${status.session.early_close ? "Early close" : "Closes"} ${status.session.close} ET`
            : status.session.holiday || "Weekend"
          : "—"}
        ok={!!status?.session?.trading_day}
      />
    </div>

    {pendingCount > 0 && status?.trading_mode !== "auto" && !channels.length && <div className="inline-warning" role="alert">
      {pendingCount} proposal(s) need approval, but no alert channel is configured. This page is the only way to
      see them. Add ALERT_NTFY_TOPIC (or another channel) to .env or approve them here.
    </div>}
    {status?.broker_error && <div className="inline-warning">{status.broker_error}</div>}
    {sup?.error && <div className="inline-warning">Supervisor: {sup.error}</div>}
    {status?.alerts?.last_error && <div className="inline-warning">Alert delivery failed: {status.alerts.last_error}</div>}
    {status?.realtime?.error && <div className="inline-warning">{status.realtime.error}</div>}

    <div className="actions-end">
      <button
        className="button button-quiet"
        disabled={busy || !channels.length}
        title={channels.length ? "Send a test message to every alert channel" : "Add ALERT_NTFY_TOPIC (or another channel) to .env"}
        onClick={onTestAlert}
      >Send test alert</button>
    </div>

    <div className={`${css["risk-mini"]}`}>
      <div><span>Daily trade cap</span><b>{status?.limits?.max_trades_per_day === 0 ? `${status?.trades_today ?? 0} today · no cap` : `${status?.trades_today ?? 0} / ${status?.limits?.max_trades_per_day ?? "—"}`}</b></div>
      <div><span>Daily loss stop</span><b>{money(status?.limits?.max_daily_loss_usd)}</b></div>
      <div><span>Per-order cap</span><b>{money(status?.limits?.max_order_usd)}</b></div>
      <div><span>Signal refresh</span><b>{status?.signals?.refresh_minutes ?? "—"} min</b></div>
    </div>
    <p className={`${css["risk-note"]}`}>
      The daily loss stop blocks new buys; it does not trip the kill switch. Only the drawdown and consecutive-error
      guards trip it.
    </p>
  </section>;
}

export function ActivityFeed({ events }: { events: EventRow[] }) {
  return <section className={`panel ${css["activity-panel"]}`}>
    <div className="panel-head"><div><div className="eyebrow">AUDIT TRAIL</div><h2>Activity feed</h2></div></div>
    <div className={`${css["activity-list"]}`}>
      {events.slice(0, 12).map(event => <div className={`${css["activity-row"]}`} key={event.id}>
        <span className={`${css["activity-level"]} ${event.level}`} aria-label={event.level}/>
        <span>{event.message}</span>
        <time suppressHydrationWarning>{dateTime(event.ts)}</time>
      </div>)}
      {!events.length && <Empty icon="·" text="No recent events"/>}
    </div>
  </section>;
}

function Health({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  return <div className={`${css["health-row"]}`}>
    <span><i className={`dot ${ok ? "green" : "red"}`} aria-hidden="true"/>{label}<span className={`${css["sr-only"]}`}>{ok ? " OK" : " needs attention"}</span></span>
    <b>{value}</b>
  </div>;
}

function Empty({ icon, text }: { icon: string; text: string }) {
  return <div className="empty-state"><span aria-hidden="true">{icon}</span><p>{text}</p></div>;
}

function SpyBenchmarkDisplay({ benchmark }: { benchmark: SpyBenchmarkStatus }) {
  if (!benchmark.enabled) return null;
  if (benchmark.error) {
    return <div className={`${css["health-row"]}`}><span>SPY Benchmark</span><b className="negative">{benchmark.error}</b></div>;
  }
  const passing = benchmark.passing;
  const trades = benchmark.trades_count || 0;
  const minTrades = benchmark.min_trades || 10;
  const outperformance = benchmark.outperformance_pct ?? 0;
  const target = benchmark.min_outperformance_pct || 0.5;
  const window = benchmark.window_days || 5;
  const action = benchmark.action || "warn";

  if (trades < minTrades) {
    return <div className={`${css["health-row"]}`}>
      <span><i className="dot amber" aria-hidden="true"/>SPY Benchmark <span className={`${css["sr-only"]}`}>needs more data</span></span>
      <b className="warning">{trades}/{minTrades} trades · need more data</b>
    </div>;
  }

  return <div className={`${css["health-row"]}`}>
    <span>
      <i className={`dot ${passing ? "green" : "red"}`} aria-hidden="true"/>
      SPY Benchmark <span className={`${css["sr-only"]}`}>{passing ? "passing" : "failing"}</span>
    </span>
    <b className={passing ? "positive" : "negative"}>
      {outperformance >= 0 ? "+" : ""}{outperformance.toFixed(2)}% vs SPY over {window}d (need +{target}%)
      <small> · {trades} trades · action: {action}</small>
    </b>
  </div>;
}
