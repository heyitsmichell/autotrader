"use client";

import type { Api } from "@/lib/api";
import type { PutwritePut, PutwritePayload } from "@/lib/types";
import { money, num, pct } from "@/lib/format";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import { useResearch } from "./useResearch";
import css from "./Research.module.css";

function Track({ p, spot }: { p: PutwritePut; spot: number | null | undefined }) {
  if (spot == null) return null;
  // Fixed scale: spot-10% .. spot+10%, so distance is comparable between puts.
  const lo = spot * 0.9, hi = spot * 1.1;
  const x = (v: number) => `${3 + (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * 94}%`;
  const itm = spot < p.strike;
  return <svg className={css["track"]} height={20} role="img"
    aria-label={`Strike ${money(p.strike)}, spot ${money(spot)}`}>
    <line x1="3%" x2="97%" y1={10} y2={10} stroke="#3A3D43" strokeWidth={2}/>
    <line x1={x(p.strike)} x2={x(p.strike)} y1={3} y2={17} stroke="#E5596F" strokeWidth={2}/>
    <circle cx={x(spot)} cy={10} r={4} fill={itm ? "#E5596F" : "#ECEDEF"}/>
  </svg>;
}

export default function PutwriteView({ api }: { api: Api }) {
  const { data, error } = useResearch<PutwritePayload>(api, "/api/research/putwrite");
  if (error) return <p className={css["muted"]}>{error}</p>;
  if (!data) return <p className={css["muted"]}>Loading…</p>;
  const puts = data.puts ?? [];
  return <Section title="Put-write" meta={data.underlying ? `${data.underlying} ${money(data.spot)}` : undefined}>
    <div className={css["view"]}>
      {data.rule && <p className={css["muted"]}>{data.rule}</p>}
      {puts.length === 0 ? <Empty>No open put.</Empty> : puts.map(p => {
        const pnl = p.unrealized_pnl;
        return <div key={p.symbol} className={css["put"]}>
          <div className={css["puthead"]}><b>{p.symbol}</b><span>strike {money(p.strike)} · {p.contracts} contract{p.contracts === 1 ? "" : "s"}</span></div>
          <Track p={p} spot={data.spot}/>
          <div className={css["facts"]}>
            <span><small>Days to expiry</small>{p.dte}</span>
            <span><small>Premium received</small>{money(p.premium_received)}</span>
            <span><small>Mark</small>{p.mark == null ? "—" : money(p.mark)}</span>
            <span className={pnl == null || pnl === 0 ? "" : pnl > 0 ? css["gain"] : css["loss"]}><small>Unrealized P&amp;L</small>{money(pnl, { sign: true })}</span>
            <span><small>Delta / chance in the money</small>{p.delta == null ? "—" : num(p.delta, 2)} / {p.prob_itm == null ? "—" : pct(p.prob_itm * 100, { sign: false, digits: 0 })}</span>
            <span><small>Spot above strike</small>{p.distance_pct == null ? "—" : pct(p.distance_pct * 100, { sign: false, digits: 1 })}</span>
            <span><small>Collateral</small>{money(p.collateral)}</span>
          </div>
          {p.next_action && <p className={css["sentence"]}>{p.next_action}</p>}
        </div>;
      })}
    </div>
  </Section>;
}
