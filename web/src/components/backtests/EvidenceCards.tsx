"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { num, pct, shortDate } from "@/lib/format";
import type { BtCard, BtEvidence } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import { shadowColor } from "../strategies/shadowColors";
import { sleeveColor } from "../strategies/sleeveColors";
import css from "./Bt.module.css";

const P = (v: number | null | undefined, signed = false) => (v == null ? "—" : pct(v * 100, { digits: 1, sign: signed }));

/** Evidence links: the report is fetched through the api client and shown in an expandable block. */
export function EvidenceLinks({ api, items }: { api: Api; items?: BtEvidence[] }) {
  const [open, setOpen] = useState<{ path: string; text: string | null } | null>(null);
  const list = (items ?? []).filter(e => e && e.path && e.exists !== false);
  if (!list.length) return null;
  const load = (path: string) => {
    if (open?.path === path) { setOpen(null); return; }
    setOpen({ path, text: null });
    api<{ text?: string }>(`/api/experiments/report?path=${encodeURIComponent(path)}`)
      .then(r => setOpen(o => (o?.path === path ? { path, text: r?.text ?? "This report is empty." } : o)))
      .catch(() => setOpen(o => (o?.path === path ? { path, text: "Unable to load this report." } : o)));
  };
  return <div>
    <div className={css["links"]}>{list.map(e => <button key={e.path} type="button" className={css["link"]}
      aria-expanded={open?.path === e.path} onClick={() => load(e.path)}>{e.title || e.path.split("/").pop()}</button>)}</div>
    {open && <pre className={css["pre"]}>{open.text ?? "Loading report."}</pre>}
  </div>;
}

export default function EvidenceCards({ api }: { api: Api }) {
  const [cards, setCards] = useState<BtCard[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api<{ cards?: BtCard[] }>("/api/backtests/strategies").then(d => { if (live) setCards(Array.isArray(d?.cards) ? d.cards : []); })
      .catch(() => { if (live) setErr("Strategy evidence is not available yet."); });
    return () => { live = false; };
  }, [api]);
  if (err) return <Empty>{err}</Empty>;
  if (!cards) return <Empty>Loading strategy evidence.</Empty>;
  if (!cards.length) return <Empty>No strategies to show yet.</Empty>;
  let shadowIdx = 0;
  return <Section title="Evidence per strategy" meta="Live first, then shadows">
    {cards.map(c => {
      const shadow = c.stage === "shadow";
      const color = shadow ? shadowColor(c.id, shadowIdx++) : sleeveColor(c.id);
      const b = c.backtest, l = c.live;
      const short = l == null || l.days == null || l.days < 20;
      return <div key={c.id} className={css["card"]}>
        <div className={css["head"]}>
          <i className={css["chip"]} style={{ background: color }}/>
          <span className={css["name"]}>{c.name || c.id}</span>
          <Mark tone={shadow ? "neutral" : "gain"}>{shadow ? "Shadow" : "Live"}</Mark>
          {b?.biased && <Mark tone="warn" title="Universe uses today's constituents">biased</Mark>}
        </div>
        {b ? <div className={css["stats"]}>
          <span><b>CAGR</b>{P(b.cagr, true)}</span><span><b>Sharpe</b>{num(b.sharpe)}</span>
          <span><b>Max DD</b>{P(b.max_dd)}</span>{b.window && <span><b>Window</b>{b.window}</span>}
          {b.as_of && <span title="When this backtest result was last written"><b>As of</b>{shortDate(b.as_of, true)}</span>}
        </div> : <span className={css["muted"]} title={c.note ?? undefined}>No backtest yet</span>}
        <div className={css["stats"]}>
          {short ? <span className={css["muted"]}>Live: —, needs 20 days{l?.days != null ? ` (${l.days} so far)` : ""}</span> : <>
            <span><b>Live days</b>{l?.days}</span>
            <span><b>Return</b>{l?.return_pct == null ? "—" : pct(l.return_pct)}</span>
            <span><b>Sharpe</b>{num(l?.sharpe)}</span></>}
          {c.trials?.n != null && <span>{c.trials.n} trials ({c.trials.lab ?? "lab"})</span>}
        </div>
        {c.verdict && <p className={css["verdict"]}>{c.verdict}</p>}
        <EvidenceLinks api={api} items={c.evidence}/>
      </div>;
    })}
  </Section>;
}
