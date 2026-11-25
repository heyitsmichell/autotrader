"use client";

import { useEffect, useState, type ReactNode } from "react";
import { money, pct } from "@/lib/format";
import type { Api } from "@/lib/api";
import type { OptionSpreadsResponse, Status } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import css from "./options.module.css";

export default function OptionsSettings({ status, api }: { status: Status | null; api: Api }) {
  const [spreads, setSpreads] = useState<OptionSpreadsResponse | null>(null);
  useEffect(() => {
    let live = true;
    api<OptionSpreadsResponse>("/api/options/spreads?days=20")
      .then(r => { if (live) setSpreads(r); }).catch(() => { if (live) setSpreads(null); });
    return () => { live = false; };
  }, [api]);
  const s = status?.settings;
  if (!s) return null;
  const rows: Array<[string, ReactNode]> = [
    ["Auto-entry", <Mark key="a" tone={s.options_auto_entry ? "gain" : "neutral"}>{s.options_auto_entry ? "on" : "off"}</Mark>],
    ["Target delta", s.options_entry_target_delta ?? "—"],
    ["Target days to expiry", s.options_entry_target_dte ?? "—"],
    ["Premium budget per trade", s.options_entry_max_premium_usd != null ? money(s.options_entry_max_premium_usd) : "—"],
    ["Max open", s.options_entry_max_open ? s.options_entry_max_open : "no limit"],
    ["Premium stop", s.options_exit_stop_pct ? `−${Math.round(s.options_exit_stop_pct * 100)}%` : "off"],
    ["Underlying stop", s.options_exit_underlying_stop ? "on" : "off"],
    ["Take-profit", s.options_exit_take_profit_pct ? `+${Math.round(s.options_exit_take_profit_pct * 100)}%` : "off"],
    ["Allowed underlyings", (s.options_allowed_underlyings || []).join(", ") || "—"],
  ];
  const buckets = spreads?.overall || [];
  return <Section title="Options" meta={s.options_enabled ? undefined : "disabled"}>
    <dl className={css["kv"]}>{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd className="mono">{v}</dd></div>)}</dl>
    <h3 className={css["sub"]}>Spreads at the open</h3>
    {buckets.length ? <div className="tablewrap"><table>
      <thead><tr><th scope="col" className="num">Minutes after open</th><th scope="col" className="num">Median spread</th>
        <th scope="col" className="num">p75</th><th scope="col" className="num">Passing max spread{spreads ? ` (${spreads.max_bid_ask_pct}%)` : ""}</th></tr></thead>
      <tbody>{buckets.map(b => <tr key={b.minutes_from_open}>
        <td className="num mono">{b.minutes_from_open}</td>
        <td className="num mono">{pct(b.median_spread_pct, { sign: false, digits: 1 })}</td>
        <td className="num mono">{pct(b.p75_spread_pct, { sign: false, digits: 1 })}</td>
        <td className="num mono">{pct(b.pct_pass, { sign: false, digits: 0 })}</td>
      </tr>)}</tbody>
    </table></div>
      : <Empty>No spread samples yet. The logger samples at the open and through the day once OPTIONS_SPREAD_LOG is on.</Empty>}
  </Section>;
}
