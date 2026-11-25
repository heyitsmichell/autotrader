/** Fixed identity colours by sleeve id (never by rank). */
export const SLEEVE_ORDER: string[] = ["swing", "momentum", "putwrite", "vaa"];
export const SLEEVE_COLORS: Record<string, string> = {
  swing: "#338ced", momentum: "#e76624", putwrite: "#00a888", vaa: "#d28e05",
};
const SLOTS = ["#338ced", "#e76624", "#00a888", "#d28e05"];
export const sleeveColor = (id: string, slot?: number): string =>
  SLEEVE_COLORS[id] ?? SLOTS[(((slot ?? 1) - 1) % 4 + 4) % 4];
export const sortSleeves = <T extends { id: string }>(xs: T[]): T[] =>
  [...xs].sort((a, b) => {
    const i = SLEEVE_ORDER.indexOf(a.id), j = SLEEVE_ORDER.indexOf(b.id);
    return (i < 0 ? 99 : i) - (j < 0 ? 99 : j);
  });
