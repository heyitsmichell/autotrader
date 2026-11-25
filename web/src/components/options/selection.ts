import type { Proposal } from "@/lib/types";

export interface OptionSelection {
  delta?: number | null; dte?: number | null; iv?: number | null; iv_rank?: number | null;
  ml_p?: number | null; explore?: boolean | null; ml_version?: string | null;
  spread_pct?: number | null; underlying_close?: number | null; bid?: number | null; ask?: number | null;
}
export interface OptionInstrument { underlying_stop?: number | null; selection?: OptionSelection | null }

/** instrument_json arrives decoded (object) from the API, or as a JSON string on older rows. */
export function instrumentOf(p: Proposal): OptionInstrument {
  const raw = (p as unknown as { instrument_json?: unknown }).instrument_json;
  if (!raw) return {};
  if (typeof raw === "string") {
    try { return JSON.parse(raw) as OptionInstrument; } catch { return {}; }
  }
  return raw as OptionInstrument;
}
