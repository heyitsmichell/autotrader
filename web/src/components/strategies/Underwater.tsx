"use client";
import { pct } from "@/lib/format";
import Plot from "./Plot";

export default function Underwater({ data }: { data: { day: string; dd: number }[] }) {
  if (data.length < 2) return <p style={{ color: "var(--ink-2)" }}>Not enough history for a drawdown chart.</p>;
  const lo = Math.min(...data.map(d => d.dd), -0.01);
  return <Plot days={data.map(d => d.day)} yMin={lo * 1.05} yMax={0} height={160}
    label="Drawdown from the previous peak over time"
    yFmt={v => pct(v * 100, { sign: false, digits: 0 })}
    tip={i => <><div><span>{data[i].day}</span></div><div><span>Drawdown</span><span>{pct(data[i].dd * 100)}</span></div></>}>
    {({ x, y }) => <>
      <path d={`M${x(0)},${y(0)}L${data.map((d, i) => `${x(i)},${y(d.dd)}`).join("L")}L${x(data.length - 1)},${y(0)}Z`}
        fill="#e5675c" fillOpacity={0.25}/>
      <polyline points={data.map((d, i) => `${x(i)},${y(d.dd)}`).join(" ")} fill="none" stroke="#e5675c" strokeWidth={1}/>
    </>}
  </Plot>;
}
