"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { money, signed } from "@/lib/format";
import type { RoundTrip } from "@/lib/types";
import css from "./results.module.css";
import tcss from "./ResultsTables.module.css";

export type Metric = "ret" | "pnl" | "r";

const value = (t: RoundTrip, m: Metric) => (m === "ret" ? t.ret_pct : m === "pnl" ? t.pnl : t.r);
export const fmtMetric = (v: number, m: Metric, digits = 1) =>
  m === "ret" ? `${signed(v, digits)}%` : m === "pnl" ? money(v, { sign: true }) : `${signed(v, digits)} R`;
const axisLabel = (v: number, m: Metric) => {
  const r = Math.round(v * 100) / 100;
  if (m === "pnl") return Math.abs(r) >= 1000 ? `${r < 0 ? "−" : ""}$${num1(Math.abs(r) / 1000)}k` : `${r < 0 ? "−" : ""}$${Math.abs(r)}`;
  const s = `${r < 0 ? "−" : ""}${Math.abs(r)}`;
  return m === "ret" ? `${s}%` : `${s}R`;
};
const num1 = (v: number) => (Math.round(v * 10) / 10).toString();

/** A step from the 1-2-2.5-5 ladder giving about `target` bins over `span`. */
function niceStep(span: number, target: number): number {
  const raw = span / target;
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const k of [1, 2, 2.5, 5, 10]) if (k * p >= raw - 1e-12) return k * p;
  return 10 * p;
}

function quantile(sorted: number[], q: number): number {
  const i = (sorted.length - 1) * q, a = Math.floor(i), b = Math.ceil(i);
  return sorted[a] + (sorted[b] - sorted[a]) * (i - a);
}

const H = 228, L = 34, R = 10, T = 30, B = 24, IH = H - T - B;

/** Histogram of closed-trade results. Bin edges sit on multiples of a nice step, so zero is always an
 * edge: bars left of it are losses, right of it gains. Mean and median are marked; hover a bar for its trades. */
export default function ReturnHistogram({ trips, metric }: { trips: RoundTrip[]; metric: Metric }) {
  const [hover, setHover] = useState<number | null>(null);
  // draw at the real pixel width, so text stays 11px on a phone instead of shrinking with a viewBox
  const [W, setW] = useState(640);
  const ro = useRef<ResizeObserver | null>(null);
  const box = useCallback((el: HTMLDivElement | null) => {
    ro.current?.disconnect();
    ro.current = null;
    if (!el) return;
    ro.current = new ResizeObserver(([e]) => setW(Math.max(300, Math.min(760, Math.round(e.contentRect.width)))));
    ro.current.observe(el);
  }, []);
  const IW = W - L - R;

  const h = useMemo(() => {
    const pts = trips.flatMap(t => { const v = value(t, metric); return v == null ? [] : [{ v, t }]; });
    if (pts.length === 0) return null;
    const vals = pts.map(p => p.v).sort((a, b) => a - b);
    const lo0 = Math.min(vals[0], 0), hi0 = Math.max(vals[vals.length - 1], 0);
    const target = Math.min(16, Math.max(6, Math.round(Math.sqrt(pts.length) * 2)));
    const step = niceStep(hi0 - lo0 || 1, target);
    const lo = Math.floor(lo0 / step + 1e-9) * step;
    let hi = Math.ceil(hi0 / step - 1e-9) * step;
    if (hi <= lo) hi = lo + step;
    const nb = Math.max(1, Math.round((hi - lo) / step));
    const bins = Array.from({ length: nb }, (_, i) => ({ a: lo + i * step, b: lo + (i + 1) * step, items: [] as { v: number; t: RoundTrip }[] }));
    for (const p of pts) bins[Math.min(nb - 1, Math.max(0, Math.floor((p.v - lo) / step + 1e-9)))].items.push(p);
    const mean = vals.reduce((s, v) => s + v, 0) / vals.length;
    return { bins, lo, hi, step, vals, mean, median: quantile(vals, 0.5), q1: quantile(vals, 0.25), q3: quantile(vals, 0.75) };
  }, [trips, metric]);

  if (!h) return <p className={css["note"]}>No trades with this measure yet.</p>;
  const { bins, lo, hi, vals, mean, median } = h;
  const maxC = Math.max(...bins.map(b => b.items.length));
  const yStep = maxC <= 5 ? 1 : maxC <= 10 ? 2 : Math.ceil(maxC / 5);
  const yMax = Math.ceil(maxC / yStep) * yStep;
  const x = (v: number) => L + ((v - lo) / (hi - lo)) * IW;
  const y = (c: number) => T + IH - (c / yMax) * IH;
  const labelEvery = Math.ceil(bins.length / Math.max(3, Math.floor(IW / 70)));
  const won = vals.filter(v => v > 0).length;

  // marker labels: centred on their line; when mean and median sit close, the left one reads leftwards
  // from its line and the right one rightwards, so the two never overlap
  const xm = x(median), xa = x(mean), close = Math.abs(xm - xa) < 150;
  type Anchor = "start" | "middle" | "end";
  const anchor = (px: number): Anchor => (px < L + 70 ? "start" : px > W - R - 70 ? "end" : "middle");
  const markers = [{ px: xm, label: `median ${fmtMetric(median, metric)}` }, { px: xa, label: `mean ${fmtMetric(mean, metric)}` }]
    .sort((p, q) => p.px - q.px)
    .map((mk, i) => {
      const side: Anchor = close ? (i === 0 ? "end" : "start") : anchor(mk.px);
      const edge: Anchor = side === "end" && mk.px < L + 90 ? "start" : side === "start" && mk.px > W - R - 90 ? "end" : side;
      return { ...mk, anchor: edge, tx: mk.px + (edge === "end" ? -4 : edge === "start" ? 4 : 0), row: close && edge !== side ? 1 : 0 };
    });

  const bar = (i: number) => {
    const bn = bins[i], c = bn.items.length;
    if (!c) return null;
    const x0 = x(bn.a) + 1, w = Math.max(1, x(bn.b) - x(bn.a) - 2), top = y(c), base = T + IH;
    const r = Math.min(4, w / 2, base - top);
    const d = `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + w - r}Q${x0 + w},${top} ${x0 + w},${top + r}V${base}Z`;
    const fill = bn.a >= 0 ? "var(--gain)" : "var(--loss)";
    return <path key={i} d={d} fill={fill} opacity={hover == null || hover === i ? 0.9 : 0.45}/>;
  };

  const tip = hover != null ? bins[hover] : null;
  const tipLeft = tip ? ((x(tip.a) + x(tip.b)) / 2 / W) * 100 : 0;

  return <figure className={css["hist"]}>
    <div ref={box} className={css["histBox"]} onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`Histogram of ${vals.length} closed trades by ${metric === "ret" ? "return in percent" : metric === "pnl" ? "profit and loss in dollars" : "R multiple"}`}>
        {Array.from({ length: yMax / yStep + 1 }, (_, k) => k * yStep).map(c => <g key={c}>
          {c > 0 && <line x1={L} x2={W - R} y1={y(c)} y2={y(c)} stroke="var(--rule)" strokeWidth="1" opacity="0.6"/>}
          <text x={L - 6} y={y(c) + 4} fontSize="11" fill="var(--ink-2)" textAnchor="end" className="mono">{c}</text>
        </g>)}
        {bins.map((_, i) => bar(i))}
        <line x1={L} x2={W - R} y1={T + IH} y2={T + IH} stroke="var(--rule)" strokeWidth="1"/>
        <line x1={x(0)} x2={x(0)} y1={T - 4} y2={T + IH} stroke="var(--ink)" strokeWidth="1"/>
        {markers.map(mk => <g key={mk.label}>
          <line x1={mk.px} x2={mk.px} y1={T - (mk.row ? 2 : 14)} y2={T + IH} stroke="var(--ink-2)" strokeWidth="1" strokeDasharray="3 3"/>
          <text x={mk.tx} y={mk.row ? T - 4 : T - 17} fontSize="11" fill="var(--ink-2)" textAnchor={mk.anchor} className="mono">{mk.label}</text>
        </g>)}
        {bins.map((bn, i) => i % labelEvery === 0 &&
          <text key={i} x={x(bn.a)} y={H - 6} fontSize="11" fill="var(--ink-2)" textAnchor="middle" className="mono">{axisLabel(bn.a, metric)}</text>)}
        {bins.length % labelEvery === 0 &&
          <text x={x(hi)} y={H - 6} fontSize="11" fill="var(--ink-2)" textAnchor="end" className="mono">{axisLabel(hi, metric)}</text>}
        {bins.map((bn, i) => <rect key={`hit${i}`} x={x(bn.a)} y={T} width={x(bn.b) - x(bn.a)} height={IH} fill="transparent"
          tabIndex={bn.items.length ? 0 : -1} onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}
          aria-label={`${axisLabel(bn.a, metric)} to ${axisLabel(bn.b, metric)}: ${bn.items.length} trades`}/>)}
      </svg>
      {tip && tip.items.length > 0 && <div className={css["htip"]}
        style={{ left: `${tipLeft}%`, transform: `translateX(${tipLeft > 60 ? "-100%" : tipLeft < 40 ? "0" : "-50%"})` }}>
        <div className={css["htipHead"]}>{axisLabel(tip.a, metric)} to {axisLabel(tip.b, metric)} · {tip.items.length} trade{tip.items.length === 1 ? "" : "s"}</div>
        {[...tip.items].sort((p, q) => q.v - p.v).slice(0, 8).map((p, k) =>
          <div key={k}><span>{p.t.symbol}</span><span>{fmtMetric(p.v, metric)}</span></div>)}
        {tip.items.length > 8 && <div className={css["muted"]}>and {tip.items.length - 8} more</div>}
      </div>}
    </div>
    <figcaption className={css["note"]}>
      {won} of {vals.length} trades won. Middle half of trades: {fmtMetric(h.q1, metric)} to {fmtMetric(h.q3, metric)}.
      Range {fmtMetric(vals[0], metric)} to {fmtMetric(vals[vals.length - 1], metric)}.
    </figcaption>
    <details className={css["asTable"]}>
      <summary>Show as table</summary>
      <table className={tcss["tbl"]}>
        <thead><tr><th className={tcss["l"]}>Range</th><th>Trades</th><th>P&L</th></tr></thead>
        <tbody>{bins.filter(b => b.items.length).map(b => {
          const p = b.items.reduce((s, it) => s + (it.t.pnl ?? 0), 0);
          return <tr key={b.a}><td className={tcss["l"]}>{axisLabel(b.a, metric)} to {axisLabel(b.b, metric)}</td>
            <td>{b.items.length}</td><td className={p > 0 ? css["gain"] : p < 0 ? css["loss"] : ""}>{money(p, { sign: true })}</td></tr>;
        })}</tbody>
      </table>
    </details>
  </figure>;
}
