"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { num, pct } from "@/lib/format";
import type { BtPipeline, BtPipelineEntry } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Section from "@/components/ui/Section";
import { EvidenceLinks } from "./EvidenceCards";
import css from "./Bt.module.css";

function shadowLine(e: BtPipelineEntry): string {
  const a = e.live_stats, b = e.compare_stats;
  if (a && b && (a.days ?? 0) >= 20 && (b.days ?? 0) >= 20)
    return `Shadow vs ${e.compare_to}: return ${pct(a.return_pct, { digits: 1 })} vs ${pct(b.return_pct, { digits: 1 })}, Sharpe ${num(a.sharpe)} vs ${num(b.sharpe)}`;
  return `Collecting data since ${e.since ?? "—"}`;
}

export default function PipelineView({ api }: { api: Api }) {
  const [d, setD] = useState<BtPipeline | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<BtPipeline>("/api/backtests/pipeline").then(r => { if (live) setD(r ?? {}); })
      .catch(() => { if (live) setErr("The pipeline is not available yet."); });
    return () => { live = false; };
  }, [api]);
  if (err) return <Empty>{err}</Empty>;
  if (!d) return <Empty>Loading the pipeline.</Empty>;
  const cols: [string, BtPipelineEntry[], boolean][] = [
    ["Live", d.stages?.live ?? [], false], ["Shadow", d.stages?.shadow ?? [], true], ["Rejected", d.stages?.rejected ?? [], false]];
  return <div className={css["cols"]}>{cols.map(([title, items, sh]) => <div key={title} className={css["col"]}>
    <Section title={title} meta={String(items.length)}>
      {items.length ? items.map(e => <div key={e.id} className={css["card"]}>
        <span className={css["name"]}>{e.name || e.id}</span>
        <span className={css["muted"]}>Since {e.since ?? "—"}</span>
        {sh && <span className={css["muted"]}>{shadowLine(e)}</span>}
        {e.verdict && <p className={css["verdict"]}>{e.verdict}</p>}
        <EvidenceLinks api={api} items={e.evidence}/>
      </div>) : <Empty>Nothing here yet.</Empty>}
    </Section></div>)}</div>;
}
