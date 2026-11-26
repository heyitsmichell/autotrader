"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { etDayKey, isOccSymbol, money, pct } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { SleevesPayload, EventRow, Performance, Portfolio, Proposal, Signal, Status, TradeLedgerEntry } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import EquityChart from "./EquityChart";
import StrategiesVsSpy from "./today/StrategiesVsSpy";
import Activity from "./today/Activity";
import { summarizeGate, weekdaysSince } from "./today/gate";
import OptionPositions from "./options/OptionPositions";
import Ladder, { type LadderRow } from "./today/Ladder";
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
        parts.push(covered === positions.length ? `${positions.length} ${plural}, all with stops.` : `${positions.length} ${plural}.`);
      } else parts.push(`${positions.length} ${plural}.`);
    }
  }
  parts.push(pendingCount > 0
    ? `${pendingCount} ${pendingCount === 1 ? "trade needs" : "trades need"} your decision.`
    : "Nothing needs you.");
  return parts.join(" ");
}

const num = (v: unknown): number | null => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
const tone = (v: number | null): "gain" | "loss" | undefined => (v == null || v === 0 ? undefined : v > 0 ? "gain" : "loss");

type Finding = { text: string; tone: "loss" | "warn" | "" };

export default function Today({ status, portfolio, performance, events, pendingCount, needsYou, onSymbol, signals, api, proposals: proposalsProp }: {
  status: Status | null;
  portfolio: Portfolio | null;
  performance: Performance | null;
  events: EventRow[];
  pendingCount: number;
  needsYou: ReactNode;
  controls?: ReactNode;
  onSymbol?: (symbol: string) => void;
  signals?: Signal[];
  proposals?: Proposal[];
  api?: Api;
}) {
  const [trades, setTrades] = useState<TradeLedgerEntry[]>([]);
  const sleevesData = usePoll<SleevesPayload>(api, api ? "/api/sleeves" : null, 60_000).data;
  const swingSleeve = useMemo(() => {
    const s = sleevesData?.sleeves?.find(x => x.id === "swing");
    return s ? { equity: num(s.equity), invested_pct: num(s.invested_pct) } : null;
  }, [sleevesData]);
  const allPositions = useMemo(() => portfolio?.positions || [], [portfolio]);
  const positions = useMemo(() => allPositions.filter(p => !isOccSymbol(p.symbol)), [allPositions]);
  const optionPositions = useMemo(() => [
    ...allPositions.filter(p => isOccSymbol(p.symbol)),
    ...(portfolio?.option_positions || []).map(o => ({
      symbol: o.symbol, qty: o.qty, cost: o.cost_basis ?? null,
      market_value: o.mark_price != null ? o.mark_price * 100 * o.qty : null,
      unrealized: o.unrealized_pnl ?? null, unrealized_pct: null,
    })),
  ], [allPositions, portfolio?.option_positions]);
  const optionsLine = useMemo(() => {
    if (!status?.settings?.options_enabled) return "";
    const day = etDayKey();
    let proposed = 0;
    const skips = new Map<string, number>();
    for (const e of events) {
      if (etDayKey(e.ts) !== day) continue;
      if (/^Auto option entry for/i.test(e.message)) proposed++;
      else if (/skipped:/i.test(e.message) && /option|call|put/i.test(e.message)) {
        const reason = e.message.split(/skipped:\s*/i)[1]?.trim().replace(/[.\s]+$/, "") || "unknown";
        skips.set(reason, (skips.get(reason) || 0) + 1);
      }
    }
    const top = [...skips].sort((a, b) => b[1] - a[1])[0];
    return `Options: ${proposed} ${proposed === 1 ? "call" : "calls"} proposed today${top ? ` · top skip reason: ${top[0]} (${top[1]})` : ""}`;
  }, [events, status?.settings?.options_enabled]);
  const posKey = positions.map(p => p.symbol).join(",");
  useEffect(() => {
    if (!api || !posKey) return;
    let live = true;
    api<TradeLedgerEntry[]>("/api/trades?limit=200&status=open")
      .then(r => { if (live && Array.isArray(r)) setTrades(r); })
      .catch(() => { if (live) setTrades([]); });
    return () => { live = false; };
  }, [api, posKey]);

  const maxHold = num(status?.settings?.exit_max_hold_days) ?? 20;
  const protection = portfolio?.protection;
  const stops = useMemo(() => {
    const m = new Map<string, number>();
    for (const p of portfolio?.protective || []) {
      if ((p.kind === "sl" || p.kind === "stop") && !DEAD.has(String(p.status).toLowerCase())) m.set(p.symbol, p.price);
    }
    return m;
  }, [portfolio]);
  const hasStopData = !!portfolio?.protective || !!protection;

  const rows: LadderRow[] = useMemo(() => positions.map(p => {
    const qty = Number(p.qty);
    const mv = num(p.market_value);
    const last = qty && mv != null ? mv / qty : null;
    const trade = trades.filter(t => t.symbol === p.symbol && t.status === "open")
      .sort((a, b) => String(b.entry_fill_time).localeCompare(String(a.entry_fill_time)))[0];
    const held = weekdaysSince(trade?.entry_fill_time);
    const pr = protection?.[p.symbol];
    return {
      symbol: p.symbol, qty, entry: num(p.cost) ?? num(trade?.entry_fill_price), last,
      pnl: num(p.unrealized), pnlPct: num(p.unrealized_pct),
      stop: pr ? (pr.stop ?? null) : (stops.get(p.symbol) ?? null), stopKnown: hasStopData,
      state: pr?.state ?? null, detail: pr?.detail,
      daysHeld: held, timeStopIn: held == null || maxHold <= 0 ? null : Math.max(0, maxHold - held),
    };
  }), [positions, trades, stops, hasStopData, protection, maxHold]);

  const net = num(portfolio?.balance?.net_liq);
  const dayPnl = num(portfolio?.balance?.day_pnl ?? status?.day_pnl);
  const openPnl = positions.length ? positions.reduce((a, p) => a + (num(p.unrealized) ?? 0), 0) : null;
  const unstopped = (r: LadderRow) => r.stop == null || r.state === "missing";
  const noStopCount = hasStopData ? rows.filter(unstopped).length : 0;
  const atRisk = hasStopData && positions.length
    ? rows.reduce((a, r) => a + (r.stop != null && !unstopped(r) && r.last != null ? Math.max(0, r.last - r.stop) * r.qty : 0), 0) : null;
  const swingEq = swingSleeve?.equity ?? null;
  const investDen = swingEq && swingEq > 0 ? swingEq : net;
  const investedFromSleeve = swingEq != null && swingEq > 0;
  const invested = investDen && positions.length ? positions.reduce((a, p) => a + (num(p.market_value) ?? 0), 0) / investDen : null;
  const excess = num(performance?.excess_return_pct);
  const minDays = performance?.min_days ?? performance?.significance?.min_days ?? null;
  const excessSuffix = excess != null && !performance?.enough_data
    ? ` (${performance?.days ?? 0}${minDays != null ? `/${minDays}` : ""} days, not significant yet)` : "";
  const bench = performance?.benchmark_symbol || "SPY";

  const gate = useMemo(() => summarizeGate(signals || [], positions.map(p => p.symbol)), [signals, positions]);
  const passText = gate.passHeld > 0 ? `${gate.pass} pass (${gate.passHeld} already held)` : `${gate.pass} pass`;

  const finding: Finding = useMemo(() => {
    const noStop = rows.filter(r => r.stopKnown && (r.state ? r.state === "missing" : r.stop == null)).map(r => r.symbol);
    if (noStop.length) return { text: `${noStop.join(", ")} ${noStop.length === 1 ? "has" : "have"} no protective stop.`, tone: "loss" };
    const inactive = rows.filter(r => r.state === "inactive").length;
    if (inactive && (portfolio?.session === "ALL" || portfolio?.session === "NIGHT")) {
      return { text: `${inactive} ${inactive === 1 ? "position has" : "positions have"} no active stop until 09:30 ET.`, tone: "warn" };
    }
    if (status?.kill_switch) return { text: "Kill switch is on: no new entries until it is released.", tone: "warn" };
    if (status && !status.scheduler_on) return { text: "The engine is paused, so nothing new will be evaluated.", tone: "warn" };
    if (status && !status.connected) return { text: "The broker is disconnected; orders and prices may be stale.", tone: "warn" };
    if (gate.watched && gate.pass === 0 && gate.reasons.length) {
      const top = gate.reasons[0]!;
      return { text: `No entries: ${top.count} of ${gate.watched} symbols blocked by ${top.name}.`, tone: "" };
    }
    const m = rows.filter(r => r.pnlPct != null).sort((a, b) => Math.abs(b.pnlPct!) - Math.abs(a.pnlPct!))[0];
    if (m) return { text: `Biggest mover: ${m.symbol} at ${pct(m.pnlPct, { sign: true })} since entry.`, tone: "" };
    return { text: gate.watched ? `${gate.pass} of ${gate.watched} symbols pass the gate right now.` : "", tone: "" };
  }, [rows, status, gate, portfolio?.session]);

  return <div className={css["today"]}>
    <div className={css["head"]}>
      <h1 className={css["sentence"]}>{statusSentence(status, portfolio, pendingCount)}</h1>
      {finding.text && <p className={`${css["finding"]} ${finding.tone ? css[finding.tone] : ""}`}>{finding.text}</p>}
      <FigureRow>
        <Figure label="Net value" value={money(net)}/>
        <Figure label="Day P&L" value={money(dayPnl, { sign: true })} tone={tone(dayPnl)}/>
        <Figure label="Swing open P&L" value={money(openPnl, { sign: true })} tone={tone(openPnl)}/>
        <Figure label="At risk to stops"
          value={atRisk == null ? "—" : noStopCount === rows.length ? `${noStopCount} without stop`
            : noStopCount ? `${money(atRisk)} · ${noStopCount} without stop`
            : `${money(atRisk)} ${net ? pct((atRisk / net) * 100, { sign: false }) : ""}`}
          tone={noStopCount || atRisk ? "loss" : undefined}
          title={atRisk == null ? "No positions or no stop data." : noStopCount ? "Positions without a stop have unbounded risk and are not counted in the dollar figure." : "Sum of (last price minus stop) times quantity: what the stops would cost if all hit."}/>
        <Figure label="Swing invested" value={invested == null ? "—" : pct(invested * 100, { digits: invested != null && invested < 0.1 ? 1 : 0, sign: false })}
          title={investedFromSleeve ? "Swing positions' market value divided by the swing sleeve equity." : "Swing sleeve equity unavailable: divided by whole-account net value instead."}/>
        <Figure label={`vs ${bench} since start`} value={excess == null ? "—" : `${pct(excess, { sign: true })}${excessSuffix}`}
          tone={tone(excess)}
          title={excess == null ? (performance?.message || "Not enough history yet for a fair comparison.") : excessSuffix ? "Not statistically significant yet." : undefined}/>
      </FigureRow>
      {portfolio?.balance?.stale && <p className="muted">The broker returned an empty balance; showing the last good values.</p>}
    </div>

    <div className={css["main"]}>
    <div className={css["pos"]}>
      <Section title="Positions" meta={positions.length ? `${positions.length} swing open` : undefined}>
        <Ladder rows={rows} onSymbol={onSymbol}
          emptyNote={`No open positions. The bot buys when a symbol passes the gate; ${passText} right now.`}/>
      </Section>
      {status?.settings?.options_enabled && <OptionPositions positions={optionPositions}
        proposals={proposalsProp} autoEntry={status.settings.options_auto_entry}
        budget={status.settings.options_entry_max_premium_usd ?? null}/>}
    </div>

    <div className={css["chartbox"]}>{api ? <StrategiesVsSpy api={api} performance={performance}/> : <EquityChart performance={performance}/>}</div>
    </div>

    <div className={css["rail"]}>
      <div className={css["needs"]}>
        <Section title="Needs you" meta={pendingCount ? String(pendingCount) : undefined}>
          {pendingCount > 0 ? needsYou : <Empty>Nothing needs you. Proposals appear here when a symbol passes the gate in approve mode.</Empty>}
        </Section>
      </div>
      <div className={css["gate"]}>
        <Section title="Gate today">
          {gate.watched ? <>
            <p className={`${css["funnel"]} mono`}>{gate.watched} watched → {gate.eligible} eligible → {passText}</p>
            {gate.reasons.length > 0 && <ul className={css["reasons"]}>
              {gate.reasons.slice(0, 3).map(r => <li key={r.name}><span>{r.name}</span><span className="mono">{r.count}</span></li>)}
            </ul>}
            {optionsLine && <p className={`${css["funnel"]} mono`}>{optionsLine}</p>}
          </> : <Empty>No signals yet. Gate results appear after the next analysis run.</Empty>}
        </Section>
      </div>
      <div className={css["act"]}><Activity events={events}/></div>
    </div>

  </div>;
}
