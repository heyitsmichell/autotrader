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
  outperformance_pct?: number;
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
  mae_pct: number;
  mfe_pct: number;
  hold_hours: number;
  llm_narration: string | null;
  created_at: string;
}

export interface TradeLedgerEntry {
  id: number;
  proposal_id: number;
  symbol: string;
  side: "BUY" | "SELL";
  status: "open" | "closed";
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
