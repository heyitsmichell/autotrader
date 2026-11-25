"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { Api } from "@/lib/api";
import Segmented from "@/components/ui/Segmented";
import MomentumView from "./MomentumView";
import VaaView from "./VaaView";
import PutwriteView from "./PutwriteView";
import OverlapView from "./OverlapView";
import MarketView from "./MarketView";
import css from "./Research.module.css";

type View = "swing" | "momentum" | "vaa" | "putwrite" | "overlap" | "market";
const OPTIONS: { value: View; label: string }[] = [
  { value: "swing", label: "Swing" }, { value: "momentum", label: "Momentum" }, { value: "vaa", label: "VAA" },
  { value: "putwrite", label: "Put-write" }, { value: "overlap", label: "Overlap" }, { value: "market", label: "Market" },
];
const KEY = "trader.research.view";

export default function ResearchPicker({ api, swing }: { api: Api; swing: ReactNode }) {
  const [view, setView] = useState<View>("swing");
  useEffect(() => {
    // Deferred so the first render matches the server; restores the remembered choice right after mount.
    const t = window.setTimeout(() => {
      try {
        const v = window.localStorage.getItem(KEY);
        if (v && OPTIONS.some(o => o.value === v)) setView(v as View);
      } catch { /* storage unavailable */ }
    }, 0);
    return () => window.clearTimeout(t);
  }, []);
  const choose = (v: View) => {
    setView(v);
    try { window.localStorage.setItem(KEY, v); } catch { /* ignore */ }
  };
  return <>
    <div className={css["picker"]}><Segmented label="Strategy view" options={OPTIONS} value={view} onChange={choose}/></div>
    {view === "swing" && swing}
    {view === "momentum" && <MomentumView api={api}/>}
    {view === "vaa" && <VaaView api={api}/>}
    {view === "putwrite" && <PutwriteView api={api}/>}
    {view === "overlap" && <OverlapView api={api}/>}
    {view === "market" && <MarketView api={api}/>}
  </>;
}
