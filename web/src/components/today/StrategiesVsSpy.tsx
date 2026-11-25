"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import type { Performance, SleeveEquityPoint, SleevesPayload } from "@/lib/types";
import Section from "@/components/ui/Section";
import EquityChart from "../EquityChart";
import SleeveEquityChart from "../strategies/SleeveEquityChart";

/** Today chart: every strategy and SPY indexed to 0% on the day the strategies went live.
 *  Falls back to the account-vs-SPY chart when multi-strategy mode is off. */
export default function StrategiesVsSpy({ api, performance }: { api: Api; performance: Performance | null }) {
  const [data, setData] = useState<SleevesPayload | null>(null);
  const [series, setSeries] = useState<SleeveEquityPoint[]>([]);
  const [start, setStart] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const load = () => {
      api<SleevesPayload>("/api/sleeves").then(d => { if (live) setData(d); }).catch(() => { /* fallback chart */ });
      api<{ series: SleeveEquityPoint[]; start_date?: string | null }>("/api/sleeves/equity?days=3650")
        .then(d => {
          if (!live) return;
          setSeries(Array.isArray(d?.series) ? d.series : []);
          setStart(d?.start_date ?? null);
        })
        .catch(() => { /* empty state */ });
    };
    load();
    const t = window.setInterval(load, 300_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  if (!data?.enabled || !data.sleeves?.length) return <EquityChart performance={performance}/>;
  const bench = data.benchmark || "SPY";
  return <Section title={`Strategies vs ${bench}${start ? ` since ${start}` : ""}, % return`}>
    <SleeveEquityChart points={series} sleeves={data.sleeves} benchmark={bench}/>
  </Section>;
}
