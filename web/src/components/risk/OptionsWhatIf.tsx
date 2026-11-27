"use client";
import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import type { RiskOptions } from "@/lib/types";
import { num, pct, etDateTime, occLabel } from "@/lib/format";
import Section from "../ui/Section";
import Segmented from "../ui/Segmented";
import Empty from "../ui/Empty";
import css from "./Risk.module.css";
import { compact, heatClass, tone, usd } from "./heat";

const qtyWords = (q: number | null) => (q == null ? "—" : q < 0 ? `short ${Math.abs(q)}` : `long ${q}`);

const ALL = "__all__";
const sg = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n)}%`;

export default function OptionsWhatIf({ api, refreshKey = 0, names = {} }: { api: Api; refreshKey?: number; names?: Record<string, string> }) {
  const poll = usePoll<RiskOptions>(api, "/api/risk/options", 300_000);
  const [fresh, setFresh] = useState<RiskOptions | null>(null);
  const [done, setDone] = useState(0);
  const [failed, setFailed] = useState(0);
  const busy = refreshKey > 0 && done !== refreshKey;
  const rerr = failed === refreshKey && refreshKey > 0;
  const { error, loading } = poll;
  const data = fresh && (!poll.data || (fresh.as_of ?? "") >= (poll.data.as_of ?? "")) ? fresh : poll.data;
  useEffect(() => {
    if (!refreshKey) return;
    let live = true;
    api<RiskOptions>("/api/risk/options?refresh=1")
      .then(d => { if (live) setFresh(d); })
      .catch(() => { if (live) setFailed(refreshKey); })
      .finally(() => { if (live) setDone(refreshKey); });
    return () => { live = false; };
  }, [api, refreshKey]);
  const [sel, setSel] = useState(ALL);
  const [hz, setHz] = useState("0");
  const title = "Options what-if";
  if (loading && !data) return <Section title={title}><p className={css["muted"]}>Loading…</p></Section>;
  if (error && !data) return <Section title={title}><p className={css["loss"]}>Could not load options risk: {error.message}</p></Section>;
  if (!data) return <Section title={title}><Empty>Options risk is not available right now.</Empty></Section>;
  if (data.error) return <Section title={title}>
    <p className={css["loss"]}>The options calculation failed, so nothing is shown.</p>
    {data.warnings?.length > 0 && <ul className={css["warnings"]}>{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
  </Section>;
  if (!data.positions?.length) return <Section title={title}><Empty>No options are held right now, so there is nothing to test.</Empty></Section>;

  const { grid, positions } = data;
  const legs = positions.filter(p => p.symbol === sel);
  const cur = legs[0];
  const h = Math.min(Number(hz), Math.max(grid.horizons.length - 1, 0));
  const cube = cur ? grid.pnl?.[cur.symbol] : grid.total;
  const rows = cube?.[h];
  const max = rows ? Math.max(1, ...rows.flat().map(Math.abs)) : 1;
  const shown = [...(cur ? legs : positions)].sort((x, y) => (names[x.sleeve] ?? x.sleeve).localeCompare(names[y.sleeve] ?? y.sleeve));

  return <Section title={title} meta={`Profit or loss vs. today · rate ${pct((data.rate ?? 0) * 100, { sign: false, digits: 1 })} · as of ${etDateTime(data.as_of)} ET${busy ? " · refreshing…" : ""}${rerr ? " · Options refresh failed" : ""}`}>
    <p className={css["sub"]}>What happens to my options if the price moves and volatility changes?</p>
    <div className={css["row"]}>
      <select className={css["sel"]} aria-label="Position" value={sel} onChange={e => setSel(e.target.value)}>
        <option value={ALL}>All options (total)</option>
        {positions.filter((p, i, a) => a.findIndex(q => q.symbol === p.symbol) === i).map(p => <option key={p.symbol} value={p.symbol}>{occLabel(p.symbol)}</option>)}
      </select>
      <Segmented label="Horizon" value={hz} onChange={setHz}
        options={grid.horizons.map((x, i) => ({ value: String(i), label: x.label }))} />
    </div>
    {rows ? <div className={css["scroll"]}>
      <table className={css["hm"]}>
        <thead><tr><th scope="col">Volatility ↓ / Price →</th>{grid.spot_pct.map(s => <th key={s} scope="col">{sg(s)}</th>)}</tr></thead>
        <tbody>{grid.iv_shift_pct.map((iv, i) => <tr key={iv}>
          <th scope="row">Vol {sg(iv)}</th>
          {(rows[i] ?? []).map((v, j) => <td key={j} className={heatClass(v, max)}>{compact(v)}</td>)}
        </tr>)}</tbody>
      </table>
    </div> : <Empty>No grid for this position.</Empty>}

    <h3 className={css["h3"]}>Greeks in dollars and cash if assigned</h3>
    <div className={css["scroll"]}>
      <table className={css["tbl"]}>
        <thead><tr><th>Position</th><th>Sleeve</th><th className={css["n"]}>Qty</th><th className={css["n"]}>Days left</th>
          <th className={css["n"]}>Delta $</th><th className={css["n"]}>Gamma $ / 1%</th><th className={css["n"]}>Vega $ / pt</th><th className={css["n"]}>Theta $ / day</th>
          <th className={css["n"]}>Breakeven</th><th className={css["n"]}>Max loss</th><th className={css["n"]}>Cash if assigned</th><th className={css["n"]}>Sleeve cash</th></tr></thead>
        <tbody>{shown.map((p, i) => {
          const short = p.assignment_cash != null && p.sleeve_cash != null && p.sleeve_cash < p.assignment_cash;
          return <tr key={`${p.sleeve}:${p.symbol}`}>
            <td>{occLabel(p.symbol)}{p.iv_source === "realized_est" && <span className={css["flag"]}>estimated vol</span>}</td>
            <td>{i === 0 || shown[i - 1].sleeve !== p.sleeve ? (names[p.sleeve] ?? p.sleeve) : ""}</td><td className={css["n"]}>{qtyWords(p.qty)}</td><td className={css["n"]}>{p.dte ?? "—"}</td>
            <td className={css["n"]}>{usd(p.delta_dollars, true)}</td>
            <td className={css["n"]}>{usd(p.gamma_dollars_1pct, true)}</td>
            <td className={css["n"]}>{usd(p.vega_dollars, true)}</td>
            <td className={`${css["n"]} ${tone(p.theta_dollars)}`}>{usd(p.theta_dollars, true)}</td>
            <td className={css["n"]}>{p.breakeven == null ? "—" : num(p.breakeven)}</td>
            <td className={css["n"]}>{p.max_loss == null ? "unlimited" : usd(p.max_loss)}</td>
            <td className={`${css["n"]} ${short ? css["warn"] : ""}`}>{p.assignment_cash == null ? "—" : usd(p.assignment_cash)}{short && <span className={css["flag"]}>not enough cash</span>}</td>
            <td className={css["n"]}>{usd(p.sleeve_cash)}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
    {data.warnings?.length > 0 && <ul className={css["warnings"]}>{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
  </Section>;
}
