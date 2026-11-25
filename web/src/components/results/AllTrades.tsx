"use client";

import Section from "@/components/ui/Section";
import { useEffect, useMemo, useState } from "react";
import type { Api } from "@/lib/api";
import { money, num, occLabel, signTone } from "@/lib/format";
import type { ResultTrade, ResultTradesPayload } from "@/lib/types";
import Empty from "@/components/ui/Empty";
import Segmented from "@/components/ui/Segmented";
import { strategyColor, strategyName } from "./names";
import css from "./ResultsTables.module.css";

type Strat = "all" | "swing" | "momentum" | "putwrite" | "vaa" | "shadows";
const STRATS: [Strat, string][] = [["all", "All"], ["swing", "Swing"], ["momentum", "Momentum"], ["putwrite", "Put-write"], ["vaa", "VAA"], ["shadows", "Shadows"]];
const FAILED = new Set(["rejected", "cancelled", "canceled", "failed"]);
const PAGE = 50;

const when = (ts: string) => {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const tone = (v: unknown) => { const t = signTone(v); return t === "positive" ? css["gain"] : t === "negative" ? css["loss"] : ""; };

function Strategy({ t }: { t: ResultTrade }) {
  return <><i className={css["chip"]} style={{ background: strategyColor(t.strategy, t.live) }}/>{strategyName(t.strategy)}
    {!t.live && <span className={css["tag"]}>virtual</span>}</>;
}
const symbol = (t: ResultTrade) => (t.kind === "option" ? occLabel(t.symbol) : t.symbol);

export default function AllTrades({ api }: { api: Api }) {
  const [strategy, setStrategy] = useState<Strat>("all");
  const [includeFailed, setIncludeFailed] = useState(false);
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<ResultTradesPayload | null>(null);
  const [err, setErr] = useState(false);

  // one request (every strategy, failed orders included) refreshed each minute; every filter runs here
  useEffect(() => {
    let live = true;
    const load = () => api<ResultTradesPayload>("/api/results/trades?strategy=all&days=90&include_failed=1&limit=2000")
      .then(d => { if (live) { setData(d); setErr(false); } })
      .catch(() => { if (live) setErr(true); });
    load();
    const t = window.setInterval(load, 60_000);
    return () => { live = false; window.clearInterval(t); };
  }, [api]);

  const base = useMemo(() => (data?.trades ?? []).filter(t => includeFailed || !FAILED.has(t.status)), [data, includeFailed]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: base.length, shadows: 0 };
    for (const t of base) { c[t.strategy] = (c[t.strategy] ?? 0) + 1; if (!t.live) c["shadows"]++; }
    return c;
  }, [base]);
  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    return base.filter(t => (strategy === "all" || (strategy === "shadows" ? !t.live : t.strategy === strategy))
      && (!s || (t.symbol ?? "").toLowerCase().includes(s)));
  }, [base, strategy, q]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const cur = Math.min(page, pages - 1);
  const view = rows.slice(cur * PAGE, cur * PAGE + PAGE);

  return <Section title="All trades" meta="Every fill, every strategy, last 90 days · closing fills show realized P&L"><div>
    <div className={css["filters"]}>
      <Segmented<Strat> label="Strategy" value={strategy} onChange={v => { setStrategy(v); setPage(0); }}
        options={STRATS.map(([v, l]) => ({ value: v, label: counts[v] != null ? `${l} ${counts[v]}` : l }))}/>
      <label className={css["toggle"]}><input type="checkbox" checked={includeFailed}
        onChange={e => { setIncludeFailed(e.target.checked); setPage(0); }}/>Include failed</label>
      <input type="search" placeholder="Symbol" aria-label="Search by symbol" value={q}
        onChange={e => { setQ(e.target.value); setPage(0); }}/>
    </div>
    {err && !data && <p className={css["muted"]}>Trades are not available right now.</p>}
    {data && rows.length === 0 && <Empty>No trades match these filters.</Empty>}
    {rows.length > 0 && <>
      <div className={`${css["scroll"]} ${css["wide"]}`}>
        <table className={css["tbl"]}>
          <thead><tr><th>Time</th><th className={css["l"]}>Strategy</th><th className={css["l"]}>Symbol</th><th className={css["l"]}>Side</th>
            <th>Qty</th><th>Price</th><th>Value</th><th>P&L</th><th className={css["l"]}>Status</th><th className={css["l"]}>Reason</th></tr></thead>
          <tbody>{view.map((t, i) => <tr key={`${t.ts}-${t.symbol}-${i}`}>
            <td>{when(t.ts)}</td><td className={css["l"]}><Strategy t={t}/></td><td className={css["l"]} title={t.symbol}>{symbol(t)}</td>
            <td className={css["l"]}>{t.side}</td><td>{num(t.qty, 0)}</td><td>{money(t.price)}</td><td>{money(t.notional)}</td>
            <td className={tone(t.pnl)}>{t.pnl == null ? "—" : money(t.pnl, { sign: true })}</td>
            <td className={css["l"]}>{t.status}</td>
            <td className={css["l"]}><span className={css["reason"]} title={t.reason ?? ""}>{t.reason || "—"}</span></td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className={css["cards"]}>
        {view.map((t, i) => <div key={`${t.ts}-${t.symbol}-${i}`} className={css["card"]}>
          <div><span><Strategy t={t}/> · {symbol(t)}</span><span className={tone(t.pnl)}>{t.pnl == null ? "—" : money(t.pnl, { sign: true })}</span></div>
          <div><span>{t.side} {num(t.qty, 0)} @ {money(t.price)}</span><span>{money(t.notional)}</span></div>
          <div className={css["sub"]}><span>{when(t.ts)} · {t.status}</span></div>
          {t.reason && <div className={css["sub"]} title={t.reason}><span>{t.reason.length > 80 ? `${t.reason.slice(0, 80)}…` : t.reason}</span></div>}
        </div>)}
      </div>
      <div className={css["pager"]}>
        <button type="button" disabled={cur === 0} onClick={() => setPage(cur - 1)}>Previous</button>
        <span>Page {cur + 1} of {pages} ({rows.length} trades)</span>
        <button type="button" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>Next</button>
      </div>
    </>}
  </div></Section>;
}
