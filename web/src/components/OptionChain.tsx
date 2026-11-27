"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { etClock, money, num, occLabel, pct } from "@/lib/format";
import type { Api } from "@/lib/api";
import type { Status } from "@/lib/types";
import css from "./OptionChain.module.css";

interface OptionContract {
  symbol: string;
  underlying: string;
  expiration: string;
  strike: number;
  type: "CALL" | "PUT";
  bid: number;
  ask: number;
  last: number;
  volume: number;
  open_interest: number;
  iv: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
}

interface OptionChainResponse {
  underlying: string;
  contracts: OptionContract[];
}

export default function OptionChain({ status, api, onSymbolSelect }: {
  status: Status | null;
  api: Api;
  onSymbolSelect?: (symbol: string) => void;
}) {
  const [underlying, setUnderlying] = useState("");
  const [minDte, setMinDte] = useState(7);
  const [maxDte, setMaxDte] = useState(45);
  const [data, setData] = useState<OptionChainResponse | null>(null); const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<"ALL" | "CALL" | "PUT">("ALL");
  const [sortKey, setSortKey] = useState<keyof OptionContract>("strike");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const allowedUnderlyings = useMemo(() => {
    if (status?.settings?.options_allowed_underlyings && status.settings.options_allowed_underlyings.length > 0) {
      return status.settings.options_allowed_underlyings;
    }
    if (status?.watchlist && status.watchlist.length > 0) {
      return status.watchlist;
    }
    return [] as string[];
  }, [status]);

  const watchlist = useMemo(() => status?.watchlist || [], [status?.watchlist]);
  const selectableUnderlyings = useMemo(() => {
    const combined = Array.from(new Set([...allowedUnderlyings, ...watchlist]));
    return combined.sort();
  }, [allowedUnderlyings, watchlist]);

  const reqId = useRef(0);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => { reqId.current = -1; ctrl.current?.abort(); }, []);

  const fetchChain = useCallback(async () => {
    if (!underlying) return;
    const id = ++reqId.current;
    ctrl.current?.abort();
    const ac = new AbortController();
    ctrl.current = ac;
    setLoading(true);
    setError("");
    try {
      const res = await api<OptionChainResponse>(
        `/api/options/chain?underlying=${encodeURIComponent(underlying)}&min_dte=${minDte}&max_dte=${maxDte}`
      );
      if (id !== reqId.current || ac.signal.aborted) return;
      setData(res); setFetchedAt(new Date());
    } catch (e) {
      if (id !== reqId.current || ac.signal.aborted) return;
      setError(e instanceof Error ? e.message : "Failed to fetch option chain");
    } finally {
      if (id === reqId.current) setLoading(false);
    }
  }, [api, underlying, minDte, maxDte]);

  // Debounced: the DTE inputs change on every keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => { void fetchChain(); }, 250);
    return () => window.clearTimeout(timer);
  }, [fetchChain]);

  const expirations = useMemo(() => {
    if (!data) return [] as string[];
    const exps = [...new Set(data.contracts.map(c => c.expiration))];
    return exps.sort();
  }, [data]);

  const filteredContracts = useMemo(() => {
    if (!data) return [];
    let contracts = data.contracts;
    if (filter) {
      const f = filter.toUpperCase();
      contracts = contracts.filter(c =>
        c.symbol.includes(f) || c.expiration.includes(f) || String(c.strike).includes(f)
      );
    }
    if (typeFilter !== "ALL") {
      contracts = contracts.filter(c => c.type === typeFilter);
    }
    contracts.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return contracts;
  }, [data, filter, typeFilter, sortKey, sortDir]);

  const handleSort = (key: keyof OptionContract) => {
    if (sortKey === key) {
      setSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  const renderSortIndicator = (key: keyof OptionContract) => {
    if (sortKey !== key) return <span className={`${css["sort-indicator"]} ${css["inactive"]}`}>⇅</span>;
    return <span className={`${css["sort-indicator"]} active`}>{sortDir === "asc" ? "▲" : "▼"}</span>;
  };

  if (!status?.connected) {
    return <section className="panel"><div className="panel-head"><div className="eyebrow">Option chain</div><h2>Not connected</h2></div><p className="muted">Connect to broker to view option chains.</p></section>;
  }

  return <section className="panel" id="option-chain">
    <div className="panel-head">
      <div><div className="eyebrow">Option chain</div><h2>{underlying || "Select underlying"}</h2></div>
      <span className="muted">DTE {minDte}–{maxDte} · {data ? `${data.contracts.length} contracts` : "—"}</span>
    </div>

    {selectableUnderlyings.length === 0 && <div className="empty-state"><p>No underlyings available from the server yet.</p></div>}
    <div className="controls-row">
      <select
        value={underlying}
        onChange={e => { setUnderlying(e.target.value); setError(""); }}
        className="input"
      >
        <option value="">— Select underlying —</option>
        {selectableUnderlyings.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <div className={`${css["input-group"]}`}>
        <label className="muted">Min DTE</label>
        <input type="number" min="0" max={maxDte} value={minDte} onChange={e => setMinDte(Math.max(0, Math.min(parseInt(e.target.value) || 0, maxDte)))} className={`input ${css["w-80"]}`} />
      </div>
      <div className={`${css["input-group"]}`}>
        <label className="muted">Max DTE</label>
        <input type="number" min={minDte} value={maxDte} onChange={e => setMaxDte(Math.max(minDte, parseInt(e.target.value) || minDte))} className={`input ${css["w-80"]}`} />
      </div>
      <select value={typeFilter} onChange={e => setTypeFilter(e.target.value as "ALL" | "CALL" | "PUT")} className={`input ${css["w-120"]}`}>
        <option value="ALL">All types</option>
        <option value="CALL">Calls only</option>
        <option value="PUT">Puts only</option>
      </select>
      <input type="text" placeholder="Filter (symbol, strike, exp…)" value={filter} onChange={e => setFilter(e.target.value)} className={`input ${css["grow"]}`} />
      {loading && <span className="spinner" aria-label="Loading"/>}
      {fetchedAt && !loading && <span className="muted small">Quotes as of {etClock(fetchedAt)} ET</span>}
      {error && <span className={`inline-warning ${css["push"]}`}>{error}</span>}
    </div>

    {data && (
      <div className={`${css["expirations-tabs"]}`}>
        <button
          className={`${css["tab-button"]} ${filter === "" ? "active" : ""}`}
          onClick={() => setFilter("")}
        >
          All ({data.contracts.length})
        </button>
        {expirations.map(exp => (
          <button
            key={exp}
            className={`${css["tab-button"]} ${filter === exp ? "active" : ""}`}
            onClick={() => setFilter(filter === exp ? "" : exp)}
          >
            {exp} ({data.contracts.filter(c => c.expiration === exp).length})
          </button>
        ))}
      </div>
    )}

    {error && (
      <div className={`inline-warning ${css["gap-y"]}`} role="alert">
        {error}
      </div>
    )}

    {data && data.contracts.length === 0 && !loading && !error && (
      <div className="empty-state"><p>No option contracts found for {underlying} with DTE {minDte}–{maxDte}.</p></div>
    )}

    {data && data.contracts.length > 0 && filteredContracts.length === 0 && !loading && (
      <div className="empty-state"><p>No contracts match the current filters.</p></div>
    )}

    {data && (
      <div className="table-wrapper">
        <table className="data-table">
          <thead>
            <tr>
              <th onClick={() => handleSort("symbol")}>Symbol {renderSortIndicator("symbol")}</th>
              <th onClick={() => handleSort("expiration")}>Exp {renderSortIndicator("expiration")}</th>
              <th onClick={() => handleSort("type")}>Type {renderSortIndicator("type")}</th>
              <th onClick={() => handleSort("strike")}>Strike {renderSortIndicator("strike")}</th>
              <th onClick={() => handleSort("bid")}>Bid {renderSortIndicator("bid")}</th>
              <th onClick={() => handleSort("ask")}>Ask {renderSortIndicator("ask")}</th>
              <th onClick={() => handleSort("last")}>Last {renderSortIndicator("last")}</th>
              <th onClick={() => handleSort("volume")}>Vol {renderSortIndicator("volume")}</th>
              <th onClick={() => handleSort("open_interest")}>OI {renderSortIndicator("open_interest")}</th>
              <th onClick={() => handleSort("iv")}>IV {renderSortIndicator("iv")}</th>
              <th onClick={() => handleSort("delta")}>Δ {renderSortIndicator("delta")}</th>
              <th onClick={() => handleSort("gamma")}>Γ {renderSortIndicator("gamma")}</th>
              <th onClick={() => handleSort("theta")}>Θ {renderSortIndicator("theta")}</th>
              <th onClick={() => handleSort("vega")}>ν {renderSortIndicator("vega")}</th>
            </tr>
          </thead>
          <tbody>
            {filteredContracts.map(c => (
              <tr key={c.symbol} onClick={() => onSymbolSelect?.(c.symbol)} className={css["row-link"]}>
                <td title={c.symbol}>{occLabel(c.symbol)}</td>
                <td>{c.expiration}</td>
                <td><span className={c.type === "CALL" ? `${css["call-badge"]}` : `${css["put-badge"]}`}>{c.type === "CALL" ? "Call" : "Put"}</span></td>
                <td>{money(c.strike)}</td>
                <td>{money(c.bid)}</td>
                <td>{money(c.ask)}</td>
                <td>{money(c.last)}</td>
                <td>{num(c.volume, 0)}</td>
                <td>{num(c.open_interest, 0)}</td>
                <td>{pct(c.iv)}</td>
                <td>{num(c.delta, 3)}</td>
                <td>{num(c.gamma, 4)}</td>
                <td>{num(c.theta, 2)}</td>
                <td>{num(c.vega, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}
  </section>;
}