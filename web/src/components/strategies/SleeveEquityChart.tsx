"use client";

import { ColorType, createChart, createSeriesMarkers, LineSeries, LineStyle, type SeriesMarker, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { pct } from "@/lib/format";
import type { Sleeve, SleeveEquityPoint, SleeveEvent } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Segmented from "@/components/ui/Segmented";
import { eventLabel, eventStyle } from "./events";
import { sleeveColor, sortSleeves } from "./sleeveColors";
import css from "./Strategies.module.css";

type Range = "1M" | "3M" | "YTD" | "All";
type Key = "swing" | "momentum" | "putwrite" | "vaa" | "total" | "spy";
const INK = "#e6e8eb", INK2 = "#9aa3ad";

function cutoff(range: Range, last: string): string {
  const d = new Date(`${last}T00:00:00Z`);
  if (range === "1M") d.setUTCMonth(d.getUTCMonth() - 1);
  else if (range === "3M") d.setUTCMonth(d.getUTCMonth() - 3);
  else if (range === "YTD") return `${d.getUTCFullYear()}-01-01`;
  else return "";
  return d.toISOString().slice(0, 10);
}

export default function SleeveEquityChart({ points, sleeves, benchmark, events = [] }: {
  points: SleeveEquityPoint[]; sleeves: Sleeve[]; benchmark: string; events?: SleeveEvent[];
}) {
  const host = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [range, setRange] = useState<Range>("All");
  const [showTable, setShowTable] = useState(false);
  const [showEvents, setShowEvents] = useState(true);

  const defs = useMemo(() => [
    ...sortSleeves(sleeves).map(s => ({ key: s.id as Key, name: s.name, color: sleeveColor(s.id, s.color_slot), kind: "sleeve" as const })),
    { key: "total" as Key, name: "Total", color: INK, kind: "total" as const },
    { key: "spy" as Key, name: benchmark, color: INK2, kind: "spy" as const },
  ], [sleeves, benchmark]);

  const rows = useMemo(() => {
    const from = points.length ? cutoff(range, points[points.length - 1].day) : "";
    const sel = points.filter(p => p.day >= from);
    const base: Partial<Record<Key, number>> = {};
    return sel.map(p => {
      const out: { day: string; v: Partial<Record<Key, number | null>> } = { day: p.day, v: {} };
      for (const d of defs) {
        const raw = (p as unknown as Record<string, number | null | undefined>)[d.key];
        if (raw == null || !Number.isFinite(raw)) { out.v[d.key] = null; continue; }
        if (base[d.key] == null) base[d.key] = raw;
        const b = base[d.key] as number;
        out.v[d.key] = b ? (raw / b - 1) * 100 : null;
      }
      return out;
    });
  }, [points, range, defs]);

  const enough = rows.length >= 2;

  const visEvents = useMemo(() => {
    if (!showEvents || rows.length < 2) return [];
    const first = rows[0].day, last = rows[rows.length - 1].day;
    return events.filter(e => e.day >= first && e.day <= last);
  }, [events, rows, showEvents]);

  useEffect(() => {
    const el = host.current;
    if (!el || !enough) return;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#17191c" }, textColor: INK2, fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e33" } },
      rightPriceScale: { borderVisible: false },
      leftPriceScale: { visible: false },
      timeScale: { borderVisible: false },
      handleScroll: false, handleScale: false,
      localization: { priceFormatter: (v: number) => `${v.toFixed(1)}%` },
    });
    const made = defs.map(d => {
      const s = c.addSeries(LineSeries, {
        color: d.color, lineWidth: d.kind === "spy" ? 1 : 2,
        lineStyle: d.kind === "spy" ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData(rows.filter(r => r.v[d.key] != null).map(r => ({ time: r.day as Time, value: r.v[d.key] as number })));
      return { d, s };
    });
    c.timeScale().fitContent();
    const days = rows.map(r => r.day);
    const evByDay = new Map<string, SleeveEvent[]>();
    for (const e of visEvents) evByDay.set(e.day, [...(evByDay.get(e.day) ?? []), e]);
    for (const { d, s } of made) {
      const own = new Set(rows.filter(r => r.v[d.key] != null).map(r => r.day));
      const ms: SeriesMarker<Time>[] = [];
      for (const e of visEvents) {
        if (d.kind === "total" ? e.sleeve != null && e.sleeve !== "" : e.sleeve !== d.key) continue;
        let day = e.day;
        if (!own.has(day)) { const prev = days.filter(x => x <= day && own.has(x)); day = prev[prev.length - 1] ?? ""; }
        if (!day) continue;
        const st = eventStyle(e.kind);
        ms.push({ time: day as Time, position: "aboveBar", shape: st.shape, color: st.color, text: eventLabel(e) });
      }
      if (ms.length) createSeriesMarkers(s, ms.sort((a, b) => String(a.time).localeCompare(String(b.time))));
    }
    const byDay = new Map(rows.map(r => [r.day, r]));
    c.subscribeCrosshairMove(param => {
      const t = tip.current;
      if (!t) return;
      const day = typeof param.time === "string" ? param.time : "";
      const row = byDay.get(day);
      if (!row || !param.point) { t.style.display = "none"; return; }
      t.replaceChildren();
      const head = document.createElement("div");
      head.textContent = day;
      t.appendChild(head);
      for (const { d } of made) {
        const line = document.createElement("div");
        const a = document.createElement("span"), b = document.createElement("span");
        a.textContent = d.name;
        const v = row.v[d.key];
        b.textContent = v == null ? "—" : pct(v);
        line.append(a, b);
        t.appendChild(line);
      }
      for (const e of evByDay.get(day) ?? []) {
        const line = document.createElement("div");
        const a = document.createElement("span"), b = document.createElement("span");
        a.textContent = e.kind.replace(/_/g, " ");
        b.textContent = e.text;
        line.append(a, b);
        t.appendChild(line);
      }
      t.style.display = "block";
      const w = el.clientWidth;
      t.style.left = `${param.point.x > w / 2 ? Math.max(0, param.point.x - t.offsetWidth - 12) : param.point.x + 12}px`;
      t.style.top = "8px";
    });
    return () => c.remove();
  }, [rows, defs, enough, visEvents]);

  const last30 = rows.slice(-30);
  return <div>
    <div className={css["chartTop"]}>
      <div className={css["legend"]}>
        {defs.map(d => <span key={d.key}>
          <i className={`${css["line"]} ${d.kind === "spy" ? css["dash"] : ""}`} style={{ borderTopColor: d.color }}/>{d.name}
        </span>)}
      </div>
      <label className={css["toggle"]}><input type="checkbox" checked={showEvents}
        onChange={e => setShowEvents(e.target.checked)}/>Show events</label>
      <Segmented<Range> label="Chart range" value={range} onChange={setRange}
        options={(["1M", "3M", "YTD", "All"] as Range[]).map(v => ({ value: v, label: v }))}/>
    </div>
    {enough
      ? <div className={css["chartBox"]}>
        <div ref={host} className={css["chart"]} role="img"
          aria-label={`Percent return since the start of the range for each strategy, the total and ${benchmark}`}/>
        <div ref={tip} className={css["ctip"]}/>
      </div>
      : <Empty>The chart appears after two days of history; it fills in automatically.</Empty>}
    {enough && <button type="button" className={css["linkBtn"]} onClick={() => setShowTable(v => !v)}>
      {showTable ? "Hide table" : "Show table"}</button>}
    {enough && showTable && <div className={css["scroll"]}>
      <table className={css["dtbl"]}>
        <thead><tr><th>Day</th>{defs.map(d => <th key={d.key}>{d.name}</th>)}</tr></thead>
        <tbody>{last30.map(r => <tr key={r.day}><td>{r.day}</td>
          {defs.map(d => <td key={d.key}>{r.v[d.key] == null ? "—" : pct(r.v[d.key])}</td>)}</tr>)}</tbody>
      </table>
    </div>}
  </div>;
}
