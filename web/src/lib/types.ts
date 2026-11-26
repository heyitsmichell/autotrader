/** Typed shapes for the FastAPI responses consumed by the dashboard. */

export interface GateCheck {
  name: string;
  ok: boolean;
  detail?: string | null;
  value?: number | null;
  threshold?: number | null;
}

export interface Signal {
  id?: number;
  features?: { rsi14?: number | null; pct_from_high10?: number | null; high10?: number | null; adx14?: number | null; atr_pct?: number | null; [k: string]: unknown } | null;
  symbol: string;
  created_at?: string | null;
  close?: number | null;
  atr?: number | null;
  trend?: string | null;
  strength?: string | null;
  vol?: string | null;
  ta_score?: number | null;
  ta_components?: Array<{ name?: string; note?: string; points?: number }> | null;
  sent_score?: number | null;
  sent_n?: number | null;
  sentiment_source?: string | null;
  ml_p?: number | null;
  ml_model_id?: number | null;
  consensus?: string | null;
  consensus_checks?: GateCheck[] | null;
  sl?: number | null;
  tp?: number | null;
}

export interface Proposal {
  id: number;
  created_at: string;
  symbol: string;
  side: "BUY" | "SELL" | string;
  quantity: number;
  limit_price: number;
  ref_price?: number | null;
  sl?: number | null;
  tp?: number | null;
  confidence?: number | null;
  rationale?: string | null;
  status: string;
  reason?: string | null;
  preview?: string | null;
  client_order_id?: string | null;
  broker_response?: string | null;
  decided_by?: string | null;
  placed_at?: string | null;
  // Option-specific fields
  option_type?: "CALL" | "PUT" | null;
  strike_price?: number | null;
  expiration_date?: string | null;
  position_intent?: "BUY_TO_OPEN" | "BUY_TO_CLOSE" | "SELL_TO_OPEN" | "SELL_TO_CLOSE" | null;
  option_strategy?: string | null;
  legs?: unknown[] | null;
  multiplier?: number | null;
}

export interface ProposalDetail {
  proposal: Proposal;
  signal: Signal | null;
  headlines: Headline[];
  protective: Protective[];
}

export interface Headline {
  news_id?: string;
  title?: string;
  url?: string;
  source?: string;
  published_at?: string;
  webull_sentiment?: string | null;
  llm_label?: string | null;
}

export interface Protective {
  id: number;
  symbol: string;
  kind: string;
  price: number;
  status: string;
}

export interface Balance {
  net_liq?: number | null;
  cash?: number | null;
  buying_power?: number | null;
  day_pnl?: number | null;
  unrealized?: number | null;
  stale?: boolean;
}

export interface Position {
  symbol: string;
  qty: number;
  cost?: number | null;
  market_value?: number | null;
  unrealized?: number | null;
  unrealized_pct?: number | null;
}

export interface OpenOrder {
  client_order_id?: string;
  symbol: string;
  side: string;
  quantity: number;
  limit_price?: number | null;
  status?: string;
}

export interface OptionPositionRow {
  symbol: string; underlying?: string | null; option_type?: string | null; strike?: number | null;
  expiration?: string | null; qty: number; cost_basis?: number | null; mark_price?: number | null;
  unrealized_pnl?: number | null;
}

export interface Portfolio {
  balance: Balance;
  positions: Position[];
  open_orders: OpenOrder[];
  protective?: Protective[];
  protection?: Record<string, ProtectionInfo>;
  /** Held option contracts (parsed from the same positions text; cost/mark are per-share premium). */
  option_positions?: OptionPositionRow[];
  session?: "CORE" | "ALL" | "NIGHT" | null;
  errors?: Record<string, string>;
}

export interface Run {
  id: number;
  trigger: string;
  started_at: string;
  finished_at?: string | null;
  status: string;
  summary?: string | null;
  error?: string | null;
  tool_calls?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

export interface EventRow {
  id: number;
  ts: string;
  level: string;
  message: string;
}

export interface ResearchPacket {
  id: number;
  created_at: string;
  asset_class: string;
  symbol: string;
  packet_type: string;
  score?: number | null;
  evidence?: Array<{ source?: string; excerpt?: string }>;
  caveats?: string[];
  approval_only?: number | boolean;
}

export interface Research {
  last_refresh?: string | null;
  refreshing?: boolean;
  errors?: Record<string, string>;
  approval_only?: boolean;
  scan_interval_minutes?: number;
  auto_candidate_trading?: boolean;
  auto_candidate_execution?: boolean;
  candidate_cap?: number;
  manual_watchlist?: string[];
  auto_watchlist?: string[];
  auto_eligibility?: Record<string, unknown>;
  packets?: ResearchPacket[];
}

export interface ModelInfo {
  id: number;
  created_at: string;
  status: string;
  path?: string | null;
  n_samples?: number | null;
  n_symbols?: number | null;
  metrics?: Record<string, unknown> | null;
  note?: string | null;
}

export interface MlStatus {
  available?: boolean;
  error?: string | null;
  training?: boolean;
  active_id?: number | null;
  enabled?: boolean;
  min_p?: number;
  champion?: ModelInfo | null;
  challenger?: ModelInfo | null;
  models?: ModelInfo[];
  retrain_days?: number;
}

export interface TokenStatus {
  status: string;
  expires_at?: string | null;
  days_left?: number | null;
  estimated?: boolean;
}

export interface SpyBenchmarkStatus {
  enabled?: boolean;
  min_outperformance_pct?: number;
  window_days?: number;
  min_trades?: number;
  action?: string;
  trades_count?: number;
  avg_buy_pct?: number | null;
  avg_spy_pct?: number | null;
  avg_alpha_pct?: number | null;
  alpha_hit_rate?: number | null;
  passing?: boolean;
  error?: string;
}

export interface SupervisorStatus {
  last_tick?: string | null;
  auto_start?: boolean;
  error?: string | null;
  token?: TokenStatus | null;
  drawdown_pct?: number | null;
  max_drawdown_pct?: number | null;
  run_failures?: number | null;
  order_failures?: number | null;
  max_errors?: number | null;
  kill_action?: string | null;
  kill_at?: string | null;
  unresolved_orders?: number | null;
  spy_benchmark?: SpyBenchmarkStatus;
}

export interface SessionInfo {
  trading_day?: boolean;
  early_close?: boolean;
  close?: string;
  next_open?: string;
  holiday?: string | null;
}

export interface Status {
  broker_label?: string;
  broker_mode?: string;
  is_live?: boolean;
  connected?: boolean;
  broker_error?: string | null;
  trading_mode?: string;
  allow_live_auto?: boolean;
  kill_switch?: boolean;
  kill_reason?: string | null;
  scheduler_on?: boolean;
  next_run_at?: string | null;
  running?: boolean;
  market_open?: boolean;
  now_et?: string;
  server_time?: string;
  provider?: string;
  model?: string;
  watchlist?: string[];
  cycle_minutes?: number;
  limits?: {
    max_order_usd?: number;
    max_position_usd?: number;
    max_order_qty?: number;
    max_trades_per_day?: number;
    max_daily_loss_usd?: number;
    price_band_pct?: number;
    proposal_ttl_minutes?: number;
    market_hours_only?: boolean;
    max_risk_per_trade_usd?: number;
  };
  consensus?: {
    ta_min?: number;
    sent_min?: number;
    min_news?: number;
    ml_min_p?: number;
    ml_enabled?: boolean;
    sl_atr_mult?: number;
    tp_atr_mult?: number;
    bracket_orders?: boolean;
  };
  sentiment?: { source?: string; llm?: string; scoring?: boolean; last_scoring?: string | null };
  signals?: {
    refreshing?: boolean;
    error?: string | null;
    last_refresh?: string | null;
    refresh_minutes?: number;
  };
  research?: Research;
  ml?: { available?: boolean; training?: boolean; active_id?: number | null; enabled?: boolean };
  trades_today?: number;
  day_pnl?: number | null;
  usage_today?: Array<{ cost_usd?: number | null }>;
  cost_today_usd?: number;
  realtime?: { mode?: string; error?: string | null; poll_seconds?: number };
  web_built?: boolean;
  session?: SessionInfo;
  flattening?: boolean;
  supervisor?: SupervisorStatus;
  alerts?: { channels?: string[]; last_error?: string | null };
  duplicate_instance?: { timestamp: number; message: string } | null;
  /** Seconds since the newest market data the engine has seen (null: none yet). */
  data_age_seconds?: number | null;
  /** Set when the broker transport differs from what the settings asked for. */
  broker_mode_warning?: string | null;
  /** True while the scheduler is off (no automatic analysis). */
  paused?: boolean;
  daily_loss_guard?: { enabled?: boolean; tripped?: boolean; pnl?: number | null; limit?: number | null };
  token_required?: boolean;
  settings?: {
    options_enabled?: boolean;
    options_allowed_strategies?: string;
    options_allowed_underlyings?: string[];
    options_max_contracts_per_order?: number;
    options_max_notional_usd?: number;
    options_min_dte?: number;
    options_max_dte?: number;
    options_require_approval?: boolean;
    options_iv_rank_max?: number;
    options_iv_rank_min?: number;
    options_delta_min?: number;
    options_delta_max?: number;
    options_min_volume?: number;
    options_min_open_interest?: number;
    options_max_bid_ask_pct?: number;
    options_auto_entry?: boolean;
    options_entry_target_delta?: number;
    options_entry_target_dte?: number;
    options_entry_max_premium_usd?: number;
    options_entry_max_open?: number;
    options_exit_take_profit_pct?: number;
    options_exit_stop_pct?: number;
    options_exit_underlying_stop?: boolean;
    eligibility_earnings_blackout_days?: number;
    extended_hours_soft_stop?: boolean;
    exit_max_hold_days?: number;
    overnight_trading?: boolean;
    overnight_symbols?: string[];
  };
}

export interface PerformanceSignificance {
  state: "ok" | "insufficient_data" | "no_variance";
  n?: number;
  min_days?: number;
  confidence?: number | null;
  lags?: number | null;
  message?: string | null;
  p_value?: number | null;
  t_stat?: number | null;
  significant?: boolean | null;
  mean_daily_pct?: number | null;
  mean_annualized_pct?: number | null;
  ci_low_annualized_pct?: number | null;
  ci_high_annualized_pct?: number | null;
  days_needed?: number | null;
}

export interface PerformanceBlock {
  total_return_pct?: number | null;
  annualized_return_pct?: number | null;
  volatility_pct?: number | null;
  max_drawdown_pct?: number | null;
  sharpe?: number | null;
  beta?: number | null;
  alpha_pct?: number | null;
}

export interface Performance {
  state: "ok" | "no_data";
  source?: string;
  method?: string;
  benchmark_symbol?: string;
  min_days?: number;
  risk_free_rate_pct?: number;
  as_of?: string | null;
  days?: number;
  start?: string;
  end?: string;
  enough_data?: boolean;
  message?: string;
  first_net_liq?: number | null;
  last_net_liq?: number | null;
  flows_total?: number;
  flows_detected?: boolean;
  account?: PerformanceBlock;
  benchmark?: PerformanceBlock;
  excess_return_pct?: number | null;
  tracking_error_pct?: number | null;
  information_ratio?: number | null;
  hit_rate?: number | null;
  turnover?: number | null;
  significance?: PerformanceSignificance;
  series?: Array<{ day: string; account: number; benchmark: number }>;
}

export interface LearningStats {
  n?: number;
  hit_rate?: number | null;
  avg_5d?: number | null;
}

export interface Learning {
  stats?: {
    all?: LearningStats;
    groups?: Record<string, LearningStats>;
    buys_vs_spy?: { n?: number; avg_buy_5d?: number | null; avg_spy_5d?: number | null };
  };
  analytics?: { signals_scored?: number; consensus?: { spy_avg_5d?: number | null } };
  last_review?: { created_at?: string } | null;
  reviewing?: boolean;
  min_outcomes?: number;
  review_every_days?: number;
  lessons?: string;
}

export interface AutopsyCause {
  cause: string;
  evidence: string;
  strength: number;
  category: "process" | "good_process" | "outcome";
}

export interface Autopsy {
  id: number;
  trade_id: number;
  proposal_id: number;
  symbol: string;
  side: string;
  outcome_class: string;
  process_class: string;
  causes: AutopsyCause[];
  pnl_pct: number;
  mae_pct: number | null;
  mfe_pct: number | null;
  hold_hours: number;
  llm_narration: string | null;
  created_at: string;
}

export interface TradeLedgerEntry {
  id: number;
  proposal_id: number;
  symbol: string;
  side: "BUY" | "SELL";
  status: "open" | "closed" | "cancelled";
  entry_fill_price: number | null;
  entry_fill_time: string | null;
  entry_slippage: number | null;
  exit_fill_price: number | null;
  exit_fill_time: string | null;
  exit_slippage: number | null;
  exit_reason: string | null;
  quantity_requested: number | null;
  quantity_filled: number | null;
  limit_price: number | null;
  stop_price: number | null;
  target_price: number | null;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  mae: number | null;
  mfe: number | null;
  hold_seconds: number | null;
  entry_ta_score: number | null;
  entry_sent_score: number | null;
  entry_ml_p: number | null;
  entry_trend: string | null;
  entry_atr: number | null;
  created_at: string;
}

export interface Lesson {
  id: number;
  version: number;
  param_name: string;
  param_value: number;
  param_scope: string;
  sample_size: number;
  min_sample_size: number;
  train_pct: number;
  val_pct: number;
  test_pct: number;
  baseline_metric: number | null;
  proposed_metric: number | null;
  metric_name: string | null;
  p_value: number | null;
  supporting_autopsies: string | null;  // JSON array
  evidence_summary: string | null;
  status: "proposed" | "approved" | "active" | "retired" | "rejected";
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  activated_at: string | null;
  retired_at: string | null;
  retired_reason: string | null;
  retired_by: string | null;
  previous_version_id: number | null;
  created_at: string;
  updated_at: string | null;
  /** M9: what the engine's auto-apply step did with this lesson. */
  apply_state?: "pending" | "applied" | "skipped" | string | null;
  apply_reason?: string | null;
}

export interface ProtectionInfo {
  state: "broker" | "soft" | "inactive" | "closed" | "missing" | "soft_fired";
  stop: number | null;
  detail: string;
}

export interface ExperimentRow {
  name: string;
  kind: string;
  cagr: number | null;
  sharpe: number | null;
  maxdd: number | null;
  dsr: number | null;
  universe: string;
  survivorship_biased: boolean;
}

export interface ExperimentReport {
  path: string;
  title: string;
  modified: string;
  bytes: number;
}

export interface Experiments {
  registry: { n_trials: number; by_lab: Record<string, number> };
  leaderboard: ExperimentRow[];
  benchmarks: ExperimentRow[];
  intraday_entry: Record<string, unknown> | null;
  reports: ExperimentReport[];
}

export interface OptionSpreadBucket {
  minutes_from_open: number;
  n: number;
  median_spread_pct: number;
  p75_spread_pct: number;
  pct_pass: number;
}

export interface OptionSpreadsResponse {
  rows: number;
  days: number;
  max_bid_ask_pct: number;
  overall: OptionSpreadBucket[];
  by_underlying: Record<string, OptionSpreadBucket[]>;
}

export interface OptionsModelRecent {
  id: number;
  symbol: string;
  created_at: string | null;
  status: string | null;
  ml_p: number | null;
  explore: boolean;
  arm: [number, number] | null;
  delta: number | null;
  dte: number | null;
  ret: number | null;
  exit_reason: string | null;
}

export interface OptionsModelResponse {
  available: boolean;
  enabled: boolean;
  version: string | null;
  trained_at: string | null;
  n_backtest: number | null;
  n_live: number | null;
  val_auc: number | null;
  baseline_win_rate: number | null;
  explore_pct: number;
  min_p: number;
  recent: OptionsModelRecent[];
}

/* ---- Strategies tab (sleeves) ---- */
export interface Sleeve {
  id: string; name: string; color_slot: number; description: string;
  status: "active" | "paused" | "disabled" | "waiting"; status_reason: string | null;
  start_capital: number | null; start_date: string | null; equity: number | null; cash: number | null;
  invested: number | null; invested_pct: number | null; day_pnl: number | null; day_pnl_pct: number | null;
  total_return_pct: number | null; peak_equity: number | null;
  drawdown_pct: number | null; pause_dd_pct: number; positions_n: number | null;
  last_rebalance: string | null; next_rebalance: string | null; rebalance_rule: string;
  expected: { cagr: number | null; sharpe: number | null; max_dd: number | null; window: string } | null;
  live: { days: number; sharpe: number | null; vol: number | null } | null;
}
export interface SleevesPayload {
  enabled: boolean; as_of?: string; benchmark?: string;
  account?: { net_liq: number | null; day_pnl: number | null; target_per_sleeve: number; sleeves_equity: number | null };
  last_error?: string | null;
  sleeves: Sleeve[];
}
export interface SleeveEquityPoint {
  day: string; swing: number | null; momentum: number | null; putwrite: number | null;
  vaa: number | null; total: number | null; spy: number | null; baseline?: boolean;
}
export interface SleeveHolding {
  symbol: string; kind: string; qty: number; avg_cost: number | null; last: number | null;
  market_value: number | null; weight_pct: number | null; target_weight_pct: number | null;
  unrealized_pnl: number | null; unrealized_pct: number | null; note: string | null;
}
export interface SleeveTarget { symbol: string; weight_pct: number; reason: string | null }
export interface SleeveOrder {
  ts: string; symbol: string; side: string; qty: number; limit_price: number | null; status: string;
  filled_qty: number | null; avg_fill_price: number | null; reason: string | null;
}
export interface SleeveDetail extends Sleeve {
  holdings: SleeveHolding[]; targets: SleeveTarget[]; orders: SleeveOrder[];
}

export interface SleeveEvent {
  id: number | string; ts: string; day: string; sleeve: string | null; kind: string; text: string;
}
export interface SleeveBandPoint {
  day: string; live: number | null; p5: number; p25: number; p50: number; p75: number; p95: number;
}
export interface SleeveBand {
  sleeve: string; start: string | null; window: string;
  status: "inside" | "below" | "above" | "insufficient"; below_days: number;
  points: SleeveBandPoint[];
}
export interface SleeveTearSheet {
  sleeve: string; source: "live" | "backtest"; days: number; start: string | null; end: string | null;
  metrics: {
    cagr: number | null; vol: number | null; sharpe: number | null; sortino: number | null;
    max_dd: number | null; calmar: number | null; best_day: number | null; worst_day: number | null;
    pct_positive_days: number | null;
  } | null;
  drawdown: { day: string; dd: number }[];
  rolling_sharpe: { day: string; value: number | null }[];
  monthly: { year: number; month: number; ret: number }[];
  yearly: { year: number; ret: number }[];
  worst_drawdowns: { start: string; trough: string; end: string | null; depth: number; days: number }[];
}

/* ---- Shadow strategies (virtual money) ---- */
export interface Shadow {
  id: string; name: string; description: string; rule: string;
  start_date: string | null; start_capital: number | null;
  equity: number | null; cash: number | null;
  total_return_pct: number | null; drawdown_pct: number | null; positions_n: number | null;
  last_rebalance: string | null; next_rebalance: string | null;
  live: { days: number; sharpe: number | null } | null;
  expected: { cagr: number | null; sharpe: number | null; max_dd: number | null; window: string } | null;
}
export interface ShadowsPayload { enabled: boolean; shadows: Shadow[] }
export type ShadowEquityPoint = { day: string } & Record<string, number | string | null | undefined>;
export interface ShadowHolding {
  symbol: string; qty: number; avg_cost: number | null; last: number | null;
  market_value: number | null; weight_pct: number | null;
}
export interface ShadowTrade {
  ts: string; symbol: string; side: string; qty: number; price: number | null; reason: string | null;
}
export interface ShadowDetail extends Shadow { holdings: ShadowHolding[]; trades: ShadowTrade[] }

export interface ResultTrade {
  ts: string; strategy: string; live: boolean; symbol: string; kind: string;
  side: string; qty: number | null; price: number | null; notional: number | null; status: string;
  pnl: number | null; reason: string | null; source: string;
}
export interface ResultTradesPayload { trades: ResultTrade[]; counts: Record<string, number> }
export type PnlRow = { day: string } & Record<string, number | string | null>;
export type PnlPeriods = Record<string, Record<string, number | null>>;
export interface PnlPayload {
  days: PnlRow[]; cumulative: PnlRow[]; periods: PnlPeriods;
  shadows?: { days: PnlRow[]; cumulative?: PnlRow[]; periods?: PnlPeriods; names?: Record<string, string> };
}
export type ExitBucket = "stop" | "time" | "manual" | "rebalance" | "expiry" | "other";
export interface RoundTrip {
  strategy: string; live: boolean; symbol: string; kind: string; direction: "long" | "short"; qty: number | null;
  entry_ts: string | null; exit_ts: string | null; entry_price: number | null; exit_price: number | null;
  pnl: number | null; ret_pct: number | null; r: number | null; hold_days: number | null;
  exit_reason: string | null; bucket: ExitBucket;
}
export interface TripPick { symbol: string; pnl: number; ret_pct: number | null }
export interface TripStats {
  n: number; wins: number; losses: number; win_rate: number | null; avg_win: number | null; avg_loss: number | null;
  expectancy: number | null; profit_factor: number | null; total: number | null; avg_ret_pct: number | null;
  median_ret_pct: number | null; avg_hold_days: number | null; avg_r: number | null; n_r: number;
  best: TripPick | null; worst: TripPick | null; exits: Partial<Record<ExitBucket, { n: number; pnl: number }>>;
}
export interface RoundTripsPayload { trips: RoundTrip[]; stats: Record<string, TripStats> }

// ---- Research tab (docs/RESEARCH_TAB.md) ----
export interface ResearchBase { as_of?: string; error?: string }
export interface MomentumRow { rank: number; symbol: string; score: number; held: boolean; held_qty?: number; in_target: boolean; gap_to_cutoff: number }
export interface MomentumPayload extends ResearchBase { n_target?: number; rows?: MomentumRow[]; next_rebalance?: string; universe_n?: number }
export interface VaaOffensive { symbol: string; score: number; rank?: number; bad: boolean }
export interface VaaPayload extends ResearchBase {
  B?: number; bad?: number; cash_fraction?: number; offensive?: VaaOffensive[]; defensive?: { symbol: string; score: number }[];
  best_defensive?: string; allocation_if_today?: Record<string, number>; held?: Record<string, number>; next_rebalance?: string;
}
export interface PutwritePut {
  symbol: string; strike: number; expiration: string; dte: number; contracts: number; premium_received: number;
  mark: number | null; unrealized_pnl: number | null; delta: number | null; iv: number | null; prob_itm: number | null;
  distance_pct: number | null; collateral: number; next_action?: string;
}
export interface PutwritePayload extends ResearchBase { underlying?: string; spot?: number | null; puts?: PutwritePut[]; cash?: number; rule?: string }
export interface OverlapHolder { strategy: string; live: boolean; qty: number; value: number }
export interface OverlapRow { symbol: string; total_value: number; pct_of_net: number; holders: OverlapHolder[] }
export interface OverlapPayload extends ResearchBase { net_liq?: number; rows?: OverlapRow[]; shadow_overlap?: OverlapRow[] }
export interface RegimePayload extends ResearchBase {
  spy?: { last: number; sma50: number; sma200: number; above_200: boolean; ret_1m: number; ret_3m: number };
  vix?: { last: number; pct_rank_1y: number };
  breadth?: { pct_above_200: number; n: number };
  vaa_bad?: number;
  swing_gate?: { watched: number; eligible: number; pass: number; top_blockers?: { name: string; n: number }[] };
}
/** Backtests tab (docs/BACKTESTS_TAB.md). Every field may be missing. */
export interface BtEvidence { path: string; title?: string; exists?: boolean }
export interface BtCard {
  id: string; name?: string; stage?: string; kind?: string; since?: string; verdict?: string;
  backtest?: { cagr?: number | null; vol?: number | null; sharpe?: number | null; max_dd?: number | null; window?: string | null; source?: string | null; biased?: boolean } | null;
  live?: { days?: number | null; return_pct?: number | null; sharpe?: number | null; max_dd?: number | null } | null;
  trials?: { lab?: string; n?: number } | null;
  evidence?: BtEvidence[]; compare_to?: string | null; note?: string | null;
}
export interface BtPortfolio {
  window?: string | null;
  schemes?: { name: string; cagr?: number | null; vol?: number | null; sharpe?: number | null; max_dd?: number | null }[];
  curve?: { day: string; equal?: number | null; invvol?: number | null }[];
  correlation?: { ids?: string[]; matrix?: (number | null)[][] } | null;
  live?: { days?: number | null; return_pct?: number | null; sharpe?: number | null } | null;
}
export interface BtPipelineEntry {
  id: string; name?: string; kind?: string; stage?: string; since?: string; verdict?: string;
  evidence?: BtEvidence[]; compare_to?: string | null;
  live_stats?: { days?: number | null; return_pct?: number | null; sharpe?: number | null } | null;
  compare_stats?: { days?: number | null; return_pct?: number | null; sharpe?: number | null } | null;
}
export interface BtPipeline { stages?: { live?: BtPipelineEntry[]; shadow?: BtPipelineEntry[]; rejected?: BtPipelineEntry[] } }
export interface BtMl {
  models?: { id: string | number; created_at?: string; status?: string; kind?: string; auc?: number | null; metrics?: Record<string, unknown> }[];
  options_model?: Record<string, unknown> | null;
  experiments?: BtPipelineEntry[]; summary?: string;
}

/* ---- Shadow -> live promotion check ---- */
export interface PromotionCriterion {
  id: string; label: string; value: number | null; target_value: number | null; pass: boolean; gate: boolean;
}
export interface PromotionCheck {
  shadow: string; target: string; days: number; min_days: number; criteria: PromotionCriterion[];
  passed: boolean; verdict: string; first_day: string | null; last_day: string | null;
}
