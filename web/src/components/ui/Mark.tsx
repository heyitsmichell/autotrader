import type { ReactNode } from "react";

/** Status mark: pale tint with same-hue text (gain, loss, warn or neutral). */
export default function Mark({ tone = "neutral", children, title }: {
  tone?: "gain" | "loss" | "warn" | "neutral";
  children: ReactNode;
  title?: string;
}) {
  return <span className={`mark ${tone}`} title={title}>{children}</span>;
}
