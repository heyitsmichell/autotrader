"use client";

import { etDayKey, money, num, occExpiry, occLabel, pct } from "@/lib/format";
import type { Position, Proposal } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Section from "@/components/ui/Section";
import { instrumentOf } from "./selection";

const n = (v: unknown): number | null => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));

/** Held option contracts. Cost and mark are per-share premium; one contract is 100 shares. */
export default function OptionPositions({ positions, proposals, autoEntry, budget }: {
  positions: Position[];
  proposals?: Proposal[];
  autoEntry: boolean | undefined;
  budget?: number | null;
}) {
  const stopFor = (sym: string): number | null => {
    const p = (proposals || []).filter(x => x.symbol === sym).sort((a, b) => b.id - a.id)[0];
    return p ? n(instrumentOf(p).underlying_stop ?? p.sl) : null;
  };
  const [ty, tm, td] = etDayKey().split("-").map(Number);
  const todayUtc = Date.UTC(ty, tm - 1, td);
  return <Section title="Options" meta={positions.length ? `${positions.length} open` : undefined}>
    {positions.length ? <div className="tablewrap"><table>
      <thead><tr><th scope="col">Contract</th><th scope="col" className="num">Qty</th><th scope="col" className="num">Cost</th>
        <th scope="col" className="num">Mark</th><th scope="col" className="num">P&amp;L</th><th scope="col" className="num">P&amp;L %</th>
        <th scope="col" className="num">Max loss</th><th scope="col" className="num">Days left</th><th scope="col" className="num">Underlying stop</th></tr></thead>
      <tbody>{positions.map(p => {
        const qty = Number(p.qty);
        const cost = n(p.cost);
        const mv = n(p.market_value);
        const mark = mv != null && qty ? mv / qty / 100 : null;
        const pnl = n(p.unrealized);
        const exp = occExpiry(p.symbol);
        const days = exp ? Math.round((exp.getTime() - todayUtc) / 86400000) : null;
        const stop = stopFor(p.symbol);
        const tone = pnl == null || pnl === 0 ? "" : pnl > 0 ? "gain" : "loss";
        return <tr key={p.symbol}>
          <td>{occLabel(p.symbol)}</td>
          <td className="num mono">{num(qty, 0)}</td>
          <td className="num mono">{cost == null ? "—" : money(cost)}</td>
          <td className="num mono">{mark == null ? "—" : money(mark)}</td>
          <td className={`num mono ${tone}`}>{pnl == null ? "—" : money(pnl, { sign: true })}</td>
          <td className={`num mono ${tone}`}>{p.unrealized_pct != null ? pct(p.unrealized_pct, { sign: true })
            : cost && mark != null ? pct((mark / cost - 1) * 100, { sign: true, digits: 1 }) : "—"}</td>
          <td className="num mono">{cost == null ? "—" : money(cost * qty * 100)}</td>
          <td className="num mono">{days == null ? "—" : days}</td>
          <td className="num mono">{stop == null ? "—" : money(stop)}</td>
        </tr>;
      })}</tbody>
    </table></div>
      : <Empty>{autoEntry === false ? "Options auto-entry is off."
        : `No option positions. Calls are bought on gate passes in the regular session up to ${budget != null ? money(budget) : "a set amount of"} premium per trade.`}</Empty>}
  </Section>;
}
