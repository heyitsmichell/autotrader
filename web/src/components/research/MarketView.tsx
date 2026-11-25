"use client";

import type { Api } from "@/lib/api";
import type { RegimePayload } from "@/lib/types";
import { num, pct } from "@/lib/format";
import Section from "@/components/ui/Section";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Mark from "@/components/ui/Mark";
import { useResearch } from "./useResearch";
import css from "./Research.module.css";

export default function MarketView({ api }: { api: Api }) {
  const { data, error } = useResearch<RegimePayload>(api, "/api/research/regime");
  if (error) return <p className={css["muted"]}>{error}</p>;
  if (!data) return <p className={css["muted"]}>Loading…</p>;
  const { spy, vix, breadth, swing_gate: g } = data;
  return <Section title="Market">
    <div className={css["view"]}>
      <FigureRow>
        <Figure label="SPY vs 200-day" value={spy ? <>{num(spy.last)} <Mark tone={spy.above_200 ? "gain" : "loss"}>{spy.above_200 ? "above" : "below"}</Mark></> : "—"}
          title={spy ? `200-day ${num(spy.sma200)}` : undefined}/>
        <Figure label="VIX" value={vix ? num(vix.last, 1) : "—"}/>
        <Figure label="VIX, 1y percentile" value={vix ? pct(vix.pct_rank_1y * 100, { sign: false, digits: 0 }) : "—"}/>
        <Figure label="Above 200-day" value={breadth ? pct(breadth.pct_above_200 * 100, { sign: false, digits: 0 }) : "—"} title={breadth ? `${breadth.n} stocks` : undefined}/>
        <Figure label="VAA bad assets" value={data.vaa_bad == null ? "—" : data.vaa_bad}/>
      </FigureRow>
      {g ? <>
        <p className={css["sentence"]}>Swing gate: {g.watched} watched, {g.eligible} eligible, {g.pass} pass.</p>
        {(g.top_blockers ?? []).length > 0 && <div className={css["wrap"]}><table>
          <thead><tr><th scope="col">Top blockers</th><th scope="col" className="num">Symbols</th></tr></thead>
          <tbody>{(g.top_blockers ?? []).map(b => <tr key={b.name}><th scope="row">{b.name}</th><td className="num">{b.n}</td></tr>)}</tbody>
        </table></div>}
      </> : <p className={css["muted"]}>No swing gate data yet.</p>}
    </div>
  </Section>;
}
