import { pct } from "@/lib/format";
import type { SleeveTearSheet } from "@/lib/types";
import css from "./Strategies.module.css";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const tint = (r: number, full: number) => {
  const a = Math.min(0.6, (Math.abs(r) / full) * 0.6);
  return r >= 0 ? `rgba(63,178,127,${a})` : `rgba(229,103,92,${a})`;
};

export default function MonthlyHeatmap({ monthly, yearly }: Pick<SleeveTearSheet, "monthly" | "yearly">) {
  const years = Array.from(new Set([...monthly.map(m => m.year), ...yearly.map(y => y.year)])).sort((a, b) => b - a);
  if (!years.length) return <p className={css["muted"]}>No monthly returns yet.</p>;
  const cell = (key: string, r: number | undefined, full: number) => r == null || !Number.isFinite(r)
    ? <td key={key}/> : <td key={key} style={{ background: tint(r, full) }}>{pct(r * 100, { digits: 1 })}</td>;
  return <div className={css["scroll"]}><table className={`${css["dtbl"]} ${css["heat"]}`}>
    <caption className={css["srOnly"]}>Monthly returns by year</caption>
    <thead><tr><th>Year</th>{MONTHS.map(m => <th key={m}>{m}</th>)}<th>Year</th></tr></thead>
    <tbody>{years.map(y => <tr key={y}><td>{y}</td>
      {MONTHS.map((m, i) => cell(m, monthly.find(q => q.year === y && q.month === i + 1)?.ret, 0.10))}
      {cell("year", yearly.find(q => q.year === y)?.ret, 0.30)}</tr>)}</tbody>
  </table></div>;
}
