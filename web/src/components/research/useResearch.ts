"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";

/** Fetch a research endpoint on mount, then every 5 minutes. Failure becomes an error sentence, never a throw. */
export function useResearch<T extends { error?: string }>(api: Api, path: string): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api<T>(path).then(d => {
      if (!alive) return;
      if (!d || typeof d !== "object") { setError("No data came back."); return; }
      setError(d.error ? String(d.error) : null);
      setData(d);
    }).catch(() => { if (alive) setError("Could not load this view right now."); });
    void load();
    const t = window.setInterval(load, 5 * 60 * 1000);
    return () => { alive = false; window.clearInterval(t); };
  }, [api, path]);
  return { data, error };
}
