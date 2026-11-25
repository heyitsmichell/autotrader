import type { SleeveEvent } from "@/lib/types";

const WARN = "#e0b35a", INK2 = "#9aa3ad";
const LABELS: Record<string, string> = {
  rebalance: "Rebal", pause_on: "Pause", pause: "Pause", pause_off: "Resume", resume: "Resume",
  kill_on: "Kill", kill_off: "Kill off", settings: "Settings", put: "Put", drift: "Drift",
  put_open: "Put sold", put_close: "Buyback", put_expired: "Expired", assigned: "Assigned",
};
export function eventLabel(e: SleeveEvent): string {
  const t = LABELS[e.kind] ?? e.text ?? e.kind;
  return t.length > 10 ? `${t.slice(0, 9)}…` : t;
}
export function eventStyle(kind: string): { color: string; shape: "circle" | "arrowDown" } {
  const warn = kind === "kill_on" || kind === "drift" || kind === "assigned";
  const arrow = warn || kind === "pause" || kind === "pause_on";
  return { color: warn ? WARN : INK2, shape: arrow ? "arrowDown" : "circle" };
}
