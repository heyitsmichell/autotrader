"use client";

import { dateTime } from "@/lib/format";
import type { Run, Status } from "@/lib/types";
import css from "./CommandBar.module.css";

export default function CommandBar({ status, lastRun, busy, onMode, onReconnect, onRunNow }: {
  status: Status | null;
  lastRun?: Run;
  busy: boolean;
  onMode: (mode: "approve" | "auto") => void;
  onReconnect: () => void;
  onRunNow: () => void;
}) {
  const runBlocked = !status?.connected ? "Broker is not connected"
    : status?.kill_switch ? "Kill switch is on"
      : status?.running ? "A run is already in progress" : "";
  const skipped = lastRun && (lastRun.status === "skipped" || lastRun.status === "aborted");
  const runLabel = status?.running
    ? "Analysis running"
    : skipped
      ? `Last run ${lastRun?.status}`
      : "Ready";

  return <section className={`${css["commandbar"]} panel`}>
    <div className={`${css["command-main"]}`}>
      <span className={`run-indicator ${status?.running ? "working" : ""}`}>
        <i className={`dot ${status?.running ? "green" : ""}`} aria-hidden="true"/>{runLabel}
      </span>
      <span className={`${css["command-divider"]}`} aria-hidden="true"/>
      <span className="muted">
        Auto analysis <small>{status?.scheduler_on ? `· every ${status?.cycle_minutes}m` : "· scheduler off"}</small>
      </span>
      <span className="next-run" title="Scheduler next run (local time)">
        {status?.next_run_at ? `Next ${dateTime(status.next_run_at)}` : "Next run not scheduled"}
      </span>
      {skipped && <span className={`${css["run-reason"]}`} title={lastRun?.summary || lastRun?.error || ""}>
        {lastRun?.summary || lastRun?.error || "Skipped without a recorded reason"}
      </span>}
    </div>
    <div className={`${css["mode-control"]}`}>
      <span className="eyebrow">EXECUTION MODE</span>
      <div className={`${css["segmented"]}`} role="group" aria-label="Execution mode">
        <button
          className={status?.trading_mode === "approve" ? "active" : ""}
          aria-pressed={status?.trading_mode === "approve"}
          onClick={() => onMode("approve")}
        >Approve</button>
        <button
          className={status?.trading_mode === "auto" ? "active auto" : ""}
          aria-pressed={status?.trading_mode === "auto"}
          onClick={() => onMode("auto")}
        >Auto-trade</button>
      </div>
    </div>
    <button className="button button-quiet" disabled={busy || !!runBlocked} title={runBlocked || "Run one analysis cycle now"}
      onClick={onRunNow}>
      <span aria-hidden="true">▶</span> Run now
    </button>
    <button className="button button-quiet reconnect" disabled={busy} onClick={onReconnect}>
      <span aria-hidden="true">↻</span> Reconnect
    </button>
  </section>;
}
