"use client";

import { useEffect, useState } from "react";
import { pct } from "@/lib/format";
import type { Api } from "@/lib/api";
import type { Status } from "@/lib/types";

const OPTIONS_STATUS_REFRESH_MS = 30_000;

interface OptionsStatusData {
  enabled: boolean;
  broker_supports_options: boolean;
  allowed_strategies: string | string[];
  allowed_underlyings: string[] | string | null;
  max_contracts_per_order: number;
  max_notional_usd: number;
  min_dte: number;
  max_dte: number;
  require_approval: boolean;
  consensus_gate: {
    iv_rank_max: number;
    iv_rank_min: number;
    delta_min: number;
    delta_max: number;
    min_volume: number;
    min_open_interest: number;
    max_bid_ask_pct: number;
  };
  broker_label: string;
  is_live: boolean;
}

export default function OptionsStatus({ status, api }: {
  status: Status | null;
  api: Api;
}) {
  const [data, setData] = useState<OptionsStatusData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Auto-refresh (L10): the limits and the gate can change with a restart or a lesson refresh.
  useEffect(() => {
    let active = true;
    const load = async (first: boolean) => {
      if (first) setLoading(true);
      try {
        const res = await api<OptionsStatusData>("/api/options/status");
        if (!active) return;
        setData(prev => (JSON.stringify(prev) === JSON.stringify(res) ? prev : res));
        setError("");
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "Failed to load options status");
      } finally {
        if (active && first) setLoading(false);
      }
    };
    void load(true);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(false);
    }, OPTIONS_STATUS_REFRESH_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, [api]);

  if (!status?.connected) {
    return <section className="panel"><div className="panel-head"><div className="eyebrow">OPTIONS</div><h2>Not connected</h2></div><p className="muted">Connect to broker to view options status.</p></section>;
  }

  if (loading) {
    return <section className="panel"><div className="panel-head"><div className="eyebrow">OPTIONS</div><h2>Loading…</h2></div><span className="spinner" aria-label="Loading"/></section>;
  }

  if (error) {
    return <section className="panel"><div className="panel-head"><div className="eyebrow">OPTIONS</div><h2>Error</h2></div><div className="inline-warning">{error}</div></section>;
  }

  if (!data) return null;

  return <section className="panel" id="options-status">
    <div className="panel-head">
      <div><div className="eyebrow">OPTIONS</div><h2>Options Trading</h2></div>
      <span className={data.enabled ? "pill-green" : "pill-red"}>{data.enabled ? "ENABLED" : "DISABLED"}</span>
    </div>

    {!data.enabled && (
      <div className="inline-warning" role="alert">
        Options trading is disabled. Set <code>OPTIONS_ENABLED=true</code> in .env and restart.
      </div>
    )}

    <div className="grid grid-2">
      <div>
        <h4>Broker</h4>
        <dl className="kv">
          <dt>Label</dt><dd>{data.broker_label}</dd>
          <dt>Environment</dt><dd>{data.is_live ? "LIVE" : "Sandbox (UAT)"}</dd>
          <dt>Supports options</dt><dd>{data.broker_supports_options ? "Yes" : "No"}</dd>
        </dl>
      </div>
      <div>
        <h4>Risk Limits</h4>
        <dl className="kv">
          <dt>Max contracts/order</dt><dd>{data.max_contracts_per_order}</dd>
          <dt>Max notional/order</dt><dd>${data.max_notional_usd.toLocaleString()}</dd>
          <dt>DTE range</dt><dd>{data.min_dte}–{data.max_dte} days</dd>
          <dt>Requires approval</dt><dd>{data.require_approval ? "Yes" : "No"}</dd>
        </dl>
      </div>
      <div>
        <h4>Consensus Gate</h4>
        <dl className="kv">
          <dt>IV rank max (long)</dt><dd>{pct(data.consensus_gate.iv_rank_max)}</dd>
          <dt>IV rank min (short)</dt><dd>{pct(data.consensus_gate.iv_rank_min)}</dd>
          <dt>Delta range (long)</dt><dd>{data.consensus_gate.delta_min}–{data.consensus_gate.delta_max}</dd>
          <dt>Min volume</dt><dd>{data.consensus_gate.min_volume.toLocaleString()}</dd>
          <dt>Min open interest</dt><dd>{data.consensus_gate.min_open_interest.toLocaleString()}</dd>
          <dt>Max bid-ask spread</dt><dd>{data.consensus_gate.max_bid_ask_pct}%</dd>
        </dl>
      </div>
      <div>
        <h4>Allowed Strategies</h4>
        <p className="muted">
          {Array.isArray(data.allowed_strategies)
            ? (data.allowed_strategies.length > 0 ? data.allowed_strategies.join(", ") : "None specified")
            : (data.allowed_strategies || "None specified")}
        </p>
        <h4 style={{ marginTop: 12 }}>Allowed Underlyings</h4>
        <p className="muted">
          {Array.isArray(data.allowed_underlyings)
            ? (data.allowed_underlyings.length > 0 ? data.allowed_underlyings.join(", ") : "None configured")
            : (typeof data.allowed_underlyings === "string" && data.allowed_underlyings.trim() ? data.allowed_underlyings : "None configured")}
        </p>
      </div>
    </div>
  </section>;
}