"use client";

/** One place for every dashboard-wide warning, most severe first (QA §7 banner rail). */

import type { ReactNode } from "react";
import { dateTime, money } from "@/lib/format";
import type { Status } from "@/lib/types";
import { dataAgeTone, STALE_DATA_SECONDS } from "./TopBar";

type Level = "critical" | "warning" | "info";
interface Banner { key: string; level: Level; body: ReactNode }

export default function BannerRail({ status, error, streamConnected, pendingCount, duplicateInstance }: {
  status: Status | null;
  error: string;
  streamConnected: boolean;
  pendingCount: number;
  duplicateInstance?: { timestamp: number; message: string } | null;
}) {
  const banners: Banner[] = [];
  const s = status;

  if (s?.kill_switch) {
    banners.push({ key: "kill", level: "critical", body: <>
      <b>Kill switch is on</b> · {s.kill_reason || "no reason recorded"}
      {s.supervisor?.kill_at ? ` · since ${dateTime(s.supervisor.kill_at)}` : ""}. No new orders are placed until you
      release it. Exits and protective stops still work.
    </> });
  }
  if (s?.is_live) {
    banners.push({ key: "live", level: "critical", body: <>
      <b>LIVE account.</b> Orders place real trades · {s.broker_label}
    </> });
  }
  if (duplicateInstance) {
    banners.push({ key: "dup", level: "critical", body: <>
      <b>Duplicate instance detected.</b> {duplicateInstance.message}
    </> });
  }
  if (s && !s.connected) {
    banners.push({ key: "offline", level: "critical", body: <>
      <b>Broker offline.</b> {s.broker_error || "Reconnecting."} Approvals are disabled until it is back.
    </> });
  }
  const guard = s?.daily_loss_guard;
  if (guard?.tripped) {
    banners.push({ key: "guard", level: "critical", body: <>
      <b>Daily loss guard tripped:</b> day P&amp;L {money(guard.pnl)} vs limit {money(guard.limit != null ? -Math.abs(guard.limit) : null)}.
      New entries are blocked for today.
    </> });
  }
  if (dataAgeTone(s) === "stale") {
    banners.push({ key: "stale", level: "warning", body: <>
      <b>Market data is stale</b> ({Math.round((s?.data_age_seconds || 0) / 60)} min old, limit {STALE_DATA_SECONDS / 60} min).
      Approvals are disabled until prices refresh.
    </> });
  }
  if (s?.broker_mode_warning) {
    banners.push({ key: "mode", level: "warning", body: <><b>Broker mode:</b> {s.broker_mode_warning}</> });
  }
  const token = s?.supervisor?.token;
  if (s?.broker_mode === "webull" && (!token || (token.status !== "NORMAL" && token.status !== "not_required"))) {
    banners.push({ key: "login", level: "warning", body: <>
      <b>Webull login needs attention.</b> Run <code>./auth.sh</code> (macOS/Linux) or <code>Auth_Trader.bat</code> (Windows),
      then approve the login in the Webull app.{token?.status ? ` Current status: ${token.status}.` : ""}
      {token?.expires_at ? ` Token expires ${dateTime(token.expires_at)}.` : ""}
    </> });
  }
  if ((s?.supervisor?.unresolved_orders ?? 0) > 0) {
    banners.push({ key: "unknown", level: "warning", body: <>
      {s?.supervisor?.unresolved_orders} order(s) with unknown status are being looked up at the broker.
    </> });
  }
  if (s?.trading_mode === "approve" && !(s.alerts?.channels || []).length && pendingCount > 0) {
    banners.push({ key: "alerts", level: "warning", body: <>
      <b>Approval mode cannot reach you:</b> no alert channel is configured. Configure ALERT_NTFY_TOPIC (or another channel).
    </> });
  }
  if (s && s.web_built === false) {
    banners.push({ key: "web", level: "info", body: <>The web build is missing. Run the setup script to rebuild the dashboard.</> });
  }
  if (error) banners.push({ key: "error", level: "warning", body: error });
  if (!streamConnected && s) {
    banners.push({ key: "stream", level: "info", body: <>Live updates are reconnecting; the dashboard is polling every 10 s meanwhile.</> });
  }

  if (!banners.length) return null;
  return <div className="banner-rail" aria-live="polite">
    {banners.map(b => <div key={b.key} className={`banner banner-${b.level}`} role={b.level === "critical" ? "alert" : "status"}>
      {b.body}
    </div>)}
  </div>;
}
