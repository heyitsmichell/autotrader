"use client";

import { useMemo, useState } from "react";
import { dateTime } from "@/lib/format";
import type { Research, ResearchPacket } from "@/lib/types";

export default function ResearchPanel({ research }: { research: Research | null }) {
  const [expanded, setExpanded] = useState(false);
  const candidates = (research?.packets || []).filter(p => p.packet_type === "candidate");

  const grouped = useMemo(() => {
    const map = new Map<string, { symbol: string; asset: string; packets: ResearchPacket[] }>();
    for (const packet of candidates) {
      const key = `${packet.asset_class}:${packet.symbol}`;
      const existing = map.get(key);
      if (existing) existing.packets.push(packet);
      else map.set(key, { symbol: packet.symbol, asset: packet.asset_class, packets: [packet] });
    }
    return [...map.values()].map(group => {
      const sorted = [...group.packets].sort((a, b) => b.id - a.id);
      const latest = sorted[0];
      return {
        ...group,
        latest,
        count: sorted.length,
        evidence: (latest.evidence || []).slice(0, 2).map(item => `${item.source || "source"}: ${item.excerpt || "data received"}`).join(" · "),
        caveats: latest.caveats || [],
      };
    }).sort((a, b) => b.latest.id - a.latest.id);
  }, [candidates]);

  const visible = expanded ? grouped : grouped.slice(0, 8);

  return <section className="panel">
    <div className="panel-head">
      <div>
        <div className="eyebrow">CROSS-MARKET DISCOVERY</div>
        <h2>Research candidates <span className="count-pill">{grouped.length}</span></h2>
      </div>
      <div className="panel-actions">
        <span className="muted">{research?.refreshing ? "Scanning…" : research?.last_refresh ? `Updated ${dateTime(research.last_refresh)}` : "Startup scan pending"}</span>
      </div>
    </div>
    <div className="panel-foot">
      Read-only discovery. Qualifying equities must appear in both mover and activity scans, with price ≥ $5 and
      volume ≥ 250k. Auto-managed equities: {(research?.auto_watchlist || []).join(", ") || "none"} (max 10).
      Groq reviews these leads and verifies current evidence; proposals still pass the signal and risk gates.
      <b> Options and crypto are research-only and not authorized for trading.</b>
    </div>
    {research?.auto_candidate_trading && <div className="inline-warning" role="alert">
      AUTO_TRADE_RESEARCH_CANDIDATES is on. Auto-discovered symbols can be proposed without an approval step.
      The audit recommends switching this off until candidate eligibility rules are enforced in code.
    </div>}
    {research?.errors && Object.keys(research.errors).length > 0 && <div className="inline-warning">
      Unavailable sources: {Object.keys(research.errors).join(", ")}
    </div>}
    {!grouped.length
      ? <Empty icon="⌕" text="Run a scan to collect broker research packets."/>
      : <div className="table-scroll">
        <table>
          <thead><tr><th>MARKET</th><th>SYMBOL</th><th>OBSERVED EVIDENCE</th><th>STATUS</th></tr></thead>
          <tbody>
            {visible.map(group => <tr key={`${group.asset}:${group.symbol}`}>
              <td>{group.asset}</td>
              <td>
                <b>{group.symbol}</b>
                <small>{group.count > 1 ? `${group.count} packets · latest ${dateTime(group.latest.created_at)}` : dateTime(group.latest.created_at)}</small>
              </td>
              <td className="ellipsis" title={group.caveats.join(" ")}>{group.evidence || "No structured evidence in the latest packet"}</td>
              <td>{(() => {
                const st = rowStatus(group.asset, group.symbol, research);
                return <><span className={`status-chip ${st.tone}`}>{st.label}</span><small>{st.note}</small></>;
              })()}</td>
            </tr>)}
          </tbody>
        </table>
      </div>}
    {grouped.length > 8 && <div className="actions-end">
      <button className="button button-quiet" aria-expanded={expanded} onClick={() => setExpanded(v => !v)}>
        {expanded ? "Show fewer" : `Show all ${grouped.length}`}
      </button>
    </div>}
    <div className="panel-foot">
      Option collateral and crypto order impact are not assumed from buying power. Cross-market live automation
      remains blocked until broker-specific risk evidence is available.
    </div>
  </section>;
}

// Real per-row state, mirroring the backend rules (app/research.py and engine.execute).
function rowStatus(asset: string, symbol: string, research: Research | null) {
  if (asset === "option") return { label: "Research only", tone: "research", note: "no option order path" };
  if (asset === "crypto_spot") return { label: "Research only", tone: "research", note: "no crypto order path" };
  if ((research?.auto_watchlist || []).includes(symbol)) {
    return research?.auto_candidate_execution
      ? { label: "AI review", tone: "pending", note: "auto-trade allowed; risk checks still apply" }
      : { label: "AI review", tone: "pending", note: "trade needs your approval" };
  }
  return { label: "Not eligible", tone: "dismissed", note: "needs both movers + most-active scans" };
}

function Empty({ icon, text }: { icon: string; text: string }) {
  return <div className="empty-state"><span aria-hidden="true">{icon}</span><p>{text}</p></div>;
}
