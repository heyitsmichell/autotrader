"use client";
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import css from "./Strategies.module.css";

export interface Scale { x: (i: number) => number; y: (v: number) => number; w: number; h: number; }
const ML = 46, MR = 8, MT = 8, MB = 20;

/** Responsive SVG plot frame: y ticks, x end labels, hover cursor and tooltip. */
export default function Plot({ days, yMin, yMax, height = 220, label, yFmt, tip, refs, children }: {
  days: string[]; yMin: number; yMax: number; height?: number; label: string;
  yFmt: (v: number) => string;
  tip: (i: number) => ReactNode;
  refs?: { v: number; label: string }[];
  children: (s: Scale) => ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const n = days.length;
  const span = yMax - yMin || 1;
  const pw = Math.max(10, w - ML - MR), ph = height - MT - MB;
  const x = (i: number) => ML + (n > 1 ? (i / (n - 1)) * pw : 0);
  const y = (v: number) => MT + (1 - (v - yMin) / span) * ph;
  const ticks = [0, 1, 2, 3, 4].map(k => yMin + (span * k) / 4);
  const move = (e: MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const f = (e.clientX - r.left - ML) / pw;
    setHover(Math.min(n - 1, Math.max(0, Math.round(f * (n - 1)))));
  };
  const hi = hover != null && hover < n ? hover : null;
  const hx = hi == null ? 0 : x(hi);
  return <div ref={box} className={css["plot"]}>
    {w > 0 && <svg width={w} height={height} role="img" aria-label={label}
      onMouseMove={move} onMouseLeave={() => setHover(null)}>
      {ticks.map((t, k) => <g key={k}>
        <line x1={ML} x2={w - MR} y1={y(t)} y2={y(t)} stroke="#2a2e33" strokeWidth={1}/>
        <text x={ML - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill="#9aa3ad">{yFmt(t)}</text>
      </g>)}
      {children({ x, y, w, h: height })}
      {(refs ?? []).map(r => <g key={r.label}>
        <line x1={ML} x2={w - MR} y1={y(r.v)} y2={y(r.v)} stroke="#9aa3ad" strokeWidth={1} strokeDasharray="4 3"/>
        <text x={w - MR - 2} y={y(r.v) - 3} textAnchor="end" fontSize={11} fill="#9aa3ad">{r.label}</text>
      </g>)}
      <text x={ML} y={height - 4} fontSize={11} fill="#9aa3ad">{days[0]}</text>
      <text x={w - MR} y={height - 4} textAnchor="end" fontSize={11} fill="#9aa3ad">{days[n - 1]}</text>
      {hi != null && <line x1={hx} x2={hx} y1={MT} y2={MT + ph} stroke="#9aa3ad" strokeWidth={1}/>}
    </svg>}
    {hi != null && <div className={css["ctip"]} style={{
      display: "block", top: 8, left: hx > w / 2 ? Math.max(0, hx - 190) : hx + 12 }}>{tip(hi)}</div>}
  </div>;
}
