"use client";
import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { pct } from "@/lib/format";
import type { Sleeve, SleeveBand, SleeveBandPoint } from "@/lib/types";
import Section from "@/components/ui/Section";
import Plot from "./Plot";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

const p = (v: number | null | undefined) => (v == null ? "—" : pct(v * 100));

function area(pts: SleeveBandPoint[], a: (q: SleeveBandPoint) => number, b: (q: SleeveBandPoint) => number,
  X: (i: number) => number, Y: (v: number) => number): string {
  const top = pts.map((q, i) => `${X(i)},${Y(a(q))}`);
  const bot = pts.map((q, i) => `${X(i)},${Y(b(q))}`).reverse();
  return `M${top.join("L")}L${bot.join("L")}Z`;
}

export default function BandChart({ api, sleeve }: { api: Api; sleeve: Sleeve }) {
  const [band, setBand] = useState<SleeveBand | null>(null);
  const [msg, setMsg] = useState("Loading…");
  const id = sleeve.id;
  useEffect(() => {
    let live = true;
    api<SleeveBand>(`/api/sleeves/${id}/band`)
      .then(d => {
        if (!live) return;
        if (d && Array.isArray(d.points) && d.points.length) setBand(d);
        else setMsg("No live history to compare yet.");
      })
      .catch(() => { if (live) setMsg("Live vs backtest is not available right now."); });
    return () => { live = false; };
  }, [api, id]);
  const color = sleeveColor(sleeve.id, sleeve.color_slot);
  const pts = band?.points ?? [];
  let status = "";
  let tone = "";
  if (band) {
    if (band.status === "inside") status = "Inside expected range";
    else if (band.status === "below") { status = `Below range for ${band.below_days} days`; tone = css["warnText"] ?? ""; }
    else if (band.status === "above") status = "Above range";
    else { status = "Too early (needs 5 days)"; tone = css["muted"] ?? ""; }
  }
  const vals = pts.flatMap(q => [q.p5, q.p95, q.live ?? q.p50, 0]);
  const lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 0;
  const pad = (hi - lo) * 0.05 || 0.01;
  return <Section title="Live vs backtest" meta={status ? <span className={tone}>{status}</span> : undefined}>
    {!band || pts.length < 2
      ? <p className={css["muted"]}>{band ? "The chart appears after two days of history." : msg}</p>
      : <>
        <Plot days={pts.map(q => q.day)} yMin={lo - pad} yMax={hi + pad} height={260}
          label={`Live return of ${sleeve.name} against the expected range from the backtest`}
          yFmt={v => pct(v * 100, { sign: false, digits: 0 })}
          tip={i => <>
            <div><span>{pts[i].day}</span></div>
            <div><span>Live</span><span>{p(pts[i].live)}</span></div>
            <div><span>High (p95)</span><span>{p(pts[i].p95)}</span></div>
            <div><span>Median (p50)</span><span>{p(pts[i].p50)}</span></div>
            <div><span>Low (p5)</span><span>{p(pts[i].p5)}</span></div></>}>
          {({ x, y }) => <>
            <path d={area(pts, q => q.p95, q => q.p5, x, y)} fill={color} fillOpacity={0.12}/>
            <path d={area(pts, q => q.p75, q => q.p25, x, y)} fill={color} fillOpacity={0.25}/>
            <line x1={x(0)} x2={x(pts.length - 1)} y1={y(0)} y2={y(0)} stroke="#9aa3ad" strokeWidth={1} strokeOpacity={0.5}/>
            <polyline points={pts.map((q, i) => `${x(i)},${y(q.p50)}`).join(" ")} fill="none" stroke="#9aa3ad" strokeWidth={1} strokeDasharray="4 3"/>
            <polyline points={pts.flatMap((q, i) => (q.live == null ? [] : [`${x(i)},${y(q.live)}`])).join(" ")} fill="none" stroke={color} strokeWidth={2}/>
          </>}
        </Plot>
        <p className={css["muted"]}>{`Band = 5-95% of 2,000 simulated paths from the ${band.window || "backtest"}`}</p>
      </>}
  </Section>;
}
