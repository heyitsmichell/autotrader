"use client";

import { useEffect, useMemo, useState } from "react";
import type { Api } from "@/lib/api";
import { num, pct } from "@/lib/format";
import Empty from "@/components/ui/Empty";
import Mark from "@/components/ui/Mark";
import Section from "@/components/ui/Section";
import Segmented from "@/components/ui/Segmented";
import css from "./Backtests.module.css";

interface Row {
  name: string; kind: string; cagr: number | null; sharpe: number | null; maxdd: number | null;
  dsr: number | null; universe: string; survivorship_biased: boolean;
}
interface Variant {
  fill_rate?: number | null; n?: number | null; entry_effect?: number | null;
  net_vs_random?: number | null; net_ci90?: [number, number] | null;
}
interface Experiments {
  registry?: { n_trials: number; by_lab?: Record<string, number> };
  leaderboard?: Row[]; leaderboard_clean?: Row[]; benchmarks?: Row[];
  intraday_entry?: Record<string, Variant> | null;
  reports?: { path: string; title: string; modified: string; bytes: number }[];
}

const P = (v: number | null | undefined, signed = false) => (v == null ? "—" : pct(v * 100, { digits: 1, sign: signed }));
const tone = (v: number | null) => (v == null || v === 0 ? "" : v > 0 ? css["gain"] : css["loss"]);
const BENCH: Record<string, string> = { SPY_buyhold: "SPY buy-and-hold", "60_40_SPY_IEF": "60/40 SPY/IEF", EW_sectors: "Equal-weight sectors" };
const day = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

function ReportText({ text }: { text: string }) {
  return <div className={css["text"]}>{text.split("\n").map((l, i) => {
    const m = /^#{1,6}\s+(.*)$/.exec(l);
    return m ? <b key={i}>{m[1]}</b> : <span key={i}>{l}{"\n"}</span>;
  })}</div>;
}

export default function Backtests({ api }: { api: Api }) {
  const [data, setData] = useState<Experiments | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<{ path: string; text: string | null } | null>(null);
  const [scope, setScope] = useState<"all" | "clean">("all");

  useEffect(() => {
    let live = true;
    api<Experiments>("/api/experiments").then(d => { if (live) setData(d); })
      .catch(e => { if (live) setErr(e instanceof Error ? e.message : "Unable to load experiments"); });
    return () => { live = false; };
  }, [api]);

  const load = (path: string) => {
    if (open?.path === path) { setOpen(null); return; }
    setOpen({ path, text: null });
    api<{ text: string }>(`/api/experiments/report?path=${encodeURIComponent(path)}`)
      .then(r => setOpen(o => (o?.path === path ? { path, text: r.text } : o)))
      .catch(() => setOpen(o => (o?.path === path ? { path, text: "Unable to load this report." } : o)));
  };

  const spy = data?.benchmarks?.find(b => b.name === "SPY_buyhold") ?? null;
  const board = useMemo(() => [...((scope === "clean" ? data?.leaderboard_clean : data?.leaderboard) || [])]
    .sort((a, b) => (b.dsr ?? -1) - (a.dsr ?? -1)), [data, scope]);
  const beats = (r: Row) => spy?.sharpe != null && r.sharpe != null && r.sharpe > spy.sharpe;
  const passes = (r: Row) => (r.dsr ?? 0) > 0.95 && beats(r);
  const winners = (data?.leaderboard || []).filter(passes);
  const clean = (data?.leaderboard_clean || []).filter(passes);
  const bestClean = data?.leaderboard_clean?.[0] ?? null;

  const variants = Object.entries(data?.intraday_entry || {});
  const anyEdge = variants.some(([, v]) => (v.net_ci90?.[0] ?? -1) > 0);

  if (err) return <Empty>{err}</Empty>;
  if (!data) return <Empty>Loading experiment results.</Empty>;

  return <div className={css["wrap"]}>
    <div>
      <h1 className={css["verdict"]}>Has any strategy beaten buy-and-hold SPY, after adjusting for how many we tried?</h1>
      <p className={css["sub"]}>{spy && board.length
        ? `${data.registry?.n_trials ?? 0} strategies logged. Among the top ${data.leaderboard?.length ?? 0} by deflated Sharpe, ${winners.length} beat SPY's Sharpe${winners.length && winners.every(r => r.survivorship_biased) ? ", all on universes picked with hindsight" : ""}. `
          + (clean.length ? `${clean.length} survivorship-free strategies also pass.`
            : `On survivorship-free universes none pass${bestClean ? ` (best: Sharpe ${num(bestClean.sharpe)}, DSR ${num(bestClean.dsr)} vs SPY ${num(spy.sharpe)})` : ""}.`)
        : "No leaderboard yet. Experiment results appear here after a research run."}</p>
    </div>

    <Section title="Benchmarks" meta="Buy-and-hold references">
      {data.benchmarks?.length ? <div className="table-scroll"><table>
        <thead><tr><th scope="col">Benchmark</th><th scope="col" className="num">CAGR</th><th scope="col" className="num">Sharpe</th><th scope="col" className="num">Max drawdown</th></tr></thead>
        <tbody>{data.benchmarks.map(b => <tr key={b.name}>
          <th scope="row">{BENCH[b.name] || b.name}</th>
          <td className={`num ${tone(b.cagr)}`}>{P(b.cagr, true)}</td><td className="num">{num(b.sharpe)}</td><td className="num">{P(b.maxdd)}</td>
        </tr>)}</tbody></table></div> : <Empty>No benchmark rows yet.</Empty>}
    </Section>

    <div className={css["cols"]}>
      <div className={css["lb"]}>
        <Section title="Leaderboard" meta="Top by deflated Sharpe (DSR)"
          right={<Segmented label="Universe" value={scope} onChange={setScope}
            options={[{ value: "all", label: "All" }, { value: "clean", label: "Survivorship-free" }]}/>}>
          {board.length ? <div className="table-scroll"><table>
            <thead><tr><th scope="col" className="num">#</th><th scope="col">Name</th><th scope="col">Universe</th>
              <th scope="col" className="num">CAGR</th><th scope="col" className="num">Sharpe</th><th scope="col" className="num">Max DD</th>
              <th scope="col" className="num">DSR</th><th scope="col">Survivorship</th><th scope="col">vs SPY</th></tr></thead>
            <tbody>{board.map((r, i) => <tr key={r.name}>
              <td className="num">{i + 1}</td>
              <td className="mono"><span className={css["name"]} title={r.name}>{r.name}</span></td>
              <td>{r.universe || "—"}</td>
              <td className={`num ${tone(r.cagr)}`}>{P(r.cagr, true)}</td>
              <td className="num">{num(r.sharpe)}</td><td className="num">{P(r.maxdd)}</td>
              <td className="num">{num(r.dsr)}</td>
              <td>{r.survivorship_biased ? <Mark tone="warn" title="Universe uses today's constituents">biased</Mark> : <Mark>clean</Mark>}</td>
              <td>{beats(r) ? <Mark>beats SPY</Mark> : <span className="muted">—</span>}</td>
            </tr>)}</tbody></table></div> : <Empty>No scored strategies yet. The leaderboard fills after a rotation sweep finishes.</Empty>}
        </Section>
      </div>

      <div className={css["side"]}>
        <Section title="Intraday entry timing (5-min)">
          {variants.length ? <>
            <p>{anyEdge ? "At least one 5-minute entry rule beat random skipping." : "No 5-minute entry rule beat buying at the open."}</p>
            <div className="table-scroll"><table>
              <thead><tr><th scope="col">Variant</th><th scope="col" className="num">Fill</th><th scope="col" className="num">Entry effect</th>
                <th scope="col" className="num" title="Return per signal minus skipping the same share of signals at random; 90% CI below">Net vs random</th></tr></thead>
              <tbody>{variants.map(([k, v]) => <tr key={k}>
                <th scope="row" className="mono">{k}</th>
                <td className="num">{P(v.fill_rate)}</td><td className="num">{P(v.entry_effect, true)}</td>
                <td className="num">{P(v.net_vs_random, true)}
                  {v.net_ci90 && <span className={css["ci"]}>[{P(v.net_ci90[0], true)}, {P(v.net_ci90[1], true)}]</span>}</td>
              </tr>)}</tbody></table></div>
          </> : <Empty>No intraday entry study yet.</Empty>}
        </Section>

        <Section title="Reports" meta={data.reports?.length ? String(data.reports.length) : undefined}>
          {data.reports?.length ? <>
            <ul className={css["reports"]}>{data.reports.map(r => <li key={r.path}>
              <button type="button" aria-expanded={open?.path === r.path} onClick={() => load(r.path)}>
                <span>{r.title}</span><span className={css["date"]}>{day(r.modified)}</span>
              </button></li>)}</ul>
            {open && (open.text == null ? <Empty>Loading report.</Empty> : <ReportText text={open.text}/>)}
          </> : <Empty>No reports written yet.</Empty>}
        </Section>
      </div>
    </div>
  </div>;
}
