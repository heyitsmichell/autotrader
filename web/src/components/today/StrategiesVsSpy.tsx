"use client";

import { useMemo } from "react";
import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { Performance, SleeveEquityPoint, SleevesPayload } from "@/lib/types";
import Section from "@/components/ui/Section";
import EquityChart from "../EquityChart";
import SleeveEquityChart from "../strategies/SleeveEquityChart";

/** Today chart: every strategy and SPY indexed to 0% on the day the strategies went live.
 *  Falls back to the account-vs-SPY chart when multi-strategy mode is off. */
export default function StrategiesVsSpy({ api, performance }: { api: Api; performance: Performance | null }) {

  const data = usePoll<SleevesPayload>(api, "/api/sleeves", 300_000).data;
  const eq = usePoll<{ series: SleeveEquityPoint[]; start_date?: string | null }>(api, "/api/sleeves/equity?days=3650", 300_000).data;
  const series = useMemo(() => (Array.isArray(eq?.series) ? eq.series : []), [eq]);
  const start = eq?.start_date ?? null;

  if (!data?.enabled || !data.sleeves?.length) return <EquityChart performance={performance}/>;
  const bench = data.benchmark || "SPY";
  return <Section title={`Strategies vs ${bench}${start ? ` since ${start}` : ""}, % return`}>
    <SleeveEquityChart points={series} sleeves={data.sleeves} benchmark={bench}/>
  </Section>;
}
