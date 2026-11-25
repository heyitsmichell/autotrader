"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import type { BtMl } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import { EvidenceLinks } from "./EvidenceCards";
import css from "./Bt.module.css";

const TONE: Record<string, "gain" | "warn" | "neutral"> = { champion: "gain", challenger: "neutral", failed: "warn" };
const local = (v?: string) => {
  const t = v ? new Date(v) : null;
  return t && !Number.isNaN(t.getTime()) ? t.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
};
const show = (v: unknown) => (v == null ? "—" : typeof v === "number" ? String(Math.round(v * 1e4) / 1e4) : String(v));

export default function MlLab({ api }: { api: Api }) {
  const [d, setD] = useState<BtMl | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<BtMl>("/api/backtests/ml").then(r => { if (live) setD(r ?? {}); })
      .catch(() => { if (live) setErr("The ML lab is not available yet."); });
    return () => { live = false; };
  }, [api]);
  if (err) return <Empty>{err}</Empty>;
  if (!d) return <Empty>Loading the ML lab.</Empty>;
  const models = d.models ?? [];
  const om = d.options_model ? Object.entries(d.options_model).filter(([, v]) => v == null || typeof v !== "object") : [];
  return <div className={css["wrap"]}>
    {d.summary && <p className={css["verdict"]}>{d.summary}</p>}
    <Section title="Models">
      {models.length ? <div className={css["scroll"]}><table>
        <thead><tr><th scope="col">Model</th><th scope="col">Status</th><th scope="col" className="num">AUC</th><th scope="col">Created</th></tr></thead>
        <tbody>{models.map(m => <tr key={String(m.id)}><td className="mono">{String(m.id)}</td>
          <td><Mark tone={TONE[m.status ?? ""] ?? "neutral"}>{m.status ?? "—"}</Mark></td>
          <td className="num">{m.auc == null ? "—" : m.auc.toFixed(3)}</td><td>{local(m.created_at)}</td></tr>)}</tbody>
      </table></div> : <Empty>No models have been trained yet.</Empty>}
    </Section>
    <Section title="Options model">
      {om.length ? <dl className={css["kv"]}>{om.map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{k.replace(/_/g, " ")}</dt><dd>{show(v)}</dd></div>)}</dl>
        : <Empty>No options model is loaded.</Empty>}
    </Section>
    <Section title="ML experiments">
      {d.experiments?.length ? d.experiments.map(e => <div key={e.id} className={css["card"]}>
        <span className={css["name"]}>{e.name || e.id}</span>
        {e.verdict && <p className={css["verdict"]}>{e.verdict}</p>}
        <EvidenceLinks api={api} items={e.evidence}/>
      </div>) : <Empty>No ML experiments recorded yet.</Empty>}
    </Section>
  </div>;
}
