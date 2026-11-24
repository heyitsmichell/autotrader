"use client";

/**
 * Pending proposals (QA M15/§7): one queue and one card for equities and options. Only the
 * `<Countdown>` leaf ticks every second, so the list itself re-renders only on data changes.
 */

import { memo, useEffect, useMemo, useState } from "react";
import { countdown, dateTime, money, proposalExpiry } from "@/lib/format";
import type { Api } from "@/lib/api";
import type { Proposal, Status } from "@/lib/types";
import css from "./ProposalCard.module.css";

export type ProposalKind = "equity" | "option";

export type RequoteResult = {
  proposal: Proposal;
  previous: { limit_price: number; sl?: number | null; tp?: number | null };
  risk_ok: boolean;
  reasons: string[];
};

export interface ApproveRequest {
  proposal: Proposal;
  requote?: RequoteResult;
  duplicate: boolean;
}

export const isOptionProposal = (p: Proposal) => p.option_type != null;

/** Contracts carry a x100 multiplier; shares do not. */
export const multiplierOf = (p: Proposal) => (isOptionProposal(p) ? p.multiplier || 100 : 1);

/** Ids of older proposals that duplicate a newer one for the same instrument and side. */
export function duplicateIds(proposals: Proposal[]): Set<number> {
  const seen = new Map<string, Proposal[]>();
  for (const p of proposals) {
    const key = isOptionProposal(p)
      ? `${p.symbol}:${p.side}:${p.option_type}:${p.strike_price}:${p.expiration_date}:${p.position_intent}`
      : `${p.symbol}:${p.side}`;
    seen.set(key, [...(seen.get(key) || []), p]);
  }
  const dup = new Set<number>();
  for (const group of seen.values()) {
    if (group.length < 2) continue;
    const newest = group.reduce((a, b) => (a.id > b.id ? a : b));
    for (const p of group) if (p.id !== newest.id) dup.add(p.id);
  }
  return dup;
}

/** Re-renders itself every second; nothing else on the card does. */
export function Countdown({ expiresAt, onExpire }: { expiresAt: number; onExpire?: () => void }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const expired = now >= expiresAt;
  useEffect(() => { if (expired) onExpire?.(); }, [expired, onExpire]);
  if (expired) return <span className={`${css["expiry-chip"]} expired`}>Expired</span>;
  return <span className={`${css["expiry-chip"]}`} suppressHydrationWarning>Expires in {countdown(expiresAt, now)}</span>;
}

function sizeText(p: Proposal): string {
  if (!isOptionProposal(p)) return `${p.quantity} share${p.quantity === 1 ? "" : "s"}`;
  const strike = p.strike_price != null ? money(p.strike_price) : "";
  return `${p.quantity} contract${p.quantity === 1 ? "" : "s"} ×${multiplierOf(p)} · ${strike} ${p.option_type || ""} exp ${p.expiration_date || "—"}`;
}

export const ProposalCard = memo(function ProposalCard({
  p, ttl, duplicate, busy, api, onAction, onOpen, onApprove,
}: {
  p: Proposal;
  ttl?: number;
  duplicate: boolean;
  busy: boolean;
  api: Api;
  onAction: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  onOpen: (proposal: Proposal) => void;
  onApprove: (request: ApproveRequest) => void;
}) {
  const expires = proposalExpiry(p.created_at, ttl);
  const [expired, setExpired] = useState(() => expires != null && Date.now() >= expires);
  const [quote, setQuote] = useState<RequoteResult | undefined>();
  const [quoteError, setQuoteError] = useState("");
  const [requoting, setRequoting] = useState(false);
  const shown = quote?.proposal ?? p;
  const mult = multiplierOf(p);

  const requote = () => {
    setRequoting(true);
    void api<RequoteResult>(`/api/proposals/${p.id}/requote`, "POST")
      .then(result => { setQuote(result); setQuoteError(""); })
      .catch((e: unknown) => setQuoteError(e instanceof Error ? e.message : "Re-quote failed"))
      .finally(() => setRequoting(false));
  };

  return <article className={`${css["proposal-row"]} ${expired && !quote ? "expired" : ""}`}>
    <div className={`${css["side-badge"]} ${p.side === "BUY" ? "buy" : "sell"}`}>{p.side}</div>
    <div className={`${css["proposal-main"]}`}>
      <div className={`${css["proposal-title"]}`}>
        <b>{p.symbol}</b>
        {isOptionProposal(p) && <span className={`${css["side-badge"]} option`}>{p.position_intent || "OPTION"}</span>}
        <span>{sizeText(p)} @ {money(shown.limit_price)}</span>
        <small>≈ {money(shown.limit_price * mult * p.quantity)} · {dateTime(p.created_at)}</small>
      </div>
      <p>{p.rationale || "No rationale supplied."}</p>
      <div className={`${css["proposal-sub"]}`}>
        <span>Confidence {p.confidence == null ? "—" : `${Math.round(p.confidence * 100)}%`}</span>
        {expires != null && <Countdown expiresAt={expires} onExpire={() => setExpired(true)}/>}
        {duplicate && <span className={`${css["expiry-chip"]} warn`}>Duplicate of a newer {p.symbol} {p.side}</span>}
        <button className={`${css["text-button"]}`} onClick={() => onOpen(p)}>View decision record ↗</button>
      </div>
      {quoteError && <div className="inline-warning broker-warning" role="alert">{quoteError}</div>}
      {quote && <div className={`${css["requote-box"]} ${quote.risk_ok ? "" : "failed"}`} role="status">
        <div className={`${css["requote-title"]}`}>Re-quoted at the current price: review before approving</div>
        <div className={`${css["requote-grid"]}`}>
          <span>Limit</span><b>{money(quote.previous.limit_price)} → {money(quote.proposal.limit_price)}</b>
          <span>Stop</span><b>{quote.proposal.sl == null ? "—" : `${money(quote.previous.sl)} → ${money(quote.proposal.sl)}`}</b>
          <span>Target</span><b>{quote.proposal.tp == null ? "—" : `${money(quote.previous.tp)} → ${money(quote.proposal.tp)}`}</b>
        </div>
        {!quote.risk_ok && <div className="inline-warning">Re-quoted proposal still fails risk: {quote.reasons.join("; ") || "see proposal detail"}</div>}
      </div>}
    </div>
    <div className={`${css["proposal-actions"]}`}>
      <button className="button button-quiet" disabled={busy}
        onClick={() => void onAction(`Proposal #${p.id} dismissed`, () => api(`/api/proposals/${p.id}/dismiss`, "POST"))}>Dismiss</button>
      {(expired || quote) && <button className="button button-quiet" disabled={busy || requoting} onClick={requote}>
        {requoting ? "Re-quoting…" : "Re-quote"}
      </button>}
      <button
        className="button button-primary"
        disabled={busy || (expired && !quote)}
        title={expired && !quote ? "Expired proposals must be re-quoted before approval" : "Review and place this order"}
        onClick={() => onApprove({ proposal: shown, requote: quote, duplicate })}
      >Review & approve</button>
    </div>
  </article>;
});

export default function ProposalQueue({ kind, proposals, status, api, busy, onAction, onOpen, onApprove, compact = false }: {
  kind: ProposalKind | "all";
  proposals: Proposal[];
  status: Status | null;
  api: Api;
  busy: boolean;
  onAction: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  onOpen: (proposal: Proposal) => void;
  onApprove: (request: ApproveRequest) => void;
  compact?: boolean;
}) {
  const ttl = status?.limits?.proposal_ttl_minutes;
  const duplicates = useMemo(() => duplicateIds(proposals), [proposals]);
  const title = kind === "option" ? "Pending option proposals" : kind === "equity" ? "Pending proposals" : "Action queue";
  const eyebrow = kind === "option" ? "OPTION EXECUTION QUEUE" : kind === "equity" ? "EXECUTION QUEUE" : "NEEDS YOUR DECISION";

  return <section className={`panel ${compact ? "action-queue" : ""}`} id={kind === "all" ? "action-queue" : `pending-${kind}`}>
    <div className="panel-head">
      <div><div className="eyebrow">{eyebrow}</div><h2>{title} <span className="count-pill">{proposals.length}</span></h2></div>
      <span className="muted">Risk checks run again at approval</span>
    </div>
    {ttl == null && proposals.length > 0 && <div className="inline-warning" role="note">
      Proposal expiry is not configured, so stale proposals cannot be timed out on screen.
    </div>}
    {proposals.length === 0
      ? <div className="empty-state"><span aria-hidden="true">✓</span><p>Nothing waiting for review</p></div>
      : <div className={`${css["proposal-list"]}`}>
        {proposals.map(p => <ProposalCard key={p.id} p={p} ttl={ttl} duplicate={duplicates.has(p.id)} busy={busy}
          api={api} onAction={onAction} onOpen={onOpen} onApprove={onApprove}/>)}
      </div>}
  </section>;
}
