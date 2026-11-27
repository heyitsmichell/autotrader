import css from "./Risk.module.css";

/** Heat class for value v relative to scale max (>0). Zero/null = neutral. */
export function heatClass(v: number | null | undefined, max: number): string {
  if (v == null || !Number.isFinite(v) || max <= 0) return css["h"];
  const r = Math.abs(v) / max;
  if (r < 0.04) return css["h"];
  const lvl = r < 0.34 ? 1 : r < 0.67 ? 2 : 3;
  return css[`${v < 0 ? "n" : "p"}${lvl}`];
}
export const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? css["gain"] : css["loss"]);
export const compact = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a < 0.5) return "0";
  const s = v < 0 ? "−" : "+";
  return a >= 1e6 ? `${s}${(a / 1e6).toFixed(2)}M` : a >= 1e4 ? `${s}${(a / 1e3).toFixed(0)}k` : a >= 1e3 ? `${s}${(a / 1e3).toFixed(1)}k` : `${s}${a.toFixed(0)}`;
};
/** Neutral single-hue intensity class by |v|/max (sign is printed, not coloured). */
export function neutralClass(v: number | null | undefined, max = 1): string {
  if (v == null || !Number.isFinite(v) || max <= 0) return css["h"];
  const r = Math.abs(v) / max;
  return r < 0.15 ? css["h"] : css[r < 0.4 ? "c1" : r < 0.7 ? "c2" : "c3"];
}
export { shortDate } from "@/lib/format";
/** Whole dollars with a real minus sign; "+" only when signed. */
export const usd = (v: number | null | undefined, signed = false): string => {
  if (v == null || !Number.isFinite(v)) return "—";
  const r = Math.round(Math.abs(v));
  if (r === 0) return "$0";
  return `${v < 0 ? "−" : signed ? "+" : ""}$${r.toLocaleString("en-US")}`;
};
/** "just now" / "N min ago" from an ISO timestamp, or null. */
export const ageText = (iso: string | null | undefined): string | null => {
  const t = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(t)) return null;
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  return m === 0 ? "just now" : `${m} min ago`;
};
