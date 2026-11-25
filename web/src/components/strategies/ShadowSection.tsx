"use client";

import { Fragment, useEffect, useState, type ReactNode } from "react";
import type { Api } from "@/lib/api";
import type { ConfirmRequest } from "@/components/ConfirmModal";
import { money, num, pct } from "@/lib/format";
import type { Shadow, ShadowEquityPoint, ShadowsPayload, Sleeve, SleeveEquityPoint } from "@/lib/types";
import Section from "@/components/ui/Section";
import ShadowChart from "./ShadowChart";
import ShadowDetail from "./ShadowDetail";
import { shadowColor } from "./shadowColors";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

type Item = Pick<Shadow, "id" | "name" | "rule" | "start_date" | "equity" | "total_return_pct" | "drawdown_pct"
  | "positions_n" | "last_rebalance" | "next_rebalance" | "live">;

const when = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? css["gain"] : css["loss"]);
const sharpe = (s: Item): ReactNode => {
  const days = s.live?.days ?? 0;
  if (days < 20 || s.live?.sharpe == null) return <span title="Needs at least 20 days of live history">—</span>;
  return <>{num(s.live.sharpe, 2)} <span className={css["muted"]}>({days} days)</span></>;
};
const cells = (s: Item): ReactNode[] => [
  when(s.start_date), money(s.equity),
  <span key="r" className={tone(s.total_return_pct)}>{pct(s.total_return_pct)}</span>,
  pct(s.drawdown_pct), String(s.positions_n ?? "—"), sharpe(s), when(s.last_rebalance), when(s.next_rebalance),
];
const HEAD = ["Started", "Equity", "Return since start", "Drawdown", "Positions", "Live Sharpe", "Last rebalance", "Next rebalance"];

export default function ShadowSection({ api, sleeves, onConfirm, onAction }: {
  api: Api; sleeves: Sleeve[];
  onConfirm?: (r: ConfirmRequest | null) => void;
  onAction?: (label: string, fn: () => Promise<unknown>) => void | Promise<void>;
}) {
  const [data, setData] = useState<ShadowsPayload | null>(null);
  const [err, setErr] = useState("");
  const [points, setPoints] = useState<ShadowEquityPoint[]>([]);
  const [liveSeries, setLiveSeries] = useState<SleeveEquityPoint[]>([]);
  const [open, setOpen] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    const load = () => api<ShadowsPayload>("/api/shadows")
      .then(d => { if (live) { setData(d); setErr(""); } })
      .catch(() => { if (live) setErr("Shadow strategies are not available right now."); });
    void load();
    const t = window.setInterval(load, 60_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api, tick]);

  useEffect(() => {
    let live = true;
    const load = () => {
      api<{ series: ShadowEquityPoint[] }>("/api/shadows/equity?days=400")
        .then(d => { if (live) setPoints(Array.isArray(d?.series) ? d.series : []); })
        .catch(() => { /* chart shows its empty state */ });
      api<{ series: SleeveEquityPoint[] }>("/api/sleeves/equity?days=400")
        .then(d => { if (live) setLiveSeries(Array.isArray(d?.series) ? d.series : []); })
        .catch(() => { /* no reference line */ });
    };
    load();
    const t = window.setInterval(load, 300_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api, tick]);

  const title = "Shadow strategies";
  if (!data) return <Section title={title} meta="Virtual money, no orders">
    <p className={css["muted"]}>{err || "Loading…"}</p></Section>;
  if (!data.enabled) return <Section title={title} meta="Virtual money, no orders">
    <p className={css["muted"]}>Shadow strategies are off (SHADOWS_ENABLED=false).</p></Section>;

  const shadows = Array.isArray(data.shadows) ? data.shadows : [];
  const refs = sleeves.map(s => ({ sleeveId: s.id, item: { ...s, name: `Live: ${s.name}`, rule: s.rebalance_rule } as Item }));
  const detail = (s: Shadow) => <ShadowDetail api={api} shadow={s} onConfirm={onConfirm} onAction={onAction}
    onReset={() => setTick(t => t + 1)}/>;
  const toggle = (id: string) => setOpen(o => (o === id ? "" : id));

  return <Section title={title} meta="Virtual money, no orders">
    {err && <p className={css["muted"]}>{err} Showing the last data received.</p>}
    {shadows.length === 0 ? <p className={css["muted"]}>No shadow strategies yet.</p> : <>
      <div className={css["tableWrap"]}>
        <table className={`${css["tbl"]} ${css["wide"]}`}>
          <thead><tr><th scope="col">Strategy</th>{HEAD.map(h => <th key={h} scope="col">{h}</th>)}</tr></thead>
          <tbody>
            {shadows.map((s, i) => <Fragment key={s.id}>
              <tr className={css["clickRow"]} onClick={() => toggle(s.id)}>
                <th scope="row" title={s.rule}><button type="button" className={css["rowBtn"]} aria-expanded={open === s.id}>
                  <i className={css["chip"]} style={{ background: shadowColor(s.id, i) }}/>{s.name}</button></th>
                {cells(s).map((c, j) => <td key={j}>{c}</td>)}
              </tr>
              {open === s.id && <tr><td colSpan={9}>{detail(s)}</td></tr>}
            </Fragment>)}
            {refs.map(({ sleeveId, item }) => <tr key={sleeveId} className={css["refRow"]}>
              <th scope="row" title={item.rule}><i className={css["chip"]} style={{ background: sleeveColor(sleeveId) }}/>{item.name}</th>
              {cells(item).map((c, j) => <td key={j}>{c}</td>)}</tr>)}
          </tbody>
        </table>
        <div className={css["blocks"]}>
          {shadows.map((s, i) => <div key={s.id} className={css["block"]}>
            <h3 title={s.rule}><button type="button" className={css["rowBtn"]} aria-expanded={open === s.id}
              onClick={() => toggle(s.id)}><i className={css["chip"]} style={{ background: shadowColor(s.id, i) }}/>{s.name}</button></h3>
            {HEAD.map((h, j) => <div key={h} className={css["kv"]}><span>{h}</span><span>{cells(s)[j]}</span></div>)}
            {open === s.id && detail(s)}
          </div>)}
          {refs.map(({ sleeveId, item }) => <div key={sleeveId} className={`${css["block"]} ${css["refRow"]}`}>
            <h3>{item.name}</h3>
            {HEAD.map((h, j) => <div key={h} className={css["kv"]}><span>{h}</span><span>{cells(item)[j]}</span></div>)}
          </div>)}
        </div>
      </div>
      <ShadowChart shadows={shadows} points={points} live={liveSeries}/>
    </>}
  </Section>;
}
