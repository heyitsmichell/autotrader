"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Lesson } from "@/lib/types";
import { useConfirm } from "./ConfirmModal";
import Dialog from "./Dialog";
import Section from "./ui/Section";
import Segmented from "./ui/Segmented";
import Mark from "./ui/Mark";
import Empty from "./ui/Empty";
import { dateTime, num } from "@/lib/format";

import LessonTable from "./learning/LessonTable";
import LessonDetail from "./learning/LessonDetail";
import { readApprover, saveApprover, statusLabel } from "./learning/lessonMeta";

type Perform = (label: string, fn: () => Promise<unknown>) => Promise<void>;

export { default as ApplyBadge } from "./learning/ApplyBadge";

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
        <LessonTable lessons={lessons} filtered={filteredLessons} loading={loading} onSelect={setSelectedLesson}
          onStatus={handleStatusChange} onRollback={handleRollback}/>
      </Section>

      {selectedLesson && (
        <LessonDetail lesson={selectedLesson} onClose={() => setSelectedLesson(null)} />
      )}
    </div>
  );
}
