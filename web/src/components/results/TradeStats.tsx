"use client";

<<<<<<< HEAD
import { useEffect, useMemo, useState } from "react";
import type { Api } from "@/lib/api";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import Segmented from "@/components/ui/Segmented";
import { money, num, pct, signed } from "@/lib/format";
import type { ExitBucket, RoundTrip, RoundTripsPayload, TripStats } from "@/lib/types";
import { LIVE_IDS, strategyColor, strategyName } from "./names";
import ReturnHistogram, { type Metric } from "./ReturnHistogram";
import css from "./results.module.css";
import tcss from "./ResultsTables.module.css";

const BUCKETS: [ExitBucket, string][] = [["stop", "stop"], ["time", "time"], ["rebalance", "rebalance"],
  ["expiry", "expiry"], ["manual", "manual"], ["other", "other"]];
const MIN_HIST = 3;
const SMALL = 30;

const tone = (n: number | null | undefined): "gain" | "loss" | undefined =>
  n == null || n === 0 ? undefined : n > 0 ? "gain" : "loss";
const cls = (n: number | null | undefined) => (tone(n) === "gain" ? css["gain"] : tone(n) === "loss" ? css["loss"] : "");
const rate = (s: TripStats) => (s.win_rate != null ? pct(s.win_rate * 100, { sign: false, digits: 0 }) : "—");
const groupName = (g: string) => (g === "live" ? "All live" : g === "shadows" ? "All shadows" : strategyName(g));

/** Closed-trade stats for every strategy (one row each) and the result distribution of the selected one. */
export default function TradeStats({ api }: { api: Api }) {
  const [data, setData] = useState<RoundTripsPayload | null>(null);
  const [error, setError] = useState(false);
  const [pick, setPick] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>("ret");

  useEffect(() => {
    let live = true;
    const load = () => api<RoundTripsPayload>("/api/results/round_trips?days=3650")
      .then(d => { if (live) { setData(d); setError(false); } })
      .catch(() => { if (live) setError(true); });
    load();
    const t = window.setInterval(load, 300_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  const shadowIds = useMemo(() => Object.keys(data?.stats ?? {})
    .filter(k => !LIVE_IDS.includes(k) && k !== "live" && k !== "shadows").sort(), [data]);
  const groups = useMemo(() => ["live", ...LIVE_IDS, ...(shadowIds.length ? ["shadows", ...shadowIds] : [])]
    .filter(g => (data?.stats[g]?.n ?? 0) > 0), [data, shadowIds]);
  const sel = pick && groups.includes(pick) ? pick : groups[0] ?? null;
  const trips = useMemo<RoundTrip[]>(() => {
    if (!sel) return [];
    return (data?.trips ?? []).filter(t => sel === "live" ? t.live : sel === "shadows" ? !t.live : t.strategy === sel);
  }, [data, sel]);

  if (error && !data) return <p className="error">Trade stats are not available right now.</p>;
  if (!data) return <p className="loading">Loading trade stats…</p>;
  const st = data.stats;
  const virtualN = st["shadows"]?.n ?? 0;
  const meta = `Closed trades, every strategy · ${st["live"]?.n ?? 0} live${virtualN ? `, ${virtualN} virtual` : ""}`;
  if (!groups.length) return <Section title="Trade stats" meta={meta}>
    <Empty>No closed trades yet. Stats appear after the first exit; sleeve buys count once they are sold.</Empty>
  </Section>;

  const s = sel ? st[sel] : null;
  const hasR = (s?.n_r ?? 0) >= MIN_HIST;
  const m: Metric = metric === "r" && !hasR ? "ret" : metric;

  const row = (g: string) => {
    const x = st[g];
    const total = g === "live" || g === "shadows";
    const live = g === "live" || LIVE_IDS.includes(g);
    return <tr key={g} className={`${css["pick"]} ${g === sel ? css["on"] : ""} ${total ? css["totalRow"] : ""}`}
      onClick={() => setPick(g)}>
      <td className={tcss["l"]}>
        <button type="button" className={css["rowBtn"]} onClick={e => { e.stopPropagation(); setPick(g); }}
          aria-pressed={g === sel}>
          {!total && <i className={tcss["chip"]} style={{ background: strategyColor(g, live, Math.max(0, shadowIds.indexOf(g))) }}/>}
          {groupName(g)}{!live && !total && <span className={tcss["tag"]}>virtual</span>}
        </button>
      </td>
      <td>{x.n}</td><td>{rate(x)}</td>
      <td className={cls(x.avg_win)}>{money(x.avg_win, { sign: true })}</td>
      <td className={cls(x.avg_loss)}>{money(x.avg_loss, { sign: true })}</td>
      <td className={cls(x.expectancy)}>{money(x.expectancy, { sign: true })}</td>
      <td>{x.profit_factor != null ? num(x.profit_factor) : x.losses === 0 && x.wins > 0 ? "no losses" : "—"}</td>
      <td>{x.avg_hold_days != null ? `${num(x.avg_hold_days, 1)} d` : "—"}</td>
      <td className={cls(x.total)}>{money(x.total, { sign: true })}</td>
    </tr>;
  };

  return <Section title="Trade stats" meta={meta}>
    <div className={tcss["scroll"]}>
      <table className={tcss["tbl"]}>
        <thead><tr><th className={tcss["l"]}>Strategy</th><th>Closed</th><th>Win rate</th><th>Avg win</th><th>Avg loss</th>
          <th title="Average P&L per closed trade">Expectancy</th><th title="Gross wins divided by gross losses">Profit factor</th>
          <th>Avg hold</th><th>Realized</th></tr></thead>
        <tbody>{groups.map(row)}</tbody>
      </table>
    </div>
    <p className={css["note"]}>Sleeve and shadow fills are matched first in, first out, so a rebalance sell closes the
      oldest shares first. Pick a row to see its distribution.</p>

    {s && sel && <div className={css["detail"]}>
      <h3 className={css["sub"]}>{groupName(sel)}</h3>
      <FigureRow>
        <Figure label="Win rate" value={rate(s)} title={`${s.wins} won, ${s.losses} lost of ${s.n}`}/>
        <Figure label="Expectancy" value={money(s.expectancy, { sign: true })} tone={tone(s.expectancy)}
          title={s.avg_r != null ? `${signed(s.avg_r)} R across ${s.n_r} trades with a recorded stop` : undefined}/>
        <Figure label="Median return" value={s.median_ret_pct != null ? pct(s.median_ret_pct, { sign: true, digits: 1 }) : "—"}
          tone={tone(s.median_ret_pct)}/>
        <Figure label="Best" value={s.best ? `${s.best.symbol} ${money(s.best.pnl, { sign: true })}` : "—"} tone={tone(s.best?.pnl)}/>
        <Figure label="Worst" value={s.worst ? `${s.worst.symbol} ${money(s.worst.pnl, { sign: true })}` : "—"} tone={tone(s.worst?.pnl)}/>
        {s.avg_r != null && <Figure label="Avg R" value={`${signed(s.avg_r)} R`} tone={tone(s.avg_r)}
          title="P&L divided by the risk at entry (entry to stop); swing trades only"/>}
      </FigureRow>
      <p className={css["exits"]}>Exits:{" "}
        {BUCKETS.filter(([b]) => s.exits[b]).map(([b, label], i) => {
          const e = s.exits[b]!;
          return <span key={b}>{i > 0 && " · "}<span className="mono">{e.n}</span> {label}{" "}
            <span className={`mono ${cls(e.pnl)}`}>({money(e.pnl, { sign: true })})</span></span>;
        })}
      </p>

      <div className={css["histHead"]}>
        <h4 className={css["sub"]}>Distribution of trade results</h4>
        <Segmented<Metric> label="Distribution measure" value={m} onChange={setMetric}
          options={[{ value: "ret", label: "Return %" }, { value: "pnl", label: "P&L $" },
            ...(hasR ? [{ value: "r" as Metric, label: "R multiple" }] : [])]}/>
      </div>
      {trips.length >= MIN_HIST
        ? <>
          <ReturnHistogram trips={trips} metric={m}/>
          {s.n < SMALL && <p className={css["note"]}>Small sample ({s.n} trades): read the shape loosely until there are about {SMALL}.</p>}
        </>
        : <p className={css["note"]}>Too few closed trades for a distribution: {trips.length} so far.</p>}
    </div>}
  </Section>;
}
=======
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import { money, num, pct, signed } from "@/lib/format";
import type { TradeLedgerEntry } from "@/lib/types";
import css from "./results.module.css";

const exitBucket = (r: string | null): "stop" | "time" | "manual" | "other" => {
  const s = (r || "").toLowerCase();
  if (s.includes("stop")) return "stop";
  if (s.includes("time") || s.includes("expir") || s.includes("days")) return "time";
  if (s.includes("manual") || s.includes("flatten") || s.includes("kill") || s.includes("user")) return "manual";
  return "other";
};

const tone = (n: number | null): "gain" | "loss" | undefined => (n == null || n === 0 ? undefined : n > 0 ? "gain" : "loss");

export default function TradeStats() {
  const [rows, setRows] = useState<TradeLedgerEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api<TradeLedgerEntry[]>("/api/trades?limit=500&status=closed")
      .then(d => { if (active) { setRows(Array.isArray(d) ? d.filter(t => t.status === "closed") : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); });
    return () => { active = false; };
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!rows) return <p className="loading">Loading trade stats…</p>;
  const n = rows.length;
  const meta = `All closed trades, n = ${n}`;
  if (n === 0) return <Section title="Trade stats" meta={meta}><Empty>No closed trades yet. Stats appear after the first exit.</Empty></Section>;

  const pnl = rows.map(t => t.realized_pnl).filter((v): v is number => v != null);
  const wins = pnl.filter(v => v > 0);
  const losses = pnl.filter(v => v < 0);
  const sum = (a: number[]) => a.reduce((s, v) => s + v, 0);
  const avgWin = wins.length ? sum(wins) / wins.length : null;
  const avgLoss = losses.length ? sum(losses) / losses.length : null;
  const expectancy = pnl.length ? sum(pnl) / pnl.length : null;
  const grossLoss = Math.abs(sum(losses));
  const pf = grossLoss > 0 ? sum(wins) / grossLoss : null;
  const holds = rows.map(t => t.hold_seconds).filter((v): v is number => v != null);
  const avgHold = holds.length ? sum(holds) / holds.length / 86400 : null;
  const rs = rows.flatMap(t => {
    if (t.realized_pnl == null || t.entry_fill_price == null || t.stop_price == null) return [];
    const q = t.quantity_filled ?? t.quantity_requested;
    const risk = Math.abs(t.entry_fill_price - t.stop_price) * (q ?? 0);
    return risk > 0 ? [t.realized_pnl / risk] : [];
  });
  const expR = rs.length ? sum(rs) / rs.length : null;
  const ex = { stop: 0, time: 0, manual: 0, other: 0 };
  rows.forEach(t => { ex[exitBucket(t.exit_reason)]++; });
  const rets = rows.map(t => t.realized_pnl_pct).filter((v): v is number => v != null);

  return <Section title="Trade stats" meta={meta}>
    <div className={css["statsWrap"]}>
      <div className={css["statsMain"]}>
        <FigureRow>
          <Figure label="Win rate" value={pnl.length ? pct(wins.length / pnl.length * 100, { sign: false, digits: 1 }) : "—"} />
          <Figure label="Avg win" value={money(avgWin, { sign: true })} tone={tone(avgWin)} />
          <Figure label="Avg loss" value={money(avgLoss, { sign: true })} tone={tone(avgLoss)} />
          <Figure label="Expectancy per trade" value={money(expectancy, { sign: true })} tone={tone(expectancy)}
            title={expR != null ? `${signed(expR)} R across ${rs.length} trades with a recorded stop` : "R needs a recorded stop price"} />
          <Figure label="Profit factor" value={pf != null ? num(pf) : "—"} title={pf == null ? "No losing trades yet" : undefined} />
          <Figure label="Avg hold" value={avgHold != null ? `${num(avgHold, 1)} d` : "—"} />
        </FigureRow>
        <p className={css["exits"]}>
          Exits: <span className="mono">{ex.stop}</span> stop · <span className="mono">{ex.time}</span> time ·{" "}
          <span className="mono">{ex.manual}</span> manual · <span className="mono">{ex.other}</span> other
          {expR != null && <> · Expectancy <span className="mono">{signed(expR)} R</span></>}
        </p>
      </div>
      <div className={css["statsHist"]}>
        {n >= 10 && rets.length >= 10
          ? <Histogram values={rets} />
          : <p className={css["muted"]}>Too few closed trades for a distribution: {n} so far.</p>}
      </div>
    </div>
  </Section>;
}

function Histogram({ values }: { values: number[] }) {
  const lo = Math.min(...values, 0), hi = Math.max(...values, 0);
  const span = hi - lo || 1;
  const bins = Math.min(12, Math.max(5, Math.round(Math.sqrt(values.length) * 1.5)));
  const w = span / bins;
  const counts = Array.from({ length: bins }, () => 0);
  values.forEach(v => { counts[Math.min(bins - 1, Math.floor((v - lo) / w))]++; });
  const maxC = Math.max(...counts);
  const W = 360, H = 130, L = 4, B = 20, T = 6;
  const iw = W - L * 2, ih = H - B - T;
  const x = (v: number) => L + ((v - lo) / span) * iw;
  const y = (c: number) => T + ih - (c / maxC) * ih;
  return <figure className={css["hist"]}>
    <figcaption className={css["muted"]}>Distribution of trade returns, % per trade</figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Histogram of trade returns in percent">
      {[0.25, 0.5, 0.75].map(f => <line key={f} x1={L} x2={W - L} y1={T + ih * f} y2={T + ih * f} stroke="var(--rule)" strokeWidth="1" opacity="0.6" />)}
      {counts.map((c, i) => {
        if (!c) return null;
        const a = lo + i * w, b = a + w;
        const fill = a >= 0 ? "var(--gain)" : b <= 0 ? "var(--loss)" : "var(--ink-2)";
        return <rect key={i} x={x(a) + 1} y={y(c)} width={Math.max(1, x(b) - x(a) - 2)} height={T + ih - y(c)} fill={fill} opacity="0.85"><title>{`${num(a, 1)}% to ${num(b, 1)}%: ${c}`}</title></rect>;
      })}
      <line x1={x(0)} x2={x(0)} y1={T} y2={T + ih} stroke="var(--ink)" strokeWidth="1" />
      <line x1={L} x2={W - L} y1={T + ih} y2={T + ih} stroke="var(--rule)" strokeWidth="1" />
      <text x={L} y={H - 5} fontSize="11" fill="var(--ink-2)" className="mono">{signed(lo, 1)}%</text>
      <text x={x(0)} y={H - 5} fontSize="11" fill="var(--ink-2)" textAnchor="middle" className="mono">0</text>
      <text x={W - L} y={H - 5} fontSize="11" fill="var(--ink-2)" textAnchor="end" className="mono">{signed(hi, 1)}%</text>
    </svg>
  </figure>;
}
>>>>>>> 2e32c5e54414a1984ef1cfefe57d83289c20ffb5
