import type { ReactNode } from "react";
import css from "./ui.module.css";

/** Unboxed section: h2 (16/600), optional muted meta text, optional right-side slot, optional top rule. */
export default function Section({ title, meta, right, rule, children, id }: {
  title: ReactNode;
  meta?: ReactNode;
  right?: ReactNode;
  rule?: boolean;
  children?: ReactNode;
  id?: string;
}) {
  return <section id={id} className={`${css["section"]} ${rule ? css["ruled"] : ""}`}>
    <div className={css["head"]}>
      <div className={css["title"]}>
        <h2>{title}</h2>
        {meta != null && <span className={css["meta"]}>{meta}</span>}
      </div>
      {right != null && <div className={css["slot"]}>{right}</div>}
    </div>
    {children}
  </section>;
}
