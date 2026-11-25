"use client";

import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle,
  type IPriceLine, type SeriesMarker, type Time,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Api } from "@/lib/api";
import { sma } from "@/lib/indicators";
import type { Signal, Status } from "@/lib/types";
import Segmented from "@/components/ui/Segmented";
import ChartRead from "./ChartRead";
import css from "./MarketChart.module.css";

type Bar = { time: Time; open: number; high: number; low: number; close: number; volume: number };
type OverlayLine = { kind: string; price: number; label: string; status?: string };
type OverlayMarker = { id: number; time: string; side: string; status: string; price: number; quantity: number };
type Overlays = { symbol: string; markers: OverlayMarker[]; lines: OverlayLine[] };

const GAIN = "#46a882", LOSS = "#e5596f", WARN = "#d9a441", ACCENT = "#5b9be0", MUTED = "#8c9199", INK2 = "#bdbfc4";
const LINE_COLORS: Record<string, string> = { cost: MUTED, pending: WARN, sl: LOSS, tp: GAIN };
const frames = ["1m", "5m", "15m", "1h", "1D"].map(f => ({ value: f, label: f }));
const OVERLAYS = [
  ["sma20", "20-day average", ACCENT], ["sma50", "50-day average", INK2], ["sma200", "200-day average", MUTED],
  ["volume", "Volume", "#4a4e55"], ["levels", "Entry and stop", MUTED], ["trades", "Trades", GAIN],
] as const;
type OverlayKey = (typeof OVERLAYS)[number][0];
type HistoryResponse = { bars?: Array<Omit<Bar, "time"> & { time: number | string }> };

export default function MarketChart({ symbols, api, signals = [], status = null }: { symbols: string[]; api: Api; signals?: Signal[]; status?: Status | null }) {
  const host = useRef<HTMLDivElement>(null);
  const [picked, setPicked] = useState("");
  const symbol = picked && symbols.includes(picked) ? picked : symbols[0] || "";
  const [frame, setFrame] = useState("1D");
  const [extended, setExtended] = useState(true);
  const [price, setPrice] = useState<number | null>(null);
  const [note, setNote] = useState("Select a symbol to load market data");
  const [live, setLive] = useState("connecting");
  const [on, setOn] = useState<Record<OverlayKey, boolean>>({ sma20: true, sma50: true, sma200: true, volume: true, levels: true, trades: true });
  const onRef = useRef(on);
  const apply = useRef<() => void>(() => {});
  useEffect(() => { onRef.current = on; apply.current(); }, [on]);
  const signal = signals.filter(x => x.symbol === symbol).sort((a, b) => (b.id ?? 0) - (a.id ?? 0))[0];

  useEffect(() => {
    const choose = (e: Event) => { const next = (e as CustomEvent<string>).detail; if (symbols.includes(next)) setPicked(next); };
    window.addEventListener("chart-symbol", choose);
    return () => window.removeEventListener("chart-symbol", choose);
  }, [symbols]);

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#16171a" }, textColor: "#8c9199", panes: { separatorColor: "#26292e", separatorHoverColor: "#3a3d43", enableResize: true }, fontFamily: "IBM Plex Sans, system-ui, sans-serif" },
      grid: { vertLines: { visible: false }, horzLines: { color: "#26292e" } },
      crosshair: { vertLine: { labelBackgroundColor: "#1d1f23" }, horzLine: { labelBackgroundColor: "#1d1f23" } },
      rightPriceScale: { borderColor: "#26292e" }, timeScale: { borderColor: "#26292e", timeVisible: true },
      localization: { timeFormatter: (t: Time) => {
        const date = typeof t === "number" ? new Date(t * 1000) : typeof t === "string" ? new Date(t) : new Date(Date.UTC(t.year, t.month - 1, t.day));
        return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
      } },
    });
    const candle = c.addSeries(CandlestickSeries, { upColor: GAIN, downColor: LOSS, wickUpColor: GAIN, wickDownColor: LOSS, borderVisible: false });
    const mk = (color: string) => c.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const sma20 = mk(ACCENT), sma50 = mk(INK2), sma200 = mk(MUTED);
    const volume = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }, 1);
    try { c.panes()[1]?.setHeight(64); } catch { /* single pane fallback */ }
    let source: EventSource | null = null;
    let bars: Bar[] = [];
    let active = true;
    let priceLines: IPriceLine[] = [];
    let markerList: SeriesMarker<Time>[] = [];
    const markers = createSeriesMarkers(candle, []);
    const span = ({ "1m": 60, "5m": 300, "15m": 900, "1h": 3600 } as Record<string, number>)[frame] || 86400;
    const volColor = (b: Bar) => (b.close >= b.open ? "#46a88233" : "#e5596f33");
    const smaOf = (n: number) => sma(bars as Array<{ time: string | number; close: number }>, n) as unknown as Array<{ time: Time; value: number }>;
    const showing = () => {
      const o = onRef.current;
      sma20.applyOptions({ visible: o.sma20 }); sma50.applyOptions({ visible: o.sma50 }); sma200.applyOptions({ visible: o.sma200 });
      volume.applyOptions({ visible: o.volume });
      priceLines.forEach(l => l.applyOptions({ lineVisible: o.levels, axisLabelVisible: o.levels }));
      markers.setMarkers(o.trades ? markerList : []);
    };
    apply.current = showing;
    const draw = () => {
      candle.setData(bars); volume.setData(bars.map(b => ({ time: b.time, value: b.volume, color: volColor(b) })));
      sma20.setData(smaOf(20)); sma50.setData(smaOf(50)); sma200.setData(smaOf(200));
    };
    // Overlays (cost basis, pending limits, stops, targets, trade markers). Best effort.
    const drawOverlays = async () => {
      try {
        const o = await api<Overlays>(`/api/chart/overlays?symbol=${encodeURIComponent(symbol)}`);
        if (!active) return;
        priceLines.forEach(l => candle.removePriceLine(l));
        priceLines = (o.lines || []).filter(l => Number(l.price) > 0).map(l => candle.createPriceLine({
          price: Number(l.price),
          color: LINE_COLORS[l.kind] || ACCENT,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: l.label,
        }));
        if (!bars.length) { markerList = []; showing(); return; }
        const first = frame === "1D" ? 0 : Number(bars[0].time);
        const ms: SeriesMarker<Time>[] = (o.markers || []).flatMap(m => {
          const epoch = Math.floor(Date.parse(m.time.endsWith("Z") || m.time.includes("+") ? m.time : `${m.time}Z`) / 1000);
          if (!Number.isFinite(epoch)) return [];
          const day = new Date(epoch * 1000).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
          if (frame === "1D" ? day < String(bars[0].time) : epoch < first) return [];
          const buy = m.side === "BUY";
          return [{
            time: (frame === "1D" ? day : Math.floor(epoch / span) * span) as Time,
            position: buy ? "belowBar" : "aboveBar",
            color: m.status === "placed" ? (buy ? GAIN : LOSS) : MUTED,
            shape: buy ? "arrowUp" : "arrowDown",
            text: `#${m.id} ${m.side} ${m.quantity} · ${m.status}`,
          }];
        });
        ms.sort((a, b) => (frame === "1D" ? String(a.time).localeCompare(String(b.time)) : Number(a.time) - Number(b.time)));
        markerList = ms; showing();
      } catch {
        /* overlays are optional */
      }
    };
    const load = async () => {
      if (!symbol) return;
      source?.close(); setNote("Loading bars…"); setLive("connecting");
      try {
        const data = await api<HistoryResponse>(`/api/chart/history?symbol=${encodeURIComponent(symbol)}&tf=${frame}&extended=${extended}`);
        if (!active) return;
        bars = (data.bars || []).map(b => ({ ...b, time: (frame === "1D" ? b.time : Math.floor(Number(b.time) / span) * span) as Time }));
        bars = bars.filter((b: Bar, i: number) => i === 0 || (frame === "1D" ? String(b.time) > String(bars[i - 1].time) : Number(b.time) > Number(bars[i - 1].time)));
        draw(); showing(); c.timeScale().fitContent();
        if (bars.length) setPrice(bars[bars.length - 1].close);
        setNote(`${bars.length} bars · US Eastern${extended && frame !== "1D" ? " · extended hours" : ""}`);
        void drawOverlays();
      } catch (e) { if (active) setNote(`Price history unavailable · ${e instanceof Error ? e.message : "broker error"}`); }
      if (!active) return;
      source = new EventSource(`/api/stream?symbol=${encodeURIComponent(symbol)}`);
      source.addEventListener("hello", (e) => { const v = JSON.parse((e as MessageEvent).data); setLive(v.mode === "stream" ? "streaming" : "delayed"); });
      source.onmessage = (e) => {
        const tick = JSON.parse(e.data); if (tick.symbol !== symbol) return;
        setPrice(tick.p); const t = frame === "1D" ? new Date(tick.t * 1000).toLocaleDateString("en-CA", { timeZone: "America/New_York" }) : Math.floor(tick.t / span) * span;
        const last = bars[bars.length - 1];
        if (last && String(last.time) === String(t)) { last.high = Math.max(last.high, tick.p); last.low = Math.min(last.low, tick.p); last.close = tick.p; last.volume += tick.v || 0; }
        else { bars.push({ time: t as Time, open: tick.p, high: tick.p, low: tick.p, close: tick.p, volume: tick.v || 0 }); }
        const b = bars[bars.length - 1];
        candle.update({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
        volume.update({ time: b.time, value: b.volume, color: volColor(b) });
      };
      source.onerror = () => setLive("reconnecting");
    };
    void load();
    // Keep the price lines current (an approved or dismissed proposal drops its limit line).
    const overlayTimer = window.setInterval(() => { if (document.visibilityState === "visible") void drawOverlays(); }, 30_000);
    return () => { active = false; apply.current = () => {}; window.clearInterval(overlayTimer); source?.close(); c.remove(); };
  }, [symbol, frame, extended, api]);

  const bench = symbols.includes("SPY") ? "SPY" : symbols.includes("SPYM") ? "SPYM" : "";
  return <section className={css.wrap}>
    <div className={css.chartCol}>
      <div className={css.head}>
        <div className={css.title}>
          <select aria-label="Chart symbol" value={symbol} onChange={e => setPicked(e.target.value)}>{symbols.map(s => <option key={s}>{s}</option>)}</select>
          <span className={css.price}>{price == null ? "—" : `$${price.toFixed(2)}`}</span>
          {live !== "streaming" && live !== "connecting" && <span className={css.muted}>{live}</span>}
        </div>
        <div className={css.controls}>
          <Segmented label="Timeframe" options={frames} value={frame} onChange={setFrame}/>
          <button type="button" className="button" aria-pressed={extended} onClick={() => setExtended(!extended)} title="Include pre-market and after-hours">Extended hours: {extended ? "on" : "off"}</button>
        </div>
      </div>
      <div className={css.legend} role="group" aria-label="Chart overlays">
        {OVERLAYS.map(([k, label, color]) => <button key={k} type="button" aria-pressed={on[k]} className={`${css.chip} ${on[k] ? css.chipOn : ""}`} onClick={() => setOn(o => ({ ...o, [k]: !o[k] }))}><i style={{ background: on[k] ? color : "transparent", borderColor: color }}/>{label}</button>)}
      </div>
      <div ref={host} className={css.host}/>
      <div className={css.foot}>{note}</div>
    </div>
    <ChartRead symbol={symbol} signal={signal} api={api} price={price} benchmark={bench} status={status}/>
  </section>;
}
