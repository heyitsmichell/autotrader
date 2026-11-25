"use client";

/**
 * Dashboard data (QA M12/M13/M14/L10).
 *
 * Live state (status, portfolio, proposals, events, kill, fill) arrives over the SSE mux
 * (`/api/stream`). Polling is the fallback, in tiers:
 *   fast tier  status, portfolio, proposals, events   10 s while the stream is down, 60 s resync otherwise
 *   slow tier  signals, runs, ml, performance   30 s
 * Every setState is structural-equality guarded, so an identical payload never re-renders.
 * Every failed request counts toward the error banner, the portfolio included.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Api } from "@/lib/api";
import { useHubStream } from "@/lib/stream";
import type {
  EventRow, Learning, MlStatus, Performance, Portfolio, Proposal, Run, Signal, Status,
} from "@/lib/types";

export type { Api };

const FAST_POLL_MS = 10_000;
const FAST_RESYNC_MS = 60_000;
const SLOW_POLL_MS = 30_000;

function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

/** useState whose setter ignores a value structurally equal to the current one. */
function useStable<T>(initial: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(initial);
  const set = useCallback((next: T) => setValue(prev => (same(prev, next) ? prev : next)), []);
  return [value, set];
}

const asList = <T,>(x: unknown): T[] => (Array.isArray(x) ? (x as T[]) : []);

export function useDashboardData() {
  const mounted = typeof window !== "undefined";
  const [status, setStatus] = useStable<Status | null>(null);
  const [portfolio, setPortfolio] = useStable<Portfolio | null>(null);
  const [signals, setSignals] = useStable<Signal[]>([]);
  const [proposals, setProposals] = useStable<Proposal[]>([]);
  const [runs, setRuns] = useStable<Run[]>([]);
  const [events, setEvents] = useStable<EventRow[]>([]);
  const [ml, setMl] = useStable<MlStatus | null>(null);
  const [performance, setPerformance] = useStable<Performance | null>(null);
  const [learning, setLearning] = useStable<Learning | null>(null);
  const [strategy, setStrategy] = useState("");
  const [lessons, setLessons] = useState("");
  const [loaded, setLoaded] = useState({ strategy: false, lessons: false });
  const [failures, setFailures] = useStable<Record<string, string>>({});
  const [duplicateInstance, setDuplicateInstance] = useState<{ timestamp: number; message: string } | null>(null);
  const failRef = useRef<Record<string, string>>({});

  const track = useCallback(async <T,>(name: string, fn: () => Promise<T>, apply: (v: T) => void) => {
    try {
      const v = await fn();
      apply(v);
      if (name in failRef.current) {
        const rest = { ...failRef.current };
        delete rest[name];
        failRef.current = rest;
        setFailures(rest);
      }
    } catch (e) {
      const offline = e instanceof ApiError && e.status === 409 && name === "portfolio";
      failRef.current = { ...failRef.current, [name]: offline ? "broker offline" : (e instanceof Error ? e.message : "failed") };
      setFailures(failRef.current);
      if (name === "portfolio") setPortfolio(null);
    }
  }, [setFailures, setPortfolio]);

  const applyStatus = useCallback((s: Status) => {
    setStatus(s);
    if (s?.duplicate_instance) setDuplicateInstance(s.duplicate_instance);
  }, [setStatus]);

  const fastInFlight = useRef(false);
  const refreshFast = useCallback(async () => {
    if (fastInFlight.current) return;
    fastInFlight.current = true;
    try {
      await Promise.all([
        track("status", () => api<Status>("/api/status"), applyStatus),
        track("portfolio", () => api<Portfolio>("/api/portfolio"), setPortfolio),
        track("proposals", () => api<Proposal[]>("/api/proposals?limit=50"), v => setProposals(asList(v))),
        track("events", () => api<EventRow[]>("/api/events?limit=50"), v => setEvents(asList(v))),
      ]);
    } finally {
      fastInFlight.current = false;
    }
  }, [track, applyStatus, setPortfolio, setProposals, setEvents]);

  const slowInFlight = useRef(false);
  const refreshSlow = useCallback(async () => {
    if (slowInFlight.current) return;
    slowInFlight.current = true;
    try {
      await Promise.all([
        track("signals", () => api<Signal[]>("/api/signals"), v => setSignals(asList(v))),
        track("runs", () => api<Run[]>("/api/runs"), v => setRuns(asList(v))),
        track("ml", () => api<MlStatus>("/api/ml"), setMl),
        track("performance", () => api<Performance>("/api/performance"), setPerformance),
      ]);
    } finally {
      slowInFlight.current = false;
    }
  }, [track, setSignals, setRuns, setMl, setPerformance]);

  const refreshMl = useCallback(
    () => track("ml", () => api<MlStatus>("/api/ml"), setMl), [track, setMl]);

  const refreshLearning = useCallback(async () => {
    await track("learning", () => api<Learning>("/api/learning"), x => {
      setLearning(x);
      setLessons(x.lessons || "");
      setLoaded(l => ({ ...l, lessons: true }));
    });
  }, [track, setLearning]);

  /** Everything now (used after an action). */
  const refresh = useCallback(async () => {
    await Promise.all([refreshFast(), refreshSlow()]);
  }, [refreshFast, refreshSlow]);

  // Live updates.
  const statusRef = useRef<Status | null>(null);
  useEffect(() => { statusRef.current = status; }, [status]);
  const stream = useHubStream({
    status: d => applyStatus(d as Status),
    portfolio: d => setPortfolio(d as Portfolio),
    proposals: d => setProposals(asList<Proposal>(d)),
    events: d => setEvents(asList<EventRow>(d)),
    kill: d => {
      const k = d as { on?: boolean; reason?: string | null };
      setStatus(statusRef.current ? { ...statusRef.current, kill_switch: !!k.on, kill_reason: k.reason ?? null } : null);
    },
    fill: () => {
      void track("portfolio", () => api<Portfolio>("/api/portfolio"), setPortfolio);
      void track("performance", () => api<Performance>("/api/performance"), setPerformance);
      void track("runs", () => api<Run[]>("/api/runs"), v => setRuns(asList(v)));
    },
  });
  // Initial load: snapshot, strategy, lessons.
  useEffect(() => {
    let active = true;
    void refresh();
    void track("strategy", () => api<{ text?: string }>("/api/strategy"), x => {
      if (!active) return;
      setStrategy(x.text || "");
      setLoaded(l => ({ ...l, strategy: true }));
    });
    void refreshLearning();
    return () => { active = false; };
  }, [refresh, refreshLearning, track]);

  // Tiered polling, only while the tab is visible.
  const fastMs = stream.connected ? FAST_RESYNC_MS : FAST_POLL_MS;
  useEffect(() => {
    let fast: number | undefined;
    let slow: number | undefined;
    const stop = () => {
      if (fast) window.clearInterval(fast);
      if (slow) window.clearInterval(slow);
      fast = slow = undefined;
    };
    const start = () => {
      if (fast) return;
      fast = window.setInterval(() => { void refreshFast(); }, fastMs);
      slow = window.setInterval(() => { void refreshSlow(); }, SLOW_POLL_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void refresh();
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fastMs, refresh, refreshFast, refreshSlow]);

  // Browser tab badge for proposals that need a decision.
  const pendingCount = proposals.filter(p => p.status === "pending").length;
  useEffect(() => {
    document.title = pendingCount > 0
      ? `(${pendingCount}) Pending · Trader`
      : "Trader | Market Intelligence";
    return () => { document.title = "Trader | Market Intelligence"; };
  }, [pendingCount]);

  const failedNames = Object.keys(failures);
  const error = failedNames.length
    ? `${failedNames.length} dashboard request(s) failing: ${failedNames
      .map(n => (failures[n] === "broker offline" ? `${n} (broker offline)` : n)).join(", ")}. Retrying automatically.`
    : "";

  return {
    mounted, status, portfolio, signals, proposals, runs, events, ml, learning,
    performance, strategy, setStrategy, lessons, setLessons, refresh, refreshMl, refreshLearning,
    api, error, failures, pendingCount, duplicateInstance,
    streamConnected: stream.connected,
    strategyLoaded: loaded.strategy, lessonsLoaded: loaded.lessons,
  };
}
