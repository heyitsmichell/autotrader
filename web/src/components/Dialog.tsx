"use client";

/**
 * The one modal primitive (QA M11). Focus is trapped inside while open, Escape closes (unless
 * `dismissable` is false), focus returns to the element that opened it, and on a `danger`
 * dialog the Cancel button (`data-dialog-cancel`) gets the initial focus so Enter never
 * confirms a destructive action by accident. Elsewhere `data-autofocus` or the first field wins.
 */

import { useEffect, useId, useRef, type ReactNode } from "react";
import css from "./Dialog.module.css";

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

export interface DialogProps {
  title: ReactNode;
  eyebrow?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  danger?: boolean;
  /** When false, Escape and backdrop clicks do nothing (e.g. while a request is in flight). */
  dismissable?: boolean;
  size?: "sm" | "md" | "lg";
  className?: string;
}

export default function Dialog({
  title, eyebrow, onClose, children, footer, danger = false, dismissable = true, size = "md", className = "",
}: DialogProps) {
  const ref = useRef<HTMLElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  const dismissRef = useRef(dismissable);
  useEffect(() => {
    closeRef.current = onClose;
    dismissRef.current = dismissable;
  });

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = ref.current;
    if (root) {
      const pick = (danger && root.querySelector<HTMLElement>("[data-dialog-cancel]"))
        || root.querySelector<HTMLElement>("[data-autofocus]")
        || root.querySelector<HTMLElement>("input, select, textarea")
        || root.querySelector<HTMLElement>(FOCUSABLE)
        || root;
      pick.focus();
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (dismissRef.current) {
          e.stopPropagation();
          closeRef.current();
        }
        return;
      }
      if (e.key !== "Tab" || !ref.current) return;
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter(el => el.offsetParent !== null || el === document.activeElement);
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !ref.current.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (opener && document.contains(opener)) opener.focus();
    };
    // danger is read once, at open time
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div className={`${css["modal-backdrop"]} backdrop-${size}`} role="presentation" onMouseDown={e => {
    if (e.target === e.currentTarget && dismissable) onClose();
  }}>
    <section
      ref={ref}
      className={`dialog dialog-${size} ${danger ? "dialog-danger" : ""} ${className}`}
      role={danger ? "alertdialog" : "dialog"}
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
    >
      <div className={`${css["modal-head"]}`}>
        <div>
          {eyebrow && <div className="eyebrow">{eyebrow}</div>}
          <h2 id={titleId}>{title}</h2>
        </div>
        <button className="button" aria-label="Close dialog" disabled={!dismissable} onClick={onClose}>Close</button>
      </div>
      <div className="dialog-body">{children}</div>
      {footer && <div className={`${css["modal-actions"]}`}>{footer}</div>}
    </section>
  </div>;
}
