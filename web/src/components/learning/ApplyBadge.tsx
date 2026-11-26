import type { Lesson } from "@/lib/types";
import Mark from "../ui/Mark";

export default function ApplyBadge({ lesson }: { lesson: Lesson }) {
  const state = lesson.apply_state || "pending";
  if (lesson.status !== "active" && state === "pending") return null;
  const label = state === "applied" ? "Applied" : state === "skipped" ? "Skipped" : "Not yet applied";
  return <Mark tone={state === "applied" ? "gain" : state === "skipped" ? "warn" : "neutral"} title={lesson.apply_reason || undefined}>
    {label}{lesson.apply_reason ? `: ${lesson.apply_reason}` : ""}
  </Mark>;
}
