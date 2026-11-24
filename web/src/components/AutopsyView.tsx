"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Dialog from "./Dialog";
import type { Autopsy } from "@/lib/types";
import css from "./AutopsyView.module.css";

type Category = "process" | "good_process" | "outcome";

const categoryLabel: Record<Category, string> = {
  process: "Process Issue",
  good_process: "Good Process",
  outcome: "Outcome/Luck",
};

const categoryColor: Record<Category, string> = {
  process: "var(--color-process)",
  good_process: "var(--color-good-process)",
  outcome: "var(--color-outcome)",
};

export default function AutopsyView() {
  const [autopsies, setAutopsies] = useState<Autopsy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAutopsy, setSelectedAutopsy] = useState<Autopsy | null>(null);
  const [filter, setFilter] = useState<"all" | "win" | "loss" | "breakeven">("all");
  const [symbolFilter, setSymbolFilter] = useState("");

  useEffect(() => {
    let active = true;
    api<Autopsy[]>("/api/autopsies?limit=100")
      .then(data => { if (active) { setAutopsies(Array.isArray(data) ? data : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const filteredAutopsies = autopsies.filter((a) => {
    if (filter !== "all" && a.outcome_class !== filter) return false;
    if (symbolFilter && !a.symbol.toLowerCase().includes(symbolFilter.toLowerCase())) return false;
    return true;
  });

  const processCounts = filteredAutopsies.reduce(
    (acc, a) => {
      acc[a.process_class]++;
      return acc;
    },
    { bad: 0, good: 0, mixed: 0 } as Record<string, number>
  );

  return (
    <div className="autopsy-view">
      <header className="view-header">
        <h2>Trade Autopsies</h2>
        <div className="view-stats">
          <span className="stat stat-bad">Bad Process: {processCounts.bad}</span>
          <span className="stat stat-good">Good Process: {processCounts.good}</span>
          <span className="stat stat-mixed">Mixed: {processCounts.mixed}</span>
        </div>
      </header>

      <div className="filters">
        <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
          <option value="all">All Outcomes</option>
          <option value="win">Wins</option>
          <option value="loss">Losses</option>
          <option value="breakeven">Breakeven</option>
        </select>
        <input
          type="text"
          placeholder="Filter by symbol..."
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
        />
      </div>

      {loading && <div className="loading">Loading autopsies…</div>}
      {error && <div className="error">{error}</div>}

      <table className={`${css["autopsy-table"]}`}>
        <thead>
          <tr>
            <th>Trade</th>
            <th>Symbol</th>
            <th>Side</th>
            <th>P&L</th>
            <th>MAE</th>
            <th>MFE</th>
            <th>Hold</th>
            <th>Process</th>
            <th>Outcome</th>
            <th>Top Cause</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {filteredAutopsies.map((a) => (
            <tr
              key={a.id}
              className={selectedAutopsy?.id === a.id ? "selected" : ""}
              onClick={() => setSelectedAutopsy(a)}
            >
              <td>#{a.trade_id}</td>
              <td>{a.symbol}</td>
              <td className={a.side === "BUY" ? "side-buy" : "side-sell"}>{a.side}</td>
              <td className={a.pnl_pct > 0 ? "positive" : a.pnl_pct < 0 ? "negative" : "neutral"}>
                {a.pnl_pct > 0 ? "+" : ""}{a.pnl_pct.toFixed(2)}%
              </td>
              <td className="negative">{a.mae_pct.toFixed(2)}%</td>
              <td className="positive">{a.mfe_pct.toFixed(2)}%</td>
              <td>{a.hold_hours.toFixed(1)}h</td>
              <td>
                <span className={`process-badge process-${a.process_class}`}>
                  {a.process_class}
                </span>
              </td>
              <td>
                <span className={`outcome-badge outcome-${a.outcome_class}`}>
                  {a.outcome_class}
                </span>
              </td>
              <td className={`${css["top-cause"]}`}>
                {a.causes.length > 0 ? (
                  <>
                    <span className={categoryColor[a.causes[0].category as Category]}>
                      {a.causes[0].cause}
                    </span>
                    <span className="strength">({(a.causes[0].strength * 100).toFixed(0)}%)</span>
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td>
                <button className="button button-quiet" onClick={(e) => { e.stopPropagation(); setSelectedAutopsy(a); }}>
                  View
                </button>
              </td>
            </tr>
          ))}
          {filteredAutopsies.length === 0 && (
            <tr>
              <td colSpan={11} className="empty">No autopsies found</td>
            </tr>
          )}
        </tbody>
      </table>

      {selectedAutopsy && (
        <AutopsyDetail autopsy={selectedAutopsy} onClose={() => setSelectedAutopsy(null)} />
      )}
    </div>
  );
}

function AutopsyDetail({ autopsy, onClose }: { autopsy: Autopsy; onClose: () => void }) {
  return (
    <Dialog
      title={<>Autopsy #{autopsy.id} · {autopsy.symbol} {autopsy.side}</>}
      eyebrow="AUTOPSY"
      size="lg"
      className={`${css["autopsy-detail"]}`}
      onClose={onClose}
      footer={<button className="button" onClick={onClose}>Close</button>}
    >

        <div className="detail-grid">
          <div className="detail-section">
            <h4>Trade Summary</h4>
            <dl>
              <dt>P&L</dt>
              <dd className={autopsy.pnl_pct > 0 ? "positive" : autopsy.pnl_pct < 0 ? "negative" : "neutral"}>
                {autopsy.pnl_pct > 0 ? "+" : ""}{autopsy.pnl_pct.toFixed(2)}%
              </dd>
              <dt>MAE</dt>
              <dd className="negative">{autopsy.mae_pct.toFixed(2)}%</dd>
              <dt>MFE</dt>
              <dd className="positive">{autopsy.mfe_pct.toFixed(2)}%</dd>
              <dt>Hold Time</dt>
              <dd>{autopsy.hold_hours.toFixed(1)} hours</dd>
              <dt>Process Class</dt>
              <dd>
                <span className={`process-badge process-${autopsy.process_class}`}>
                  {autopsy.process_class}
                </span>
              </dd>
              <dt>Outcome Class</dt>
              <dd>
                <span className={`outcome-badge outcome-${autopsy.outcome_class}`}>
                  {autopsy.outcome_class}
                </span>
              </dd>
            </dl>
          </div>

          <div className="detail-section">
            <h4>Evidence-Based Causes (ranked by strength)</h4>
            {autopsy.causes.length === 0 ? (
              <p className="no-causes">No causes recorded</p>
            ) : (
              <table className={`${css["causes-table"]}`}>
                <thead>
                  <tr>
                    <th>Category</th>
                    <th>Cause</th>
                    <th>Evidence</th>
                    <th>Strength</th>
                  </tr>
                </thead>
                <tbody>
                  {autopsy.causes.map((cause, i) => (
                    <tr key={i}>
                      <td>
                        <span
                          className={`category-badge ${cause.category}`}
                          style={{ backgroundColor: categoryColor[cause.category as Category] }}
                        >
                          {categoryLabel[cause.category as Category]}
                        </span>
                      </td>
                      <td>{cause.cause}</td>
                      <td>{cause.evidence}</td>
                      <td>
                        <div className={`${css["strength-bar"]}`}>
                          <div
                            className={`${css["strength-fill"]}`}
                            style={{ width: `${cause.strength * 100}%` }}
                          />
                        </div>
                        <span className={`${css["strength-value"]}`}>{(cause.strength * 100).toFixed(0)}%</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {autopsy.llm_narration && (
          <div className={`detail-section ${css["narration"]}`}>
            <h4>LLM Narration</h4>
            <pre>{autopsy.llm_narration}</pre>
          </div>
        )}

    </Dialog>
  );
}