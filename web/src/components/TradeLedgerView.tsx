"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import Dialog from "./Dialog";
import Section from "./ui/Section";
import Segmented from "./ui/Segmented";
import Mark from "./ui/Mark";
import Empty from "./ui/Empty";
import TradeStats from "./results/TradeStats";
import OptionsModel from "./results/OptionsModel";
import { money, num, pct } from "@/lib/format";
import type { TradeLedgerEntry } from "@/lib/types";
import css from "./TradeLedgerView.module.css";
import rcss from "./results/results.module.css";

const CAP = 25;
const pnlCls = (v: number | null) => (v == null || v === 0 ? "" : v > 0 ? rcss["gain"] : rcss["loss"]);

export default function TradeLedgerView() {
  const [trades, setTrades] = useState<TradeLedgerEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedTrade, setSelectedTrade] = useState<TradeLedgerEntry | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | "open" | "closed">("all");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [showAll, setShowAll] = useState(false);

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
  const shown = showAll ? filteredTrades : filteredTrades.slice(0, CAP);

  return (
    <div className="trade-ledger-view">
      <TradeStats />
      <OptionsModel />
      <Section title="Trades" meta={`${filteredTrades.length} shown`} rule right={
        <div className={rcss["filters"]}>
          <Segmented label="Status" value={statusFilter} onChange={setStatusFilter}
            options={[{ value: "all", label: "All" }, { value: "open", label: "Open" }, { value: "closed", label: "Closed" }]} />
          <input type="text" aria-label="Filter by symbol" placeholder="Symbol" value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)} />
        </div>
      }>
        {loading && <p className="loading">Loading trades…</p>}
        {error && <p className="error">{error}</p>}
        {filteredTrades.length === 0 && !loading ? <Empty>No trades match. Trades appear here once the bot places an order.</Empty> : (
          <div className={rcss["tableWrap"]}>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th><th>Side</th><th>Status</th>
                  <th className="num">Entry</th><th className="num">Exit</th><th className="num">Qty</th>
                  <th className="num">P&amp;L</th><th className="num">Return</th><th className="num">Hold</th>
                  <th>Exit reason</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <tr key={t.id} onClick={() => setSelectedTrade(t)}>
                    <td>{t.symbol}</td>
                    <td>{t.side}</td>
                    <td><Mark tone={t.status === "open" ? "warn" : "neutral"}>{t.status}</Mark></td>
                    <td className="num mono">{num(t.entry_fill_price)}</td>
                    <td className="num mono">{num(t.exit_fill_price)}</td>
                    <td className="num mono">{t.quantity_filled ?? t.quantity_requested ?? "—"}</td>
                    <td className={`num mono ${pnlCls(t.realized_pnl)}`}>{money(t.realized_pnl, { sign: true })}</td>
                    <td className={`num mono ${pnlCls(t.realized_pnl_pct)}`}>{pct(t.realized_pnl_pct)}</td>
                    <td className="num mono">{t.hold_seconds != null ? `${num(t.hold_seconds / 86400, 1)} d` : "—"}</td>
                    <td>{t.exit_reason || "—"}</td>
                    <td><button className="button" onClick={(e) => { e.stopPropagation(); setSelectedTrade(t); }}>View</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!showAll && filteredTrades.length > CAP &&
          <button className={`button ${rcss["more"]}`} onClick={() => setShowAll(true)}>Show all {filteredTrades.length}</button>}
      </Section>

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