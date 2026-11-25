"use client";

import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import { dateTime, money } from "@/lib/format";
import { classifyProposal } from "@/lib/proposalStatus";
import type { Proposal, Run } from "@/lib/types";
import css from "./HistoryTable.module.css";

const TONE = { placed: "gain", filled: "gain", rejected: "loss", webull: "loss", failed: "loss", pending: "warn", submitting: "warn", unknown: "warn", orphan: "warn" } as const;

export default function HistoryTable({ proposals, lastRun, onOpen }: {
  proposals: Proposal[];
  lastRun?: Run;
  onOpen: (proposal: Proposal) => void;
}) {
  return <Section title="Proposal history" meta={`Latest AI run: ${lastRun ? dateTime(lastRun.started_at) : "none"}`}>
    {lastRun && <p className={css.run}>
      {lastRun.status === "error" ? "Analysis error" : lastRun.summary || `Analysis ${lastRun.status}`}
      <span className={css.muted}>
        {" · "}{lastRun.trigger}
        {lastRun.input_tokens != null ? ` · ${Number(lastRun.input_tokens).toLocaleString()} in / ${Number(lastRun.output_tokens || 0).toLocaleString()} out tokens` : ""}
        {lastRun.tool_calls ? ` · ${lastRun.tool_calls}` : ""}
      </span>
    </p>}
    {!proposals.length ? <Empty>No proposals yet. The bot records one each time it wants to trade, whether or not it goes through.</Empty> :
      <div className="table-scroll">
        <table>
          <thead><tr><th>Time</th><th>Trade</th><th>Status</th><th>Detail</th></tr></thead>
          <tbody>
            {proposals.map(p => {
              const state = classifyProposal(p);
              const tone = (TONE as Record<string, "gain" | "loss" | "warn">)[state.tone] || "neutral";
              return <tr key={p.id} className={css.clickable} onClick={() => onOpen(p)}>
                <td className="mono">{dateTime(p.created_at)}</td>
                <td className="mono">{p.side} {p.quantity} {p.symbol} @ {money(p.limit_price)}</td>
                <td><Mark tone={tone} title={state.detail}>{state.label}</Mark></td>
                <td className={css.detail}>{p.reason || p.rationale || p.broker_response || "—"}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>}
  </Section>;
}
