"use client";

import { useMemo } from "react";
import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import { minutesAgo, money, num, pct, signed } from "@/lib/format";
import type { MlStatus, Signal, Status } from "@/lib/types";
import css from "./SignalsTable.module.css";

const gateRank = (s: Signal) => (s.consensus === "blocked" ? 1 : s.consensus ? 0 : 2);
const tone = (n: number | null | undefined) => (n == null || n === 0 ? "" : n > 0 ? css.pos : css.neg);

export default function SignalsTable({ signals, status, ml, onSelectSymbol }: {
  signals: Signal[];
  status: Status | null;
  ml: MlStatus | null;
  onSelectSymbol: (symbol: string) => void;
}) {
  const watchlistSrc = status?.watchlist;
  const rows = useMemo(() => {
    const bySymbol = new Map(signals.map(s => [s.symbol, s]));
    const watchlist = watchlistSrc || [];
    const list: Signal[] = watchlist.length ? watchlist.map(symbol => bySymbol.get(symbol) || { symbol }) : signals;
    return [...list].sort((a, b) => gateRank(a) - gateRank(b) || (b.ta_score ?? -Infinity) - (a.ta_score ?? -Infinity));
  }, [signals, watchlistSrc]);

  const age = minutesAgo(status?.signals?.last_refresh);
  const every = status?.signals?.refresh_minutes;
  const meta = <span suppressHydrationWarning>
    {age == null ? "Waiting for first snapshot" : `Updated ${age}m ago${every ? ` · every ${every} min` : ""}`}
  </span>;
  const modelNote = ml?.enabled === false ? "ML filter off" : "";

  return <Section title="Signals" meta={meta} right={modelNote ? <span className={css.muted}>{modelNote}</span> : undefined}>
    {status?.signals?.error && <div className="inline-warning" role="alert">{status.signals.error}</div>}
    {rows.length === 0 ? <Empty>No symbols on the watchlist yet. Signals appear here once the bot has one to watch.</Empty> :
      <div className="table-scroll">
        <table>
          <thead><tr>
            <th>Symbol</th><th className="num">Last</th><th>Gate</th><th>Trend</th>
            <th className="num">TA score</th><th className="num">ML p</th><th className="num">RSI</th><th className="num">From 10-day high</th>
          </tr></thead>
          <tbody>
            {rows.map(s => {
              const failed = (s.consensus_checks || []).find(c => !c.ok);
              const blocked = s.consensus === "blocked" || !!failed;
              const sigAge = minutesAgo(s.created_at);
              const stale = sigAge != null && sigAge > Math.max(15, (every ?? 15) * 3);
              return <tr key={s.symbol} className={css.row} onClick={() => onSelectSymbol(s.symbol)}>
                <td><button type="button" className={css.sym} onClick={e => { e.stopPropagation(); onSelectSymbol(s.symbol); }}>{s.symbol}</button></td>
                <td className="num" title={stale ? `Snapshot is ${sigAge} minutes old` : undefined}>{s.close == null ? "—" : money(s.close)}</td>
                <td>{s.consensus || failed
                  ? <span className={css.gate}><Mark tone={blocked ? "loss" : "gain"}>{blocked ? "Blocked" : "Pass"}</Mark>
                    {blocked && <span className={css.reason} title={failed?.detail || failed?.name}>{failed?.detail || failed?.name || ""}</span>}</span>
                  : <Mark>Waiting</Mark>}</td>
                <td>{s.trend || "—"}</td>
                <td className={`num ${tone(s.ta_score)}`}>{signed(s.ta_score)}</td>
                <td className="num">{s.ml_p == null ? "—" : num(s.ml_p, 2)}</td>
                <td className="num">{s.features?.rsi14 == null ? "—" : num(s.features.rsi14, 0)}</td>
                <td className="num">{s.features?.pct_from_high10 == null ? "—" : pct(s.features.pct_from_high10, { digits: 1 })}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>}
  </Section>;
}
