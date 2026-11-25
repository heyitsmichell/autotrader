import type { ReactNode } from "react";
import css from "./ui.module.css";

/** One-sentence empty state: muted, left-aligned, framed by 1px rules, no icon. */
export default function Empty({ children }: { children: ReactNode }) {
  return <p className={css["empty"]}>{children}</p>;
}
