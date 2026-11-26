"use client";

/**
 * Sticky header: wordmark, tabs, and the always-visible safety controls
 * (mode, pause, kill (Shift+K), flatten). It sits outside every error boundary.
 */

import { useEffect } from "react";
import type { Status } from "@/lib/types";
import { TABS, type Tab } from "@/lib/tabs";
import css from "./TopBar.module.css";

/** Market data older than this (seconds) is flagged as stale while the market is open. */
export const STALE_DATA_SECONDS = 120;
const AGING_DATA_SECONDS = 30;

export function dataAgeTone(status: Status | null): "fresh" | "aging" | "stale" | "none" {
  const age = status?.data_age_seconds;
  if (age == null) return "none";
  if (!status?.market_open) return "fresh";
  if (age >= STALE_DATA_SECONDS) return "stale";
  if (age >= AGING_DATA_SECONDS) return "aging";
  return "fresh";
}

export type { Tab };

export default function TopBar({ status, busy, tab, onTab, onMode, onFlatten, onKill, onScheduler }: {
  status: Status | null;
  busy: boolean;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onMode: (mode: "approve" | "auto") => void;
  onFlatten: () => void;
  onKill: () => void;
  /** Turn the analysis scheduler on (resume) or off (pause). */
  onScheduler: (on: boolean) => void;
}) {
  const killOn = !!status?.kill_switch;
  const paused = !status?.scheduler_on;
  const tabs: [Tab, string][] = TABS.filter(t => !t.hidden).map(t => [t.id, t.label]);

  // Shift+K opens the kill-switch dialog from anywhere (not while typing in a field).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.shiftKey || e.key.toLowerCase() !== "k" || e.ctrlKey || e.metaKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return;
      if (document.querySelector("[aria-modal='true']")) return;
      e.preventDefault();
      onKill();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onKill]);

  const age = status?.data_age_seconds;
  const ageTone = dataAgeTone(status);
  const ageText = age == null ? null
    : age < 90 ? `Prices ${Math.round(age)}s old`
    : age < 5400 ? `Prices ${Math.round(age / 60)}m old`
    : `Prices ${Math.round(age / 3600)}h old`;

  return <header className={`topbar ${css["bar"]}`}>
    <div className={css["brand"]}>
      <svg className={css["mark"]} width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
        <path d="M3 15 H9 V9 H15 V4 H19" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"/>
        <path d="M3 19 H19" stroke="var(--ink-2)" strokeWidth="1.5" strokeLinecap="round"/>
      </svg>
      <b className={css["wordmark"]}>Trader</b>
    </div>
    <nav className={css["tabs"]} role="tablist" aria-label="Views">
      {tabs.map(([t, label]) => <button
        key={t}
        role="tab"
        aria-selected={tab === t}
        className={`${css["tab"]} ${tab === t ? css["on"] : ""}`}
        onClick={() => onTab(t)}
      ><span className={css["label"]}>{label}</span></button>)}
    </nav>
    {ageText && <span className={`${css["fresh"]} ${ageTone === "stale" ? css["stale"] : ""}`}
      title="Age of the latest market data">{ageText}</span>}
    <div className={`${css["controls"]} ${ageText ? "" : css["push"]}`}>
      <div className={css["mode"]} role="group" aria-label="Execution mode">
        <button aria-pressed={status?.trading_mode === "approve"} disabled={!status}
          onClick={() => onMode("approve")}><span>Approve</span></button>
        <button aria-pressed={status?.trading_mode === "auto"} disabled={!status}
          onClick={() => onMode("auto")}><span>Auto</span></button>
      </div>
      <button
        className="button"
        disabled={busy || !status || killOn}
        title={paused ? "Start the automatic analysis scheduler" : "Stop automatic analysis (exits and the risk loop keep running)"}
        onClick={() => onScheduler(paused)}
      >{paused ? "Resume" : "Pause"}</button>
      <button
        className={`button danger ${killOn ? css["is-on"] : ""}`}
        disabled={busy}
        title={killOn ? "Release the kill switch (Shift+K)" : "Stop all new orders now (Shift+K)"}
        aria-pressed={killOn}
        aria-keyshortcuts="Shift+K"
        onClick={onKill}
      >{killOn ? "Release kill" : "Kill"}</button>
      <button
        className="button danger"
        disabled={!status?.connected || busy || status?.flattening}
        title="Kill switch, cancel every open order, sell every position"
        onClick={onFlatten}
      >{status?.flattening ? "Flattening" : "Flatten"}</button>
    </div>
  </header>;
}
