"use client";

/**
 * Pending proposals (QA M15/§7): one queue and one card for equities and options. Only the
 * `<Countdown>` leaf ticks every second, so the list itself re-renders only on data changes.
 */

import { memo, useEffect, useMemo, useState } from "react";
import { countdown, money, num, occLabel, pct, proposalExpiry } from "@/lib/format";
import { instrumentOf } from "./options/selection";
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
  if (isOptionProposal(p)) return `buy ${p.quantity}`;
  if (!isOptionProposal(p)) return `${p.quantity} share${p.quantity === 1 ? "" : "s"}`;
  const strike = p.strike_price != null ? money(p.strike_price) : "";
  return `${p.quantity} contract${p.quantity === 1 ? "" : "s"} ×${multiplierOf(p)} · ${strike} ${p.option_type || ""} exp ${p.expiration_date || "—"}`;
}

function OptionDetail({ p, settings }: { p: Proposal; settings?: Status["settings"] }) {
  const sel = instrumentOf(p).selection;
  const stop = instrumentOf(p).underlying_stop ?? p.sl;
  const bits: string[] = [];
  if (sel?.delta != null) bits.push(`${num(sel.delta, 2)}Δ`);
  if (sel?.dte != null) bits.push(`${sel.dte} days`);
  if (sel?.iv_rank != null) bits.push(`IV rank ${num(sel.iv_rank, 2)}`);
  if (sel?.ml_version && sel?.ml_p != null) bits.push(`ML ${num(sel.ml_p, 2)}${sel.explore ? " · explore" : ""}`);
  else if (sel?.explore) bits.push("explore");
  if (sel?.spread_pct != null) bits.push(`spread ${pct(sel.spread_pct, { sign: false, digits: 1 })}`);
  bits.push(`max loss ${money(p.limit_price * multiplierOf(p) * p.quantity)}`);
  const exits: string[] = [];
  if (stop != null && settings?.options_exit_underlying_stop !== false) exits.push(`underlying stop ${money(stop)}`);
  if (settings?.options_exit_stop_pct) exits.push(`−${Math.round(settings.options_exit_stop_pct * 100)}% premium stop`);
  if (settings?.options_exit_take_profit_pct) exits.push(`+${Math.round(settings.options_exit_take_profit_pct * 100)}% take-profit`);
  const hold = Number(settings?.exit_max_hold_days);
  if (hold > 0) exits.push(`${hold}-day time stop`);
  return <>
    <p className={`${css["gate"]} mono`}>{bits.join(" · ")}</p>
    {exits.length > 0 && <p className={css["gate"]}>Exits: {exits.join(" · ")}</p>}
  </>;
}

export const ProposalCard = memo(function ProposalCard({
  p, ttl, duplicate, busy, api, status, onAction, onOpen, onApprove,
}: {
  p: Proposal;
  status?: Status | null;
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

  const gateLine = (p.rationale || "").split(/(?<=[.!?])\s/)[0] || "No rationale supplied.";
  return <article className={`${css["item"]} ${expired && !quote ? "expired" : ""}`}>
    <div className={css["line"]}>
      <button className={css["sym"]} onClick={() => onOpen(p)} title="Open the decision record">{isOptionProposal(p) ? occLabel(p.symbol) : p.symbol}</button>
      {isOptionProposal(p)
        ? <span className="mono">· {p.side.toLowerCase()} {p.quantity} @ {money(shown.limit_price)}</span>
        : <><span className={p.side === "BUY" ? css["buy"] : css["sell"]}>{p.side.toLowerCase()}</span>
          <span className="mono">{sizeText(p)} @ {money(shown.limit_price)}</span></>}
    </div>
    {isOptionProposal(p) && <OptionDetail p={shown} settings={status?.settings}/>}
    <p className={css["gate"]}>{gateLine}{p.confidence != null && <> Confidence {Math.round(p.confidence * 100)}%.</>}</p>
    <div className={css["sub"]}>
      {expires != null && <Countdown expiresAt={expires} onExpire={() => setExpired(true)}/>}
      {duplicate && <span className={css["expiry-chip"]}>Duplicate of a newer {p.symbol} {p.side}</span>}
      <span>≈ {money(shown.limit_price * mult * p.quantity)}</span>
    </div>
    {quoteError && <div className="inline-warning broker-warning" role="alert">{quoteError}</div>}
    {quote && <div className={`${css["requote-box"]} ${quote.risk_ok ? "" : "failed"}`} role="status">
      <div className={css["requote-title"]}>Re-quoted at the current price: review before approving</div>
      <div className={css["requote-grid"]}>
        <span>Limit</span><b>{money(quote.previous.limit_price)} → {money(quote.proposal.limit_price)}</b>
        <span>Stop</span><b>{quote.proposal.sl == null ? "—" : `${money(quote.previous.sl)} → ${money(quote.proposal.sl)}`}</b>
        <span>Target</span><b>{quote.proposal.tp == null ? "—" : `${money(quote.previous.tp)} → ${money(quote.proposal.tp)}`}</b>
      </div>
      {!quote.risk_ok && <div className="inline-warning">Re-quoted proposal still fails risk: {quote.reasons.join("; ") || "see proposal detail"}</div>}
    </div>}
    <div className={css["actions"]}>
      <button className="button" disabled={busy}
        onClick={() => void onAction(`Proposal #${p.id} dismissed`, () => api(`/api/proposals/${p.id}/dismiss`, "POST"))}>Dismiss</button>
      {(expired || quote) && <button className="button" disabled={busy || requoting} onClick={requote}>
        {requoting ? "Re-quoting…" : "Re-quote"}
      </button>}
      <button
        className="button"
        disabled={busy || (expired && !quote)}
        title={expired && !quote ? "Expired proposals must be re-quoted before approval" : "Review and place this order"}
        onClick={() => onApprove({ proposal: shown, requote: quote, duplicate })}
      >Review & approve</button>
    </div>
  </article>;
});

export default function ProposalQueue({ kind, proposals, status, api, busy, onAction, onOpen, onApprove }: {
  kind: ProposalKind | "all";
  proposals: Proposal[];
  status: Status | null;
  api: Api;
  busy: boolean;
  onAction: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  onOpen: (proposal: Proposal) => void;
  onApprove: (request: ApproveRequest) => void;
  compact?: boolean; // accepted for compatibility; layout no longer differs
}) {
  const ttl = status?.limits?.proposal_ttl_minutes;
  const duplicates = useMemo(() => duplicateIds(proposals), [proposals]);

  return <section id={kind === "all" ? "action-queue" : `pending-${kind}`} aria-label="Pending proposals">
    {ttl == null && proposals.length > 0 && <div className="inline-warning" role="note">
      Proposal expiry is not configured, so stale proposals cannot be timed out on screen.
    </div>}
    {proposals.length === 0
      ? <p className="muted">Nothing waiting for review.</p>
      : <div className={css["list"]}>
        {proposals.map(p => <ProposalCard key={p.id} p={p} ttl={ttl} duplicate={duplicates.has(p.id)} busy={busy}
          api={api} status={status} onAction={onAction} onOpen={onOpen} onApprove={onApprove}/>)}
      </div>}
  </section>;
}
