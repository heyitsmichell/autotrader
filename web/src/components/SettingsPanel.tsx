"use client";

import { dateTime, money } from "@/lib/format";
import type { Learning, Status } from "@/lib/types";
import css from "./SettingsPanel.module.css";

export default function SettingsPanel({
  status, learning, strategy, lessons, busy, onStrategy, onLessons, onAction, api,
  strategyLoaded = true, lessonsLoaded = true,
}: {
  status: Status | null;
  learning: Learning | null;
  strategy: string;
  lessons: string;
  busy: boolean;
  onStrategy: (value: string) => void;
  onLessons: (value: string) => void;
  onAction: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  api: <T = unknown>(path: string, method?: string, body?: unknown) => Promise<T>;
  /** Save stays disabled until the text was loaded, so an empty editor never overwrites the file. */
  strategyLoaded?: boolean;
  lessonsLoaded?: boolean;
}) {
  const evaluated = learning?.stats?.all?.n ?? 0;
  const minimum = learning?.min_outcomes ?? 10;
  const scoredSignals = learning?.analytics?.signals_scored ?? 0;
  const hitRate = learning?.stats?.all?.hit_rate;
  const buysVsSpy = learning?.stats?.buys_vs_spy;

  return <details className={`panel ${css["settings-panel"]}`}>
    <summary>
      <span><span className="eyebrow">REVIEWER NOTES & LEARNING</span><b>Reviewer notes (does not change trading) and outcome review</b></span>
      <span className="muted">Expand</span>
    </summary>
    <div className={`${css["settings-grid"]}`}>
      <section>
        <div className="panel-head">
          <div>
            <h2>Strategy notes</h2>
            <p className="muted">Reviewer notes (does not change trading). Given to the outcome review as context;
              the trading rules live in the settings and the code.</p>
          </div>
          <button
            className="button button-quiet"
            disabled={busy || !strategyLoaded}
            title={strategyLoaded ? undefined : "Not loaded yet: saving now would overwrite the file"}
            onClick={e => { e.preventDefault(); void onAction("Strategy notes saved", () => api("/api/strategy", "PUT", { text: strategy })); }}
          >Save strategy</button>
        </div>
        <textarea value={strategy} onChange={e => onStrategy(e.target.value)} rows={12} aria-label="Strategy notes"
          disabled={!strategyLoaded} placeholder={strategyLoaded ? "" : "Loading…"}/>
      </section>
      <section>
        <div className="panel-head">
          <div><h2>Learning loop</h2><p className="muted">Outcomes scored against forward price moves and SPY.</p></div>
          <div className="inline-actions">
            <button className="button button-quiet" disabled={busy} onClick={() => void onAction("Outcome scoring started", () => api("/api/learning/evaluate", "POST"))}>Score outcomes</button>
            <button className="button button-quiet" disabled={busy} onClick={() => void onAction("Model review started", () => api("/api/learning/review", "POST"))}>Run review</button>
          </div>
        </div>

        <div className="learning-health">
          <div className="learning-stat"><span>Signals scored</span><b>{scoredSignals}</b></div>
          <div className="learning-stat"><span>Proposals evaluated</span><b>{evaluated}</b></div>
          <div className="learning-stat"><span>Review threshold</span><b>{minimum} outcomes</b></div>
          <div className="learning-stat"><span>Last review</span><b>{dateTime(learning?.last_review?.created_at)}</b></div>
          <div className="learning-stat"><span>Hit rate</span><b>{hitRate == null ? "Not enough data" : `${hitRate}%`}</b></div>
          <div className="learning-stat"><span>BUY vs SPY</span><b>{buysVsSpy?.avg_buy_5d == null ? "Not enough data" : `${buysVsSpy.avg_buy_5d}% vs ${buysVsSpy.avg_spy_5d}%`}</b></div>
        </div>

        {evaluated < minimum && <div className="inline-warning" role="note">
          Not enough scored outcomes for a review yet: {evaluated} of {minimum} required.
          {scoredSignals > 0 ? ` ${scoredSignals} signal(s) are scored separately so blocked setups can be compared with acted ones.` : ""}
        </div>}

        <h3 className={`${css["subhead"]}`}>Lesson notes</h3>
        <p className="muted">Reviewer notes (does not change trading). Parameter changes go through the lessons table
          (Learning tab), where each one needs approval and activation.</p>
        <textarea value={lessons} onChange={e => onLessons(e.target.value)} rows={8}
          placeholder={lessonsLoaded ? "No lesson notes saved yet." : "Loading…"} aria-label="Lesson notes" disabled={!lessonsLoaded}/>
        <div className="actions-end">
          <button className="button button-quiet" disabled={busy || !lessonsLoaded} onClick={() => void onAction("Lesson notes saved", () => api("/api/lessons", "PUT", { text: lessons }))}>Save lessons</button>
        </div>
      </section>
    </div>
    <div className={`${css["settings-foot"]}`}>
      <span>Model: {status?.model || "AI analyst"}</span>
      <span>Provider: {status?.provider || "—"}</span>
      <span>Estimated LLM spend today: {money(status?.cost_today_usd)}</span>
    </div>
  </details>;
}
