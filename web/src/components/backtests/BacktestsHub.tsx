"use client";

import { useEffect, useState } from "react";
import type { Api } from "@/lib/api";
import Segmented from "@/components/ui/Segmented";
import Backtests from "./Backtests";
import EvidenceCards from "./EvidenceCards";
import MlLab from "./MlLab";
import PipelineView from "./PipelineView";
import PortfolioView from "./PortfolioView";
import css from "./Bt.module.css";

type View = "strategies" | "portfolio" | "pipeline" | "ml" | "library";
const KEY = "backtests.view";
const VIEWS: View[] = ["strategies", "portfolio", "pipeline", "ml", "library"];

export default function BacktestsHub({ api }: { api: Api }) {
  const [view, setView] = useState<View>("strategies");
  useEffect(() => {
    try {
      const v = localStorage.getItem(KEY) as View | null;
      if (v && VIEWS.includes(v)) setView(v); // eslint-disable-line react-hooks/set-state-in-effect
    } catch { /* storage unavailable */ }
  }, []);
  const pick = (v: View) => {
    setView(v);
    try { localStorage.setItem(KEY, v); } catch { /* storage unavailable */ }
  };
  return <div className={css["wrap"]}>
    <Segmented<View> label="Backtests view" value={view} onChange={pick} options={[
      { value: "strategies", label: "Strategies" }, { value: "portfolio", label: "Portfolio" }, { value: "pipeline", label: "Pipeline" },
      { value: "ml", label: "ML lab" }, { value: "library", label: "Library" }]}/>
    {view === "strategies" && <EvidenceCards api={api}/>}
    {view === "portfolio" && <PortfolioView api={api}/>}
    {view === "pipeline" && <PipelineView api={api}/>}
    {view === "ml" && <MlLab api={api}/>}
    {view === "library" && <Backtests api={api}/>}
  </div>;
}
