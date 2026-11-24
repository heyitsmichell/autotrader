"use client";

import { dateTime } from "@/lib/format";
import type { Run, Status } from "@/lib/types";
import css from "./CommandBar.module.css";

/** Run now, reconnect broker, and the last-run line. Used on Today (small) and in Settings. */
export default function CommandBar({ status, lastRun, busy, onReconnect, onRunNow, onTestAlert }: {
  status: Status | null;
  lastRun?: Run;
  busy: boolean;
  onReconnect: () => void;
  onRunNow: () => void;
  onTestAlert?: () => void;
}) {
  const runBlocked = !status?.connected ? "Broker is not connected"
    : status?.kill_switch ? "Kill switch is on"
      : status?.running ? "A run is already in progress" : "";
  const skipped = lastRun && (lastRun.status === "skipped" || lastRun.status === "aborted");
  const reason = skipped ? lastRun?.summary || lastRun?.error : "";
  const line = status?.running ? "Analysis is running."
    : lastRun ? `Last run ${lastRun.status}${reason ? `: ${reason}` : ""}.`
      : "No runs yet.";
  const next = !status?.scheduler_on ? "Scheduler is off."
    : status?.next_run_at ? `Next run ${dateTime(status.next_run_at)}, every ${status.cycle_minutes} min.`
      : "Next run not scheduled.";

  return <div className={css["row"]}>
    <span className={css["text"]}>{line} {next}</span>
    <span className={css["actions"]}>
      <button className="button" disabled={busy || !!runBlocked} title={runBlocked || "Run one analysis cycle now"}
        onClick={onRunNow}>Run now</button>
      <button className="button" disabled={busy} onClick={onReconnect}>Reconnect broker</button>
      {onTestAlert && <button className="button" disabled={busy} onClick={onTestAlert}>Send test alert</button>}
    </span>
  </div>;
}
