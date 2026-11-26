"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { MlStatus, ModelInfo } from "@/lib/types";
import { useConfirm } from "./ConfirmModal";
import css from "./MLScorecard.module.css";
import rcss from "./results/results.module.css";
import Section from "./ui/Section";
import Mark from "./ui/Mark";
import Empty from "./ui/Empty";
import { dateTime, num } from "@/lib/format";

interface MLScoreCard {
  precision?: number | null;
  recall?: number | null;
  f1?: number | null;
  calibration_error?: number | null;
  auc?: number | null;
  saturation_pct?: number | null;
  hit_rate?: number | null;
  auc_mean_folds?: number | null;
  mean_avg_return?: number | null;
  total_test_trades?: number | null;
  total_independent_test_trades?: number | null;
  passed?: boolean | null;
  rejection_reason?: string | null;
  checks?: Record<string, boolean> | null;
  thresholds?: Record<string, unknown> | null;
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

  const hasModel = models.length > 0 || !!champion || !!challenger;
  const trainBtn = <button className="button" disabled={busy || mlStatus.training || mlStatus.available === false}
    title={mlStatus.available === false ? (mlStatus.error || "PyTorch is not available") : undefined}
    onClick={() => act("Train", "/api/ml/train", "Train a new challenger model?",
      "Training runs in the background on the universe's history. The new model is evaluated in shadow mode before it can be promoted.")}
  >{mlStatus.training ? "Training…" : "Train challenger"}</button>;

  const controls = <div className={`${css["ml-controls"]}`} role="group" aria-label="Model controls">
    {trainBtn}
    {challenger && <button className="button" disabled={busy}
      onClick={() => act("Promote", "/api/ml/promote", `Promote challenger #${challenger.id}?`,
        "The challenger becomes the champion that filters live entries. Signals are re-scored right away.", true)}
    >Promote challenger</button>}
    {challenger && <button className="button" disabled={busy}
      onClick={() => act("Reject", "/api/ml/reject", `Reject challenger #${challenger.id}?`,
        "The challenger is discarded so a new one can be trained.")}
    >Reject challenger</button>}
    {champion && <button className="button danger" disabled={busy}
      onClick={() => act("Deactivate", "/api/ml/deactivate", "Deactivate the ML filter's champion?",
        "Entries are then gated by the rules only (TA, sentiment, risk) until a model is promoted again.", true)}
    >Deactivate champion</button>}
  </div>;

  const meta = hasModel
    ? [champion ? `Champion #${champion.id}` : "No champion", challenger ? `challenger #${challenger.id} (shadow)` : null,
       mlStatus.training ? "training" : null].filter(Boolean).join(" · ")
    : undefined;

  if (!hasModel) {
    return (
      <div className="ml-scorecard-view">
        <Section title="Entry model" meta={mlStatus.available === false ? "Unavailable" : undefined}>
          <Empty>No model trained yet. Entries run on rules only.</Empty>
          {mlStatus.error && <p className="inline-warning">{mlStatus.error}</p>}
          <div className={rcss["more"]}>{controls}</div>
        </Section>
      </div>
    );
  }

  const sc = scorecard;
  const hasChecks = !!sc?.checks && Object.keys(sc.checks).length > 0;
  const hit = sc?.hit_rate ?? sc?.precision ?? null;
  const auc = sc?.auc_mean_folds ?? sc?.auc ?? null;
  const fb = !hasChecks; // hardcoded thresholds only as fallback
  const gateAll: { metric: string; value: string | null; threshold: string; ok: boolean | null }[] = [
    { metric: "Hit rate (walk-forward)", value: hit != null ? (hit * 100).toFixed(1) + "%" : null, threshold: "> 50%", ok: hit != null ? hit > 0.5 : null },
    { metric: "Recall", value: sc?.recall != null ? (sc.recall * 100).toFixed(1) + "%" : null, threshold: "> 40%", ok: sc?.recall != null ? sc.recall > 0.4 : null },
    { metric: "F1 score", value: sc?.f1 != null ? sc.f1.toFixed(3) : null, threshold: "> 0.45", ok: sc?.f1 != null ? sc.f1 > 0.45 : null },
    { metric: "Calibration error", value: sc?.calibration_error != null ? sc.calibration_error.toFixed(4) : null, threshold: "< 0.10", ok: sc?.calibration_error != null ? sc.calibration_error < 0.10 : null },
    { metric: "AUC (mean of folds)", value: auc != null ? auc.toFixed(3) : null, threshold: "< 0.70 (saturation guard)", ok: auc != null ? auc < 0.70 : null },
    { metric: "Saturation", value: sc?.saturation_pct != null ? (sc.saturation_pct * 100).toFixed(1) + "%" : null, threshold: "< 80%", ok: sc?.saturation_pct != null ? sc.saturation_pct < 0.80 : null },
    { metric: "Avg return per trade", value: sc?.mean_avg_return != null ? sc.mean_avg_return.toFixed(2) + "%" : null, threshold: "", ok: null },
    { metric: "Test trades", value: sc?.total_test_trades != null ? `${sc.total_test_trades}${sc.total_independent_test_trades != null ? ` (${sc.total_independent_test_trades} independent)` : ""}` : null, threshold: "", ok: null },
  ];
  const gate = gateAll.filter(g => g.value != null).map(g => ({ ...g, value: g.value as string,
    threshold: fb ? g.threshold : "", ok: fb ? g.ok : null }));

  return (
    <div className="ml-scorecard-view">
      <Section title="Entry model" meta={meta} right={controls}>
        {mlStatus.error && <p className="inline-warning">{mlStatus.error}</p>}

        <h3 className={rcss["sub"]}>Gate metrics{scorecardFor != null ? ` for model #${scorecardFor}` : ""}</h3>
        <div className={rcss["tableWrap"]}>
          <table>
            <thead><tr><th>Metric</th><th className="num">Value</th><th>Threshold</th><th>Result</th></tr></thead>
            <tbody>
              {gate.map(g => (
                <tr key={g.metric}>
                  <td>{g.metric}</td>
                  <td className="num mono">{g.value}</td>
                  <td className="mono">{g.threshold}</td>
                  <td>{g.ok == null ? <span className={rcss["dim"]}>—</span> : <Mark tone={g.ok ? "gain" : "loss"}>{g.ok ? "pass" : "fail"}</Mark>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {(() => {
          const missing = [
            sc?.recall == null && "recall", sc?.f1 == null && "F1",
            sc?.calibration_error == null && "calibration error",
            sc?.saturation_pct == null && "saturation",
            !(sc?.feature_importance && sc.feature_importance.length > 0) && "feature importance",
          ].filter(Boolean) as string[];
          return sc && missing.length > 0 ? (
            <p className={rcss["dim"]}>Not computed for this model: {missing.join(", ")} (retrain to compute).</p>
          ) : null;
        })()}

        {sc?.feature_importance && sc.feature_importance.length > 0 && (<>
          <h3 className={rcss["sub"]}>Feature importance</h3>
          <div className={rcss["tableWrap"]}>
            <table>
              <thead><tr><th className="num">Rank</th><th>Feature</th><th className="num">Importance</th></tr></thead>
              <tbody>
                {[...sc.feature_importance].sort((a, b) => b.importance - a.importance).slice(0, 20).map((f, i) => (
                  <tr key={f.feature}>
                    <td className="num mono">{i + 1}</td>
                    <td>{f.feature}</td>
                    <td className="num mono">{(f.importance * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>)}

        <h3 className={rcss["sub"]}>Models</h3>
        <div className={rcss["tableWrap"]}>
          <table>
            <thead>
              <tr><th>Model</th><th>Created</th><th>Status</th><th className="num">Samples</th><th className="num">Symbols</th>
                <th>Broker mode</th><th>Environment</th><th>Note</th><th /></tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.id}>
                  <td className="mono">#{m.id}</td>
                  <td>{dateTime(m.created_at)}</td>
                  <td><Mark tone={m.status === "active" ? "gain" : "neutral"}>{m.status}</Mark></td>
                  <td className="num mono">{m.n_samples ? num(m.n_samples, 0) : "—"}</td>
                  <td className="num mono">{m.n_symbols ? num(m.n_symbols, 0) : "—"}</td>
                  <td>{String(m.metrics?.broker_mode || "—")}</td>
                  <td>{String(m.metrics?.webull_environment || "—")}</td>
                  <td>{m.note || "—"}</td>
                  <td>
                    {m.id === scorecardFor
                      ? <span className={rcss["dim"]}>{m.id === champion?.id ? "Champion · shown" : "Shown"}</span>
                      : <button className="button" onClick={() => fetchScorecard(m.id)}>View scorecard</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <h3 className={rcss["sub"]}>ML settings</h3>
        <dl className={rcss["kv"]}>
          <dt>ML enabled</dt><dd className="mono">{mlStatus.enabled ? "Yes" : "No"}</dd>
          <dt>Retrain interval</dt><dd className="mono">{mlStatus.retrain_days != null ? `${mlStatus.retrain_days} days` : "—"}</dd>
          <dt>Min probability threshold</dt><dd className="mono">{mlStatus.min_p != null ? mlStatus.min_p : "—"}</dd>
        </dl>
      </Section>
    </div>
  );
}
