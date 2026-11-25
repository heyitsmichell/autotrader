"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Lesson } from "@/lib/types";
import { useConfirm } from "./ConfirmModal";
import Dialog from "./Dialog";
import css from "./LearningView.module.css";
import rcss from "./results/results.module.css";
import Section from "./ui/Section";
import Segmented from "./ui/Segmented";
import Mark from "./ui/Mark";
import Empty from "./ui/Empty";
import { dateTime, num } from "@/lib/format";

const statusLabel: Record<string, string> = {
  proposed: "Proposed",
  approved: "Approved",
  active: "Active",
  retired: "Retired",
  rejected: "Rejected",
};

const statusColor: Record<string, string> = {
  proposed: "var(--color-proposed)",
  approved: "var(--color-approved)",
  active: "var(--color-active)",
  retired: "var(--color-retired)",
  rejected: "var(--color-rejected)",
};

const APPROVER_KEY = "trader.approver";

function readApprover(): string {
  try { return window.localStorage.getItem(APPROVER_KEY) || ""; } catch { return ""; }
}

function saveApprover(name: string) {
  try { window.localStorage.setItem(APPROVER_KEY, name); } catch { /* private mode */ }
}

export function ApplyBadge({ lesson }: { lesson: Lesson }) {
  const state = lesson.apply_state || "pending";
  if (lesson.status !== "active" && state === "pending") return null;
  const label = state === "applied" ? "Applied" : state === "skipped" ? "Skipped" : "Not yet applied";
  return <Mark tone={state === "applied" ? "gain" : state === "skipped" ? "warn" : "neutral"} title={lesson.apply_reason || undefined}>
    {label}{lesson.apply_reason ? `: ${lesson.apply_reason}` : ""}
  </Mark>;
}

type Perform = (label: string, fn: () => Promise<unknown>) => Promise<void>;

export default function LearningView({ onAction }: { onAction?: Perform }) {
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedLesson, setSelectedLesson] = useState<Lesson | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const ask = useConfirm();

  // Bumped after every action; the effect refetches (state is only set in its callbacks).
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    api<Lesson[]>("/api/lessons?limit=100")
      .then(data => { if (active) { setLessons(Array.isArray(data) ? data : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);

  const run = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    if (onAction) {
      await onAction(label, fn);
    } else {
      try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : "Action failed"); }
    }
    setReload(n => n + 1);
  }, [onAction]);

  const filteredLessons = lessons.filter((l) =>
    statusFilter === "all" || l.status === statusFilter
  );

  const statusCounts = lessons.reduce(
    (acc, l) => {
      acc[l.status] = (acc[l.status] || 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  const handleStatusChange = (lesson: Lesson, newStatus: string) => {
    const what = `lesson #${lesson.id} (${lesson.param_name} → ${lesson.param_value})`;
    if (newStatus === "approved") {
      ask({
        title: `Approve ${what}?`,
        confirmLabel: "Approve",
        message: <p>Approval records who signed off. The lesson becomes <b>active</b> (and can change
          trading parameters within their bounds) only after you activate it.</p>,
        input: { label: "Approved by (your name)", initial: readApprover(), required: true },
        onConfirm: async (name) => {
          saveApprover(name || "");
          await run(`Lesson #${lesson.id} approved`, () =>
            api(`/api/lessons/${lesson.id}/status`, "POST", { status: "approved", approved_by: name }));
        },
      });
      return;
    }
    const verbs: Record<string, [string, string, boolean]> = {
      rejected: ["Reject", "The lesson is closed and never applied.", false],
      active: ["Activate", "The engine applies it on the next lesson refresh, within the allowed bounds. Risk limits are never auto-applied.", true],
      retired: ["Retire", "The engine stops applying it; the previous value comes back on the next refresh.", false],
    };
    const [verb, note, danger] = verbs[newStatus] || [newStatus, "", false];
    ask({
      title: `${verb} ${what}?`,
      confirmLabel: verb,
      danger,
      message: <p>{note}</p>,
      onConfirm: async () => {
        await run(`Lesson #${lesson.id} ${newStatus}`, () =>
          api(`/api/lessons/${lesson.id}/status`, "POST", { status: newStatus }));
      },
    });
  };

  const handleRollback = (lesson: Lesson) => {
    ask({
      title: `Roll back lesson #${lesson.id} (${lesson.param_name})?`,
      confirmLabel: "Roll back",
      danger: true,
      message: <p>The previous version (#{lesson.previous_version_id}) becomes active again.</p>,
      input: { label: "Rolled back by (your name)", initial: readApprover(), required: true },
      onConfirm: async (name) => {
        saveApprover(name || "");
        await run(`Lesson #${lesson.id} rolled back`, () =>
          api(`/api/lessons/${lesson.id}/rollback`, "POST", { rolled_back_by: name }));
      },
    });
  };

  const statusTone = (s: string): "gain" | "loss" | "warn" | "neutral" =>
    s === "active" ? "gain" : s === "rejected" ? "loss" : s === "proposed" ? "warn" : "neutral";
  const meta = Object.entries(statusCounts).map(([s, c]) => `${c} ${(statusLabel[s] || s).toLowerCase()}`).join(" · ");
  const btn = (label: string, fn: () => void) =>
    <button className="button" onClick={(e) => { e.stopPropagation(); fn(); }}>{label}</button>;

  return (
    <div className="learning-view">
      <Section title="Lessons" meta={meta || undefined} right={
        <Segmented label="Status" value={statusFilter} onChange={setStatusFilter}
          options={[{ value: "all", label: "All" }, ...Object.keys(statusLabel).map(s => ({ value: s, label: statusLabel[s] }))]} />
      }>
        {loading && <p className="loading">Loading lessons…</p>}
        {error && <p className="error">{error}</p>}
        {filteredLessons.length === 0 && !loading ? (
          <Empty>{lessons.length === 0
            ? "No lessons yet. A lesson is proposed when the autopsies show a pattern strong enough to test."
            : "No lessons with this status."}</Empty>
        ) : (
          <div className={rcss["tableWrap"]}>
            <table>
              <thead>
                <tr>
                  <th>Lesson</th><th>Parameter</th><th className="num">Value</th><th className="num">Sample</th>
                  <th>Metric</th><th className="num">Baseline</th><th className="num">Proposed</th><th className="num">p-value</th>
                  <th>Status</th><th>Created</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredLessons.map((lesson) => (
                  <tr key={lesson.id} onClick={() => setSelectedLesson(lesson)}>
                    <td className="mono">#{lesson.id} v{lesson.version}</td>
                    <td>{lesson.param_name}</td>
                    <td className="num mono">{num(lesson.param_value, 4)}</td>
                    <td className="num mono">{lesson.sample_size}</td>
                    <td>{lesson.metric_name || "—"}</td>
                    <td className="num mono">{num(lesson.baseline_metric, 4)}</td>
                    <td className="num mono">{num(lesson.proposed_metric, 4)}</td>
                    <td className="num mono">{num(lesson.p_value, 4)}</td>
                    <td>
                      <Mark tone={statusTone(lesson.status)}>{statusLabel[lesson.status] || lesson.status}</Mark>{" "}
                      <ApplyBadge lesson={lesson}/>
                    </td>
                    <td>{dateTime(lesson.created_at)}</td>
                    <td>
                      {lesson.status === "proposed" && (
                        <>
                          {btn("Approve", () => handleStatusChange(lesson, "approved"))}
                          {btn("Reject", () => handleStatusChange(lesson, "rejected"))}
                        </>
                      )}
                      {lesson.status === "approved" && btn("Activate", () => handleStatusChange(lesson, "active"))}
                      {lesson.status === "active" && (
                        <>
                          {btn("Retire", () => handleStatusChange(lesson, "retired"))}
                          {lesson.previous_version_id && btn("Rollback", () => handleRollback(lesson))}
                        </>
                      )}
                      {lesson.status === "retired" && lesson.previous_version_id && btn("Rollback", () => handleRollback(lesson))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {selectedLesson && (
        <LessonDetail lesson={selectedLesson} onClose={() => setSelectedLesson(null)} />
      )}
    </div>
  );
}

function LessonDetail({ lesson, onClose }: { lesson: Lesson; onClose: () => void }) {
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
              <dd>{new Date(lesson.created_at).toLocaleString()}</dd>
              {lesson.approved_by && (
                <>
                  <dt>Approved By</dt>
                  <dd>{lesson.approved_by}</dd>
                  <dt>Approved</dt>
                  <dd>{lesson.approved_at ? new Date(lesson.approved_at).toLocaleString() : "—"}</dd>
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