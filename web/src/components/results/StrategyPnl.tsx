"use client";

import Section from "@/components/ui/Section";
import { ColorType, createChart, LineSeries, LineStyle, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Api } from "@/lib/api";
import { money, signTone } from "@/lib/format";
import type { PnlPayload, PnlRow } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Segmented from "@/components/ui/Segmented";
import { sleeveColor } from "@/components/strategies/sleeveColors";
import { shadowColor } from "@/components/strategies/shadowColors";
import css from "./ResultsTables.module.css";

type Range = "1M" | "3M" | "All";
const INK = "#e6e8eb", INK2 = "#9aa3ad";
const SLEEVES: [string, string][] = [["swing", "Swing"], ["momentum", "Momentum"], ["putwrite", "Put-write"], ["vaa", "Defensive (VAA)"]];
const PERIODS: [string, string][] = [["today", "Today"], ["week", "This week"], ["month", "This month"], ["since_start", "Since start"]];

function cutoff(range: Range, last: string): string {
  if (range === "All") return "";
  const d = new Date(`${last}T00:00:00Z`);
  d.setUTCMonth(d.getUTCMonth() - (range === "1M" ? 1 : 3));
  return d.toISOString().slice(0, 10);
}
const tone = (v: unknown) => { const t = signTone(v); return t === "positive" ? css["gain"] : t === "negative" ? css["loss"] : ""; };

export default function StrategyPnl({ api }: { api: Api }) {
  const [data, setData] = useState<PnlPayload | null>(null);
  const [failed, setFailed] = useState(false);
  const [range, setRange] = useState<Range>("All");
  const [shadows, setShadows] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    const load = () => api<PnlPayload>("/api/results/pnl?days=365")
      .then(d => { if (live) { setData(d); setFailed(false); } })
      .catch(() => { if (live) setFailed(true); });
    load();
    const t = window.setInterval(load, 300_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  const shadowIds = useMemo(() => {
    const ids = new Set<string>();
    const src = data?.shadows?.cumulative ?? data?.shadows?.days ?? [];
    for (const r of src) for (const k of Object.keys(r)) if (k !== "day") ids.add(k);
    return [...ids];
  }, [data]);

  const defs = useMemo(() => [
    ...SLEEVES.map(([k, n]) => ({ key: k, name: n, color: sleeveColor(k), w: 2, dashed: false })),
    { key: "total", name: "Total", color: INK, w: 2, dashed: false },
    ...(shadows ? shadowIds.map((k, i) => ({ key: k, name: `${data?.shadows?.names?.[k] ?? k} (virtual)`, color: shadowColor(k, i), w: 1, dashed: true })) : []),
  ], [shadows, shadowIds, data]);

  const rows = useMemo(() => {
    if (!data) return [];
    const sh = new Map<string, PnlRow>();
    const shCum = data.shadows?.cumulative ?? [];
    if (shCum.length === 0) {
      const run: Record<string, number> = {};
      for (const r of data.shadows?.days ?? []) {
        const o: PnlRow = { day: r.day };
        for (const k of Object.keys(r)) {
          if (k === "day") continue;
          const v = r[k];
          if (typeof v === "number") run[k] = (run[k] ?? 0) + v;
          o[k] = k in run ? run[k] : null;
        }
        sh.set(r.day, o);
      }
    } else for (const r of shCum) sh.set(r.day, r);
    const all = (data.cumulative ?? []).map(r => ({ ...sh.get(r.day), ...r }) as PnlRow);
    const from = all.length ? cutoff(range, String(all[all.length - 1].day)) : "";
    return all.filter(r => String(r.day) >= from);
  }, [data, range]);

  const enough = rows.length >= 2;

  useEffect(() => {
    const el = host.current;
    if (!el || !enough) return;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#17191c" }, textColor: INK2, fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e33" } },
      rightPriceScale: { borderVisible: false }, leftPriceScale: { visible: false },
      timeScale: { borderVisible: false }, handleScroll: false, handleScale: false,
      localization: { priceFormatter: (v: number) => money(v) },
    });
    for (const d of defs) {
      const s = c.addSeries(LineSeries, {
        color: d.color, lineWidth: d.w as 1 | 2, lineStyle: d.dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData(rows.filter(r => typeof r[d.key] === "number").map(r => ({ time: String(r.day) as Time, value: r[d.key] as number })));
    }
    c.timeScale().fitContent();
    const byDay = new Map(rows.map(r => [String(r.day), r]));
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
      for (const d of defs) {
        const line = document.createElement("div");
        const a = document.createElement("span"), b = document.createElement("span");
        a.textContent = d.name;
        const v = row[d.key];
        b.textContent = typeof v === "number" ? money(v, { sign: true }) : "—";
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

  if (!data) return <p className={css["muted"]}>{failed ? "P&L by strategy is not available right now." : "Loading P&L by strategy…"}</p>;
  const periods = data.periods ?? {};
  return <Section title="P&L by strategy" meta="Dollars; from daily equity snapshots"><div>
    <div className={css["scroll"]}>
      <table className={css["tbl"]}>
        <thead><tr><th>Period</th>{SLEEVES.map(([k, n]) => <th key={k}>{n}</th>)}<th>Total</th></tr></thead>
        <tbody>{PERIODS.map(([p, label]) => <tr key={p}><td>{label}</td>
          {[...SLEEVES.map(s => s[0]), "total"].map(k => {
            const v = periods[p]?.[k];
            return <td key={k} className={tone(v)}>{v == null ? "—" : money(v, { sign: true })}</td>;
          })}</tr>)}</tbody>
      </table>
    </div>
    <div className={css["top"]}>
      <div className={css["legend"]}>
        {defs.map(d => <span key={d.key}><i className={`${css["line"]} ${d.dashed ? css["dash"] : ""}`} style={{ borderTopColor: d.color }}/>{d.name}</span>)}
      </div>
      {shadowIds.length > 0 && <label className={css["toggle"]}><input type="checkbox" checked={shadows}
        onChange={e => setShadows(e.target.checked)}/>Show shadows (virtual)</label>}
      <Segmented<Range> label="Chart range" value={range} onChange={setRange}
        options={(["1M", "3M", "All"] as Range[]).map(v => ({ value: v, label: v }))}/>
    </div>
    {enough
      ? <div className={css["chartBox"]}>
        <div ref={host} className={css["chart"]} role="img" aria-label="Cumulative profit and loss in dollars for each strategy and the total"/>
        <div ref={tip} className={css["ctip"]}/>
      </div>
      : <Empty>The P&L chart appears after two days of history; it fills in automatically.</Empty>}
  </div></Section>;
}
