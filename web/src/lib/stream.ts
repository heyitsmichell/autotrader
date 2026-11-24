"use client";

/**
 * Subscription to the multiplexed hub stream `GET /api/stream` (see docs/API_STREAM.md).
 * Keep EVENT_TYPES in sync with `app.eventhub.EVENT_TYPES`.
 */

import { useEffect, useRef, useState } from "react";

export const EVENT_TYPES = [
  "status", "portfolio", "proposals", "events", "quote", "fill", "kill", "heartbeat",
] as const;
export type HubEventType = typeof EVENT_TYPES[number];
export type HubHandlers = Partial<Record<HubEventType, (data: unknown) => void>>;

/** No event (heartbeats included) for this long means the stream is treated as down. */
const STALE_MS = 45_000;

export interface StreamState {
  /** True while the stream is open and has delivered an event within STALE_MS. */
  connected: boolean;
  lastEventAt: number | null;
}

export function useHubStream(handlers: HubHandlers, enabled = true): StreamState {
  const handlersRef = useRef(handlers);
  useEffect(() => { handlersRef.current = handlers; });
  const [state, setState] = useState<StreamState>({ connected: false, lastEventAt: null });

  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof EventSource === "undefined") return;
    let source: EventSource | null = null;
    let last = 0;
    let closed = false;

    const mark = (connected: boolean) => {
      setState(prev => (prev.connected === connected && prev.lastEventAt === (last || null)
        ? prev : { connected, lastEventAt: last || null }));
    };

    const open = () => {
      source = new EventSource("/api/stream");
      source.onopen = () => { last = Date.now(); mark(true); };
      source.onerror = () => {
        // EventSource reconnects on its own; until it does, polling takes over.
        mark(false);
      };
      for (const type of EVENT_TYPES) {
        source.addEventListener(type, (ev: MessageEvent) => {
          last = Date.now();
          let data: unknown = null;
          try { data = JSON.parse(ev.data); } catch { return; }
          handlersRef.current[type]?.(data);
          if (type === "heartbeat" || type === "status") mark(true);
        });
      }
    };
    open();

    const watchdog = window.setInterval(() => {
      if (closed) return;
      if (!last || Date.now() - last > STALE_MS) mark(false);
    }, 5_000);

    return () => {
      closed = true;
      window.clearInterval(watchdog);
      source?.close();
    };
  }, [enabled]);

  return state;
}
