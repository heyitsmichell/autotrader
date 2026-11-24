"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { lastSma, PERIODS, periodReturn, type CloseBar } from "@/lib/indicators";
import type { Signal } from "@/lib/types";
import css from "./ChartRead.module.css";

const sgn = (n: number, d = 2) => `${n >= 0 ? "+" : ""}${n.toFixed(d)}`;
const cls = (n: number) => (n >= 0 ? css.pos : css.neg);

function useDaily(api: Api, symbol: string) {
  const [bars, setBars] = useState<{ symbol: string; bars: CloseBar[] } | null>(null);
  useEffect(() => {
    let live = true;
    if (!symbol) return;
    api<{ bars?: CloseBar[] }>(`/api/chart/history?symbol=${encodeURIComponent(symbol)}&tf=1D&extended=false`)
      .then(d => { if (live) setBars({ symbol, bars: d.bars || [] }); })
      .catch(() => { if (live) setBars({ symbol, bars: [] }); });
    return () => { live = false; };
  }, [api, symbol]);
  return bars && bars.symbol === symbol ? bars.bars : null;
}

export default function ChartRead({ symbol, signal, api, price, benchmark }: {
  symbol: string; signal?: Signal; api: Api; price: number | null; benchmark: string;
}) {
  const hasSpy = !!benchmark;   // SPY, or SPYM (same S&P 500 index) when SPY is not on the watchlist
  const bars = useDaily(api, symbol);
  const spy = useDaily(api, hasSpy && symbol !== benchmark ? benchmark : "");
  const s = signal;
  const comps = s?.ta_components || [];
  const checks = s?.consensus_checks || [];
  const failing = checks.find(c => !c.ok);
  const entry = price ?? s?.close ?? null;
  const risk = entry != null && s?.sl ? entry - s.sl : null;
  const reward = entry != null && s?.tp ? s.tp - entry : null;
  const ma200 = bars ? lastSma(bars, 200) : null;
  const last = bars?.length ? bars[bars.length - 1].close : null;

  const rets = [["3-month", PERIODS.m3], ["6-month", PERIODS.m6], ["12-month", PERIODS.m12]] as const;
  const rel = (days: number) => {
    const a = bars && periodReturn(bars, days), b = spy && periodReturn(spy, days);
    return a != null && b != null ? a - b : null;
  };

  return <aside className={css.read} aria-label={`What the strategy sees for ${symbol}`}>
    <section>
      <h3>What the strategy sees</h3>
      {!s ? <p className={css.muted}>No signal yet for {symbol}.</p> : <>
        <p>
          Trend is <b>{s.trend || "unknown"}</b>{s.strength ? `, ${s.strength} strength` : ""}.
          {s.ta_score != null && <> Technical score <b className={cls(s.ta_score)}>{sgn(s.ta_score)}</b>.</>}
        </p>
        {(s.features?.rsi14 != null || s.features?.pct_from_high10 != null) && <p className={css.muted}>
          {s.features?.rsi14 != null && <>RSI {Number(s.features.rsi14).toFixed(0)}{Number(s.features.rsi14) > 70 ? " (stretched)" : Number(s.features.rsi14) < 30 ? " (oversold)" : ""}. </>}
          {s.features?.pct_from_high10 != null && <>{Math.abs(Number(s.features.pct_from_high10)).toFixed(1)}% below the 10-day high.</>}
        </p>}
        {comps.length > 0 && <ul className={css.list}>{comps.map((c, i) =>
          <li key={i} className={css.row}><span>{c.note || c.name}</span><span className={cls(c.points ?? 0)}>{sgn(c.points ?? 0)}</span></li>)}</ul>}
      </>}
    </section>
    {s && <section>
      <h3>Decision</h3>
      <p className={failing ? css.warn : css.pos}>
        {failing ? `Blocked: ${failing.detail || failing.name}` : s.consensus ? `Consensus: ${s.consensus}` : "No consensus result yet."}
      </p>
      {checks.length > 0 && <p className={css.muted}>{checks.filter(c => c.ok).length} of {checks.length} checks pass.</p>}
      {s.ml_p != null && <p>Model probability <b>{(s.ml_p * 100).toFixed(0)}%</b>.</p>}
      {s.sl ? <ul className={css.list}>
        <li className={css.row}><span>Stop</span><span>${s.sl.toFixed(2)}</span></li>
        {s.tp ? <li className={css.row}><span>Target</span><span>${s.tp.toFixed(2)}</span></li> : null}
        {risk != null && risk > 0 && <li className={css.row}><span>Risk per share</span><span>${risk.toFixed(2)}</span></li>}
        {risk != null && risk > 0 && reward != null && reward > 0 && <li className={css.row}><span>Reward to risk</span><span>{(reward / risk).toFixed(2)} : 1</span></li>}
      </ul> : <p className={css.muted}>No stop or target set.</p>}
    </section>}
    <section>
      <h3>Momentum context</h3>
      {bars == null ? <p className={css.muted}>Loading daily history…</p> : bars.length === 0 ? <p className={css.muted}>Daily history unavailable.</p> : <ul className={css.list}>
        {rets.map(([label, d]) => { const r = periodReturn(bars, d);
          return <li key={label} className={css.row}><span>{label} return</span>{r == null ? <span className={css.muted}>not enough history</span> : <span className={cls(r)}>{sgn(r, 1)}%</span>}</li>; })}
        {symbol !== benchmark && ([["3-month", PERIODS.m3], ["6-month", PERIODS.m6]] as const).map(([label, d]) => { const r = rel(d);
          return <li key={`rs${label}`} className={css.row}><span>{label} vs {benchmark || "SPY"}</span>{r == null ? <span className={css.muted}>{hasSpy ? "unavailable" : "no S&P 500 ETF on the watchlist"}</span> : <span className={cls(r)}>{sgn(r, 1)} pts</span>}</li>; })}
        <li className={css.row}><span>Versus 200-day average</span>{ma200 == null || last == null ? <span className={css.muted}>not enough history</span> :
          <span className={cls(last - ma200)}>{sgn((last / ma200 - 1) * 100, 1)}% ({last > ma200 ? "above" : "below"})</span>}</li>
      </ul>}
      <p className={css.muted}>Medium-term momentum and relative strength were the only robust signals in our research.</p>
    </section>
  </aside>;
}
