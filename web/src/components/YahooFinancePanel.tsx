"use client";

import { useEffect, useState } from "react";
import { money } from "@/lib/format";
import type { Api } from "@/lib/api";

interface YFinanceSummary {
  symbol: string;
  name?: string;
  sector?: string;
  industry?: string;
  market_cap?: number;
  pe_trailing?: number;
  pe_forward?: number;
  peg_ratio?: number;
  price_to_book?: number;
  beta?: number;
  profit_margins?: number;
  revenue_growth?: number;
  target_mean_price?: number;
  target_high?: number;
  target_low?: number;
  recommendation?: string;
  number_of_analyst_opinions?: number;
  earnings_date?: string;
  fetched_at?: string;
  error?: string;
}

interface MacroContext {
  VIX?: { price: number; prev_close: number };
  SP500?: { price: number; prev_close: number };
  NASDAQ?: { price: number; prev_close: number };
  US10Y?: { price: number; prev_close: number };
  error?: string;
}

export default function YahooFinancePanel({ symbols, api }: { symbols: string[]; api: Api }) {
  const [picked, setSymbol] = useState(symbols[0] || "AAPL");
  const symbol = symbols.length && !symbols.includes(picked) ? symbols[0] : picked;
  const [data, setData] = useState<YFinanceSummary | null>(null);
  const [macro, setMacro] = useState<MacroContext | null>(null);
  // Loading and errors are keyed by symbol, so switching symbols needs no synchronous reset.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ symbol: string; message: string } | null>(null);
  const loading = !!symbol && loadedFor !== symbol;
  const error = failure && failure.symbol === symbol ? failure.message : "";

  useEffect(() => {
    const handleSymbolEvent = (e: Event) => {
      const sym = (e as CustomEvent<string>).detail;
      if (sym) setSymbol(sym);
    };
    window.addEventListener("chart-symbol", handleSymbolEvent);
    return () => window.removeEventListener("chart-symbol", handleSymbolEvent);
  }, []);

  useEffect(() => {
    // Fetch macro context once
    void api<MacroContext>("/api/yfinance/macro/context")
      .then(res => setMacro(res))
      .catch(() => {});
  }, [api]);

  useEffect(() => {
    if (!symbol) return;
    let active = true;
    void api<YFinanceSummary>(`/api/yfinance/${encodeURIComponent(symbol)}`)
      .then(res => {
        if (!active) return;
        if (res.error) setFailure({ symbol, message: res.error });
        else { setData(res); setFailure(null); }
      })
      .catch(e => { if (active) setFailure({ symbol, message: e instanceof Error ? e.message : "Failed to load Yahoo Finance data" }); })
      .finally(() => { if (active) setLoadedFor(symbol); });
    return () => { active = false; };
  }, [symbol, api]);

  const formatPct = (val?: number | null) => {
    if (val == null) return "—";
    return `${(val * 100).toFixed(1)}%`;
  };

  const formatCap = (cap?: number | null) => {
    if (cap == null) return "—";
    if (cap >= 1e12) return `$${(cap / 1e12).toFixed(2)}T`;
    if (cap >= 1e9) return `$${(cap / 1e9).toFixed(2)}B`;
    if (cap >= 1e6) return `$${(cap / 1e6).toFixed(2)}M`;
    return `$${cap.toLocaleString()}`;
  };

  return (
    <section className="panel" id="yfinance-fundamentals">
      <div className="panel-head">
        <div>
          <div className="eyebrow">YAHOO FINANCE INSIGHTS</div>
          <h2>
            {symbol} {data?.name ? <span className="muted" style={{ fontSize: 13, fontWeight: "normal" }}>({data.name})</span> : null}
          </h2>
        </div>
        <div className="panel-actions">
          <select
            value={symbol}
            onChange={e => setSymbol(e.target.value)}
            className="input"
            style={{ width: "100px", padding: "4px 8px" }}
          >
            {symbols.map(s => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {loading && <span className="spinner" aria-label="Loading" />}
        </div>
      </div>

      {macro && (
        <div className="controls-row" style={{ marginTop: -4, marginBottom: 12, padding: "6px 12px", fontSize: 11 }}>
          <span className="muted" style={{ marginRight: 6 }}>Macro:</span>
          {macro.SP500 && <span>S&P: <b>{macro.SP500.price?.toFixed(1) || "—"}</b></span>}
          {macro.NASDAQ && <span>Nasdaq: <b>{macro.NASDAQ.price?.toFixed(1) || "—"}</b></span>}
          {macro.VIX && <span>VIX: <b style={{ color: macro.VIX.price > 25 ? "var(--red)" : "var(--green)" }}>{macro.VIX.price?.toFixed(1) || "—"}</b></span>}
          {macro.US10Y && <span>US 10Y: <b>{macro.US10Y.price?.toFixed(2)}%</b></span>}
        </div>
      )}

      {error && <div className="inline-warning">{error}</div>}

      {data && !error && (
        <div className="grid grid-2" style={{ gap: 14 }}>
          <div>
            <h4>Fundamentals & Valuation</h4>
            <dl className="kv">
              <dt>Sector / Industry</dt>
              <dd>{data.sector || "—"} · {data.industry || "—"}</dd>
              <dt>Market Cap</dt>
              <dd>{formatCap(data.market_cap)}</dd>
              <dt>Trailing P/E</dt>
              <dd>{data.pe_trailing ? data.pe_trailing.toFixed(2) : "—"}</dd>
              <dt>Forward P/E</dt>
              <dd>{data.pe_forward ? data.pe_forward.toFixed(2) : "—"}</dd>
              <dt>PEG Ratio</dt>
              <dd>{data.peg_ratio ? data.peg_ratio.toFixed(2) : "—"}</dd>
              <dt>Price to Book</dt>
              <dd>{data.price_to_book ? data.price_to_book.toFixed(2) : "—"}</dd>
              <dt>Beta</dt>
              <dd>{data.beta ? data.beta.toFixed(2) : "—"}</dd>
              <dt>Profit Margin</dt>
              <dd>{formatPct(data.profit_margins)}</dd>
              <dt>Revenue Growth (YoY)</dt>
              <dd>{formatPct(data.revenue_growth)}</dd>
            </dl>
          </div>

          <div>
            <h4>Wall Street Consensus</h4>
            <dl className="kv">
              <dt>Analyst Rating</dt>
              <dd>
                <span className="pill-green" style={{ textTransform: "uppercase" }}>
                  {data.recommendation ? String(data.recommendation).replace(/_/g, " ") : "N/A"}
                </span>
                {data.number_of_analyst_opinions ? ` (${data.number_of_analyst_opinions} analysts)` : ""}
              </dd>
              <dt>Mean Target</dt>
              <dd>{data.target_mean_price != null ? money(data.target_mean_price) : "—"}</dd>
              <dt>Target Range</dt>
              <dd>
                {data.target_low != null ? money(data.target_low) : "—"} –{" "}
                {data.target_high != null ? money(data.target_high) : "—"}
              </dd>
              <dt>Next Earnings Date</dt>
              <dd>{data.earnings_date || "Not scheduled"}</dd>
            </dl>
          </div>
        </div>
      )}
    </section>
  );
}
