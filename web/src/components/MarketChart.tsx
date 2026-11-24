"use client";

import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type SeriesMarker, type Time,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Api } from "@/lib/api";
import css from "./MarketChart.module.css";

type Bar = { time: Time; open: number; high: number; low: number; close: number; volume: number };
type OverlayLine = { kind: string; price: number; label: string; status?: string };
type OverlayMarker = { id: number; time: string; side: string; status: string; price: number; quantity: number };
type Overlays = { symbol: string; markers: OverlayMarker[]; lines: OverlayLine[] };

const LINE_COLORS: Record<string, string> = { cost: "#9aa8b2", pending: "#e8b85c", sl: "#ef737a", tp: "#45c99a" };
const frames = ["1m", "5m", "15m", "1h", "1D"];

type HistoryResponse = { bars?: Array<Omit<Bar, "time"> & { time: number | string }> };

export default function MarketChart({ symbols, api }: { symbols: string[]; api: Api }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const [picked, setPicked] = useState("");
  const symbol = picked && symbols.includes(picked) ? picked : symbols[0] || "";
  const [frame, setFrame] = useState("1m");
  const [extended, setExtended] = useState(true);
  const [price, setPrice] = useState<number | null>(null);
  const [note, setNote] = useState("Select a symbol to load market data");
  const [live, setLive] = useState("connecting");

  useEffect(() => {
    const choose = (e: Event) => { const next = (e as CustomEvent<string>).detail; if (symbols.includes(next)) setPicked(next); };
    window.addEventListener("chart-symbol", choose);
    return () => window.removeEventListener("chart-symbol", choose);
  }, [symbols]);
  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#111820" }, textColor: "#8e9ba7", fontFamily: "Inter, system-ui, sans-serif" },
      grid: { vertLines: { color: "#1d2831" }, horzLines: { color: "#1d2831" } },
      rightPriceScale: { borderColor: "#26343e" }, timeScale: { borderColor: "#26343e", timeVisible: true },
      localization: { timeFormatter: (t: Time) => {
        const date = typeof t === "number" ? new Date(t * 1000) : typeof t === "string" ? new Date(t) : new Date(Date.UTC(t.year, t.month - 1, t.day));
        return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
      } },
    });
    chart.current = c;
    const candle = c.addSeries(CandlestickSeries, { upColor: "#41c99a", downColor: "#ec6b72", wickUpColor: "#41c99a", wickDownColor: "#ec6b72", borderVisible: false });
    const volume = c.addSeries(HistogramSeries, { priceScaleId: "volume", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    c.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const sma20 = c.addSeries(LineSeries, { color: "#65a9ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const sma50 = c.addSeries(LineSeries, { color: "#e4b85a", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    let source: EventSource | null = null;
    let bars: Bar[] = [];
    let active = true;
    const span = ({ "1m": 60, "5m": 300, "15m": 900, "1h": 3600 } as Record<string, number>)[frame] || 86400;
    const sma = (n: number) => bars.flatMap((b, i) => i >= n - 1 ? [{ time: b.time, value: bars.slice(i - n + 1, i + 1).reduce((a, x) => a + x.close, 0) / n }] : []);
    const draw = () => {
      candle.setData(bars); volume.setData(bars.map(b => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "#41c99a55" : "#ec6b7255" })));
      sma20.setData(sma(20)); sma50.setData(sma(50));
    };
    // Overlays (cost basis, pending limits, stops, targets, proposal markers). Best effort.
    let priceLines: IPriceLine[] = [];
    const markers = createSeriesMarkers(candle, []);
    const drawOverlays = async () => {
      try {
        const o = await api<Overlays>(`/api/chart/overlays?symbol=${encodeURIComponent(symbol)}`);
        if (!active) return;
        priceLines.forEach(l => candle.removePriceLine(l));
        priceLines = (o.lines || []).filter(l => Number(l.price) > 0).map(l => candle.createPriceLine({
          price: Number(l.price),
          color: LINE_COLORS[l.kind] || "#6eaaff",
          lineWidth: 1,
          lineStyle: l.kind === "cost" ? LineStyle.Solid : LineStyle.Dashed,
          axisLabelVisible: true,
          title: l.label,
        }));
        if (frame === "1D" || !bars.length) { markers.setMarkers([]); return; }
        const first = Number(bars[0].time);
        const ms: SeriesMarker<Time>[] = (o.markers || []).flatMap(m => {
          const epoch = Math.floor(Date.parse(m.time.endsWith("Z") || m.time.includes("+") ? m.time : `${m.time}Z`) / 1000);
          if (!Number.isFinite(epoch) || epoch < first) return [];
          const buy = m.side === "BUY";
          return [{
            time: (Math.floor(epoch / span) * span) as Time,
            position: buy ? "belowBar" : "aboveBar",
            color: m.status === "placed" ? (buy ? "#45c99a" : "#ef737a") : "#82919c",
            shape: buy ? "arrowUp" : "arrowDown",
            text: `#${m.id} ${m.side} ${m.quantity} · ${m.status}`,
          }];
        });
        ms.sort((a, b) => Number(a.time) - Number(b.time));
        markers.setMarkers(ms);
      } catch {
        /* overlays are optional */
      }
    };
    const load = async () => {
      if (!symbol) return;
      source?.close(); setNote("Loading Webull bars…"); setLive("connecting");
      try {
        const data = await api<HistoryResponse>(`/api/chart/history?symbol=${encodeURIComponent(symbol)}&tf=${frame}&extended=${extended}`);
        if (!active) return;
        bars = (data.bars || []).map(b => ({ ...b, time: (frame === "1D" ? b.time : Math.floor(Number(b.time) / span) * span) as Time }));
        bars = bars.filter((b: Bar, i: number) => i === 0 || (frame === "1D" ? String(b.time) > String(bars[i - 1].time) : Number(b.time) > Number(bars[i - 1].time)));
        draw(); c.timeScale().fitContent();
        if (bars.length) setPrice(bars[bars.length - 1].close);
        setNote(`${bars.length} bars · US Eastern${extended && frame !== "1D" ? " (Ext)" : ""}`);
        void drawOverlays();
      } catch (e) { if (active) setNote(`Price history unavailable · ${e instanceof Error ? e.message : "broker error"}`); }
      if (!active) return;
      source = new EventSource(`/api/stream?symbol=${encodeURIComponent(symbol)}`);
      source.addEventListener("hello", (e) => { const v = JSON.parse((e as MessageEvent).data); setLive(v.mode === "stream" ? "streaming" : `polling ${v.mode}`); });
      source.onmessage = (e) => {
        const tick = JSON.parse(e.data); if (tick.symbol !== symbol) return;
        setPrice(tick.p); const t = frame === "1D" ? new Date(tick.t * 1000).toLocaleDateString("en-CA", { timeZone: "America/New_York" }) : Math.floor(tick.t / span) * span;
        const last = bars[bars.length - 1];
        if (last && Number(last.time) === Number(t)) { last.high = Math.max(last.high, tick.p); last.low = Math.min(last.low, tick.p); last.close = tick.p; last.volume += tick.v || 0; }
        else { bars.push({ time: t as Time, open: tick.p, high: tick.p, low: tick.p, close: tick.p, volume: tick.v || 0 }); }
        candle.update({ time: bars[bars.length - 1].time, open: bars[bars.length - 1].open, high: bars[bars.length - 1].high, low: bars[bars.length - 1].low, close: bars[bars.length - 1].close });
        volume.update({ time: bars[bars.length - 1].time, value: bars[bars.length - 1].volume, color: bars[bars.length - 1].close >= bars[bars.length - 1].open ? "#41c99a55" : "#ec6b7255" });
      };
      source.onerror = () => setLive("reconnecting");
    };
    void load();
    // Keep the price lines current (an approved or dismissed proposal drops its limit line).
    const overlayTimer = window.setInterval(() => { if (document.visibilityState === "visible") void drawOverlays(); }, 30_000);
    return () => { active = false; window.clearInterval(overlayTimer); source?.close(); c.remove(); chart.current = null; };
  }, [symbol, frame, extended, api]);

  return <section className={`panel chart-panel`}><div className={`panel-head ${css["chart-head"]}`}><div><div className="eyebrow">LIVE MARKET</div><h2>{symbol || "Market chart"} <span className={`${css["chart-price"]}`}>{price == null ? "—" : `$${price.toFixed(2)}`}</span></h2></div><div className={`${css["chart-controls"]}`}><select aria-label="Chart symbol" value={symbol} onChange={e => setPicked(e.target.value)}>{symbols.map(s => <option key={s}>{s}</option>)}</select><div className={`${css["frame-tabs"]}`}>{frames.map(f => <button key={f} className={frame === f ? "selected" : ""} onClick={() => setFrame(f)}>{f}</button>)}<button className={`${css["ext-toggle"]} ${extended ? "selected" : ""}`} onClick={() => setExtended(!extended)} title="Toggle Pre/Post Market (Extended Hours)">EXT</button></div></div></div><div className={`${css["chart-legend"]}`}><span><i className={`${css["legend-dot"]} ${css["blue"]}`}/>SMA20</span><span><i className={`${css["legend-dot"]} ${css["gold"]}`}/>SMA50</span><span className={`${css["live-label"]}`}><i className={live === "reconnecting" ? "dot red" : "dot green"}/>{live}</span></div><div ref={host} className={`${css["chart-host"]}`}/><div className={`${css["chart-foot"]}`}><span>{note}</span><span>Data via Webull MCP</span></div></section>;
}
