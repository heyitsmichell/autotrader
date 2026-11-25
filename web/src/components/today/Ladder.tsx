"use client";

import { money, pct } from "@/lib/format";
import Empty from "@/components/ui/Empty";
import css from "../Today.module.css";

export type ProtectionState = "broker" | "soft" | "inactive" | "closed" | "missing" | "soft_fired";

const STATE: Record<ProtectionState, [string, string]> = {
  broker: ["at broker", ""],
  soft: ["app soft stop", "warn"],
  inactive: ["broker stop off until 09:30", "warn"],
  closed: ["market closed", "muted"],
  missing: ["no stop", "loss"],
  soft_fired: ["soft stop sent", "warn"],
};

function StateLabel({ r }: { r: LadderRow }) {
  if (r.state) {
    const [label, tone] = STATE[r.state];
    return <span className={tone === "muted" ? "muted" : tone ? css[tone] : undefined} title={r.detail || undefined}>{label}</span>;
  }
  return r.stop != null ? <>at broker</> : r.stopKnown ? <span className={css["loss"]}>no stop</span> : <span className="muted">—</span>;
}

export interface LadderRow {
  symbol: string;
  qty: number;
  entry: number | null;
  last: number | null;
  pnl: number | null;
  pnlPct: number | null;
  stop: number | null;
  stopKnown: boolean;
  state?: ProtectionState | null;
  detail?: string;
  daysHeld: number | null;
  timeStopIn: number | null;
}

const W = 140;

function Track({ r, full }: { r: LadderRow; full?: boolean }) {
  const { stop, entry, last } = r;
  if (last == null) return <span className="muted">—</span>;
  const vals = [entry, last].filter((v): v is number => v != null);
  const lo = stop != null ? Math.min(stop, ...vals) : Math.min(...vals);
  const hi = Math.max(...vals, lo + 1e-9);
  // Percent coordinates (no viewBox), so the track stretches to any width while the dot stays round.
  const x = (v: number) => `${3 + ((v - lo) / (hi - lo || 1)) * 94}%`;
  const below = stop != null && last <= stop;
  return <svg className={css["track"]} width={full ? "100%" : W} height={20} role="img"
    aria-label={stop != null ? `Stop ${money(stop)}, entry ${money(entry)}, last ${money(last)}` : "No stop set"}>
    <line x1="3%" x2="97%" y1={10} y2={10} stroke="#3A3D43" strokeWidth={2} strokeDasharray={stop == null ? "3 3" : undefined}/>
    {stop != null && <line x1="3%" x2="3%" y1={3} y2={17} stroke="#E5596F" strokeWidth={2}/>}
    {entry != null && <line x1={x(entry)} x2={x(entry)} y1={5} y2={15} stroke="#8C9199" strokeWidth={1}/>}
    <circle cx={x(last)} cy={10} r={4} fill={below ? "#E5596F" : "#ECEDEF"}/>
  </svg>;
}

export default function Ladder({ rows, onSymbol, emptyNote }: {
  rows: LadderRow[];
  onSymbol?: (s: string) => void;
  emptyNote: string;
}) {
  if (!rows.length) return <Empty>{emptyNote}</Empty>;
  return <>
  <div className={css["cards"]}>{rows.map(r => <div key={r.symbol} className={css["card"]}>
    <div className={css["card1"]}>
      {onSymbol ? <button className={css["sym"]} onClick={() => onSymbol(r.symbol)}>{r.symbol}</button> : <b>{r.symbol}</b>}
      <span className={`mono ${r.pnl == null || r.pnl === 0 ? "" : r.pnl > 0 ? css["gain"] : css["loss"]}`}>
        {money(r.pnl, { sign: true })} {pct(r.pnlPct, { sign: true })}</span>
    </div>
    <div className={css["card2"]}><Track r={r} full/></div>
    <p className={css["card3"]}>{r.stop != null
      ? <>Stop {money(r.stop)} · <StateLabel r={r}/>{r.timeStopIn == null ? "" : r.timeStopIn === 0 ? " · time stop due" : ` · time stop in ${r.timeStopIn} days`}</>
      : r.stopKnown ? <><StateLabel r={r}/></> : "Stop unknown"}</p>
  </div>)}</div>
  <div className={`table-scroll ${css["tablewrap"]}`}>
    <table>
      <thead><tr>
        <th scope="col">Symbol</th><th scope="col" className="num">Qty</th><th scope="col" className="num">Entry</th>
        <th scope="col" className="num">Last</th><th scope="col" className="num">P&amp;L</th>
        <th scope="col">Stop to last</th><th scope="col" className="num">Stop</th><th scope="col">Stop state</th>
        <th scope="col" className="num">Held</th><th scope="col">Time stop</th>
      </tr></thead>
      <tbody>{rows.map(r => <tr key={r.symbol}>
        <th scope="row">{onSymbol
          ? <button className={css["sym"]} onClick={() => onSymbol(r.symbol)}>{r.symbol}</button> : r.symbol}</th>
        <td className="num">{r.qty}</td>
        <td className="num">{money(r.entry)}</td>
        <td className="num">{money(r.last)}</td>
        <td className={`num ${r.pnl == null || r.pnl === 0 ? "" : r.pnl > 0 ? css["gain"] : css["loss"]}`}>
          {money(r.pnl, { sign: true })}<small> {pct(r.pnlPct, { sign: true })}</small>
        </td>
        <td><Track r={r}/></td>
        <td className="num">{money(r.stop)}</td>
        <td><StateLabel r={r}/></td>
        <td className="num">{r.daysHeld == null ? "—" : `${r.daysHeld}d`}</td>
        <td className="muted">{r.timeStopIn == null ? "—" : r.timeStopIn === 0 ? "due" : `in ${r.timeStopIn} days`}</td>
      </tr>)}</tbody>
    </table>
  </div></>;
}
