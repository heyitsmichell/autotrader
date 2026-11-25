import { sleeveColor } from "@/components/strategies/sleeveColors";
import { shadowColor } from "@/components/strategies/shadowColors";

/** Display names for every strategy id the Results tab shows (live sleeves, then shadows). */
export const STRATEGY_NAMES: Record<string, string> = {
  swing: "Swing", momentum: "Momentum", putwrite: "Put-write", vaa: "VAA",
  mom_skip1w: "Momentum, skip 1 week", overnight_mom: "Overnight momentum", vaa_g4: "VAA-G4",
  swing_dynamic: "Swing, dynamic top-100",
};
export const LIVE_IDS = ["swing", "momentum", "putwrite", "vaa"];

export const strategyName = (id: string): string => STRATEGY_NAMES[id] ?? id;
export const strategyColor = (id: string, live: boolean, index = 0): string =>
  live ? sleeveColor(id) : shadowColor(id, index);
