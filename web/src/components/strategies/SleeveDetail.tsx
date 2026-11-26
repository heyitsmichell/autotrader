"use client";

import type { ReactNode } from "react";
import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { ConfirmRequest } from "@/components/ConfirmModal";
import { dateTime, money, num, pct } from "@/lib/format";
import type { Sleeve, SleeveDetail as Detail } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Segmented from "@/components/ui/Segmented";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? css["gain"] : css["loss"]);

export default function SleeveDetail({ api, sleeves, id, onSelect, middle, onConfirm, onAction }: {
  api: Api; sleeves: Sleeve[];
  id: string; onSelect: (id: string) => void; middle?: ReactNode;
  onConfirm?: (r: ConfirmRequest | null) => void;
  onAction?: (label: string, fn: () => Promise<unknown>) => void | Promise<void>;
}) {
  const poll = usePoll<Detail>(api, id ? `/api/sleeves/${id}` : null, 3_600_000);
  const data = poll.data;
  const err = poll.error ? (poll.error.message || "Could not load this strategy.") : "";

  const sel = sleeves.find(s => s.id === id);
  if (!sel) return null;
  const d = data && data.id === id ? data : null;
  const paused = (d ?? sel).status === "paused";

  const ask = (r: ConfirmRequest) => (onConfirm ? onConfirm(r) : void r.onConfirm());
  const run = (label: string, fn: () => Promise<unknown>) => {
    const wrapped = async () => { await fn(); poll.reload(); };
    return onAction ? onAction(label, wrapped) : wrapped();
  };
  const togglePause = () => ask({
    title: paused ? `Resume ${sel.name}?` : `Pause ${sel.name}?`,
    confirmLabel: paused ? "Resume" : "Pause",
    message: <p>{paused ? "This strategy may place orders again." : "This strategy stops placing new orders. Existing positions stay open."}</p>,
    onConfirm: async () => { onConfirm?.(null); await run(paused ? `${sel.name} resumed` : `${sel.name} paused`,
      () => api(`/api/sleeves/${id}/pause`, "POST", { on: !paused })); },
  });
  const rebalance = () => ask({
    title: `Rebalance ${sel.name} now?`,
    confirmLabel: "Rebalance now",
    message: <p>Orders are placed now to bring this strategy back to its target weights.</p>,
    onConfirm: async () => { onConfirm?.(null); await run(`${sel.name} rebalance`,
      () => api(`/api/sleeves/${id}/rebalance`, "POST")); },
  });

  return <div className={css["detail"]}>
    <div className={css["segwrap"]}>
      <Segmented label="Strategy" value={id} onChange={onSelect}
        options={sleeves.map(s => ({ value: s.id, label: s.name }))}/>
    </div>
    <p className={css["rule"]}><i className={css["chip"]} style={{ background: sleeveColor(sel.id, sel.color_slot) }}/>
      {sel.description}</p>
    {middle}
    {err && <p className={css["muted"]}>{err}</p>}
    {!d && !err && <p className={css["muted"]}>Loading…</p>}
    {d && <>
      <h3>Holdings</h3>
      {d.holdings?.length ? <div className={css["scroll"]}><table className={css["dtbl"]}>
        <thead><tr><th>Symbol</th><th>Qty</th><th>Last</th><th>Value</th><th>Weight / target</th><th>Unrealized $</th><th>Unrealized %</th></tr></thead>
        <tbody>{d.holdings.map(h => <tr key={h.symbol}>
          <td>{h.symbol}</td><td>{num(h.qty, 0)}</td><td>{money(h.last)}</td><td>{money(h.market_value)}</td>
          <td>{h.weight_pct == null ? "—" : pct(h.weight_pct, { sign: false, digits: 1 })} / {h.target_weight_pct == null ? "—" : pct(h.target_weight_pct, { sign: false, digits: 1 })}</td>
          <td className={tone(h.unrealized_pnl)}>{money(h.unrealized_pnl, { sign: true })}</td>
          <td className={tone(h.unrealized_pct)}>{pct(h.unrealized_pct)}</td>
        </tr>)}</tbody></table></div> : <Empty>No holdings.</Empty>}

      <h3>Targets</h3>
      {d.targets?.length ? <div className={css["scroll"]}><table className={css["dtbl"]}>
        <thead><tr><th>Symbol</th><th>Target weight</th><th>Reason</th></tr></thead>
        <tbody>{d.targets.map(t => <tr key={t.symbol}><td>{t.symbol}</td>
          <td>{pct(t.weight_pct, { sign: false, digits: 1 })}</td><td>{t.reason || "—"}</td></tr>)}</tbody>
      </table></div> : <Empty>No targets right now.</Empty>}

      <h3>Last 20 orders</h3>
      {d.orders?.length ? <div className={css["scroll"]}><table className={css["dtbl"]}>
        <thead><tr><th>Time</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Status</th></tr></thead>
        <tbody>{d.orders.slice(0, 20).map((o, i) => <tr key={`${o.ts}-${i}`}>
          <td>{dateTime(o.ts)}</td><td>{o.side}</td><td>{o.symbol}</td><td>{num(o.qty, 0)}</td>
          <td>{money(o.avg_fill_price ?? o.limit_price)}</td><td>{o.status}</td></tr>)}</tbody>
      </table></div> : <Empty>No orders yet.</Empty>}

      <h3>Rule</h3>
      <p className={css["rule"]}>{d.rebalance_rule || "—"}</p>
    </>}
    <div className={css["actions"]}>
      <button className="button" onClick={togglePause}>{paused ? "Resume" : "Pause"}</button>
      {sel.id !== "swing" && <button className="button" onClick={rebalance}>Rebalance now</button>}
    </div>
  </div>;
}
