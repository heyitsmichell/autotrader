"use client";

/**
 * Sticky header (QA §7): environment and mode badge, data-age chip, broker and market state,
 * Pause engine, Flatten, and the kill switch (Shift+K). It sits outside every error boundary.
 */

import { useEffect } from "react";
import { dateTime } from "@/lib/format";
import type { Status } from "@/lib/types";
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

function ageText(seconds: number | null | undefined): string {
  if (seconds == null) return "no data yet";
  if (seconds < 60) return `${Math.round(seconds)}s old`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m old`;
  return `${(seconds / 3600).toFixed(1)}h old`;
}

export default function TopBar({ status, pendingCount, busy, streamConnected, onFlatten, onKill, onScheduler }: {
  status: Status | null;
  pendingCount: number;
  busy: boolean;
  streamConnected: boolean;
  onFlatten: () => void;
  onKill: () => void;
  /** Turn the analysis scheduler on (resume) or off (pause). */
  onScheduler: (on: boolean) => void;
}) {
  const session = status?.session;
  const live = !!status?.is_live;
  const env = live ? "LIVE" : "PAPER";
  const envDetail = live ? "Real-money account"
    : status?.broker_mode === "webull" ? "Webull UAT sandbox" : "Mock broker (simulated)";
  const mode = status?.trading_mode === "auto" ? "AUTO" : "APPROVE";
  const tone = dataAgeTone(status);
  const killOn = !!status?.kill_switch;
  const paused = !status?.scheduler_on;

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

  return <header className={`topbar ${css["sticky-top"]} ${live ? `${css["is-live"]}` : ""}`}>
    <div className={`${css["brand-mark"]}`} aria-hidden="true">T</div>
    <div className={`${css["brand-copy"]}`}><b>TRADER</b><span>MARKET INTELLIGENCE</span></div>

    <span className={`${css["env-badge"]} ${live ? "live" : "paper"} ${mode === "AUTO" ? "auto" : ""}`}
      title={`${envDetail} · ${status?.broker_label || "connecting"}`}>
      <b>{env}</b><span aria-hidden="true">·</span><b>{mode}</b>
    </span>

    <span className={`${css["age-chip"]} ${tone}`} title={streamConnected ? "Live updates connected" : "Live updates down: polling"}>
      <i className={`dot ${tone === "stale" ? "red" : tone === "aging" ? "amber" : tone === "fresh" ? "green" : ""}`} aria-hidden="true"/>
      Data {ageText(status?.data_age_seconds)}{streamConnected ? "" : " · polling"}
    </span>

    {pendingCount > 0 && <a className={`${css["pending-badge"]}`} href="#action-queue" aria-label={`${pendingCount} proposals pending approval`}>
      {pendingCount} pending
    </a>}
    <span className={`${css["state-tag"]} ${status?.connected ? "good" : "bad"}`}>
      <i className={`dot ${status?.connected ? "green" : "red"}`} aria-hidden="true"/>
      {status?.connected ? "Broker connected" : "Broker offline"}
    </span>
    <span className={`${css["state-tag"]} ${status?.market_open ? "good" : ""}`} title={session ? `Next open ${dateTime(session.next_open)}` : ""}>
      <i className={`dot ${status?.market_open ? "green" : ""}`} aria-hidden="true"/>
      {status?.market_open
        ? `Market open${session?.early_close ? ` · closes ${session.close} ET` : ""}`
        : session?.holiday ? `Closed · ${session.holiday}` : "Market closed"}
    </span>
    <div className={`${css["top-spacer"]}`}/>
    <span className={`${css["clock"]}`} suppressHydrationWarning>{status?.now_et || "—"}</span>

    <button
      className="button button-quiet"
      disabled={busy || !status || killOn}
      aria-pressed={!paused}
      title={paused ? "Start the automatic analysis scheduler" : "Stop automatic analysis (no new proposals; exits and the risk loop keep running)"}
      onClick={() => onScheduler(paused)}
    >{paused ? "▶ Resume engine" : "❚❚ Pause engine"}</button>
    <button
      className={`button ${css["button-flatten"]}`}
      disabled={!status?.connected || busy || status?.flattening}
      title="Kill switch + cancel every open order + sell every position"
      onClick={onFlatten}
    >{status?.flattening ? "Flattening…" : "Flatten all"}</button>
    <button
      className={`button ${css["kill-button"]} ${killOn ? `${css["is-on"]}` : ""}`}
      disabled={busy}
      title={killOn ? "Release the kill switch (Shift+K)" : "Stop all new orders now (Shift+K)"}
      aria-pressed={killOn}
      aria-keyshortcuts="Shift+K"
      onClick={onKill}
    >{killOn ? "KILL ON · release" : "KILL"}</button>
  </header>;
}
