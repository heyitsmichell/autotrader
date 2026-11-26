"use client";

import type { Lesson } from "@/lib/types";
import { dateTime, num } from "@/lib/format";
import rcss from "../results/results.module.css";
import Mark from "../ui/Mark";
import Empty from "../ui/Empty";
import ApplyBadge from "./ApplyBadge";
import { statusLabel } from "./lessonMeta";

const statusTone = (s: string): "gain" | "loss" | "warn" | "neutral" =>
  s === "active" ? "gain" : s === "rejected" ? "loss" : s === "proposed" ? "warn" : "neutral";

export default function LessonTable({ lessons, filtered, loading, onSelect, onStatus, onRollback }: {
  lessons: Lesson[]; filtered: Lesson[]; loading: boolean;
  onSelect: (l: Lesson) => void;
  onStatus: (l: Lesson, status: string) => void;
  onRollback: (l: Lesson) => void;
}) {
  const filteredLessons = filtered;
  const setSelectedLesson = onSelect;
  const handleStatusChange = onStatus;
  const handleRollback = onRollback;
  const btn = (label: string, fn: () => void) =>
    <button className="button" onClick={(e) => { e.stopPropagation(); fn(); }}>{label}</button>;
  return <>
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
  </>;
}
