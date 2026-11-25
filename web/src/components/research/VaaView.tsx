"use client";

import type { Api } from "@/lib/api";
import type { VaaPayload } from "@/lib/types";
import { num, pct } from "@/lib/format";
import Section from "@/components/ui/Section";
import Mark from "@/components/ui/Mark";
import { useResearch } from "./useResearch";
import css from "./Research.module.css";

const list = (m?: Record<string, number>, asPct = false) => {
  const e = Object.entries(m ?? {}).filter(([, v]) => v);
  return e.length ? e.map(([k, v]) => `${k} ${asPct ? pct(v * 100, { sign: false, digits: 0 }) : v}`).join(", ") : "cash";
};

export default function VaaView({ api }: { api: Api }) {
  const { data, error } = useResearch<VaaPayload>(api, "/api/research/vaa");
  if (error) return <p className={css["muted"]}>{error}</p>;
  if (!data) return <p className={css["muted"]}>Loading…</p>;
  const bad = data.bad ?? 0;
  const B = data.B ?? 4;
  const off = data.offensive ?? [];
  return <Section title="VAA" meta={data.next_rebalance ? `next rebalance ${data.next_rebalance}` : undefined}>
    <div className={css["view"]}>
      <div className={css["meter"]}>
        <span className={css["sentence"]}>bad assets: {bad} of {off.length || 12} ({B} = fully defensive)</span>
        <div className={css["meterbar"]} aria-hidden="true">
          {Array.from({ length: B }, (_, i) => <span key={i} className={`${css["seg"]} ${i < bad ? css["segon"] : ""}`}/>)}
        </div>
      </div>
      <p className={css["sentence"]}>If it rebalanced today: {list(data.allocation_if_today, true)}.</p>
      <p className={css["sentence"]}>Holds now: {list(data.held)}.</p>
      <div className={css["wrap"]}><table>
        <thead><tr><th scope="col">Offensive</th><th scope="col" className="num">Score</th><th scope="col">Signal</th></tr></thead>
        <tbody>{off.map(o => <tr key={o.symbol}>
          <th scope="row">{o.symbol}</th><td className="num">{num(o.score, 3)}</td>
          <td>{o.bad ? <Mark tone="loss">bad</Mark> : ""}</td>
        </tr>)}</tbody>
      </table></div>
      <div className={css["wrap"]}><table>
        <thead><tr><th scope="col">Defensive</th><th scope="col" className="num">Score</th><th scope="col">Best</th></tr></thead>
        <tbody>{(data.defensive ?? []).map(d => <tr key={d.symbol}>
          <th scope="row">{d.symbol}</th><td className="num">{num(d.score, 3)}</td>
          <td>{d.symbol === data.best_defensive ? <Mark>best</Mark> : ""}</td>
        </tr>)}</tbody>
      </table></div>
    </div>
  </Section>;
}
