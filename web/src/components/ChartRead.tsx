"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { money, num, pct, signed } from "@/lib/format";
import { lastSma, PERIODS, periodReturn, type CloseBar } from "@/lib/indicators";
import type { Signal, Status } from "@/lib/types";
import Mark from "@/components/ui/Mark";
import css from "./ChartRead.module.css";

const sign = (n: number) => (n > 0 ? css.pos : n < 0 ? css.neg : "");

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

/** Next earnings date from the cached Yahoo summary; null when unknown. */
function useEarnings(api: Api, symbol: string) {
  const [e, setE] = useState<{ symbol: string; date: string | null } | null>(null);
  useEffect(() => {
    let live = true;
    if (!symbol) return;
    api<{ earnings_date?: string | null }>(`/api/yfinance/${encodeURIComponent(symbol)}`)
      .then(d => { if (live) setE({ symbol, date: d.earnings_date || null }); })
      .catch(() => { if (live) setE({ symbol, date: null }); });
    return () => { live = false; };
  }, [api, symbol]);
  return e && e.symbol === symbol ? e.date : null;
}

const fmtDate = (iso: string) => {
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

function KV({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className={css.kv}><span className={css.k}>{label}</span><span className={css.v}>{children}</span></div>;
}

const minus = (t: string) => t.replace(/(^|[\s(])-(?=\d)/g, "$1−");

export default function ChartRead({ symbol, signal, api, price, benchmark, status }: {
  symbol: string; signal?: Signal; api: Api; price: number | null; benchmark: string; status: Status | null;
}) {
  const hasSpy = !!benchmark;   // SPY, or SPYM (same S&P 500 index) when SPY is not on the watchlist
  const bars = useDaily(api, symbol);
  const spy = useDaily(api, hasSpy && symbol !== benchmark ? benchmark : "");
  const earnings = useEarnings(api, symbol);
  const [now] = useState(() => Date.now());
  const blackout = status?.settings?.eligibility_earnings_blackout_days ?? null;
  const earnLabel = earnings ? fmtDate(earnings) : null;
  const earnDays = earnings ? Math.round((new Date(`${earnings.slice(0, 10)}T12:00:00`).getTime() - now) / 86400000) : null;
  const inWindow = earnDays != null && blackout != null && earnDays >= 0 && earnDays <= blackout;
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
          {s.ta_score != null && <> Technical score <b className={`${css.mono} ${sign(s.ta_score)}`}>{signed(s.ta_score)}</b>.</>}
        </p>
        {(s.features?.rsi14 != null || s.features?.pct_from_high10 != null) && <p className={css.muted}>
          {s.features?.rsi14 != null && <>RSI {Number(s.features.rsi14).toFixed(0)}{Number(s.features.rsi14) > 70 ? " (stretched)" : Number(s.features.rsi14) < 30 ? " (oversold)" : ""}. </>}
          {s.features?.pct_from_high10 != null && <>{Math.abs(Number(s.features.pct_from_high10)).toFixed(1)}% below the 10-day high.</>}
        </p>}
        {earnLabel && (inWindow
          ? <p style={{ color: "var(--warn)" }}>Earnings in {earnDays} {earnDays === 1 ? "day" : "days"} · entries blocked</p>
          : <p className={css.muted}>Earnings {earnLabel}{blackout ? ` · entries blocked within ${blackout} days` : ""}</p>)}
        {comps.length > 0 && <ul className={css.list}>{comps.map((c, i) =>
          <li key={i} className={css.comp}>
            <span className={css.compText}>
              <span>{c.name || "Component"}</span>
              {c.note && <span className={css.sub}>{c.note}</span>}
            </span>
            <span className={`${css.mono} ${sign(c.points ?? 0)}`}>{signed(c.points ?? 0)}</span>
          </li>)}</ul>}
      </>}
    </section>
    {s && <section>
      <h3>Decision</h3>
      <p className={css.decision}>
        {s.consensus || failing
          ? <Mark tone={failing || s.consensus === "blocked" ? "loss" : "gain"}>{failing || s.consensus === "blocked" ? "Blocked" : "Pass"}</Mark>
          : <Mark>Waiting</Mark>}
        <span>{failing ? minus(failing.detail || failing.name) : s.consensus ? "All checks clear" : "No consensus result yet."}</span>
      </p>
      {checks.length > 0 && <p className={css.muted}>{checks.filter(c => c.ok).length} of {checks.length} checks pass.</p>}
      {s.ml_p != null && <p>Model probability <b className={css.mono}>{(s.ml_p * 100).toFixed(0)}%</b>.</p>}
      {s.sl ? <div className={css.kvs}>
        <KV label="Stop">{money(s.sl)}</KV>
        <KV label="Target">{s.tp ? money(s.tp) : "—"}</KV>
        <KV label="Risk per share">{risk != null && risk > 0 ? money(risk) : "—"}</KV>
        <KV label="Reward to risk">{risk != null && risk > 0 && reward != null && reward > 0 ? `${num(reward / risk)} : 1` : "—"}</KV>
      </div> : <p className={css.muted}>No stop or target set.</p>}
    </section>}
    <section>
      <h3>Momentum context</h3>
      {bars == null ? <p className={css.muted}>Loading daily history…</p> : bars.length === 0 ? <p className={css.muted}>Daily history unavailable.</p> : <div className={css.kvs}>
        {rets.map(([label, d]) => { const r = periodReturn(bars, d);
          return <KV key={label} label={`${label} return`}>{r == null ? <span className={css.muted}>—</span> : <span className={sign(r)}>{pct(r, { digits: 1 })}</span>}</KV>; })}
        {symbol !== benchmark && ([["3-month", PERIODS.m3], ["6-month", PERIODS.m6]] as const).map(([label, d]) => { const r = rel(d);
          return <KV key={`rs${label}`} label={`${label} vs ${benchmark || "SPY"}`}>{r == null ? <span className={css.muted} title={hasSpy ? "unavailable" : "no S&P 500 ETF on the watchlist"}>—</span> : <span className={sign(r)}>{signed(r, 1)} pts</span>}</KV>; })}
        <KV label="Versus 200-day average">{ma200 == null || last == null ? <span className={css.muted}>—</span> :
          <span className={sign(last - ma200)}>{pct((last / ma200 - 1) * 100, { digits: 1 })} ({last > ma200 ? "above" : "below"})</span>}</KV>
      </div>}
      <p className={css.muted}>Medium-term momentum and relative strength were the only robust signals in our research.</p>
    </section>
  </aside>;
}
