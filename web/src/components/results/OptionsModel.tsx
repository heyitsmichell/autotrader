"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Figure, FigureRow } from "@/components/ui/Figure";
import Section from "@/components/ui/Section";
import Empty from "@/components/ui/Empty";
import { dateTime, num, pct } from "@/lib/format";
import type { OptionsModelResponse } from "@/lib/types";
import css from "./results.module.css";

export default function OptionsModel() {
  const [d, setD] = useState<OptionsModelResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [training, setTraining] = useState(false);
  const [trainMsg, setTrainMsg] = useState<string | null>(null);

  async function trainNow() {
    if (!window.confirm("Retrain the options model now? It takes a few seconds and does not place any orders.")) return;
    setTraining(true); setTrainMsg(null);
    try {
      const r = await api<OptionsModelResponse>("/api/options/train", "POST");
      setD(r); setTrainMsg("Retrained.");
    } catch (e: unknown) {
      setTrainMsg(e instanceof Error ? e.message : "Training failed");
    } finally { setTraining(false); }
  }
  const trainBtn = <span>
    <button className="button" disabled={training} onClick={trainNow}>{training ? "Training…" : "Train now"}</button>
    {trainMsg && <span className={css["muted"]}> {trainMsg}</span>}
  </span>;

  useEffect(() => {
    let active = true;
    api<OptionsModelResponse>("/api/options/model")
      .then(r => { if (active) { setD(r); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); });
    return () => { active = false; };
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!d) return <p className="loading">Loading options model…</p>;
  if (!d.available) {
    return <Section title="Options model" meta="Learning mode" right={trainBtn}>
      <Empty>No options model yet. Press Train now, or it trains itself when OPTIONS_ML_ENABLED is on.</Empty>
    </Section>;
  }
  const rows = (d.n_backtest ?? 0) + (d.n_live ?? 0);
  const auc = d.val_auc;
  return <Section title="Options model" meta={d.version ?? undefined} right={trainBtn}>
    <FigureRow>
      <Figure label="Trained" value={dateTime(d.trained_at)} />
      <Figure label="Rows" value={rows.toLocaleString("en-US")} title={`${d.n_backtest ?? 0} modelled + ${d.n_live ?? 0} live`} />
      <Figure label="Validation AUC" value={auc != null ? `${num(auc, 3)} vs 0.50` : "—"}
        title="0.50 means the model cannot tell winners from losers" />
      <Figure label="Baseline win rate" value={d.baseline_win_rate != null ? pct(d.baseline_win_rate * 100, { sign: false, digits: 1 }) : "—"} />
      <Figure label="Explore" value={pct(d.explore_pct * 100, { sign: false, digits: 0 })} />
    </FigureRow>
    {d.min_p === 0 && <p className={css["muted"]}>Learning mode: the model ranks and explores but does not block trades.</p>}
    {d.recent.length === 0
      ? <Empty>No model-scored option entries yet.</Empty>
      : <div className="table-scroll"><table>
        <thead><tr>
          <th scope="col">Contract</th><th scope="col" className="num">P(win)</th>
          <th scope="col">Mode</th><th scope="col">Status</th><th scope="col" className="num">Return</th>
        </tr></thead>
        <tbody>{d.recent.map(r => <tr key={r.id}>
          <th scope="row">{r.symbol}</th>
          <td className="num">{r.ml_p != null ? num(r.ml_p, 2) : "—"}</td>
          <td>{r.explore ? `explore${r.arm ? ` ${num(r.arm[0], 2)}Δ/${r.arm[1]}d` : ""}` : "target"}</td>
          <td>{r.status ?? "—"}</td>
          <td className={`num ${r.ret == null ? "" : r.ret > 0 ? "positive" : "negative"}`}>
            {r.ret != null ? pct(r.ret * 100, { sign: true, digits: 1 }) : "open"}
          </td>
        </tr>)}</tbody></table></div>}
  </Section>;
}
