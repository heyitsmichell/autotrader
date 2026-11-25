"use client";

import { ColorType, createChart, LineSeries, LineStyle, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";
import { pct } from "@/lib/format";
import type { Shadow, ShadowEquityPoint, SleeveEquityPoint } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import { shadowColor } from "./shadowColors";
import css from "./Strategies.module.css";

const INK2 = "#9aa3ad";
interface Def { key: string; name: string; color: string; ref: boolean }

export default function ShadowChart({ shadows, points, live }: {
  shadows: Shadow[]; points: ShadowEquityPoint[]; live: SleeveEquityPoint[];
}) {
  const host = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);

  const defs = useMemo<Def[]>(() => [
    ...shadows.map((s, i) => ({ key: s.id, name: s.name, color: shadowColor(s.id, i), ref: false })),
    { key: "__live", name: "Live: Momentum", color: "#e76624", ref: true },
  ], [shadows]);

  const rows = useMemo(() => {
    const liveBy = new Map(live.map(p => [p.day, p.momentum]));
    const base: Record<string, number> = {};
    return points.map(p => {
      const v: Record<string, number | null> = {};
      for (const d of defs) {
        const raw = d.ref ? liveBy.get(p.day) : p[d.key];
        if (typeof raw !== "number" || !Number.isFinite(raw)) { v[d.key] = null; continue; }
        if (base[d.key] == null) base[d.key] = raw;
        v[d.key] = base[d.key] ? (raw / base[d.key] - 1) * 100 : null;
      }
      return { day: p.day, v };
    });
  }, [points, live, defs]);

  const enough = rows.filter(r => defs.some(d => !d.ref && r.v[d.key] != null)).length >= 2;

  useEffect(() => {
    const el = host.current;
    if (!el || !enough) return;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#17191c" }, textColor: INK2, fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e33" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      handleScroll: false, handleScale: false,
      localization: { priceFormatter: (v: number) => `${v.toFixed(1)}%` },
    });
    for (const d of defs) {
      const s = c.addSeries(LineSeries, {
        color: d.color, lineWidth: d.ref ? 1 : 2,
        lineStyle: d.ref ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData(rows.filter(r => r.v[d.key] != null).map(r => ({ time: r.day as Time, value: r.v[d.key] as number })));
    }
    c.timeScale().fitContent();
    const byDay = new Map(rows.map(r => [r.day, r]));
    c.subscribeCrosshairMove(param => {
      const t = tip.current;
      if (!t) return;
      const row = byDay.get(typeof param.time === "string" ? param.time : "");
      if (!row || !param.point) { t.style.display = "none"; return; }
      t.replaceChildren();
      const head = document.createElement("div");
      head.textContent = row.day;
      t.appendChild(head);
      for (const d of defs) {
        const line = document.createElement("div");
        const a = document.createElement("span"), b = document.createElement("span");
        a.textContent = d.name;
        b.textContent = row.v[d.key] == null ? "—" : pct(row.v[d.key]);
        line.append(a, b);
        t.appendChild(line);
      }
      t.style.display = "block";
      const w = el.clientWidth;
      t.style.left = `${param.point.x > w / 2 ? Math.max(0, param.point.x - t.offsetWidth - 12) : param.point.x + 12}px`;
      t.style.top = "8px";
    });
    return () => c.remove();
  }, [rows, defs, enough]);

  return <div>
    <div className={css["chartTop"]}>
      <div className={css["legend"]}>
        {defs.map(d => <span key={d.key}>
          <i className={`${css["line"]} ${d.ref ? css["dash"] : ""}`} style={{ borderTopColor: d.color }}/>{d.name}
        </span>)}
      </div>
    </div>
    {enough
      ? <div className={css["chartBox"]}>
        <div ref={host} className={css["shadowChart"]} role="img"
          aria-label="Percent return since the start for each shadow strategy and the live Momentum strategy"/>
        <div ref={tip} className={css["ctip"]}/>
      </div>
      : <Empty>The chart appears after two days of shadow history; it fills in automatically.</Empty>}
  </div>;
}
