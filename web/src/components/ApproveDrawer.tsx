"use client";

/**
 * Always-on approval review (QA §7). Every approval passes through this sheet: size in shares or
 * contracts x100, notional, max loss, stop/target, buying-power impact, re-quote deltas and the
 * environment. Approve is disabled while market data is stale, the broker is offline, a
 * duplicate exists, or (for an opening order) the kill switch is on. A live account also needs
 * the symbol typed in.
 */

import { useState } from "react";
import { money } from "@/lib/format";
import type { Portfolio, Status } from "@/lib/types";
import Dialog from "./Dialog";
import { isOptionProposal, multiplierOf, type ApproveRequest } from "./ProposalCard";
import { dataAgeTone } from "./TopBar";
import css from "./ApproveDrawer.module.css";

function maxLoss(req: ApproveRequest): { value: number | null; note: string } {
  const p = req.proposal;
  const mult = multiplierOf(p);
  const notional = p.limit_price * mult * p.quantity;
  const intent = (p.position_intent || "").toUpperCase();
  if (isOptionProposal(p)) {
    if (intent.endsWith("_TO_CLOSE")) return { value: null, note: "Closing order: reduces risk" };
    if (intent === "BUY_TO_OPEN") return { value: notional, note: "Premium paid (a long option can expire worthless)" };
    return { value: null, note: "Short option: loss is not bounded by the premium" };
  }
  if (p.side === "SELL") return { value: null, note: "Exit order: reduces risk" };
  if (p.sl != null && p.sl > 0 && p.sl < p.limit_price) {
    return { value: (p.limit_price - p.sl) * p.quantity, note: "To the stop, before slippage and gaps" };
  }
  return { value: notional, note: "No stop on this order: up to the full notional" };
}

export default function ApproveDrawer({ request, status, portfolio, busy, onClose, onConfirm }: {
  request: ApproveRequest;
  status: Status | null;
  portfolio: Portfolio | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const p = request.proposal;
  const q = request.requote;
  const option = isOptionProposal(p);
  const mult = multiplierOf(p);
  const notional = p.limit_price * mult * p.quantity;
  const loss = maxLoss(request);
  const live = !!status?.is_live;
  const opening = option ? !(p.position_intent || "").toUpperCase().endsWith("_TO_CLOSE") : p.side === "BUY";
  const bp = portfolio?.balance?.buying_power;
  const bpAfter = bp != null && opening && p.side === "BUY" ? bp - notional : null;
  const [typed, setTyped] = useState("");

  const blockers: string[] = [];
  if (!status?.connected) blockers.push("The broker is offline.");
  if (dataAgeTone(status) === "stale") blockers.push("Market data is stale; wait for prices to refresh.");
  if (request.duplicate) blockers.push("A newer proposal for the same instrument and side exists; review that one.");
  if (status?.kill_switch && opening) blockers.push("The kill switch is on; opening orders are blocked.");
  if (q && !q.risk_ok) blockers.push(`The re-quoted order fails risk checks: ${q.reasons.join("; ") || "see the decision record"}.`);
  if (bpAfter != null && bpAfter < 0) blockers.push("Not enough buying power for this order.");
  const typedOk = !live || typed.trim().toUpperCase() === p.symbol.toUpperCase();
  const canApprove = !busy && blockers.length === 0 && typedOk;

  return <Dialog
    title={<>{p.side} {p.symbol}</>}
    eyebrow={`REVIEW BEFORE PLACING · #${p.id}`}
    danger={live}
    dismissable={!busy}
    size="md"
    className="approve-drawer"
    onClose={onClose}
    footer={<>
      <button className="button button-quiet" data-dialog-cancel disabled={busy} onClick={onClose}>Cancel</button>
      <button className={`button ${live ? "button-danger" : "button-primary"}`} disabled={!canApprove} onClick={onConfirm}>
        {busy ? "Placing…" : live ? "Place real order" : "Approve & place"}
      </button>
    </>}
  >
    <div className={`${css["env-strip"]} ${live ? "live" : "paper"}`}>
      <b>{live ? "LIVE" : "PAPER"}</b> {status?.broker_label || "broker unknown"} ·
      mode {status?.trading_mode === "auto" ? "AUTO" : "APPROVE"}
    </div>
    <dl className={`${css["review-grid"]}`}>
      <dt>Size</dt>
      <dd>{option
        ? <>{p.quantity} contract{p.quantity === 1 ? "" : "s"} <b>×{mult}</b> = {p.quantity * mult} shares exposure</>
        : <>{p.quantity} share{p.quantity === 1 ? "" : "s"}</>}</dd>
      {option && <><dt>Contract</dt><dd>{p.option_type} {money(p.strike_price)} exp {p.expiration_date} · {p.position_intent}</dd></>}
      <dt>Limit</dt>
      <dd>{money(p.limit_price)}{option ? " per share (premium)" : ""}
        {q && q.previous.limit_price !== p.limit_price && <span className="delta"> (was {money(q.previous.limit_price)})</span>}</dd>
      <dt>Notional</dt><dd><b>{money(notional)}</b></dd>
      <dt>Max loss</dt><dd>{loss.value == null ? "—" : <b className={`${css["neg"]}`}>{money(loss.value)}</b>} <span className="muted">{loss.note}</span></dd>
      {!option && <>
        <dt>Stop / target</dt>
        <dd>{p.sl == null ? "no stop" : money(p.sl)} / {p.tp == null ? "no target" : money(p.tp)}
          {q && (q.previous.sl !== p.sl || q.previous.tp !== p.tp) &&
            <span className="delta"> (was {money(q.previous.sl)} / {money(q.previous.tp)})</span>}</dd>
      </>}
      <dt>Buying power</dt>
      <dd>{bp == null ? "unknown" : bpAfter == null ? money(bp) : <>{money(bp)} → <b className={bpAfter < 0 ? `${css["neg"]}` : ""}>{money(bpAfter)}</b></>}</dd>
    </dl>
    {blockers.length > 0 && <ul className={`${css["blocker-list"]}`} role="alert">
      {blockers.map(b => <li key={b}>{b}</li>)}
    </ul>}
    {live && blockers.length === 0 && <label className="confirm-type">
      <span>Real money. Type <b>{p.symbol}</b> to place this order</span>
      <input value={typed} onChange={e => setTyped(e.target.value)} placeholder={p.symbol} autoComplete="off"
        spellCheck={false} aria-label={`Type ${p.symbol} to confirm`}/>
    </label>}
    <p className="muted small">Risk checks run again at execution; a stale price is rejected rather than chased.</p>
  </Dialog>;
}
