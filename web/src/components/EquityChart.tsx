"use client";

import { ColorType, createChart, LineSeries, LineStyle, type Time } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Performance } from "@/lib/types";
import css from "./Today.module.css";

/** Quiet two-line chart: account vs benchmark, cumulative return in percent from the first stored day. */
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
        background: { type: ColorType.Solid, color: "#17191c" }, textColor: "#9aa3ad",
        fontFamily: "IBM Plex Sans, system-ui, sans-serif", fontSize: 12,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e33" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      crosshair: { horzLine: { visible: false, labelVisible: false } },
      handleScroll: false,
      handleScale: false,
      localization: { priceFormatter: (v: number) => `${v.toFixed(1)}%` },
    });
    const acct = c.addSeries(LineSeries, { color: "#e6e8eb", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    const bench = c.addSeries(LineSeries, {
      color: "#9aa3ad", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false,
    });
    acct.setData(series.map(p => ({ time: p.day as Time, value: (p.account - 1) * 100 })));
    bench.setData(series.map(p => ({ time: p.day as Time, value: (p.benchmark - 1) * 100 })));
    c.timeScale().fitContent();
    return () => c.remove();
  }, [series]);

  return <section aria-label={`Equity versus ${symbol}`}>
    <div className={css["section-head"]}>
      <h2>Equity vs {symbol}</h2>
      <span className={css["legend"]}>
        <i className={css["key-solid"]} aria-hidden="true"/>Account
        <i className={css["key-dash"]} aria-hidden="true"/>{symbol}
      </span>
    </div>
    {enough
      ? <div ref={host} className={css["chart"]} role="img" aria-label={`Cumulative return of the account and ${symbol}`}/>
      : <p className="muted">{performance?.message || "The chart appears after two trading days of history."}</p>}
  </section>;
}
