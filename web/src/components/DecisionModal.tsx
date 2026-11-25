"use client";

import { dateTime, money, score, signed } from "@/lib/format";
import { classifyProposal } from "@/lib/proposalStatus";
import type { Proposal, ProposalDetail, Status } from "@/lib/types";
import Dialog from "./Dialog";
import css from "./DecisionModal.module.css";

export default function DecisionModal({ selected, detail, status, onClose }: {
  selected: Proposal;
  detail: ProposalDetail | null;
  status: Status | null;
  onClose: () => void;
}) {
  const state = classifyProposal(selected);
  const option = selected.option_type != null;
  return <Dialog
    title={<>{selected.side} {selected.symbol}</>}
    eyebrow={`Decision record · #${selected.id}`}
    size="lg"
    className={`${css["decision-modal"]}`}
    onClose={onClose}
    footer={<>
      <span className="muted">Approval rechecks current price and risk limits.</span>
      <button className="button button-quiet" onClick={onClose}>Close</button>
    </>}
  >
      <p className={`${css["decision-sub"]}`}>{selected.quantity} {option ? `contract(s) ×${selected.multiplier || 100}` : "shares"} @ {money(selected.limit_price)} · created {dateTime(selected.created_at)}</p>
      {!detail
        ? <div className={`${css["modal-loading"]}`}>Loading signal, headlines and protection details…</div>
        : <>
          <div className={`${css["decision-summary"]}`}>
            <div><small>Analyst rationale</small><p>{detail.proposal?.rationale || "No rationale recorded."}</p></div>
            <div className={`${css["decision-facts"]}`}>
              <span>Reference price<b>{money(detail.proposal?.ref_price)}</b></span>
              <span>Confidence<b>{detail.proposal?.confidence == null ? "—" : `${Math.round(detail.proposal.confidence * 100)}%`}</b></span>
              <span>Outcome<b className={`status-text ${state.tone}`}>{state.label}</b></span>
            </div>
          </div>
          {state.detail && <div className={`inline-warning ${state.webull ? "broker-warning" : ""}`}>
            <b>{state.label}:</b> {state.detail}
          </div>}
          <div className={`${css["decision-columns"]}`}>
            <div>
              <h3 className={css["sec"]}>Technical factors</h3>
              {(detail.signal?.ta_components || []).map(item => <div className={`${css["factor-row"]}`} key={item.name}>
                <span>{item.name}<small>{item.note}</small></span>
                <b className={`mono ${Number(item.points) > 0 ? "positive" : Number(item.points) < 0 ? "negative" : ""}`}>{signed(item.points)}</b>
              </div>)}
              {!detail.signal?.ta_components?.length && <p className="muted">No stored TA factors for this proposal.</p>}
              <div className={`${css["detail-score"]}`}>
                <span>TA score</span><b>{score(detail.signal?.ta_score)}</b>
                <span>Trend regime</span><b>{detail.signal?.trend || "—"}</b>
                <span>Model odds</span><b>{detail.signal?.ml_p == null ? "Rules only" : `${Math.round(detail.signal.ml_p * 100)}%`}</b>
              </div>
            </div>
            <div>
              <h3 className={css["sec"]}>News sentiment</h3>
              <div className={`${css["sentiment-card"]}`}>
                <b className={Number(detail.signal?.sent_score) >= 0 ? "positive" : "negative"}>{score(detail.signal?.sent_score)}</b>
                <span>{detail.signal?.sent_n ?? 0} headlines · {status?.sentiment?.source || "configured source"}</span>
              </div>
              {(detail.headlines || []).map((headline, index) => <article className={`${css["headline"]}`} key={headline.news_id || index}>
                <a href={headline.url || "#"} target="_blank" rel="noreferrer">{headline.title || "Untitled headline"} ↗</a>
                <small>{headline.source || "News"} · {dateTime(headline.published_at)} · {headline.webull_sentiment || headline.llm_label || "unscored"}</small>
              </article>)}
              {!detail.headlines?.length && <p className="muted">No linked headlines in the sentiment window.</p>}
            </div>
          </div>
          <div className={`${css["gate-record"]}`}>
            <h3 className={css["sec"]}>Consensus checks</h3>
            {(detail.signal?.consensus_checks || []).map(check => <div className={`${css["gate-record-row"]}`} key={check.name}>
              <span className={`no-arrow ${check.ok ? "positive" : "negative"}`} aria-label={check.ok ? "pass" : "fail"}>{check.ok ? "✓" : "×"}</span>
              <span><b>{check.name}</b><small>{check.detail}</small></span>
            </div>)}
            {(detail.protective || []).length > 0 && <div className={`${css["modal-protection"]}`}>
              {detail.protective.map(item => <span key={item.id}>
                {item.kind === "sl" ? "Stop" : "Target"}: {money(item.price)} · {item.status}
              </span>)}
            </div>}
          </div>
        </>}
  </Dialog>;
}
