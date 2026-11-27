"use client";
import { shortDate } from "@/lib/format";
import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import { pct } from "@/lib/format";
import type { Sleeve, SleeveTearSheet } from "@/lib/types";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import Segmented from "@/components/ui/Segmented";
import MonthlyHeatmap from "./MonthlyHeatmap";
import RollingSharpe from "./RollingSharpe";
import Underwater from "./Underwater";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

type Src = "live" | "backtest";
const p = (v: number | null | undefined) => (v == null ? "—" : pct(v * 100));
const r2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

export default function TearSheet({ api, sleeve }: { api: Api; sleeve: Sleeve }) {
  const id = sleeve.id;
  const [src, setSrc] = useState<Src>("live");
  const [d, setD] = useState<SleeveTearSheet | null>(null);
  const [msg, setMsg] = useState("Loading…");
  const [liveDays, setLiveDays] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    api<SleeveTearSheet>(`/api/sleeves/${id}/tearsheet?source=${src}&window=${src === "live" ? 63 : 126}`)
      .then(r => {
        if (!live) return;
        if (src === "live" && r) {
          const n = r.days ?? 0;
          setLiveDays(n);
          if (n < 20) { setSrc("backtest"); return; }
        }
        if (r && r.metrics) setD(r); else setMsg("No report data yet.");
      })
      .catch(() => { if (live) setMsg("The report is not available right now."); });
    return () => { live = false; };
  }, [api, id, src]);
  const cur = d && d.source === src ? d : null;
  const m = cur?.metrics;
  const short = liveDays != null && liveDays < 20;
  return <Section title="Report" right={<Segmented<Src> label="Report source" value={src} onChange={setSrc}
    options={[{ value: "live", label: "Live" }, { value: "backtest", label: "Backtest" }]}/>}>
    {short && <p className={css["muted"]}>Live history is short ({liveDays} days); showing the backtest</p>}
    {!cur || !m ? <p className={css["muted"]}>{msg}</p> : <>
      <FigureRow>
        <Figure label="CAGR" value={p(m.cagr)}/>
        <Figure label="Volatility" value={p(m.vol)}/>
        <Figure label="Sharpe" value={r2(m.sharpe)}/>
        <Figure label="Sortino" value={r2(m.sortino)}/>
        <Figure label="Max drawdown" value={p(m.max_dd)}/>
        <Figure label="Calmar" value={r2(m.calmar)}/>
        <Figure label="Best day" value={p(m.best_day)}/>
        <Figure label="Worst day" value={p(m.worst_day)}/>
        <Figure label="Positive days" value={m.pct_positive_days == null ? "—" : pct(m.pct_positive_days * 100, { sign: false, digits: 0 })}/>
      </FigureRow>
      <h3 className={css["h3"]}>Drawdown</h3>
      <Underwater data={cur.drawdown ?? []}/>
      <h3 className={css["h3"]}>Monthly returns</h3>
      <MonthlyHeatmap monthly={cur.monthly ?? []} yearly={cur.yearly ?? []}/>
      <h3 className={css["h3"]}>Rolling Sharpe ({src === "live" ? "63" : "126"}-day)</h3>
      <RollingSharpe data={cur.rolling_sharpe ?? []} color={sleeveColor(sleeve.id, sleeve.color_slot)}/>
      <h3 className={css["h3"]}>Worst drawdowns</h3>
      {cur.worst_drawdowns?.length ? <div className={css["scroll"]}><table className={css["dtbl"]}>
        <thead><tr><th>Start</th><th>Trough</th><th>Recovered</th><th>Depth</th><th>Days</th></tr></thead>
        <tbody>{cur.worst_drawdowns.map((w, i) => <tr key={i}><td>{shortDate(w.start, true)}</td><td>{shortDate(w.trough, true)}</td>
          <td>{w.end ? shortDate(w.end, true) : "not yet"}</td><td>{p(w.depth)}</td><td>{w.days}</td></tr>)}</tbody>
      </table></div> : <p className={css["muted"]}>No drawdowns to list.</p>}
    </>}
  </Section>;
}
