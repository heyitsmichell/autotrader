export const statusLabel: Record<string, string> = {
  proposed: "Proposed",
  approved: "Approved",
  active: "Active",
  retired: "Retired",
  rejected: "Rejected",
};

export const statusColor: Record<string, string> = {
  proposed: "var(--color-proposed)",
  approved: "var(--color-approved)",
  active: "var(--color-active)",
  retired: "var(--color-retired)",
  rejected: "var(--color-rejected)",
};

export const APPROVER_KEY = "trader.approver";

export function readApprover(): string {
  try { return window.localStorage.getItem(APPROVER_KEY) || ""; } catch { return ""; }
}

export function saveApprover(name: string) {
  try { window.localStorage.setItem(APPROVER_KEY, name); } catch { /* private mode */ }
}

