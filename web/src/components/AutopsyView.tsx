"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Dialog from "./Dialog";
import Section from "./ui/Section";
import Segmented from "./ui/Segmented";
import Mark from "./ui/Mark";
import Empty from "./ui/Empty";
import { pct } from "@/lib/format";
import type { Autopsy } from "@/lib/types";
import css from "./AutopsyView.module.css";
import rcss from "./results/results.module.css";

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

const CAP = 25;
const processTone = (c: string): "gain" | "loss" | "neutral" => c === "good" ? "gain" : c === "bad" ? "loss" : "neutral";
const pnlCls = (v: number) => (v === 0 ? "" : v > 0 ? rcss["gain"] : rcss["loss"]);

/** Hours under 2 days, days after; never negative. */
function holdText(hours: number | null | undefined): string {
  if (hours == null || !Number.isFinite(hours)) return "—";
  const h = Math.max(0, hours);
  return h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} d`;
}

export default function AutopsyView() {
  const [autopsies, setAutopsies] = useState<Autopsy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAutopsy, setSelectedAutopsy] = useState<Autopsy | null>(null);
  const [filter, setFilter] = useState<"all" | "win" | "loss" | "breakeven">("all");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let active = true;
    api<Autopsy[]>("/api/autopsies?limit=100")
      .then(data => { if (active) { setAutopsies(Array.isArray(data) ? data : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const filtered = autopsies.filter((a) => {
    if (filter !== "all" && a.outcome_class !== filter) return false;
    if (symbolFilter && !a.symbol.toLowerCase().includes(symbolFilter.toLowerCase())) return false;
    return true;
  });
  const count = (c: string) => filtered.filter(a => a.process_class === c).length;
  const shown = showAll ? filtered : filtered.slice(0, CAP);
  const meta = `${filtered.length} reviewed · ${count("good")} good process · ${count("bad")} bad · ${count("mixed")} mixed`;

  return (
    <div className="autopsy-view">
      <Section title="Trade autopsies" meta={meta} right={
        <div className={rcss["filters"]}>
          <Segmented label="Outcome" value={filter} onChange={setFilter}
            options={[{ value: "all", label: "All" }, { value: "win", label: "Wins" }, { value: "loss", label: "Losses" }, { value: "breakeven", label: "Breakeven" }]} />
          <input type="text" aria-label="Filter by symbol" placeholder="Symbol" value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)} />
        </div>
      }>
        {loading && <p className="loading">Loading autopsies…</p>}
        {error && <p className="error">{error}</p>}
        {filtered.length === 0 && !loading ? (
          <Empty>{autopsies.length === 0 ? "No autopsies yet. One is written after each closed trade." : "No autopsies match this filter."}</Empty>
        ) : (
          <div className={rcss["tableWrap"]}>
            <table>
              <thead>
                <tr>
                  <th>Trade</th><th>Symbol</th><th>Side</th>
                  <th className="num">Return</th><th className="num">Worst dip</th><th className="num">Best run</th><th className="num">Hold</th>
                  <th>Process</th><th>Outcome</th><th>Top cause</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.map((a) => (
                  <tr key={a.id} onClick={() => setSelectedAutopsy(a)}>
                    <td className="mono">#{a.trade_id}</td>
                    <td>{a.symbol}</td>
                    <td>{a.side}</td>
                    <td className={`num mono ${pnlCls(a.pnl_pct)}`}>{pct(a.pnl_pct)}</td>
                    <td className="num mono">{a.mae_pct == null ? "—" : pct(a.mae_pct, { sign: false })}</td>
                    <td className="num mono">{a.mfe_pct == null ? "—" : pct(a.mfe_pct, { sign: false })}</td>
                    <td className="num mono">{holdText(a.hold_hours)}</td>
                    <td><Mark tone={processTone(a.process_class)}>{a.process_class}</Mark></td>
                    <td><Mark>{a.outcome_class}</Mark></td>
                    <td>{a.causes.length > 0 ? <>{a.causes[0].cause} <span className={`mono ${rcss["dim"]}`}>({(a.causes[0].strength * 100).toFixed(0)}%)</span></> : "—"}</td>
                    <td><button className="button" onClick={(e) => { e.stopPropagation(); setSelectedAutopsy(a); }}>View</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!showAll && filtered.length > CAP &&
          <button className={`button ${rcss["more"]}`} onClick={() => setShowAll(true)}>Show all {filtered.length}</button>}
      </Section>

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
              <dd className="negative">{autopsy.mae_pct == null ? "—" : `${autopsy.mae_pct.toFixed(2)}%`}</dd>
              <dt>MFE</dt>
              <dd className="positive">{autopsy.mfe_pct == null ? "—" : `${autopsy.mfe_pct.toFixed(2)}%`}</dd>
              <dt>Hold Time</dt>
              <dd>{holdText(autopsy.hold_hours)}</dd>
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