"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Dialog from "./Dialog";
import type { TradeLedgerEntry } from "@/lib/types";
import css from "./TradeLedgerView.module.css";

export default function TradeLedgerView() {
  const [trades, setTrades] = useState<TradeLedgerEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedTrade, setSelectedTrade] = useState<TradeLedgerEntry | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | "open" | "closed">("all");
  const [symbolFilter, setSymbolFilter] = useState("");

  const [loadedFilter, setLoadedFilter] = useState<string | null>(null);
  const loading = loadedFilter !== statusFilter;

  useEffect(() => {
    let active = true;
    api<TradeLedgerEntry[]>(`/api/trades?limit=200&status=${statusFilter}`)
      .then(data => { if (active) { setTrades(Array.isArray(data) ? data : []); setError(null); } })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (active) setLoadedFilter(statusFilter); });
    return () => { active = false; };
  }, [statusFilter]);

  const filteredTrades = trades.filter((t) =>
    symbolFilter === "" || t.symbol.toLowerCase().includes(symbolFilter.toLowerCase())
  );

  const openTrades = filteredTrades.filter((t) => t.status === "open");
  const closedTrades = filteredTrades.filter((t) => t.status === "closed");
  const totalPnl = closedTrades.reduce((sum, t) => sum + (t.realized_pnl || 0), 0);
  const winCount = closedTrades.filter((t) => (t.realized_pnl_pct || 0) > 0).length;
  const winRate = closedTrades.length > 0 ? (winCount / closedTrades.length * 100).toFixed(1) : "0.0";

  return (
    <div className="trade-ledger-view">
      <header className="view-header">
        <h2>Trade Ledger</h2>
        <div className="view-stats">
          <span className="stat">Open: {openTrades.length}</span>
          <span className="stat">Closed: {closedTrades.length}</span>
          <span className="stat">Win Rate: {winRate}%</span>
          <span className={`stat ${totalPnl >= 0 ? "positive" : "negative"}`}>
            Total P&L: ${totalPnl.toFixed(2)}
          </span>
        </div>
      </header>

      <div className="filters">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}>
          <option value="all">All</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <input
          type="text"
          placeholder="Filter by symbol..."
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
        />
      </div>

      {loading && <div className="loading">Loading trades…</div>}
      {error && <div className="error">{error}</div>}

      <table className={`${css["trades-table"]}`}>
        <thead>
          <tr>
            <th>ID</th>
            <th>Proposal</th>
            <th>Symbol</th>
            <th>Side</th>
            <th>Status</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Qty</th>
            <th>P&L</th>
            <th>P&L%</th>
            <th>MAE</th>
            <th>MFE</th>
            <th>Hold</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {filteredTrades.map((trade) => (
            <tr
              key={trade.id}
              className={selectedTrade?.id === trade.id ? "selected" : ""}
              onClick={() => setSelectedTrade(trade)}
            >
              <td>#{trade.id}</td>
              <td>#{trade.proposal_id}</td>
              <td>{trade.symbol}</td>
              <td className={trade.side === "BUY" ? "side-buy" : "side-sell"}>
                {trade.side}
              </td>
              <td>
                <span className={`status-badge status-${trade.status}`}>
                  {trade.status}
                </span>
              </td>
              <td>
                {trade.entry_fill_price != null ? trade.entry_fill_price.toFixed(2) : "—"}
                {trade.entry_fill_time && <> <br /><small>{new Date(trade.entry_fill_time).toLocaleString()}</small> </>}
              </td>
              <td>
                {trade.exit_fill_price != null ? trade.exit_fill_price.toFixed(2) : "—"}
                {trade.exit_fill_time && <> <br /><small>{new Date(trade.exit_fill_time).toLocaleString()}</small> </>}
              </td>
              <td>
                {trade.quantity_filled != null ? trade.quantity_filled : trade.quantity_requested || "—"}
                {trade.quantity_requested && trade.quantity_filled != null && trade.quantity_filled < trade.quantity_requested && (
                  <span className="partial-fill"> (partial)</span>
                )}
              </td>
              <td className={trade.realized_pnl != null && trade.realized_pnl > 0 ? "positive" : trade.realized_pnl != null && trade.realized_pnl < 0 ? "negative" : "neutral"}>
                {trade.realized_pnl != null ? (trade.realized_pnl > 0 ? "+" : "") + trade.realized_pnl.toFixed(2) : "—"}
              </td>
              <td className={trade.realized_pnl_pct != null && trade.realized_pnl_pct > 0 ? "positive" : trade.realized_pnl_pct != null && trade.realized_pnl_pct < 0 ? "negative" : "neutral"}>
                {trade.realized_pnl_pct != null ? (trade.realized_pnl_pct > 0 ? "+" : "") + trade.realized_pnl_pct.toFixed(2) + "%" : "—"}
              </td>
              <td className="negative">{trade.mae != null ? trade.mae.toFixed(2) + "%" : "—"}</td>
              <td className="positive">{trade.mfe != null ? trade.mfe.toFixed(2) + "%" : "—"}</td>
              <td>{trade.hold_seconds != null ? (trade.hold_seconds / 3600).toFixed(1) + "h" : "—"}</td>
              <td>
                <button className="button button-quiet" onClick={(e) => { e.stopPropagation(); setSelectedTrade(trade); }}>
                  View
                </button>
              </td>
            </tr>
          ))}
          {filteredTrades.length === 0 && (
            <tr>
              <td colSpan={14} className="empty">No trades found</td>
            </tr>
          )}
        </tbody>
      </table>

      {selectedTrade && (
        <TradeDetail trade={selectedTrade} onClose={() => setSelectedTrade(null)} />
      )}
    </div>
  );
}

function TradeDetail({ trade, onClose }: { trade: TradeLedgerEntry; onClose: () => void }) {
  return (
    <Dialog
      title={<>Trade #{trade.id} · {trade.symbol} {trade.side}</>}
      eyebrow="TRADE"
      size="lg"
      className={`${css["trade-detail"]}`}
      onClose={onClose}
      footer={<button className="button" onClick={onClose}>Close</button>}
    >

        <div className="detail-grid">
          <div className="detail-section">
            <h4>Entry</h4>
            <dl>
              <dt>Fill Price</dt>
              <dd>{trade.entry_fill_price != null ? trade.entry_fill_price.toFixed(2) : "—"}</dd>
              <dt>Fill Time</dt>
              <dd>{trade.entry_fill_time ? new Date(trade.entry_fill_time).toLocaleString() : "—"}</dd>
              <dt>Slippage</dt>
              <dd className={trade.entry_slippage != null && trade.entry_slippage > 0 ? "negative" : "neutral"}>
                {trade.entry_slippage != null ? trade.entry_slippage.toFixed(4) : "—"}
              </dd>
              <dt>Limit Price</dt>
              <dd>{trade.limit_price != null ? trade.limit_price.toFixed(2) : "—"}</dd>
              <dt>Quantity Requested</dt>
              <dd>{trade.quantity_requested != null ? trade.quantity_requested : "—"}</dd>
              <dt>Quantity Filled</dt>
              <dd>{trade.quantity_filled != null ? trade.quantity_filled : "—"}</dd>
              <dt>Stop Price</dt>
              <dd>{trade.stop_price != null ? trade.stop_price.toFixed(2) : "—"}</dd>
              <dt>Target Price</dt>
              <dd>{trade.target_price != null ? trade.target_price.toFixed(2) : "—"}</dd>
            </dl>
          </div>

          <div className="detail-section">
            <h4>Exit</h4>
            <dl>
              <dt>Fill Price</dt>
              <dd>{trade.exit_fill_price != null ? trade.exit_fill_price.toFixed(2) : "—"}</dd>
              <dt>Fill Time</dt>
              <dd>{trade.exit_fill_time ? new Date(trade.exit_fill_time).toLocaleString() : "—"}</dd>
              <dt>Slippage</dt>
              <dd className={trade.exit_slippage != null && trade.exit_slippage > 0 ? "negative" : "neutral"}>
                {trade.exit_slippage != null ? trade.exit_slippage.toFixed(4) : "—"}
              </dd>
              <dt>Exit Reason</dt>
              <dd>{trade.exit_reason || "—"}</dd>
              <dt>Hold Time</dt>
              <dd>{trade.hold_seconds != null ? (trade.hold_seconds / 3600).toFixed(1) + "h" : "—"}</dd>
            </dl>
          </div>

          <div className="detail-section">
            <h4>P&L & Risk Metrics</h4>
            <dl>
              <dt>Realized P&L</dt>
              <dd className={trade.realized_pnl != null && trade.realized_pnl > 0 ? "positive" : trade.realized_pnl != null && trade.realized_pnl < 0 ? "negative" : "neutral"}>
                {trade.realized_pnl != null ? (trade.realized_pnl > 0 ? "+" : "") + "$" + trade.realized_pnl.toFixed(2) : "—"}
              </dd>
              <dt>Realized P&L %</dt>
              <dd className={trade.realized_pnl_pct != null && trade.realized_pnl_pct > 0 ? "positive" : trade.realized_pnl_pct != null && trade.realized_pnl_pct < 0 ? "negative" : "neutral"}>
                {trade.realized_pnl_pct != null ? (trade.realized_pnl_pct > 0 ? "+" : "") + trade.realized_pnl_pct.toFixed(2) + "%" : "—"}
              </dd>
              <dt>MAE (Max Adverse Excursion)</dt>
              <dd className="negative">{trade.mae != null ? trade.mae.toFixed(2) + "%" : "—"}</dd>
              <dt>MFE (Max Favorable Excursion)</dt>
              <dd className="positive">{trade.mfe != null ? trade.mfe.toFixed(2) + "%" : "—"}</dd>
              <dt>MAE/P&L Ratio</dt>
              <dd>{trade.realized_pnl_pct != null && trade.mae != null && trade.realized_pnl_pct > 0 ? (trade.mae / trade.realized_pnl_pct).toFixed(2) : "—"}</dd>
            </dl>
          </div>

          <div className="detail-section">
            <h4>Entry Signal Snapshot</h4>
            <dl>
              <dt>TA Score</dt>
              <dd>{trade.entry_ta_score != null ? trade.entry_ta_score.toFixed(3) : "—"}</dd>
              <dt>Sentiment Score</dt>
              <dd>{trade.entry_sent_score != null ? trade.entry_sent_score.toFixed(3) : "—"}</dd>
              <dt>ML Probability</dt>
              <dd>{trade.entry_ml_p != null ? (trade.entry_ml_p * 100).toFixed(1) + "%" : "—"}</dd>
              <dt>Trend</dt>
              <dd>{trade.entry_trend || "—"}</dd>
              <dt>ATR</dt>
              <dd>{trade.entry_atr != null ? trade.entry_atr.toFixed(2) : "—"}</dd>
            </dl>
          </div>
        </div>

    </Dialog>
  );
}