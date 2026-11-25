"use client";

import { ColorType, createChart, LineSeries, type Time } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Performance } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Section from "@/components/ui/Section";
import css from "./Today.module.css";

/** Account vs benchmark: cumulative return in percent from the first stored day. */
export default function EquityChart({ performance }: { performance: Performance | null }) {
  const host = useRef<HTMLDivElement>(null);
  const series = performance?.series;
  const symbol = performance?.benchmark_symbol || "SPY";
  const enough = !!series && series.length >= 2;

  useEffect(() => {
    if (!host.current || !series || series.length < 2) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#16171A" }, textColor: "#8C9199",
        fontFamily: "IBM Plex Mono, monospace", fontSize: 12,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: "#26292E" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      crosshair: { horzLine: { visible: false, labelVisible: false } },
      handleScroll: false,
      handleScale: false,
      localization: { priceFormatter: (v: number) => `${v.toFixed(1)}%` },
    });
    const opts = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const acct = c.addSeries(LineSeries, { ...opts, color: "#5B9BE0", lineWidth: 2 });
    const bench = c.addSeries(LineSeries, { ...opts, color: "#8C9199", lineWidth: 1 });
    acct.setData(series.map(p => ({ time: p.day as Time, value: (p.account - 1) * 100 })));
    bench.setData(series.map(p => ({ time: p.day as Time, value: (p.benchmark - 1) * 100 })));
    c.timeScale().fitContent();
    return () => c.remove();
  }, [series]);

  return <Section title={`Account vs ${symbol} since start, % return`}
    right={<span className={css["legend"]}><span><i/>Account</span><span><i className={css["spy"]}/>{symbol}</span></span>}>
    {enough
      ? <div ref={host} className={css["chart"]} role="img" aria-label={`Cumulative return of the account and ${symbol}`}/>
      : <Empty>{performance?.message || "The chart appears after two trading days of history; it fills in automatically."}</Empty>}
  </Section>;
}
