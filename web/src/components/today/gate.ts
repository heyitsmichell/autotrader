import { weekdaysSinceEt } from "@/lib/format";
import type { Signal } from "@/lib/types";

export interface GateSummary {
  watched: number;
  eligible: number;
  pass: number;
  reasons: Array<{ name: string; count: number }>;
}

/** Funnel from consensus_checks: eligible = clears the first gate stage; pass = no failing check. */
export function summarizeGate(signals: Signal[]): GateSummary {
  const counts = new Map<string, number>();
  let eligible = 0;
  let pass = 0;
  for (const s of signals) {
    const checks = s.consensus_checks || [];
    if (!checks.length) continue;
    if (checks[0]?.ok) eligible++;
    const f = checks.find(c => !c.ok);
    if (!f) { pass++; continue; }
    counts.set(f.name, (counts.get(f.name) || 0) + 1);
  }
  const reasons = [...counts].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count);
  return { watched: signals.length, eligible, pass, reasons };
}

const SYSTEM = /prune|loop|worker|started|connected to mcp|mcp connected|live prices|scheduler|reconcil|signals refreshed|refreshed signals/i;
const TRADING = /option|call skipped|put skipped|order|fill|stop|proposal|block|kill|flatten|approv|dismiss/i;

/** System chatter is hidden by default; errors and anything about orders, fills, stops or proposals is trading. */
export function isSystemEvent(message: string, level: string): boolean {
  if (level === "error" || level === "critical") return false;
  if (/reconcil|signals refreshed|research scan|deterministic run|scheduler started/i.test(message)) return true;
  if (/rate-limited us|pausing (google news|webull news calls|it for)|news is failing/i.test(message)) return true;
  if (TRADING.test(message)) return false;
  return SYSTEM.test(message);
}

/** Weekdays elapsed from `from` up to now (entry day excluded). */
export function weekdaysSince(from: string | null | undefined, now = Date.now()): number | null {
  if (!from) return null;
  return weekdaysSinceEt(from, now);
}
