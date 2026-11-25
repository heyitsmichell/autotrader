"use client";

import type { Api } from "@/lib/api";
import type { MomentumPayload } from "@/lib/types";
import { pct } from "@/lib/format";
import Section from "@/components/ui/Section";
import Mark from "@/components/ui/Mark";
import { useResearch } from "./useResearch";
import css from "./Research.module.css";

export default function MomentumView({ api }: { api: Api }) {
  const { data, error } = useResearch<MomentumPayload>(api, "/api/research/momentum");
  if (error) return <p className={css["muted"]}>{error}</p>;
  if (!data) return <p className={css["muted"]}>Loading…</p>;
  const rows = data.rows ?? [];
  const n = data.n_target ?? 10;
  const buy = rows.filter(r => r.in_target && !r.held).map(r => r.symbol);
  const sell = rows.filter(r => r.held && !r.in_target).map(r => r.symbol);
  return <Section title="Momentum ranking" meta={data.universe_n ? `${data.universe_n} stocks, 12-1 month return` : undefined}>
    <div className={css["view"]}>
      <p className={css["sentence"]}>Next rebalance {data.next_rebalance || "date unknown"}: would buy {buy.length ? buy.join(", ") : "nothing"}, would sell {sell.length ? sell.join(", ") : "nothing"}.</p>
      {rows.length === 0 ? <p className={css["muted"]}>No ranking yet.</p> :
      <div className={css["wrap"]}><table>
        <thead><tr>
          <th scope="col" className="num">Rank</th><th scope="col">Symbol</th><th scope="col" className="num">12-1 return</th>
          <th scope="col" className="num">Gap to cutoff</th><th scope="col">Held</th><th scope="col">In target</th>
        </tr></thead>
        <tbody>{rows.map(r => <tr key={r.symbol} className={r.rank === n ? css["cut"] : undefined}>
          <td className="num">{r.rank}</td>
          <th scope="row">{r.symbol}</th>
          <td className="num">{pct(r.score * 100)}</td>
          <td className={`num ${r.gap_to_cutoff > 0 ? css["gain"] : r.gap_to_cutoff < 0 ? css["loss"] : ""}`}>{pct(r.gap_to_cutoff * 100)}</td>
          <td>{r.held ? <Mark>held{r.held_qty ? ` ${r.held_qty}` : ""}</Mark> : ""}</td>
          <td>{r.in_target ? "yes" : ""}</td>
        </tr>)}</tbody>
      </table></div>}
    </div>
  </Section>;
}
