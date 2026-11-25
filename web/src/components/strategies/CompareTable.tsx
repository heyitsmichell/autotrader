"use client";

import type { ReactNode } from "react";
import { money, num, pct } from "@/lib/format";
import type { Sleeve } from "@/lib/types";
import Mark from "@/components/ui/Mark";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

const when = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};
const fr = (v: number | null | undefined) => (v == null ? "—" : pct(v * 100, { digits: 1, sign: false }));
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? css["gain"] : css["loss"]);
const signedCell = (text: string, v: number | null | undefined) => <span className={tone(v)}>{text}</span>;

function StatusMark({ s }: { s: Sleeve }) {
  if (s.status === "paused") return <Mark tone="warn" title={s.status_reason ?? undefined}>Paused</Mark>;
  if (s.status === "disabled") return <Mark tone="neutral" title={s.status_reason ?? undefined}>Disabled</Mark>;
  if (s.status === "waiting") return <Mark tone="neutral" title={s.status_reason ?? undefined}>Waiting</Mark>;
  return <Mark tone="gain" title={s.status_reason ?? undefined}>Active</Mark>;
}

function Meter({ s }: { s: Sleeve }) {
  const dd = s.drawdown_pct, line = s.pause_dd_pct;
  const frac = dd == null || !line ? 0 : Math.min(1, Math.max(0, dd / line));
  const color = frac >= 1 ? "var(--loss)" : frac >= 0.5 ? "var(--warn)" : "var(--ink-2)";
  return <div className={css["meter"]} role="img"
    aria-label={`Drawdown ${Math.round(frac * 100)}% of the way to the ${pct(line, { sign: false, digits: 0 })} pause line`}
    title={`Pause line ${pct(line, { digits: 0 })}`}>
    <i style={{ width: `${frac * 100}%`, background: color }}/>
  </div>;
}

interface Row { label: string; group?: boolean; title?: string; cell?: (s: Sleeve) => ReactNode }

const liveSharpe = (s: Sleeve): ReactNode => {
  const days = s.live?.days ?? 0;
  if (days < 20 || s.live?.sharpe == null) return <span title="Needs at least 20 days of live history">—</span>;
  return <>{num(s.live.sharpe, 2)} <span className={css["muted"]}>({days} days)</span></>;
};

const ROWS: Row[] = [
  { label: "Equity", cell: s => money(s.equity) },
  { label: "Return since start", cell: s => signedCell(pct(s.total_return_pct), s.total_return_pct) },
  { label: "Day P&L", cell: s => signedCell(`${money(s.day_pnl, { sign: true })} (${pct(s.day_pnl_pct)})`, s.day_pnl) },
  { label: "Drawdown from peak", cell: s => <>{pct(s.drawdown_pct)}<Meter s={s}/></> },
  { label: "Invested", cell: s => `${money(s.invested)} (${pct(s.invested_pct, { sign: false, digits: 0 })})` },
  { label: "Positions", cell: s => String(s.positions_n ?? "—") },
  { label: "Live Sharpe", cell: liveSharpe },
  { label: "Backtest (expected)", group: true },
  { label: "Backtest CAGR", cell: s => fr(s.expected?.cagr), title: "cagr" },
  { label: "Backtest Sharpe", cell: s => (s.expected?.sharpe == null ? "—" : num(s.expected.sharpe, 2)) },
  { label: "Backtest max DD", cell: s => fr(s.expected?.max_dd) },
  { label: "Last rebalance", cell: s => when(s.last_rebalance) },
  { label: "Next rebalance", cell: s => when(s.next_rebalance) },
  { label: "Status reason", cell: s => s.status_reason || "—" },
];

const Head = ({ s }: { s: Sleeve }) => <>
  <i className={css["chip"]} style={{ background: sleeveColor(s.id, s.color_slot) }}/>{s.name} <StatusMark s={s}/>
</>;

export default function CompareTable({ sleeves }: { sleeves: Sleeve[] }) {
  const win = sleeves.find(s => s.expected?.window)?.expected?.window;
  return <div className={css["tableWrap"]}>
    <table className={`${css["tbl"]} ${css["wide"]}`}>
      <thead><tr><th scope="col">Metric</th>
        {sleeves.map(s => <th key={s.id} scope="col"><Head s={s}/></th>)}</tr></thead>
      <tbody>{ROWS.map(r => r.group
        ? <tr key={r.label} className={css["sub"]} title={win}><td colSpan={sleeves.length + 1}>{r.label}</td></tr>
        : <tr key={r.label} title={r.title === "cagr" ? win : undefined}>
          <th scope="row">{r.label}</th>{sleeves.map(s => <td key={s.id}
            title={r.label.startsWith("Backtest") ? s.expected?.window : undefined}>{r.cell?.(s)}</td>)}</tr>)}
      </tbody>
    </table>
    <div className={css["blocks"]}>
      {sleeves.map(s => <div key={s.id} className={css["block"]}>
        <h3><Head s={s}/></h3>
        {ROWS.filter(r => !r.group).map(r => <div key={r.label} className={css["kv"]}>
          <span>{r.label}</span><span>{r.cell?.(s)}</span></div>)}
      </div>)}
    </div>
  </div>;
}
