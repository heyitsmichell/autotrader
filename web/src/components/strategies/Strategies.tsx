"use client";

import { useEffect, useMemo, useState } from "react";
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
  const [data, setData] = useState<SleevesPayload | null>(null);
  const [series, setSeries] = useState<SleeveEquityPoint[]>([]);
  const [events, setEvents] = useState<SleeveEvent[]>([]);
  const [selId, setSelId] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    const load = () => api<SleevesPayload>("/api/sleeves")
      .then(d => { if (live) { setData(d); setErr(""); } })
      .catch(e => { if (live) setErr(e instanceof Error ? e.message : "Could not load strategies."); });
    void load();
    const t = window.setInterval(load, 30_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  useEffect(() => {
    let live = true;
    const load = () => api<{ series: SleeveEquityPoint[] }>("/api/sleeves/equity?days=365")
      .then(d => { if (live) setSeries(Array.isArray(d?.series) ? d.series : []); })
      .catch(() => { /* the chart just shows its empty state */ });
    const loadEv = () => api<{ events: SleeveEvent[] }>("/api/sleeves/events?days=400")
      .then(d => { if (live) setEvents(Array.isArray(d?.events) ? d.events : []); })
      .catch(() => { /* no markers */ });
    void loadEv();
    void load();
    const t = window.setInterval(() => { void load(); void loadEv(); }, 300_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  const sleeves = useMemo(() => {
    const list = sortSleeves(data?.sleeves ?? []);
    return data && !data.enabled ? list.map(s => ({ ...s, status: "disabled" as const })) : list;
  }, [data]);

  const curSleeve = sleeves.find(s => s.id === selId) ?? sleeves[0];
  const curId = curSleeve?.id ?? "";

  if (!data) return <Section title="Strategies">
    <p className={css["muted"]}>{err || "Loading…"}</p></Section>;

  const acct = data.account;
  const sumEq = acct?.sleeves_equity ?? sleeves.reduce((a, s) => a + (s.equity || 0), 0);
  const target = (acct?.target_per_sleeve ?? 0) * sleeves.length;
  const dayCls = !acct || !acct.day_pnl ? "" : acct.day_pnl > 0 ? css["gain"] : css["loss"];

  return <>
    {!data.enabled && <p className={css["note"]}>Multi-strategy mode is off (SLEEVES_ENABLED=false). Only Swing trades.</p>}
    {err && <p className={css["muted"]}>{err} Showing the last data received.</p>}
    <Section title="Strategies" meta="Four strategies share one account">
      {acct && <div className={css["strip"]}>
        <div><span>Account value</span><b>{money(acct.net_liq)}</b></div>
        <div><span>Day P&amp;L</span><b className={dayCls}>{money(acct.day_pnl, { sign: true })}</b></div>
        <div><span>In strategies</span><b>{money(sumEq)}{target ? <span> of {money(target)}</span> : null}</b></div>
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
