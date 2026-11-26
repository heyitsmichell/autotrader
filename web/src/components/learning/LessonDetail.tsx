"use client";

import type { Lesson } from "@/lib/types";
import { dateTime } from "@/lib/format";
import Dialog from "../Dialog";
import css from "../LearningView.module.css";
import { statusColor, statusLabel } from "./lessonMeta";

export default function LessonDetail({ lesson, onClose }: { lesson: Lesson; onClose: () => void }) {
  const supportingAutopsies = lesson.supporting_autopsies ? JSON.parse(lesson.supporting_autopsies) : [];

  return (
    <Dialog
      title={<>Lesson #{lesson.id} v{lesson.version} · {lesson.param_name}</>}
      eyebrow="LESSON"
      size="lg"
      className={`${css["lesson-detail"]}`}
      onClose={onClose}
      footer={<button className="button" onClick={onClose}>Close</button>}
    >

        <div className="detail-grid">
          <div className="detail-section">
            <h4>Parameter Change</h4>
            <dl>
              <dt>Parameter</dt>
              <dd>{lesson.param_name}</dd>
              <dt>Scope</dt>
              <dd>{lesson.param_scope}</dd>
              <dt>Proposed Value</dt>
              <dd className="value">{lesson.param_value.toFixed(4)}</dd>
              <dt>Status</dt>
              <dd>
                <span className={`status-badge status-${lesson.status}`} style={{ backgroundColor: statusColor[lesson.status] }}>
                  {statusLabel[lesson.status]}
                </span>
              </dd>
              <dt>Auto-apply</dt>
              <dd>{lesson.apply_state || "pending"}{lesson.apply_reason ? ` (${lesson.apply_reason})` : ""}</dd>
              <dt>Created By</dt>
              <dd>{lesson.created_by}</dd>
              <dt>Created</dt>
              <dd>{dateTime(lesson.created_at)} ET</dd>
              {lesson.approved_by && (
                <>
                  <dt>Approved By</dt>
                  <dd>{lesson.approved_by}</dd>
                  <dt>Approved</dt>
                  <dd>{lesson.approved_at ? `${dateTime(lesson.approved_at)} ET` : "—"}</dd>
                </>
              )}
              {lesson.retired_reason && (
                <>
                  <dt>Retired Reason</dt>
                  <dd>{lesson.retired_reason}</dd>
                  <dt>Retired By</dt>
                  <dd>{lesson.retired_by || "—"}</dd>
                </>
              )}
            </dl>
          </div>

          <div className="detail-section">
            <h4>Statistical Validation</h4>
            <dl>
              <dt>Sample Size</dt>
              <dd>{lesson.sample_size} trades</dd>
              <dt>Min Required</dt>
              <dd>{lesson.min_sample_size}</dd>
              <dt>Time Split</dt>
              <dd>Train: {lesson.train_pct * 100}% | Val: {lesson.val_pct * 100}% | Test: {lesson.test_pct * 100}%</dd>
              <dt>Metric</dt>
              <dd>{lesson.metric_name || "—"}</dd>
              <dt>Baseline</dt>
              <dd>{lesson.baseline_metric != null ? lesson.baseline_metric.toFixed(4) : "—"}</dd>
              <dt>Proposed</dt>
              <dd>{lesson.proposed_metric != null ? lesson.proposed_metric.toFixed(4) : "—"}</dd>
              <dt>Improvement</dt>
              <dd className={lesson.proposed_metric && lesson.baseline_metric && lesson.proposed_metric > lesson.baseline_metric ? "positive" : "negative"}>
                {lesson.proposed_metric && lesson.baseline_metric
                  ? ((lesson.proposed_metric - lesson.baseline_metric) / Math.abs(lesson.baseline_metric) * 100).toFixed(1) + "%"
                  : "—"}
              </dd>
              <dt>p-value</dt>
              <dd className={`no-arrow ${lesson.p_value != null && lesson.p_value < 0.05 ? "positive" : "neutral"}`}>
                {lesson.p_value != null ? lesson.p_value.toFixed(4) : "—"}
                {lesson.p_value != null && lesson.p_value < 0.05 && " (significant)"}
              </dd>
            </dl>
          </div>
        </div>

        <div className="detail-section">
          <h4>Evidence Summary</h4>
          <pre className={`${css["evidence-summary"]}`}>{lesson.evidence_summary || "No evidence summary"}</pre>
        </div>

        {supportingAutopsies.length > 0 && (
          <div className="detail-section">
            <h4>Supporting Autopsies ({supportingAutopsies.length})</h4>
            <ul className={`${css["autopsy-list"]}`}>
              {supportingAutopsies.slice(0, 10).map((id: number) => (
                <li key={id}>Autopsy #{id}</li>
              ))}
            </ul>
          </div>
        )}

    </Dialog>
  );
}