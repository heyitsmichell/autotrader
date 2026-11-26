"""The flat, frozen Settings dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = field(repr=False)
    llm_model: str
    groq_fallback_model: str
    nvidia_nim_api_key: str = field(repr=False)
    nvidia_nim_model: str

    # Broker
    broker_mode: str            # "mock" (simulated, no credentials) | "webull"
    webull_environment: str     # "uat" (Webull sandbox) | "prod" (real money)
    webull_region: str
    webull_account_id: str | None

    # Trading behaviour
    trading_mode: str           # "approve" (you click to place) | "auto"
    allow_live_auto: bool       # must be true to use auto mode against prod
    watchlist: list[str]
    cycle_minutes: int
    auto_start_scheduler: bool
    market_hours_only: bool
    overnight_trading: bool       # run/send in Webull's NIGHT session (20:00-04:00 ET)
    overnight_symbols: list[str]  # symbols that may trade in the overnight session

    # Risk limits (enforced in code, not by the AI)
    max_order_usd: float
    max_position_usd: float
    max_order_qty: int
    max_trades_per_day: int
    max_daily_loss_usd: float
    price_band_pct: float
    proposal_ttl_minutes: int

    max_agent_iterations: int
    research_scan_minutes: int  # 0 = manual only; otherwise refresh on this interval
    auto_trade_research_candidates: bool

    # Real-time chart
    realtime_source: str        # "auto" | "stream" | "poll"
    poll_seconds: float

    # Learning loop
    review_every_days: int      # 0 = only when you click "Run review"
    min_outcomes_for_review: int

    # Consensus gate (a BUY needs TA, sentiment and the ML filter to agree)
    consensus_ta_min: float     # TA score needed for a BUY (-1..+1)
    consensus_sent_min: float   # sentiment may not be below this (-1..+1)
    consensus_min_news: int     # fewer headlines than this = sentiment treated as neutral
    ml_enabled: bool            # use the trained model as a filter when one is active
    ml_min_p: float             # model probability of hitting take-profit before stop-loss needed for a BUY
    ml_history_bars: int        # daily bars per symbol used for training
    ml_min_bars: int            # minimum daily bars required for a symbol to be used in training
    ml_retrain_days: int        # 0 = only when you click "Train model"

    # Stop loss / take profit (computed in code from ATR, placed at the broker)
    sl_atr_mult: float
    tp_atr_mult: float
    min_sl_pct: float
    max_sl_pct: float
    max_risk_per_trade_usd: float
    bracket_orders: bool        # BUYs go out as OTOCO brackets (entry + TP + SL)

    # Sentiment
    sentiment_source: str       # "webull" | "llm" | "blend" (what the gate uses)
    sentiment_llm: bool         # also score headlines with Groq
    sentiment_rss_fallback: bool  # use a public RSS feed when Webull news fails
    sentiment_rss_url: str        # feed template; {symbol} is replaced
    news_lookback_hours: int
    news_refresh_minutes: int   # how often to fetch fresh headlines (0 = every signal refresh)

    # Background refresh
    signal_refresh_minutes: int  # free: TA + Webull sentiment + model, market hours
    snapshot_seconds: int        # equity snapshots for the P&L curve

    # Unattended operation (app/supervisor.py)
    watchdog_seconds: int        # how often the supervisor checks everything
    broker_down_alert_minutes: int
    token_dir: Path              # Webull token folder (WEBULL_TOKEN_DIR)
    token_warn_days: float       # alert when the Webull login expires within this many days
    reconcile_give_up_minutes: int  # an unknown order not found at the broker after this = failed

    # Automatic kill switch
    kill_max_drawdown_pct: float      # 0 = off. Net liquidation this far below its peak
    kill_daily_loss_usd: float        # 0 = off. Day P&L at or below minus this
    kill_max_consecutive_errors: int  # 0 = off. Failed runs, or failed/unknown orders, in a row
    kill_action: str                  # pause | cancel_entries | flatten
    llm_min_cycle_minutes: int        # minimum minutes between LLM analysis runs (rate limits/cost)
    sizing_mode: str                  # code = code sets BUY quantity; llm = model's quantity, clamped
    agent_result_chars: int           # max chars of one tool result sent back to the model
    agent_old_result_chars: int       # older tool results are shrunk to this before each model call
    flatten_slippage_pct: float       # flatten sells at last price minus this %

    # SPY Benchmark (auto-trading performance target)
    spy_benchmark_enabled: bool
    spy_benchmark_min_outperformance_pct: float
    spy_benchmark_window_days: int
    spy_benchmark_min_trades: int
    spy_benchmark_action: str         # warn | pause | kill

    # Daily performance measurement (Phase 0)
    performance_daily_enabled: bool   # store one benchmark row per completed session
    performance_benchmark_symbol: str # benchmark for the performance API (SPY)
    performance_min_days: int         # minimum trading days before a p-value is reported
    performance_history_days: int     # daily bars requested from the broker per refresh
    performance_risk_free_rate: float # annual risk-free rate used by Sharpe/alpha (0 = off)

    # Backtester (Phase 1). Deterministic daily bars, off by default (CLI only).
    backtest_data_dir: Path
    backtest_results_dir: Path
    backtest_initial_cash: float
    backtest_universe: list[str]       # empty = WATCHLIST
    backtest_benchmark_symbol: str
    backtest_min_bars: int
    backtest_commission_per_order: float
    backtest_slippage_bps: float
    backtest_atr_slippage_mult: float
    backtest_impact_bps: float
    backtest_max_impact_bps: float
    backtest_max_gross_exposure_pct: float
    backtest_max_hold_days: int        # 0 = no time exit
    backtest_exit_on_trend_break: bool
    backtest_ml_enabled: bool          # ML filter only when a promoted model exists
    backtest_fetch_count: int
    backtest_fetch_delay_seconds: float

    # Universe (Phase 3). Named list of symbols for research/backtest/ML.
    universe_name: str                 # "sector_etfs" | "sp500_top" | "nasdaq100_top" | "expanded" | "liquid_etfs" | "broad_etfs" | "custom"
    universe_custom: list[str]         # custom universe (when universe_name = "custom")
    universe_top_n: int                # for "sp500_top": take top N by dollar volume
    universe_min_price: float          # minimum share price
    universe_min_avg_dollar_volume: float  # minimum average daily dollar volume
    universe_min_history_days: int     # minimum daily bars for signal quality
    # Tiered scanning (Task 3)
    universe_tier1_size: int           # tier 1: core symbols always scanned
    universe_tier2_size: int           # tier 2: satellite symbols scanned regularly
    universe_tier3_size: int           # tier 3: extended universe scanned on demand
    universe_scan_all_tiers: bool      # scan all tiers (for backtest/research)
    # Diversification limits (Task 3)
    universe_max_per_sector: int       # max positions per sector (0 = no limit)
    universe_max_correlation: float    # max portfolio correlation (0 = no limit)
    universe_sector_map: dict          # symbol -> sector mapping

    # Core-Satellite portfolio (Phase 4)
    core_symbol: str                   # core holding symbol (default SPY)
    core_target_pct: float             # target core allocation % (50-95, default 80)
    satellite_max_pct: float           # max satellite allocation % (default 20)
    core_rebalance_band_pct: float     # rebalance when core drifts beyond this % of equity (default 2%)
    core_min_trade_usd: float          # minimum trade size for core rebalancing (default 100)
    core_rebalance_market_hours_only: bool  # only rebalance during market hours
    satellite_drawdown_guard_pct: float  # reduce satellite risk when drawdown exceeds this % (0 = off)
    satellite_drawdown_action: str     # "shrink" | "pause" | "flatten_satellite"

    # Entry eligibility rules (Step 3)
    eligibility_enabled: bool          # enable eligibility checks
    eligibility_min_price: float       # minimum share price
    eligibility_min_volume: float      # minimum average daily volume (USD)
    eligibility_min_bars: int          # minimum daily bars for signal quality
    eligibility_max_gap_pct: float     # maximum overnight gap percentage
    eligibility_signal_freshness_min: int  # maximum signal age in minutes
    eligibility_earnings_blackout_days: int  # days before/after earnings to block
    eligibility_first_15_min: bool     # block first 15 minutes of session
    eligibility_one_starter_per_symbol: bool  # only one active position per symbol

    # Champion/Challenger ML (Phase 5)
    ml_challenger_min_trades: int          # minimum trades in shadow before promotion decision
    ml_challenger_min_hit_rate_improvement: float  # minimum hit rate improvement to promote
    ml_challenger_min_return_improvement: float    # minimum avg return improvement to promote
    ml_challenger_max_days: int            # max days in shadow before forced decision

    # Alerts (app/alerts.py). Secrets stay here; the dashboard only sees channel names.
    alert_levels: frozenset          # activity-log levels that are always sent
    alert_dedupe_minutes: int
    alert_max_per_hour: int
    alert_daily_summary: bool
    alert_ntfy_server: str
    alert_ntfy_topic: str = field(repr=False)
    alert_telegram_token: str = field(repr=False)
    alert_telegram_chat_id: str
    alert_webhook_url: str = field(repr=False)
    alert_smtp_host: str
    alert_smtp_port: int
    alert_smtp_user: str
    alert_smtp_password: str = field(repr=False)
    alert_email_from: str
    alert_email_to: str

    # Web server
    host: str
    port: int

    # Fractional shares (Phase A)
    fractional_enabled: bool = False           # enable fractional share trading (off by default)
    fractional_min_usd: float = 1.0            # minimum notional for fractional orders ($1 default)
    fractional_decimals: int = 4               # decimal places for fractional quantities (4 default)
    fractional_whitelist: list[str] = field(default_factory=lambda: ["AAPL","MSFT","NVDA","SPY","QQQ","AMZN","GOOGL","META","TSLA"])

    # Options trading (Phase B)
    options_enabled: bool = False              # enable options trading (off by default)
    options_max_contracts_per_order: int = 10  # max contracts per single-leg order
    options_max_notional_usd: float = 5000.0   # max premium per order (multiplier * price * qty)
    options_allowed_strategies: list[str] = field(default_factory=lambda: ["SINGLE", "VERTICAL", "STRADDLE", "STRANGLE"])
    options_require_approval: bool = True      # account must have options approval
    options_min_dte: int = 7                   # minimum days to expiration
    options_max_dte: int = 45                  # maximum days to expiration for entries
    options_allowed_underlyings: list[str] = field(default_factory=lambda: ["AAPL","MSFT","NVDA","SPY","QQQ","AMZN","GOOGL","META","TSLA"])
    # Option consensus gate (Phase B): IV rank, delta, liquidity filters
    options_iv_rank_max: float = 0.85          # max IV rank (0-1) for long options (avoid overpriced IV)
    options_iv_rank_min: float = 0.10          # min IV rank (0-1) for short options (avoid underpriced IV)
    options_delta_min: float = 0.20            # min absolute delta for long directional options
    options_delta_max: float = 0.80            # max absolute delta for long directional options
    options_min_volume: int = 100              # minimum option volume (contracts)
    options_min_open_interest: int = 500       # minimum open interest (contracts)
    options_max_bid_ask_pct: float = 5.0       # max bid-ask spread as % of mid price

    # Fast loop / core-satellite / ML-first (Phase 4+)
    fast_loop_enabled: bool = False   # FAST_LOOP_ENABLED: re-check armed symbols on live prices between cycles
    fast_loop_seconds: float = 20.0   # seconds between fast-loop checks (min 5)
    fast_loop_arm_margin: float = 0.15  # arm symbols whose TA score is within this of CONSENSUS_TA_MIN
    fast_loop_max_armed: int = 10     # most symbols watched at once
    fast_loop_cooldown_minutes: int = 30  # after a trigger, leave that symbol alone this long
    # Broker request limits (Webull answers HTTP 429 "too many requests" when hammered)
    broker_max_concurrency: int = 4        # BROKER_MAX_CONCURRENCY: simultaneous broker calls
    broker_rate_limit_retries: int = 4     # BROKER_RATE_LIMIT_RETRIES: retries after a 429
    broker_call_timeout_seconds: float = 30.0  # BROKER_CALL_TIMEOUT_SECONDS: deadline per broker call
    broker_backoff_seconds: float = 2.0   # BROKER_BACKOFF_SECONDS: first backoff, doubles each retry
    core_enabled: bool = False        # CORE_ENABLED: run the core-satellite rebalancer + satellite cap (off by default)
    # ML-first mode: when a promoted model exists, the model ranks and sizes entries and the
    # TA score is only a floor. With no promoted model everything falls back to the rules.
    ml_first: bool = True             # ML_FIRST
    ml_first_ta_floor: float = 0.0    # ML_FIRST_TA_FLOOR: lowest TA score allowed when the model is in charge
    ml_first_top_n: int = 3           # ML_FIRST_TOP_N: most new BUYs per cycle, best model scores first
    ml_size_scaling: bool = True      # ML_SIZE_SCALING: scale BUY size with model confidence
    ml_size_min_factor: float = 0.4   # ML_SIZE_MIN_FACTOR: share of full size at p == ML_MIN_P
    ml_size_full_p: float = 0.62      # ML_SIZE_FULL_P: model probability that earns full size
    ml_auto_promote: bool = True      # ML_AUTO_PROMOTE: a model that passes walk-forward becomes the champion at once
    cost_edge_mult: float = 3.0       # COST_EDGE_MULT: take-profit distance must be at least this many times the round-trip cost (0 = off)

    # Strategy improvements (Task 2) - all behind config flags
    # Regime-aware entries: adjust thresholds/requirements by market regime
    regime_aware_entries: bool = False      # REGIME_AWARE_ENTRIES: tighten/relax gates by regime
    regime_ta_min_up: float = 0.10          # REGIME_TA_MIN_UP: TA floor in "up" regime
    regime_ta_min_range: float = 0.20       # REGIME_TA_MIN_RANGE: TA floor in "range" regime
    regime_ta_min_down: float = 0.30        # REGIME_TA_MIN_DOWN: TA floor in "down" regime (higher bar)

    # Relative strength ranking: rank candidates by momentum vs benchmark
    relative_strength_enabled: bool = False  # RELATIVE_STRENGTH_ENABLED: rank by RS vs SPY
    relative_strength_lookback: int = 20     # RELATIVE_STRENGTH_LOOKBACK: days for RS calc
    relative_strength_min: float = 0.0       # RELATIVE_STRENGTH_MIN: minimum RS (vs SPY) to qualify

    # Volatility scaling for position sizing
    volatility_scaling_enabled: bool = False  # VOL_SCALING_ENABLED: scale size by ATR/volatility
    volatility_scaling_target_atr_pct: float = 1.5  # VOL_TARGET_ATR_PCT: target daily ATR% per position
    volatility_scaling_max_factor: float = 2.0      # VOL_MAX_FACTOR: max size multiplier from vol scaling

    # Market regime filter: use broader market regime to gate all entries
    market_regime_filter_enabled: bool = False  # MARKET_REGIME_FILTER: block entries in bad regime
    market_regime_symbol: str = "SPY"           # MARKET_REGIME_SYMBOL: benchmark for regime
    market_regime_min_trend: str = "up"         # MARKET_REGIME_MIN_TREND: "up", "range", or "down"
    market_regime_adx_min: float = 20.0         # MARKET_REGIME_ADX_MIN: minimum ADX for trend

    # Exit improvements
    trailing_stop_enabled: bool = False         # TRAILING_STOP_ENABLED: activate trailing after R multiple
    trailing_stop_activation_r: float = 1.0     # TRAILING_ACTIVATION_R: R multiple to activate
    trailing_stop_atr_mult: float = 1.5         # TRAILING_ATR_MULT: trail distance in ATR
    # Aggressive profit-taking (live engine + exits.py). All off-by-default flags are on here
    # because the request was for a more aggressive profit-taking strategy.
    sentiment_yahoo_rss: bool = True            # SENTIMENT_YAHOO_RSS: also pull Yahoo Finance headlines
    sentiment_yahoo_rss_url: str = ""           # SENTIMENT_YAHOO_RSS_URL ({symbol}); empty = built-in
    sentiment_rss_query_suffix: str = " stock"  # SENTIMENT_RSS_QUERY_SUFFIX: disambiguates tickers like ARM/ON/MU
    bars_concurrency: int = 6                   # SIGNAL_BARS_CONCURRENCY: parallel daily-bar downloads
    news_concurrency: int = 4                   # NEWS_CONCURRENCY: parallel news requests across symbols
    news_max_per_source: int = 10               # NEWS_MAX_PER_SOURCE: headlines kept per source per symbol
    profit_scale_out_enabled: bool = True       # PROFIT_SCALE_OUT: sell part of a winner early
    profit_scale_out_r: float = 1.0             # PROFIT_SCALE_OUT_R: gain (in R) that triggers it
    profit_scale_out_fraction: float = 0.5      # PROFIT_SCALE_OUT_FRACTION: share of position sold
    profit_scale_out_second_r: float = 2.0      # PROFIT_SCALE_OUT_SECOND_R: second tranche (0=off)
    profit_scale_out_second_fraction: float = 0.5   # share of the REMAINDER sold at the 2nd level
    profit_breakeven_after_r: float = 0.75      # PROFIT_BREAKEVEN_R: after this gain, never give back past entry
    profit_giveback_pct: float = 35.0           # PROFIT_GIVEBACK_PCT: exit if this % of peak gain is given back
    profit_giveback_min_gain_pct: float = 2.0   # PROFIT_GIVEBACK_MIN_GAIN_PCT: giveback rule needs this much peak gain
    profit_rsi_exhaustion: float = 78.0         # PROFIT_RSI_EXHAUSTION: sell into strength when RSI14 >= this and in profit (0=off)
    exit_on_trend_break_enabled: bool = True    # EXIT_ON_TREND_BREAK: exit when trend turns down
    exit_ml_drop_enabled: bool = True           # EXIT_ML_DROP: exit if ML prob drops significantly
    exit_ml_drop_margin: float = 0.10           # EXIT_ML_MARGIN: ML prob drop below min_p that triggers exit

    # Signal-based exits for held positions (see app/exits.py). The broker bracket stays the primary exit.
    exit_max_hold_days: int = 20      # EXIT_MAX_HOLD_DAYS: time stop, matches BACKTEST_MAX_HOLD_DAYS (0 = off)
    exit_confirm_readings: int = 3    # EXIT_CONFIRM_READINGS: consecutive "down" readings before a trend exit
    exit_confirm_minutes: float = 10.0  # EXIT_CONFIRM_MINUTES: those readings must span at least this long
    exit_ta_floor: float = -0.10      # EXIT_TA_FLOOR: exit bar for TA score (entry bar is CONSENSUS_TA_MIN)
    exit_ml_margin: float = 0.05      # EXIT_ML_MARGIN: model p must fall this far below ML_MIN_P to exit

    # ML training quality gate
    ml_min_auc: float = 0.51          # ML_MIN_AUC: minimum mean AUC for model promotion

    # Diversification limits (Task 3)
    max_sector_exposure_pct: float = 30.0  # MAX_SECTOR_EXPOSURE_PCT
    max_correlation: float = 0.70          # MAX_CORRELATION
    sector_map: str = "XLK:Technology,XLF:Financials,XLV:Healthcare,XLE:Energy,XLI:Industrials,XLP:Staples,XLY:Discretionary,XLB:Materials,XLRE:RealEstate,XLU:Utilities,XLC:Communication,SPY:Broad,QQQ:Tech,VTI:Broad,IEFA:Intl,VWO:EM"

    # --- Wave 1 additions (docs/ARCHITECTURE_CONTRACTS.md section 2) ---
    # Options exits / risk loop
    options_exit_stop_pct: float = 0.50        # OPTIONS_EXIT_STOP_PCT: exit when premium is this far below entry
    options_exit_take_profit_pct: float = 0.50  # OPTIONS_EXIT_TAKE_PROFIT_PCT
    options_exit_dte: int = 2                  # OPTIONS_EXIT_DTE: force exit when DTE <= N
    options_expiry_close_time: str = "15:45"   # OPTIONS_EXPIRY_CLOSE_TIME (ET, expiry day)
    risk_loop_seconds: float = 1.0             # RISK_LOOP_SECONDS (min 0.25)
    options_quote_max_age_seconds: float = 5.0   # OPTIONS_QUOTE_MAX_AGE_SECONDS
    equity_quote_max_age_seconds: float = 15.0   # EQUITY_QUOTE_MAX_AGE_SECONDS
    options_iv_rank_min_history: int = 60      # OPTIONS_IV_RANK_MIN_HISTORY: days needed for a true IV rank
    # Options auto-entry (HANDOFF section 0 item 9): long calls alongside the stock entry
    options_auto_entry: bool = False           # OPTIONS_AUTO_ENTRY: buy a call when the stock gate passes
    options_entry_target_delta: float = 0.70   # OPTIONS_ENTRY_TARGET_DELTA
    options_entry_target_dte: int = 30         # OPTIONS_ENTRY_TARGET_DTE
    options_entry_max_premium_usd: float = 500.0  # OPTIONS_ENTRY_MAX_PREMIUM_USD per trade
    options_entry_max_open: int = 0            # OPTIONS_ENTRY_MAX_OPEN (0 = no limit)
    options_entry_max_per_day: int = 0         # OPTIONS_ENTRY_MAX_PER_DAY (0 = no limit)
    options_entry_earnings_days: int = 7       # OPTIONS_ENTRY_EARNINGS_DAYS: skip when earnings this close
    options_entry_skip_open_close_minutes: int = 15  # OPTIONS_ENTRY_SKIP_OPEN_CLOSE_MINUTES
    options_spread_log: bool = False           # OPTIONS_SPREAD_LOG: sample real option spreads
    # TRUSTED_HOSTS: extra Host names the dashboard answers to besides loopback, e.g. the server's
    # private Tailscale name. Only for private networks: the dashboard has no login.
    trusted_hosts: list[str] = field(default_factory=list)
    # Options ML (learning mode): ranks, explores and records outcomes; never widens risk
    options_ml_enabled: bool = False           # OPTIONS_ML_ENABLED
    options_ml_explore_pct: float = 0.20       # OPTIONS_ML_EXPLORE_PCT: share of entries that try a random delta/DTE arm
    options_ml_min_p: float = 0.0              # OPTIONS_ML_MIN_P: skip when P(win) is below this (0 = never blocks)
    options_ml_retrain_days: int = 7           # OPTIONS_ML_RETRAIN_DAYS
    options_ml_live_weight: float = 5.0        # OPTIONS_ML_LIVE_WEIGHT: weight of a live outcome vs a modelled one
    options_spread_log_times: str = "09:31,09:33,09:35,09:40,09:45,10:00,10:30,12:00,15:30,15:40,15:45,15:55"  # OPTIONS_SPREAD_LOG_TIMES (ET)
    options_exit_underlying_stop: bool = True  # OPTIONS_EXIT_UNDERLYING_STOP: exit on the stock's stop
    # Entry quality
    entry_pullback_gate: bool = False          # ENTRY_PULLBACK_GATE (opt-in)
    entry_pullback_min_pct: float = 0.02       # ENTRY_PULLBACK_MIN_PCT
    entry_pullback_max_pct: float = 0.05       # ENTRY_PULLBACK_MAX_PCT
    max_adds_per_symbol: int = 1               # MAX_ADDS_PER_SYMBOL
    block_add_to_loser: bool = True            # BLOCK_ADD_TO_LOSER
    eligibility_earnings_fail_closed: bool = True  # ELIGIBILITY_EARNINGS_FAIL_CLOSED
    # Storage
    db_retention_days: int = 90                # DB_RETENTION_DAYS
    # Broker / feed
    broker_execution: str = "mcp"              # BROKER_EXECUTION: "mcp" | "sdk" (mock mode ignores it)
    symbol_whitelist: list[str] = field(default_factory=list)  # WEBULL_SYMBOL_WHITELIST (empty = no restriction)
    feed_ring_size: int = 2000                 # FEED_RING_SIZE: ticks kept per symbol
    feed_max_subscriptions: int = 100          # FEED_MAX_SUBSCRIPTIONS
    feed_options_top_k: int = 4                # FEED_OPTIONS_TOP_K
    # LLM / yfinance / headlines
    llm_timeout_seconds: float = 25.0          # LLM_TIMEOUT_SECONDS
    llm_deadline_seconds: float = 90.0         # LLM_DEADLINE_SECONDS
    yfinance_timeout_seconds: float = 8.0      # YFINANCE_TIMEOUT_SECONDS
    yfinance_negative_ttl_seconds: float = 120.0  # YFINANCE_NEGATIVE_TTL_SECONDS
    yfinance_breaker_failures: int = 5         # YFINANCE_BREAKER_FAILURES
    yfinance_breaker_cooldown_seconds: float = 300.0  # YFINANCE_BREAKER_COOLDOWN_SECONDS
    headline_max_chars: int = 300              # HEADLINE_MAX_CHARS
    # API
    api_token_file: Path = Path("data") / "api_token"   # API_TOKEN_FILE (default DATA_DIR / "api_token")
    stream_heartbeat_seconds: float = 15.0     # STREAM_HEARTBEAT_SECONDS
    extended_hours_soft_stop: bool = True      # EXTENDED_HOURS_SOFT_STOP: app-side stop watch in premarket/after-hours (ext-hours-protection)
    # news-pipeline
    news_half_life_hours: float = 24.0         # NEWS_HALF_LIFE_HOURS: recency half-life of a headline's weight
    news_relevance_filter: bool = True         # NEWS_RELEVANCE_FILTER: keep only headlines naming the ticker/company
    news_google_concurrency: int = 1           # NEWS_GOOGLE_CONCURRENCY: parallel Google News requests
    news_rss_spacing_seconds: float = 0.7      # NEWS_RSS_SPACING_SECONDS: minimum gap between Google/Yahoo RSS requests
    news_google_cooldown_minutes: float = 30.0 # NEWS_GOOGLE_COOLDOWN_MINUTES: pause Google News after a 429
    news_google_spacing_seconds: float = 2.5   # NEWS_GOOGLE_SPACING_SECONDS: minimum gap between Google News requests
    news_google_refresh_minutes: int = 30      # NEWS_GOOGLE_REFRESH_MINUTES: minimum time between Google News passes
    news_outage_minutes: int = 60              # NEWS_OUTAGE_MINUTES: alert when no source returned a headline for this long
    news_nasdaq_enabled: bool = True           # NEWS_NASDAQ_ENABLED: fetch the Nasdaq per-symbol RSS feed
    news_nasdaq_rss_url: str = "https://www.nasdaq.com/feed/rssoutbound?symbol={symbol}"  # NEWS_NASDAQ_RSS_URL: Nasdaq RSS template
    news_seekingalpha_enabled: bool = True     # NEWS_SEEKINGALPHA_ENABLED: fetch the Seeking Alpha per-symbol RSS feed
    news_seekingalpha_rss_url: str = "https://seekingalpha.com/api/sa/combined/{symbol}.xml"  # NEWS_SEEKINGALPHA_RSS_URL: Seeking Alpha RSS template
    news_bing_enabled: bool = True             # NEWS_BING_ENABLED: fetch Bing News RSS search
    news_bing_rss_url: str = "https://www.bing.com/news/search?q={query}&format=rss"  # NEWS_BING_RSS_URL: Bing News RSS template
    news_webull_enabled: bool = True           # NEWS_WEBULL_ENABLED: fetch Webull news (get_stock_news/search_news). false = Google/Yahoo only (Webull UAT returns 404 for news)
    news_llm_batch_size: int = 10              # NEWS_LLM_BATCH_SIZE: headlines per LLM scoring call (keeps JSON untruncated)
    news_llm_max_batches: int = 6              # NEWS_LLM_MAX_BATCHES: LLM scoring calls per refresh
    news_llm_cooldown_minutes: float = 10.0    # NEWS_LLM_COOLDOWN_MINUTES: pause LLM scoring after a provider failure
    event_veto_enabled: bool = True            # EVENT_VETO_ENABLED: block BUYs after a negative hard-event headline
    event_veto_hours: float = 48.0             # EVENT_VETO_HOURS: how long a negative event blocks entries
    earnings_cache_hours: float = 12.0         # EARNINGS_CACHE_HOURS: per-symbol earnings-date cache

    # market-orders
    exit_order_type: str = "limit"             # EXIT_ORDER_TYPE: limit | market (market only in the CORE session)
    entry_order_type: str = "limit"            # ENTRY_ORDER_TYPE: limit | marketable (limit at ask + offset, capped at the price band)
    marketable_offset_bps: float = 10.0        # MARKETABLE_OFFSET_BPS: marketable BUY limit = ask (or last) x (1 + bps/10000)
    entry_chase_seconds: float = 0.0           # ENTRY_CHASE_SECONDS: 0 = off; re-send an unfilled entry once as marketable (CORE only)
    overnight_size_factor: float = 0.5         # OVERNIGHT_SIZE_FACTOR: BUY quantity multiplier in the NIGHT session (1 = off)
    max_spread_pct_extended: float = 1.0       # MAX_SPREAD_PCT_EXTENDED: reject a BUY outside CORE when (ask-bid)/mid exceeds this % (0 = off)
    market_notional_buffer_pct: float = 1.0    # MARKET_NOTIONAL_BUFFER_PCT: guard notional for a MARKET order = last x qty x (1 + this %)

    # swing-strategy
    bracket_take_profit: bool = True           # BRACKET_TAKE_PROFIT: false = no broker take-profit leg; entry goes out alone and the stop is placed on fill (exits are code-managed: scale-outs, trailing, give-back)
    size_from_risk: bool = False               # SIZE_FROM_RISK: size a BUY from the risk budget (budget / (entry - stop)); MAX_ORDER_USD no longer shrinks it (MAX_POSITION_USD, MAX_ORDER_QTY and buying power still cap it)
    risk_per_trade_pct: float = 0.0            # RISK_PER_TRADE_PCT: risk budget as % of equity (0 = use MAX_RISK_PER_TRADE_USD); needs the caller to pass equity
    backtest_live_exits: bool = False          # BACKTEST_LIVE_EXITS: backtester models the live profit rules (scale-outs, break-even, give-back, trailing, RSI exhaustion, no-TP bracket)
    # multi-strategy sleeves (docs/SLEEVES_ARCHITECTURE.md); off = only the swing engine trades
    sleeves_enabled: bool = False              # SLEEVES_ENABLED: run the momentum / put-write / VAA sleeves
    sleeve_start_capital: float = 500000.0     # SLEEVE_START_CAPITAL: virtual cash each non-swing sleeve starts with
    sleeve_pause_dd_pct: float = -25.0         # SLEEVE_PAUSE_DD_PCT: a sleeve stops opening at this drawdown from its peak
    sleeve_momentum_enabled: bool = True       # SLEEVE_MOMENTUM_ENABLED
    sleeve_putwrite_enabled: bool = True       # SLEEVE_PUTWRITE_ENABLED
    sleeve_vaa_enabled: bool = True            # SLEEVE_VAA_ENABLED
    sleeve_momentum_n: int = 10                # SLEEVE_MOMENTUM_N: stocks held by the momentum sleeve
    sleeve_putwrite_underlying: str = "QQQ"    # SLEEVE_PUTWRITE_UNDERLYING
    sleeve_putwrite_delta: float = 0.30        # SLEEVE_PUTWRITE_DELTA: target |delta| of the sold put
    sleeve_putwrite_dte: int = 30              # SLEEVE_PUTWRITE_DTE: target days to expiry
    sleeve_min_trade_usd: float = 1000.0       # SLEEVE_MIN_TRADE_USD: smaller rebalance trades are skipped
    sleeve_rebalance_after_et: str = "10:00"   # SLEEVE_REBALANCE_AFTER_ET: rebalances wait until this ET time
    shadows_enabled: bool = False              # SHADOWS_ENABLED: run virtual-money shadow strategies (no orders)
    shadow_start_capital: float = 500000.0     # SHADOW_START_CAPITAL: virtual cash each shadow starts with

    @property
    def is_live(self) -> bool:
        return self.broker_mode == "webull" and self.webull_environment == "prod"

    @property
    def max_order_notional(self) -> float:
        """MAX_ORDER_NOTIONAL is an alias of MAX_ORDER_USD (the env var accepts either)."""
        return self.max_order_usd

    # Grouped, read-only views (see groups.py). The flat attributes stay the source of truth.
    @property
    def risk(self):
        from .groups import RiskSettings
        return RiskSettings.from_settings(self)

    @property
    def options(self):
        from .groups import OptionsSettings
        return OptionsSettings.from_settings(self)

    @property
    def ml(self):
        from .groups import MLSettings
        return MLSettings.from_settings(self)

    @property
    def feed(self):
        from .groups import FeedSettings
        return FeedSettings.from_settings(self)

    @property
    def alerts(self):
        from .groups import AlertSettings
        return AlertSettings.from_settings(self)

    @property
    def backtest(self):
        from .groups import BacktestSettings
        return BacktestSettings.from_settings(self)

    @property
    def llm(self):
        from .groups import LLMSettings
        return LLMSettings.from_settings(self)

    @property
    def broker_label(self) -> str:
        if self.broker_mode == "mock":
            return "Mock broker (simulated)"
        env = "LIVE (prod)" if self.is_live else "Webull sandbox (uat)"
        return f"Webull {self.webull_region.upper()} - {env}"

