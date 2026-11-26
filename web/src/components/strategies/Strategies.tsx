"use client";

import { useMemo, useState } from "react";
import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { ConfirmRequest } from "@/components/ConfirmModal";
import { money } from "@/lib/format";
import type { SleeveEquityPoint, SleeveEvent, SleevesPayload, Status } from "@/lib/types";
import Section from "@/components/ui/Section";
import AllocationBar from "./AllocationBar";
import BandChart from "./BandChart";
import TearSheet from "./TearSheet";
import CompareTable from "./CompareTable";
import ShadowSection from "./ShadowSection";
import SleeveDetail from "./SleeveDetail";
import SleeveEquityChart from "./SleeveEquityChart";
import { sortSleeves } from "./sleeveColors";
import css from "./Strategies.module.css";

export default function Strategies({ api, onConfirm, onAction }: {
  api: Api;
  status?: Status | null;
  onConfirm?: (r: ConfirmRequest | null) => void;
  onAction?: (label: string, fn: () => Promise<unknown>) => void | Promise<void>;
}) {
  const [selId, setSelId] = useState("");

  const sp = usePoll<SleevesPayload>(api, "/api/sleeves", 30_000);
  const data = sp.data;
  const err = sp.error ? (sp.error.message || "Could not load strategies.") : "";
  const eqd = usePoll<{ series: SleeveEquityPoint[] }>(api, "/api/sleeves/equity?days=365", 300_000).data;
  const evd = usePoll<{ events: SleeveEvent[] }>(api, "/api/sleeves/events?days=400", 300_000).data;
  const series = useMemo(() => (Array.isArray(eqd?.series) ? eqd.series : []), [eqd]);
  const events = useMemo(() => (Array.isArray(evd?.events) ? evd.events : []), [evd]);

  const sleeves = useMemo(() => {
    const list = sortSleeves(data?.sleeves ?? []);
    return data && !data.enabled ? list.map(s => ({ ...s, status: "disabled" as const })) : list;
  }, [data]);

  const curSleeve = sleeves.find(s => s.id === selId) ?? sleeves[0];
  const curId = curSleeve?.id ?? "";

  if (!data) return <Section title="Strategies">
    <p className={css["muted"]}>{err || "Loading…"}</p></Section>;

  const acct = data.account;
  const others = sleeves.filter(s => s.id !== "swing");
  const sumEq = others.reduce((a, s) => a + (s.equity || 0), 0);
  const target = others.reduce((a, s) => a + (s.start_capital || 0), 0);
  const words = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"];
  const countWord = words[sleeves.length] ?? String(sleeves.length);
  const dayCls = !acct || !acct.day_pnl ? "" : acct.day_pnl > 0 ? css["gain"] : css["loss"];

  return <>
    {!data.enabled && <p className={css["note"]}>Multi-strategy mode is off (SLEEVES_ENABLED=false). Only Swing trades.</p>}
    {err && <p className={css["muted"]}>{err} Showing the last data received.</p>}
    <Section title="Strategies" meta={`${countWord} ${sleeves.length === 1 ? "strategy shares" : "strategies share"} one account`}>
      {acct && <div className={css["strip"]}>
        <div><span>Account value</span><b>{money(acct.net_liq)}</b></div>
        <div><span>Day P&amp;L</span><b className={dayCls}>{money(acct.day_pnl, { sign: true })}</b></div>
        <div><span title="Swing is the account remainder, so it is excluded.">In other strategies</span><b>{money(sumEq)}{target ? <span> of {money(target)}</span> : null}</b></div>
      </div>}
      <AllocationBar sleeves={sleeves}/>
    </Section>
    <Section title="Comparison"><CompareTable sleeves={sleeves}/></Section>
    <ShadowSection api={api} sleeves={sleeves} onConfirm={onConfirm} onAction={onAction}/>
    <Section title="Return since start" meta="Percent, each line indexed to 0% at the start of the range">
      <SleeveEquityChart points={series} sleeves={sleeves} benchmark={data.benchmark || "SPY"} events={events}/>
    </Section>
    <Section title="Strategy detail">
      {sleeves.length > 0 && <SleeveDetail api={api} sleeves={sleeves} id={curId} onSelect={setSelId}
        middle={curSleeve && <><BandChart key={`b-${curId}`} api={api} sleeve={curSleeve}/><TearSheet key={`t-${curId}`} api={api} sleeve={curSleeve}/></>} onConfirm={onConfirm} onAction={onAction}/>}
    </Section>
  </>;
}
