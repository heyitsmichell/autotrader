"use client";
import Plot from "./Plot";

export default function RollingSharpe({ data, color }: { data: { day: string; value: number | null }[]; color: string }) {
  const d = data.filter(r => r.value != null && Number.isFinite(r.value)) as { day: string; value: number }[];
  if (d.length < 2) return <p className="muted">Not enough history for a rolling Sharpe line.</p>;
  const vs = d.map(r => r.value);
  const lo = Math.min(...vs, 0), hi = Math.max(...vs, 1);
  const pad = (hi - lo) * 0.05;
  return <Plot days={d.map(r => r.day)} yMin={lo - pad} yMax={hi + pad} height={160}
    label="Rolling Sharpe ratio over time, with reference lines at 0 and 1"
    yFmt={v => v.toFixed(1)}
    refs={[{ v: 0, label: "0" }, { v: 1, label: "1.0" }]}
    tip={i => <><div><span>{d[i].day}</span></div><div><span>Sharpe</span><span>{d[i].value.toFixed(2)}</span></div></>}>
    {({ x, y }) => <polyline points={d.map((r, i) => `${x(i)},${y(r.value)}`).join(" ")} fill="none" stroke={color} strokeWidth={2}/>}
  </Plot>;
}
