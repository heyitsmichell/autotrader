import type { Proposal } from "@/lib/types";

/**
 * Every status the backend writes to `proposals.status` (app.engine.PROPOSAL_STATUSES):
 *   pending, submitting, placed, rejected, dismissed, failed, unknown, orphan.
 * Older rows and other views may also carry waiting, filled or expired; they are mapped too,
 * and anything else falls back to a neutral "unknown" tone with the raw status as the label.
 */
export const PROPOSAL_STATUSES = [
  "pending", "submitting", "placed", "rejected", "dismissed", "failed", "unknown", "orphan",
] as const;

export type StatusTone =
  | "pending" | "submitting" | "placed" | "filled" | "rejected" | "webull" | "failed"
  | "unknown" | "dismissed" | "orphan";

export interface ProposalState {
  tone: StatusTone;
  label: string;
  detail: string;
  webull: boolean;
  /** True while the row still needs something to happen (a decision, a reply, a review). */
  open: boolean;
}

const WEBULL_REJECTION = /webull|broker server|broker rejected|whitelist|symbol whitelist|notional|account id|client_order_id/i;

export function classifyProposal(proposal: Proposal): ProposalState {
  const text = `${proposal.reason || ""} ${proposal.broker_response || ""}`.trim();
  const status = (proposal.status || "unknown").toLowerCase();
  switch (status) {
    case "pending":
      return { tone: "pending", label: "Pending approval", detail: text, webull: false, open: true };
    case "submitting":
      return { tone: "submitting", label: "Sending to broker", detail: text, webull: false, open: true };
    case "waiting":
      return { tone: "submitting", label: "Waiting", detail: text, webull: false, open: true };
    case "placed":
      return { tone: "placed", label: "Placed", detail: text, webull: false, open: false };
    case "filled":
      return { tone: "filled", label: "Filled", detail: text, webull: false, open: false };
    case "dismissed":
      return { tone: "dismissed", label: "Dismissed", detail: text, webull: false, open: false };
    case "expired":
      return { tone: "dismissed", label: "Expired", detail: text, webull: false, open: false };
    case "unknown":
      return { tone: "unknown", label: "Status unknown", detail: text || "Being looked up at the broker", webull: false, open: true };
    case "orphan":
      return {
        tone: "orphan", label: "Orphan order",
        detail: text || "Open at the broker but not created by this app. Review it in Webull.",
        webull: false, open: true,
      };
    case "rejected":
    case "failed": {
      const webull = !!proposal.broker_response && WEBULL_REJECTION.test(text);
      if (webull) return { tone: "webull", label: "Blocked by Webull server", detail: text, webull: true, open: false };
      return {
        tone: status as StatusTone,
        label: status === "failed" ? "Failed at broker" : "Blocked by app",
        detail: text || "No reason recorded",
        webull: false,
        open: false,
      };
    }
    default:
      return { tone: "unknown", label: status, detail: text, webull: false, open: false };
  }
}
