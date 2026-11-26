"use client";

import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { ConfirmRequest } from "@/components/ConfirmModal";
import { dateTime, money, num, pct } from "@/lib/format";
import type { Shadow, ShadowDetail as Detail } from "@/lib/types";
import css from "./Strategies.module.css";

export default function ShadowDetail({ api, shadow, onConfirm, onAction, onReset }: {
  api: Api; shadow: Shadow;
  onConfirm?: (r: ConfirmRequest | null) => void;
  onAction?: (label: string, fn: () => Promise<unknown>) => void | Promise<void>;
  onReset?: () => void;
}) {
  const id = shadow.id;
  // No background polling (one-shot load); reload() after a reset.
  const poll = usePoll<Detail>(api, `/api/shadows/${encodeURIComponent(id)}`, 3_600_000);
  const data = poll.data;
  const err = poll.error ? (poll.error.message || "Could not load this shadow strategy.") : "";

  const run = async () => {
    const wrapped = async () => {
      await api(`/api/shadows/${encodeURIComponent(id)}/reset`, "POST");
      poll.reload();
      onReset?.();
    };
    if (onAction) await onAction(`${shadow.name} reset`, wrapped); else await wrapped();
  };
  const reset = () => {
    const r: ConfirmRequest = {
      title: `Reset ${shadow.name}?`, confirmLabel: "Reset", danger: true,
      message: <p>Reset {shadow.name}? Its virtual holdings and history are wiped and it restarts with fresh virtual capital.</p>,
      onConfirm: async () => { onConfirm?.(null); await run(); },
    };
    if (onConfirm) onConfirm(r); else void r.onConfirm();
  };

  const d = data && data.id === id ? data : null;
  const holdings = Array.isArray(d?.holdings) ? d.holdings : [];
  const trades = Array.isArray(d?.trades) ? d.trades : [];
  return <div className={css["detail"]}>
    {err && <p className={css["muted"]}>{err}</p>}
    {!d && !err && <p className={css["muted"]}>Loading…</p>}
    {d && <>
      <h3>Holdings</h3>
      {holdings.length === 0 ? <p className={css["muted"]}>No virtual holdings.</p> : <div className={css["scroll"]}>
        <table className={css["dtbl"]}>
          <thead><tr><th>Symbol</th><th>Qty</th><th>Avg cost</th><th>Last</th><th>Value</th><th>Weight</th></tr></thead>
          <tbody>{holdings.map(h => <tr key={h.symbol}><td>{h.symbol}</td><td>{num(h.qty, 0)}</td>
            <td>{money(h.avg_cost)}</td><td>{money(h.last)}</td><td>{money(h.market_value)}</td>
            <td>{pct(h.weight_pct, { sign: false, digits: 1 })}</td></tr>)}</tbody>
        </table></div>}
      <h3>Last trades</h3>
      {trades.length === 0 ? <p className={css["muted"]}>No virtual trades yet.</p> : <div className={css["scroll"]}>
        <table className={css["dtbl"]}>
          <thead><tr><th>Time</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Reason</th></tr></thead>
          <tbody>{trades.map((t, i) => <tr key={`${t.ts}-${t.symbol}-${i}`}><td>{dateTime(t.ts)}</td>
            <td>{t.side}</td><td>{t.symbol}</td><td>{num(t.qty, 0)}</td><td>{money(t.price)}</td>
            <td>{t.reason || "—"}</td></tr>)}</tbody>
        </table></div>}
    </>}
    <p><button type="button" className={css["linkBtn"]} onClick={reset}>Reset</button></p>
  </div>;
}
