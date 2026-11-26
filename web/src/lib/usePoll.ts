import { useCallback, useEffect, useRef, useState } from "react";
import type { Api } from "@/lib/api";

type Entry = { ts: number; promise: Promise<unknown> | null; data?: unknown; ok: boolean };
const cache = new Map<string, Entry>();
const DEDUPE_MS = 2000;

/** Shared fetch: concurrent callers on the same path share one request; a fresh success is reused. */
function shared<T>(api: Api, path: string, force: boolean): Promise<T> {
  const e = cache.get(path);
  if (e?.promise) return e.promise as Promise<T>;
  if (e && e.ok && !force && Date.now() - e.ts < DEDUPE_MS) return Promise.resolve(e.data as T);
  const entry: Entry = { ts: Date.now(), promise: null, ok: false };
  entry.promise = api<T>(path).then(
    d => { entry.data = d; entry.ok = true; entry.ts = Date.now(); entry.promise = null; return d; },
    err => { entry.promise = null; entry.ok = false; cache.delete(path); throw err; },
  );
  cache.set(path, entry);
  return entry.promise as Promise<T>;
}

export type PollState<T> = { data: T | null; error: Error | null; loading: boolean; reload: () => void };

/** Poll `path` every `intervalMs` (null path = idle). Pauses while the tab is hidden, keeps last good data on error. */
export function usePoll<T>(api: Api | undefined, path: string | null, intervalMs: number): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  // loading is derived: true while there is an active path whose first response has not settled yet
  const [settledPath, setSettledPath] = useState<string | null>(null);
  const loading = !!api && !!path && settledPath !== path;
  const reqId = useRef(0);
  const loadRef = useRef<(force: boolean) => void>(() => {});

  useEffect(() => {
    if (!api || !path) { loadRef.current = () => {}; return; }
    let alive = true;
    const load = (force: boolean) => {
      const id = ++reqId.current;
      shared<T>(api, path, force)
        .then(d => { if (alive && id === reqId.current) { setData(d); setError(null); setSettledPath(path); } })
        .catch(e => { if (alive && id === reqId.current) { setError(e instanceof Error ? e : new Error(String(e))); setSettledPath(path); } });
    };
    loadRef.current = load;
    load(false);
    const timer = window.setInterval(() => { if (!document.hidden) load(true); }, intervalMs);
    const onVis = () => { if (!document.hidden) load(false); };
    document.addEventListener("visibilitychange", onVis);
    return () => { alive = false; window.clearInterval(timer); document.removeEventListener("visibilitychange", onVis); };
  }, [api, path, intervalMs]);

  const reload = useCallback(() => loadRef.current(true), []);
  return { data, error, loading, reload };
}
