"use client";

import type { Api } from "@/lib/api";
import type { OverlapRow, OverlapPayload } from "@/lib/types";
import { money, pct } from "@/lib/format";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import { sleeveColor } from "@/components/strategies/sleeveColors";
import { shadowColor } from "@/components/strategies/shadowColors";
import { useResearch } from "./useResearch";
import css from "./Research.module.css";

function OverlapTable({ rows }: { rows: OverlapRow[] }) {
  return <div className={css["wrap"]}><table>
    <thead><tr><th scope="col">Symbol</th><th scope="col" className="num">Total value</th><th scope="col" className="num">% of net</th><th scope="col">Held by</th></tr></thead>
    <tbody>{rows.map(r => <tr key={r.symbol}>
      <th scope="row">{r.symbol}</th>
      <td className="num">{money(r.total_value)}</td>
      <td className="num">{pct(r.pct_of_net == null ? null : r.pct_of_net * 100, { sign: false, digits: 1 })}</td>
      <td><div className={css["chips"]}>{(r.holders ?? []).map((h, i) => <span key={`${h.strategy}${i}`} className={css["chip"]}
        title={h.live ? "live" : "shadow"}>
        <span className={css["dot"]} style={{ background: h.live ? sleeveColor(h.strategy) : shadowColor(h.strategy, i) }}/>
        {h.strategy} {h.qty}</span>)}</div></td>
    </tr>)}</tbody>
  </table></div>;
}

export default function OverlapView({ api }: { api: Api }) {
  const { data, error } = useResearch<OverlapPayload>(api, "/api/research/overlap");
  if (error) return <p className={css["muted"]}>{error}</p>;
  if (!data) return <p className={css["muted"]}>Loading…</p>;
  const rows = data.rows ?? [];
  const sh = data.shadow_overlap ?? [];
  return <Section title="Overlap" meta="symbols held by more than one strategy">
    <div className={css["view"]}>
      {rows.length === 0 ? <Empty>No symbol is held by more than one strategy right now.</Empty> : <OverlapTable rows={rows}/>}
      <details className={css["details"]}>
        <summary>Live vs shadow overlaps ({sh.length})</summary>
        {sh.length === 0 ? <p className={css["muted"]}>No live holding overlaps a shadow strategy.</p> : <OverlapTable rows={sh}/>}
      </details>
    </div>
  </Section>;
}
