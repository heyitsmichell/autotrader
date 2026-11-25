/** Fixed identity colours by shadow id; unknown ids fall back through the same list. */
export const SHADOW_COLORS: Record<string, string> = {
  mom_skip1w: "#e24d8b", overnight_mom: "#0aac23", vaa_g4: "#9887ef",
};
const LIST = ["#e24d8b", "#0aac23", "#9887ef"];
export const shadowColor = (id: string, index = 0): string =>
  SHADOW_COLORS[id] ?? LIST[((index % LIST.length) + LIST.length) % LIST.length];
