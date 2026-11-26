"use client";

import { useMemo, useState } from "react";
import { usePoll } from "@/lib/usePoll";
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
  const [pick, setPick] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>("ret");

  const poll = usePoll<RoundTripsPayload>(api, "/api/results/round_trips?days=3650", 300_000);
  const data = poll.data;
  const error = !!poll.error;

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
