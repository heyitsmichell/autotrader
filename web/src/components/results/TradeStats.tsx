"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import { money, num, pct, signed } from "@/lib/format";
import type { TradeLedgerEntry } from "@/lib/types";
import css from "./results.module.css";

const exitBucket = (r: string | null): "stop" | "time" | "manual" | "other" => {
  const s = (r || "").toLowerCase();
  if (s.includes("stop")) return "stop";
  if (s.includes("time") || s.includes("expir") || s.includes("days")) return "time";
  if (s.includes("manual") || s.includes("flatten") || s.includes("kill") || s.includes("user")) return "manual";
  return "other";
};

const tone = (n: number | null): "gain" | "loss" | undefined => (n == null || n === 0 ? undefined : n > 0 ? "gain" : "loss");

export default function TradeStats() {
  const [rows, setRows] = useState<TradeLedgerEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api<TradeLedgerEntry[]>("/api/trades?limit=500&status=closed")
      .then(d => { if (active) { setRows(Array.isArray(d) ? d.filter(t => t.status === "closed") : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); });
    return () => { active = false; };
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!rows) return <p className="loading">Loading trade stats…</p>;
  const n = rows.length;
  const meta = `All closed trades, n = ${n}`;
  if (n === 0) return <Section title="Trade stats" meta={meta}><Empty>No closed trades yet. Stats appear after the first exit.</Empty></Section>;

  const pnl = rows.map(t => t.realized_pnl).filter((v): v is number => v != null);
  const wins = pnl.filter(v => v > 0);
  const losses = pnl.filter(v => v < 0);
  const sum = (a: number[]) => a.reduce((s, v) => s + v, 0);
  const avgWin = wins.length ? sum(wins) / wins.length : null;
  const avgLoss = losses.length ? sum(losses) / losses.length : null;
  const expectancy = pnl.length ? sum(pnl) / pnl.length : null;
  const grossLoss = Math.abs(sum(losses));
  const pf = grossLoss > 0 ? sum(wins) / grossLoss : null;
  const holds = rows.map(t => t.hold_seconds).filter((v): v is number => v != null);
  const avgHold = holds.length ? sum(holds) / holds.length / 86400 : null;
  const rs = rows.flatMap(t => {
    if (t.realized_pnl == null || t.entry_fill_price == null || t.stop_price == null) return [];
    const q = t.quantity_filled ?? t.quantity_requested;
    const risk = Math.abs(t.entry_fill_price - t.stop_price) * (q ?? 0);
    return risk > 0 ? [t.realized_pnl / risk] : [];
  });
  const expR = rs.length ? sum(rs) / rs.length : null;
  const ex = { stop: 0, time: 0, manual: 0, other: 0 };
  rows.forEach(t => { ex[exitBucket(t.exit_reason)]++; });
  const rets = rows.map(t => t.realized_pnl_pct).filter((v): v is number => v != null);

  return <Section title="Trade stats" meta={meta}>
    <div className={css["statsWrap"]}>
      <div className={css["statsMain"]}>
        <FigureRow>
          <Figure label="Win rate" value={pnl.length ? pct(wins.length / pnl.length * 100, { sign: false, digits: 1 }) : "—"} />
          <Figure label="Avg win" value={money(avgWin, { sign: true })} tone={tone(avgWin)} />
          <Figure label="Avg loss" value={money(avgLoss, { sign: true })} tone={tone(avgLoss)} />
          <Figure label="Expectancy per trade" value={money(expectancy, { sign: true })} tone={tone(expectancy)}
            title={expR != null ? `${signed(expR)} R across ${rs.length} trades with a recorded stop` : "R needs a recorded stop price"} />
          <Figure label="Profit factor" value={pf != null ? num(pf) : "—"} title={pf == null ? "No losing trades yet" : undefined} />
          <Figure label="Avg hold" value={avgHold != null ? `${num(avgHold, 1)} d` : "—"} />
        </FigureRow>
        <p className={css["exits"]}>
          Exits: <span className="mono">{ex.stop}</span> stop · <span className="mono">{ex.time}</span> time ·{" "}
          <span className="mono">{ex.manual}</span> manual · <span className="mono">{ex.other}</span> other
          {expR != null && <> · Expectancy <span className="mono">{signed(expR)} R</span></>}
        </p>
      </div>
      <div className={css["statsHist"]}>
        {n >= 10 && rets.length >= 10
          ? <Histogram values={rets} />
          : <p className={css["muted"]}>Too few closed trades for a distribution: {n} so far.</p>}
      </div>
    </div>
  </Section>;
}

function Histogram({ values }: { values: number[] }) {
  const lo = Math.min(...values, 0), hi = Math.max(...values, 0);
  const span = hi - lo || 1;
  const bins = Math.min(12, Math.max(5, Math.round(Math.sqrt(values.length) * 1.5)));
  const w = span / bins;
  const counts = Array.from({ length: bins }, () => 0);
  values.forEach(v => { counts[Math.min(bins - 1, Math.floor((v - lo) / w))]++; });
  const maxC = Math.max(...counts);
  const W = 360, H = 130, L = 4, B = 20, T = 6;
  const iw = W - L * 2, ih = H - B - T;
  const x = (v: number) => L + ((v - lo) / span) * iw;
  const y = (c: number) => T + ih - (c / maxC) * ih;
  return <figure className={css["hist"]}>
    <figcaption className={css["muted"]}>Distribution of trade returns, % per trade</figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Histogram of trade returns in percent">
      {[0.25, 0.5, 0.75].map(f => <line key={f} x1={L} x2={W - L} y1={T + ih * f} y2={T + ih * f} stroke="var(--rule)" strokeWidth="1" opacity="0.6" />)}
      {counts.map((c, i) => {
        if (!c) return null;
        const a = lo + i * w, b = a + w;
        const fill = a >= 0 ? "var(--gain)" : b <= 0 ? "var(--loss)" : "var(--ink-2)";
        return <rect key={i} x={x(a) + 1} y={y(c)} width={Math.max(1, x(b) - x(a) - 2)} height={T + ih - y(c)} fill={fill} opacity="0.85"><title>{`${num(a, 1)}% to ${num(b, 1)}%: ${c}`}</title></rect>;
      })}
      <line x1={x(0)} x2={x(0)} y1={T} y2={T + ih} stroke="var(--ink)" strokeWidth="1" />
      <line x1={L} x2={W - L} y1={T + ih} y2={T + ih} stroke="var(--rule)" strokeWidth="1" />
      <text x={L} y={H - 5} fontSize="11" fill="var(--ink-2)" className="mono">{signed(lo, 1)}%</text>
      <text x={x(0)} y={H - 5} fontSize="11" fill="var(--ink-2)" textAnchor="middle" className="mono">0</text>
      <text x={W - L} y={H - 5} fontSize="11" fill="var(--ink-2)" textAnchor="end" className="mono">{signed(hi, 1)}%</text>
    </svg>
  </figure>;
}
