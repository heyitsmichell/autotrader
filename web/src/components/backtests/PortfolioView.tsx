"use client";

import { ColorType, createChart, LineSeries, type Time } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Api } from "@/lib/api";
import { num, pct } from "@/lib/format";
import type { BtPortfolio } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import css from "./Bt.module.css";

const INK2 = "#9aa3ad";
const SERIES = [{ key: "equal" as const, name: "Equal 25%", color: "#338ced" }, { key: "invvol" as const, name: "Inverse volatility", color: "#e76624" }];
const P = (v: number | null | undefined, s = false) => (v == null ? "—" : pct(v * 100, { digits: 1, sign: s }));
const tint = (r: number | null | undefined, diag: boolean) => {
  if (diag || r == null || !Number.isFinite(r)) return undefined;
  const a = Math.min(0.5, Math.abs(r) * 0.5);
  return r >= 0 ? `rgba(123,169,230,${a})` : `rgba(224,179,90,${a})`;
};

function Curve({ curve }: { curve: NonNullable<BtPortfolio["curve"]> }) {
  const host = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const ok = curve.length >= 2;
  useEffect(() => {
    const el = host.current;
    if (!el || !ok) return;
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#17191c" }, textColor: INK2, fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e33" } },
      rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false },
      handleScroll: false, handleScale: false,
      localization: { priceFormatter: (v: number) => `${v.toFixed(0)}%` },
    });
    for (const d of SERIES) {
      const s = c.addSeries(LineSeries, { color: d.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      s.setData(curve.filter(p => p[d.key] != null).map(p => ({ time: p.day as Time, value: (p[d.key] as number) * 100 })));
    }
    c.timeScale().fitContent();
    const by = new Map(curve.map(p => [p.day, p]));
    c.subscribeCrosshairMove(param => {
      const t = tip.current;
      if (!t) return;
      const row = typeof param.time === "string" ? by.get(param.time) : undefined;
      if (!row || !param.point) { t.style.display = "none"; return; }
      t.replaceChildren();
      const h = document.createElement("div"); h.textContent = row.day; t.appendChild(h);
      for (const d of SERIES) {
        const line = document.createElement("div");
        const a = document.createElement("span"), b = document.createElement("span");
        a.textContent = d.name; b.textContent = row[d.key] == null ? "—" : pct((row[d.key] as number) * 100);
        line.append(a, b); t.appendChild(line);
      }
      t.style.display = "block";
      const w = el.clientWidth;
      t.style.left = `${param.point.x > w / 2 ? Math.max(0, param.point.x - t.offsetWidth - 12) : param.point.x + 12}px`;
      t.style.top = "8px";
    });
    return () => c.remove();
  }, [curve, ok]);
  if (!ok) return <Empty>The chart appears once the portfolio curve is available.</Empty>;
  return <div>
    <div className={css["chartTop"]}>{SERIES.map(d => <span key={d.key}><i className={css["line"]} style={{ borderTopColor: d.color }}/>{d.name}</span>)}</div>
    <div className={css["chartBox"]}>
      <div ref={host} className={css["chart"]} role="img" aria-label="Cumulative return of equal-weight versus inverse-volatility portfolios"/>
      <div ref={tip} className={css["ctip"]}/>
    </div>
  </div>;
}

export default function PortfolioView({ api }: { api: Api }) {
  const [d, setD] = useState<BtPortfolio | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<BtPortfolio>("/api/backtests/portfolio").then(r => { if (live) setD(r ?? {}); })
      .catch(() => { if (live) setErr("Portfolio backtest is not available yet."); });
    return () => { live = false; };
  }, [api]);
  if (err) return <Empty>{err}</Empty>;
  if (!d) return <Empty>Loading portfolio backtest.</Empty>;
  const ids = d.correlation?.ids ?? [], m = d.correlation?.matrix ?? [];
  const l = d.live;
  return <div className={css["wrap"]}>
    <Section title="Weighting schemes" meta={d.window ?? undefined}>
      {d.schemes?.length ? <div className={css["scroll"]}><table>
        <thead><tr><th scope="col">Scheme</th><th scope="col" className="num">CAGR</th><th scope="col" className="num">Vol</th><th scope="col" className="num">Sharpe</th><th scope="col" className="num">Max DD</th></tr></thead>
        <tbody>{d.schemes.map(s => <tr key={s.name}><th scope="row">{s.name}</th>
          <td className="num">{P(s.cagr, true)}</td><td className="num">{P(s.vol)}</td><td className="num">{num(s.sharpe)}</td><td className="num">{P(s.max_dd)}</td></tr>)}</tbody>
      </table></div> : <Empty>No scheme results yet.</Empty>}
    </Section>
    <Section title="Cumulative return"><Curve curve={d.curve ?? []}/></Section>
    <Section title="Correlation" meta="Daily returns, common window">
      {ids.length && m.length ? <div className={css["scroll"]}><table>
        <thead><tr><th scope="col"><span className="sr-only">Strategy</span></th>{ids.map(i => <th key={i} scope="col" className="num">{i}</th>)}</tr></thead>
        <tbody>{ids.map((r, i) => <tr key={r}><th scope="row">{r}</th>
          {ids.map((c, j) => { const v = m[i]?.[j]; return <td key={c} className="num" style={{ background: tint(v, i === j) }}>
            {i === j ? <span className="muted">{num(v)}</span> : num(v)}</td>; })}</tr>)}</tbody>
      </table></div> : <Empty>No correlation matrix yet.</Empty>}
    </Section>
    <Section title="Live combined">
      {l ? <FigureRow><Figure label="Days" value={l.days ?? "—"}/><Figure label="Return" value={l.return_pct == null ? "—" : pct(l.return_pct)}/>
        <Figure label="Sharpe" value={(l.days ?? 0) < 20 ? "needs 20 days" : num(l.sharpe)}/></FigureRow>
        : <Empty>No live combined history yet.</Empty>}
    </Section>
  </div>;
}
