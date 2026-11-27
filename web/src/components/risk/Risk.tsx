"use client";
import { useState } from "react";
import type { Api } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import type { RiskReport } from "@/lib/types";
import { num, pct, etDateTime, isOccSymbol, occLabel } from "@/lib/format";
import Section from "../ui/Section";
import Empty from "../ui/Empty";
import { Figure, FigureRow } from "../ui/Figure";
import { sleeveColor, sortSleeves } from "../strategies/sleeveColors";
import OptionsWhatIf from "./OptionsWhatIf";
import css from "./Risk.module.css";
import { ageText, neutralClass, shortDate, tone, usd } from "./heat";

const loss = (v: number | null | undefined) => (v == null ? "—" : v === 0 ? "$0" : usd(-Math.abs(v)));
const lossTone = (v: number | null | undefined) => (v != null && v > 0 ? "loss" as const : undefined);
const sub = (p: number | null | undefined) => (p == null ? undefined : <span className={css["figSub"]}>{pct(Math.abs(p), { sign: false, digits: 1 })} of equity</span>);
const stressLabel = (s: { name: string; kind: string; date: string | null; spy_pct: number | null }) =>
  s.kind === "beta_shock" && s.spy_pct != null ? `Market ${pct(s.spy_pct, { digits: 0 })}` :
  s.kind === "historical" && s.date ? `Worst day: ${shortDate(s.date, true)}` : s.name;

function Corr({ ids, m, names }: { ids: string[]; m: (number | null)[][]; names: Record<string, string> }) {
  return <div className={css["scroll"]}><table className={css["hm"]}>
    <thead><tr><th />{ids.map(i => <th key={i} scope="col">{names[i] ?? i}</th>)}</tr></thead>
    <tbody>{ids.map((id, r) => <tr key={id}><th scope="row">{names[id] ?? id}</th>
      {ids.map((_, c) => {
        const v = m[r]?.[c] ?? null;
        return <td key={c} className={neutralClass(v, 1)}>{v == null ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}`}</td>;
      })}
    </tr>)}</tbody>
  </table></div>;
}

export default function Risk({ api }: { api: Api }) {
  const poll = usePoll<RiskReport>(api, "/api/risk", 300_000);
  const [fresh, setFresh] = useState<RiskReport | null>(null);
  const [rk, setRk] = useState(0);
  const [busy, setBusy] = useState(false);
  const [rerr, setRerr] = useState<string | null>(null);
  const { error, loading, reload } = poll;
  const data = fresh && (!poll.data || (fresh.as_of ?? "") >= (poll.data.as_of ?? "")) ? fresh : poll.data;

  const recompute = async () => {
    setBusy(true); setRerr(null);
    try { setFresh(await api<RiskReport>("/api/risk?refresh=1")); setRk(k => k + 1); }
    catch (e) { setRerr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  if (loading && !data) return <div className={css["root"]}><p className={css["muted"]}>Loading risk report…</p></div>;
  if (!data) return <div className={css["root"]}><p className={css["loss"]}>Could not load the risk report{error ? `: ${error.message}` : "."}</p>
    <div><button type="button" className={css["btn"]} onClick={reload}>Retry</button></div></div>;
  if (data.error || !data.account) return <div className={css["root"]}>
    <p className={css["loss"]}>The risk report could not be calculated.{data.as_of ? ` As of ${etDateTime(data.as_of)} ET.` : ""}</p>
    {data.warnings?.length > 0 && <ul className={css["warnings"]}>{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
    <div><button type="button" className={css["btn"]} disabled={busy} onClick={recompute}>{busy ? "Recomputing…" : "Recompute"}</button>
      {rerr && <span className={css["loss"]}> Recompute failed: {rerr}</span>}</div>
  </div>;

  const a = data.account;
  const sleeves = sortSleeves(data.sleeves ?? []);
  const names = Object.fromEntries(sleeves.map(s => [s.id, s.name]));
  const maxC = Math.max(1, ...sleeves.map(s => Math.abs(s.contribution_pct ?? 0)));
  const shared = new Set((data.concentration?.shared_symbols ?? []).map(s => s.symbol));
  const cor = data.correlation;
  const stress = data.stress ?? [];
  const nCor = sleeves.filter(s => (s.n_positions ?? 0) > 0).length;
  const stressCols = sleeves.filter(x => (x.n_positions ?? 0) > 0 || stress.some(s => (s.by_sleeve?.[x.id] ?? 0) !== 0));
  const maxPnl = Math.max(1, ...stress.map(s => Math.abs(s.pnl ?? 0)));
  const ddIds = Object.keys(data.drawdowns ?? {});

  return <div className={css["root"]}>
    <Section title="How much could I lose?" meta="One day, using the past year of moves on what I hold today">
      {a.var95 != null && <p className={css["sub"]}>On a bad day (1 in 20) the account could lose about {usd(Math.abs(a.var95))}{a.var95_pct != null ? ` (${pct(Math.abs(a.var95_pct), { sign: false, digits: 1 })} of equity)` : ""}.</p>}
      <div className={css["strip"]}>
        <FigureRow>
          <Figure label="On a bad day (95%)" value={<>{loss(a.var95)}{sub(a.var95_pct)}</>} tone={lossTone(a.var95)} />
          <Figure label="On a very bad day (99%)" value={<>{loss(a.var99)}{sub(a.equity && a.var99 != null ? a.var99 / a.equity * 100 : null)}</>} tone={lossTone(a.var99)} />
          <Figure label="Average of the worst days" value={<>{loss(a.es95)}{sub(a.equity && a.es95 != null ? a.es95 / a.equity * 100 : null)}</>} tone={lossTone(a.es95)} title="Expected shortfall at 95%" />
          <Figure label="Yearly ups and downs" value={pct(a.vol_ann_pct, { sign: false, digits: 1 })} />
          <Figure label="Move vs. the market (beta)" value={num(a.beta_spy)} title="1.00 moves like SPY" />
        </FigureRow>
      </div>
      <div className={css["stamp"]}>
        <span>As of {etDateTime(data.as_of)} ET{data.cached ? ` (computed ${ageText(data.as_of) ?? "earlier"})` : ""}</span>
        <button type="button" className={css["btn"]} disabled={busy} onClick={recompute}>{busy ? "Recomputing…" : "Recompute"}</button>
        {rerr && <span className={css["loss"]}>Recompute failed: {rerr}</span>}
        {error && <span className={css["warn"]}>Showing last good data; refresh failed.</span>}
      </div>
    </Section>

    <Section title="Where the risk comes from" meta="Share of the account's ups and downs; a minus sign means it offsets other risk">
      {sleeves.length === 0 ? <Empty>No sleeve holdings to measure.</Empty> : <div className={css["bars"]}>
        {sleeves.map(s => {
          const c = s.contribution_pct ?? 0, w = Math.min(50, Math.abs(c) / maxC * 50);
          return <div key={s.id} className={css["barRow"]}>
            <span>{s.name}</span>
            <span className={css["barTrack"]} role="img" aria-label={`${s.name} ${pct(c, { digits: 1 })}`}>
              <span className={css["barMid"]} />
              <span className={css["bar"]} style={{ background: sleeveColor(s.id), width: `${w}%`, left: c >= 0 ? "50%" : `${50 - w}%` }} />
            </span>
            <span className={css["barVal"]}>{pct(c, { digits: 1 })}</span>
          </div>;
        })}
      </div>}
      {(data.holdings?.length ?? 0) > 0 && <div className={css["scroll"]}><table className={css["tbl"]}>
        <thead><tr><th>Holding</th><th>Sleeves</th><th className={css["n"]}>Value</th><th className={css["n"]}>Weight</th><th className={css["n"]}>Yearly vol</th><th className={css["n"]}>Beta</th><th className={css["n"]}>Risk share</th></tr></thead>
        <tbody>{data.holdings.map(h => <tr key={h.symbol}>
          <td>{isOccSymbol(h.symbol) ? occLabel(h.symbol) : h.symbol}{shared.has(h.symbol) && <span className={css["flag"]}>held by several sleeves</span>}</td>
          <td>{h.sleeves.map(s => names[s] ?? s).join(", ")}</td>
          <td className={css["n"]}>{usd(h.market_value)}</td>
          <td className={css["n"]}>{pct(h.weight_pct, { sign: false, digits: 1 })}</td>
          <td className={css["n"]}>{pct(h.vol_ann_pct, { sign: false, digits: 1 })}</td>
          <td className={css["n"]}>{num(h.beta_spy)}</td>
          <td className={css["n"]}>{pct(h.contribution_pct, { digits: 1 })}</td>
        </tr>)}</tbody></table></div>}
      {data.concentration?.top5_pct != null && <p className={`${css["sub"]} ${css["gap"]}`}>The 5 biggest holdings are {pct(data.concentration.top5_pct, { sign: false, digits: 1 })} of the account.</p>}
    </Section>

    <Section title="How the strategies move together" meta="+1 move in step, −1 move opposite">
      {nCor >= 2 && cor?.ids?.length && cor.holdings ? <>
        <p className={css["h3"]}>Based on the holdings I have today, over the past year</p>
        <Corr ids={cor.ids} m={cor.holdings} names={names} />
        {cor.live ? <><p className={css["h3"]}>Based on live results</p><Corr ids={cor.ids} m={cor.live} names={names} /></>
          : <p className={`${css["sub"]} ${css["gap"]}`}>Live results: needs 20 days of live data, have {cor.live_days ?? 0}.</p>}
      </> : <Empty>Strategies can only be compared when at least two of them hold something.</Empty>}
    </Section>

    <Section title="What if the market falls?" meta="Estimated profit or loss on today's holdings">
      {stress.length === 0 ? <Empty>No stress scenarios available.</Empty> : <div className={css["scroll"]}><table className={css["tbl"]}>
        <thead><tr><th>Scenario</th><th className={css["n"]}>Market</th><th className={css["n"]}>Account</th><th className={css["n"]} title="% of equity">%</th>
          {stressCols.map(s => <th key={s.id} className={css["n"]}>{s.name}</th>)}</tr></thead>
        <tbody>{stress.map(s => <tr key={`${s.kind}:${s.name}`}>
          <td>{stressLabel(s)}</td>
          <td className={`${css["n"]} ${tone(s.spy_pct)}`}>{pct(s.spy_pct, { digits: 1 })}</td>
          <td className={css["n"]}><span className={css["acct"]}><span className={tone(s.pnl)}>{usd(s.pnl, true)}</span><span className={css["miniBar"]} style={{ width: `${Math.round(Math.abs(s.pnl ?? 0) / maxPnl * 80)}px` }} /></span></td>
          <td className={`${css["n"]} ${tone(s.pnl_pct)}`}>{pct(s.pnl_pct, { digits: 1 })}</td>
          {stressCols.map(x => { const v = s.by_sleeve?.[x.id]; return <td key={x.id} className={`${css["n"]} ${tone(v)}`}>{usd(v, true)}</td>; })}
        </tr>)}</tbody></table></div>}
    </Section>

    <OptionsWhatIf api={api} refreshKey={rk} names={names} />

    <Section title="Worst drawdowns" meta="Biggest drops from a peak in live results">
      {ddIds.every(id => !data.drawdowns[id]?.length) ? <Empty>No drawdowns recorded yet.</Empty> : <div className={css["scroll"]}><table className={css["tbl"]}>
        <thead><tr><th>Sleeve</th><th>Peak</th><th>Bottom</th><th>Recovered</th><th className={css["n"]}>Drop</th><th className={css["n"]}>Days</th></tr></thead>
        <tbody>{ddIds.flatMap(id => (data.drawdowns[id] ?? []).map((d, i) => <tr key={`${id}:${i}`}>
          <td>{names[id] ?? id}</td><td>{shortDate(d.peak)}</td><td>{shortDate(d.trough)}</td><td>{d.recovery ? shortDate(d.recovery) : "not yet"}</td>
          <td className={`${css["n"]} ${css["loss"]}`}>{pct(d.depth_pct, { digits: 1 })}</td><td className={css["n"]}>{d.days ?? "—"}</td>
        </tr>))}</tbody></table></div>}
    </Section>

    {data.warnings?.length > 0 && <ul className={css["warnings"]}>{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
    {data.method && <p className={css["sub"]}>{data.method}</p>}
  </div>;
}
