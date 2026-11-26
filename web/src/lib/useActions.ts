"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Tab } from "./tabs";
import { TABS } from "./tabs";

/** busy/notice handling for dashboard actions: run, show result for 6 s, refresh afterwards. */
export function useActions(refresh: () => Promise<void>) {
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const noticeTimer = useRef<number | null>(null);
  useEffect(() => () => { if (noticeTimer.current) window.clearTimeout(noticeTimer.current); }, []);

  const perform = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    setNotice("");
    try {
      await fn();
      setNotice(`${label} complete`);
      await refresh();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy("");
      if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
      noticeTimer.current = window.setTimeout(() => setNotice(""), 6000);
    }
  }, [refresh]);
  return { busy, notice, setNotice, perform };
}

/** Active tab, with /#tabid deep links (e.g. from a phone bookmark). */
export function useHashTab() {
  const [activeTab, setActiveTab] = useState<Tab>("today");
  useEffect(() => {
    const ids: Tab[] = TABS.map(t => t.id);
    const apply = () => {
      const h = window.location.hash.replace("#", "") as Tab;
      if (ids.includes(h)) setActiveTab(h);
    };
    const t = window.setTimeout(apply, 0);
    window.addEventListener("hashchange", apply);
    return () => { window.clearTimeout(t); window.removeEventListener("hashchange", apply); };
  }, []);
  return [activeTab, setActiveTab] as const;
}
