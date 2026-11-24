"use client";

import { useMemo } from "react";
import { minutesAgo, money, score } from "@/lib/format";
import type { MlStatus, Signal, Status } from "@/lib/types";
import css from "./SignalsTable.module.css";

export default function SignalsTable({ signals, status, ml, onSelectSymbol }: {
  signals: Signal[];
  status: Status | null;
  ml: MlStatus | null;
  onSelectSymbol: (symbol: string) => void;
}) {
  const watchlist = status?.watchlist || [];
  const bySymbol = useMemo(() => new Map(signals.map(s => [s.symbol, s])), [signals]);
  const rows = watchlist.length ? watchlist.map(symbol => bySymbol.get(symbol) || { symbol }) : signals;
  const activeModel = useMemo(() => {
    const id = ml?.active_id ?? status?.ml?.active_id;
    return id == null ? null : (ml?.models || []).find(model => model.id === id) || null;
  }, [ml, status]);

  const saturation = useMemo(() => {
    const values = signals.map(s => s.ml_p).filter((p): p is number => p != null);
    if (!values.length) return null;
    const extreme = values.filter(p => p < 0.05 || p > 0.95).length;
    return { extreme, total: values.length, saturated: extreme / values.length > 0.8 };
  }, [signals]);

  const modelNote = activeModel?.note || "";
  const suspect = !activeModel ? false : /mock|synthetic|invalid|retired/i.test(`${modelNote} ${activeModel.status}`);

  return <section className="panel">
    <div className="panel-head">
      <div><div className="eyebrow">MODEL MONITOR</div><h2>Watchlist signals</h2></div>
      <div className="panel-actions">
        <ModelBadge ml={ml} model={activeModel} suspect={suspect} saturation={saturation} />
        <span className="muted" suppressHydrationWarning>
          {(() => {
            const age = minutesAgo(status?.signals?.last_refresh);
            return age == null ? "Waiting for automatic snapshot" : `Updated ${age}m ago · every ${status?.signals?.refresh_minutes ?? 1}m`;
          })()}
        </span>
      </div>
    </div>
    {status?.signals?.error && <div className="inline-warning" role="alert">{status.signals.error}</div>}
    <div className="table-scroll">
      <table className="signals-table">
        <thead><tr>
          <th>SYMBOL</th><th>REGIME</th><th>TECHNICAL</th><th>NEWS SENTIMENT</th><th>MODEL</th><th>BUY GATE</th>
        </tr></thead>
        <tbody>
          {rows.map((s) => {
            const checks = s.consensus_checks || [];
            const failed = checks.filter(check => !check.ok);
            const blocked = s.consensus === "blocked";
            const age = minutesAgo(s.created_at);
            const stale = age != null && age > Math.max(15, (status?.signals?.refresh_minutes ?? 1) * 3);
            const sentimentUnavailable = s.sent_score == null || !s.sent_n;
            return <tr key={s.symbol}>
              <td>
                <button className={`${css["ticker-button"]}`} onClick={() => onSelectSymbol(s.symbol)}>
                  {s.symbol}
                </button>
                <small>{s.close == null ? "No snapshot" : money(s.close)}</small>
                {stale && <small className={`${css["stale-note"]}`} title={`Signal snapshot is ${age} minutes old`}>stale · {age}m</small>}
              </td>
              <td><span className={`regime ${s.trend || "unknown"}`}>{s.trend || "—"}</span><small>{s.strength || ""} {s.vol ? `· ${s.vol} vol` : ""}</small></td>
              <td><div className={`${css["score-value"]} ${Number(s.ta_score) >= 0 ? "positive" : "negative"}`}>{score(s.ta_score)}</div><small>{(s.ta_components || []).filter((c) => Number(c.points) !== 0).length} of {s.ta_components?.length || 0} rules active</small></td>
              <td>
                {sentimentUnavailable
                  ? <><div className={`${css["score-value"]} muted`}>Unavailable</div><small>not treated as neutral</small></>
                  : <><div className={`${css["score-value"]} ${Number(s.sent_score) > 0 ? "positive" : Number(s.sent_score) < 0 ? "negative" : ""}`}>{score(s.sent_score)}</div><small>{s.sent_n ?? 0} headlines · {s.sentiment_source || status?.sentiment?.source || "Webull"}</small></>}
              </td>
              <td>
                {s.ml_p == null
                  ? <><span className="muted">Rules only</span><small>no active model</small></>
                  : <><div className={`${css["score-value"]}`}>{Math.round(s.ml_p * 100)}%</div><small>5d up probability{activeModel ? ` · model #${activeModel.id}` : ""}</small></>}
              </td>
              <td>
                <span className={`${css["gate-chip"]} ${blocked ? "blocked" : s.consensus ? "passed" : `${css["idle"]}`}`}>
                  {s.consensus ? blocked ? "Blocked" : "Pass" : "Waiting"}
                </span>
                {failed.length > 0
                  ? <ul className={`${css["fail-list"]}`}>
                      {failed.map(check => <li key={check.name} title={check.detail || ""}>
                        <b>{check.name}</b>{check.detail ? <span> · {check.detail}</span> : null}
                      </li>)}
                    </ul>
                  : <small>{checks.length ? "All checks clear" : "Awaiting data"}</small>}
              </td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>
    <div className="panel-foot">Buy gates are deterministic. Sells to close held shares bypass signal filters.</div>
  </section>;
}

function ModelBadge({ ml, model, suspect, saturation }: {
  ml: MlStatus | null;
  model: NonNullable<MlStatus["models"]>[number] | null;
  suspect: boolean;
  saturation: { extreme: number; total: number; saturated: boolean } | null;
}) {
  if (!ml?.enabled) return <span className={`${css["model-badge"]} neutral`}>ML filter off</span>;
  if (!model) return <span className={`${css["model-badge"]} neutral`}>Rules only</span>;
  const provenance = (model.metrics?.broker_mode as string | undefined)
    || (model.metrics?.environment as string | undefined)
    || "provenance not recorded";
  return <span
    className={`${css["model-badge"]} ${suspect || saturation?.saturated ? "warn" : "healthy"}`}
    title={`Model #${model.id} · ${model.status} · trained ${model.created_at} · ${model.n_symbols ?? "?"} symbols · ${model.n_samples ?? "?"} samples · ${provenance}${model.note ? ` · ${model.note}` : ""}`}
  >
    Model #{model.id} · {model.status}
    {suspect ? " · suspect" : saturation?.saturated ? ` · saturated (${saturation.extreme}/${saturation.total})` : ""}
    {!suspect && !saturation?.saturated ? ` · ${provenance}` : ""}
  </span>;
}
