"use client";

import { useState } from "react";
import { clockTime } from "@/lib/format";
import type { EventRow } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Section from "@/components/ui/Section";
import css from "../Today.module.css";
import { isSystemEvent } from "./gate";

export default function Activity({ events }: { events: EventRow[] }) {
  const [system, setSystem] = useState(false);
  const [all, setAll] = useState(false);
  const list = events.filter(e => system || !isSystemEvent(e.message, e.level));
  const shown = all ? list : list.slice(0, 8);
  return <Section title="Activity" right={<label className={css["toggle"]}>
    <input type="checkbox" checked={system} onChange={e => setSystem(e.target.checked)}/> Show system events
  </label>}>
    {shown.length ? <ul className={css["events"]}>
      {shown.map(e => <li key={e.id}>
        <time className="mono" suppressHydrationWarning>{clockTime(e.ts)}</time>
        <span className={e.level === "error" || e.level === "critical" ? css["loss"] : ""}>{e.message}</span>
      </li>)}
    </ul> : <Empty>No trading events yet. Orders, fills, stops and blocks appear here as they happen.</Empty>}
    {list.length > 8 && <button className="button" onClick={() => setAll(a => !a)}>{all ? "Show fewer" : `Show all (${list.length})`}</button>}
  </Section>;
}
