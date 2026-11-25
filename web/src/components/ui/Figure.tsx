import type { ReactNode } from "react";
import css from "./ui.module.css";

/** Label (12px muted) over a 20px mono value; tone colours only P&L-like figures. */
export function Figure({ label, value, tone, title }: {
  label: string;
  value: ReactNode;
  tone?: "gain" | "loss" | "muted";
  title?: string;
}) {
  return <div className={css["figure"]} title={title}>
    <span className={css["label"]}>{label}</span>
    <span className={`${css["value"]} ${tone ? css[tone] : ""}`}>{value}</span>
  </div>;
}

/** Row of Figures split by 1px vertical rules; wraps to two per row under 640px. */
export function FigureRow({ children }: { children: ReactNode }) {
  return <div className={css["row"]}>{children}</div>;
}

export default Figure;
