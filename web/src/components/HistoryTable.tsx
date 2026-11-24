"use client";

import { dateTime, money } from "@/lib/format";
import { classifyProposal } from "@/lib/proposalStatus";
import type { Proposal, Run } from "@/lib/types";
import css from "./HistoryTable.module.css";

export default function HistoryTable({ proposals, lastRun, onOpen }: {
  proposals: Proposal[];
  lastRun?: Run;
  onOpen: (proposal: Proposal) => void;
}) {
  return <section className="panel">
    <div className="panel-head">
      <div><div className="eyebrow">RECENT ACTIVITY</div><h2>Proposal history</h2></div>
      <span className="muted">Latest AI run: {lastRun ? dateTime(lastRun.started_at) : "none"}</span>
    </div>
    {lastRun && <div className={`${css["run-banner"]}`}>
      <span className={`status-dot ${lastRun.status === "error" ? "red" : lastRun.status === "ok" ? "green" : "amber"}`} aria-hidden="true"/>
      <div>
        <b>{lastRun.status === "error" ? "Analysis error" : lastRun.summary || `Analysis ${lastRun.status}`}</b>
        <small>
          {lastRun.trigger} · {dateTime(lastRun.started_at)}
          {lastRun.input_tokens != null ? ` · ${Number(lastRun.input_tokens).toLocaleString()} in / ${Number(lastRun.output_tokens || 0).toLocaleString()} out tokens` : ""}
          {lastRun.tool_calls ? ` · ${lastRun.tool_calls}` : ""}
        </small>
      </div>
    </div>}
    <div className="table-scroll">
      <table>
        <thead><tr><th>TIME</th><th>TRADE</th><th>STATUS</th><th>DETAIL</th></tr></thead>
        <tbody>
          {proposals.map(p => {
            const state = classifyProposal(p);
            return <tr key={p.id} className={`${css["clickable"]}`} onClick={() => onOpen(p)}>
              <td>{dateTime(p.created_at)}</td>
              <td><b>{p.side} {p.symbol}</b><small>{p.quantity} @ {money(p.limit_price)}</small></td>
              <td>
                <span className={`status-chip ${state.tone}`} title={state.detail}>{state.label}</span>
                {state.webull && <small>broker-side rejection</small>}
              </td>
              <td className="ellipsis">{p.reason || p.rationale || p.broker_response || "—"}</td>
            </tr>;
          })}
          {!proposals.length && <tr><td colSpan={4} className={`${css["empty-cell"]}`}>No proposals recorded yet.</td></tr>}
        </tbody>
      </table>
    </div>
  </section>;
}
