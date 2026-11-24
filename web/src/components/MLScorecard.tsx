"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { MlStatus, ModelInfo } from "@/lib/types";
import { useConfirm } from "./ConfirmModal";
import css from "./MLScorecard.module.css";

interface MLScoreCard {
  precision: number | null;
  recall: number | null;
  f1: number | null;
  calibration_error: number | null;
  auc: number | null;
  saturation_pct: number | null;
  feature_importance: Array<{ feature: string; importance: number }>;
}

const EMPTY_SCORECARD: MLScoreCard = {
  precision: null, recall: null, f1: null, calibration_error: null, auc: null,
  saturation_pct: null, feature_importance: [],
};

type Perform = (label: string, fn: () => Promise<unknown>) => Promise<void>;

export default function MLScorecard({ ml, onRefresh, onAction, busy = false }: {
  ml: MlStatus | null;
  onRefresh: () => Promise<unknown> | void;
  onAction: Perform;
  busy?: boolean;
}) {
  const mlStatus = ml;
  const models: ModelInfo[] = ml?.models || [];
  const [scorecard, setScorecard] = useState<MLScoreCard | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const ask = useConfirm();
  const scorecardFor = picked ?? models[0]?.id ?? null;
  const fetchScorecard = setPicked;

  useEffect(() => {
    if (scorecardFor == null) return;
    let active = true;
    api<MLScoreCard>(`/api/ml/${scorecardFor}/scorecard`)
      .then(sc => { if (active) setScorecard(sc); })
      .catch(() => { if (active) setScorecard(EMPTY_SCORECARD); });   // the scorecard is optional
    return () => { active = false; };
  }, [scorecardFor]);

  const act = (label: string, path: string, title: string, message: string, danger = false) => ask({
    title,
    confirmLabel: label,
    danger,
    message: <p>{message}</p>,
    onConfirm: async () => {
      await onAction(`ML: ${label}`, () => api(path, "POST"));
      await onRefresh();
    },
  });

  const challenger = mlStatus?.challenger;
  const champion = mlStatus?.champion;

  if (!mlStatus) return <div className="loading">Loading ML status…</div>;

  const controls = <div className={`${css["ml-controls"]}`} role="group" aria-label="Model controls">
    <button className="button button-quiet" disabled={busy || mlStatus.training || mlStatus.available === false}
      title={mlStatus.available === false ? (mlStatus.error || "PyTorch is not available") : undefined}
      onClick={() => act("Train", "/api/ml/train", "Train a new challenger model?",
        "Training runs in the background on the universe's history. The new model is evaluated in shadow mode before it can be promoted.")}
    >{mlStatus.training ? "Training…" : "Train challenger"}</button>
    <button className="button button-quiet" disabled={busy || !challenger}
      onClick={() => act("Promote", "/api/ml/promote", `Promote challenger #${challenger?.id}?`,
        "The challenger becomes the champion that filters live entries. Signals are re-scored right away.", true)}
    >Promote challenger</button>
    <button className="button button-quiet" disabled={busy || !challenger}
      onClick={() => act("Reject", "/api/ml/reject", `Reject challenger #${challenger?.id}?`,
        "The challenger is discarded so a new one can be trained.")}
    >Reject challenger</button>
    <button className="button button-quiet" disabled={busy || !champion}
      onClick={() => act("Deactivate", "/api/ml/deactivate", "Deactivate the ML filter's champion?",
        "Entries are then gated by the rules only (TA, sentiment, risk) until a model is promoted again.", true)}
    >Deactivate champion</button>
  </div>;

  return (
    <div className="ml-scorecard-view">
      <header className="view-header">
        <h2>ML Scorecard</h2>
        <div className="ml-status">
          <span className={mlStatus.available ? "status-good" : "status-bad"}>
            {mlStatus.available ? "Available" : "Unavailable"}
          </span>
          {mlStatus.training && <span className="status-training">Training…</span>}
          {champion && <span>Champion: #{champion.id}</span>}
          {challenger && <span>Challenger (shadow): #{challenger.id}</span>}
        </div>
      </header>
      {mlStatus.error && <p className="inline-warning">{mlStatus.error}</p>}
      {controls}

      <section className="scorecard-section">
        <h3>Gate-Check Metrics</h3>
        <div className={`${css["metrics-grid"]}`}>
          <MetricCard
            label="Precision"
            value={scorecard?.precision != null ? (scorecard.precision * 100).toFixed(1) + "%" : "—"}
            threshold="> 50%"
            status={scorecard?.precision != null && scorecard.precision > 0.5 ? "pass" : "fail"}
          />
          <MetricCard
            label="Recall"
            value={scorecard?.recall != null ? (scorecard.recall * 100).toFixed(1) + "%" : "—"}
            threshold="> 40%"
            status={scorecard?.recall != null && scorecard.recall > 0.4 ? "pass" : "fail"}
          />
          <MetricCard
            label="F1 Score"
            value={scorecard?.f1 != null ? scorecard.f1.toFixed(3) : "—"}
            threshold="> 0.45"
            status={scorecard?.f1 != null && scorecard.f1 > 0.45 ? "pass" : "fail"}
          />
          <MetricCard
            label="Calibration Error"
            value={scorecard?.calibration_error != null ? scorecard.calibration_error.toFixed(4) : "—"}
            threshold="< 0.10"
            status={scorecard?.calibration_error != null && scorecard.calibration_error < 0.10 ? "pass" : "fail"}
            invert
          />
          <MetricCard
            label="AUC (Pooled)"
            value={scorecard?.auc != null ? scorecard.auc.toFixed(3) : "—"}
            threshold="< 0.70 (saturation guard)"
            status={scorecard?.auc != null && scorecard.auc < 0.70 ? "pass" : "fail"}
            invert
          />
          <MetricCard
            label="Saturation"
            value={scorecard?.saturation_pct != null ? (scorecard.saturation_pct * 100).toFixed(1) + "%" : "—"}
            threshold="< 80%"
            status={scorecard?.saturation_pct != null && scorecard.saturation_pct < 0.80 ? "pass" : "fail"}
            invert
          />
        </div>
      </section>

      {scorecard?.feature_importance && scorecard.feature_importance.length > 0 && (
        <section className="scorecard-section">
          <h3>Feature Importance</h3>
          <table className={`${css["feature-table"]}`}>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Feature</th>
                <th>Importance</th>
              </tr>
            </thead>
            <tbody>
              {scorecard.feature_importance
                .sort((a, b) => b.importance - a.importance)
                .slice(0, 20)
                .map((f, i) => (
                  <tr key={f.feature}>
                    <td>{i + 1}</td>
                    <td>{f.feature}</td>
                    <td>
                      <div className={`${css["importance-bar"]}`}>
                        <div
                          className={`${css["importance-fill"]}`}
                          style={{ width: `${Math.min(f.importance * 100, 100)}%` }}
                        />
                        <span>{(f.importance * 100).toFixed(1)}%</span>
                      </div>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="scorecard-section">
        <h3>Models</h3>
        {models.length === 0 ? (
          <p className="empty">No models trained yet</p>
        ) : (
          <table className={`${css["models-table"]}`}>
            <thead>
              <tr>
                <th>ID</th>
                <th>Created</th>
                <th>Status</th>
                <th>Samples</th>
                <th>Symbols</th>
                <th>Broker Mode</th>
                <th>Environment</th>
                <th>Account</th>
                <th>Note</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.id} className={m.id === champion?.id ? "active" : ""}>
                  <td>#{m.id}</td>
                  <td>{new Date(m.created_at).toLocaleString()}</td>
                  <td>
                    <span className={m.status === "active" ? "status-active" : "status-inactive"}>
                      {m.status}
                    </span>
                  </td>
                  <td>{m.n_samples || "—"}</td>
                  <td>{m.n_symbols || "—"}</td>
                  <td>{String(m.metrics?.broker_mode || "—")}</td>
                  <td>{String(m.metrics?.webull_environment || "—")}</td>
                  <td>{String(m.metrics?.account_id || "—")}</td>
                  <td className="note">{m.note || "—"}</td>
                  <td>
                    {m.id === scorecardFor ? (
                      <span className={`${css["active-badge"]}`}>{m.id === champion?.id ? "Champion · shown" : "Shown"}</span>
                    ) : (
                      <button
                        className="button button-quiet"
                        onClick={() => fetchScorecard(m.id)}
                      >
                        View Scorecard
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="scorecard-section">
        <h3>ML Settings</h3>
        <dl className={`${css["settings-list"]}`}>
          <dt>ML Enabled</dt>
          <dd>{mlStatus.enabled ? "Yes" : "No"}</dd>
          <dt>Retrain Interval</dt>
          <dd>{mlStatus.retrain_days} days</dd>
          <dt>Min Probability Threshold</dt>
          <dd>{mlStatus.min_p != null ? mlStatus.min_p : "—"}</dd>
        </dl>
      </section>
    </div>
  );
}

function MetricCard({
  label,
  value,
  threshold,
  status,
}: {
  label: string;
  value: string;
  threshold: string;
  status: "pass" | "fail";
  invert?: boolean;
}) {
  return (
    <div className={`metric-card ${status}`}>
      <div className={`${css["metric-label"]}`}>{label}</div>
      <div className="metric-value">{value}</div>
      <div className={`${css["metric-threshold"]}`}>Threshold: {threshold}</div>
      <div className={`${css["metric-status"]} ${status}`}>
        {status === "pass" ? "✓ PASS" : "✗ FAIL"}
      </div>
    </div>
  );
}