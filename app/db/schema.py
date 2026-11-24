"""Baseline schema (migration 0001), the added-column table and row decoding helpers."""

from __future__ import annotations

import json
import sqlite3

from ..market import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger       TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    summary       TEXT,
    error         TEXT,
    tool_calls    TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    limit_price      REAL NOT NULL,
    confidence       REAL,
    rationale        TEXT,
    ref_price        REAL,
    -- pending | rejected | dismissed | submitting | placed | failed | unknown
    status           TEXT NOT NULL,
    reason           TEXT,
    preview          TEXT,
    client_order_id  TEXT,
    broker_response  TEXT,
    decided_by       TEXT,
    placed_at        TEXT
);
CREATE TABLE IF NOT EXISTS trade_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id         INTEGER,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,              -- BUY | SELL
    quantity_requested  INTEGER NOT NULL,
    quantity_filled     INTEGER DEFAULT 0,
    limit_price         REAL NOT NULL,
    -- Entry details
    entry_fill_price    REAL,
    entry_fill_time     TEXT,
    entry_slippage      REAL,                       -- actual - limit
    entry_commission    REAL,
    -- Stop/Target from bracket
    stop_price          REAL,
    target_price        REAL,
    -- Exit details
    exit_fill_price     REAL,
    exit_fill_time      TEXT,
    exit_reason         TEXT,                       -- stop | target | manual | flatten | expired
    exit_slippage       REAL,
    exit_commission     REAL,
    -- P&L
    realized_pnl        REAL,
    realized_pnl_pct    REAL,
    -- MAE / MFE (worst/best price during the trade from entry perspective)
    mae                 REAL,                       -- maximum adverse excursion
    mfe                 REAL,                       -- maximum favorable excursion
    hold_seconds        INTEGER,
    -- Entry snapshot
    entry_price         REAL,                       -- reference price at entry decision
    entry_atr           REAL,
    entry_trend         TEXT,
    entry_ta_score      REAL,
    entry_sent_score    REAL,
    entry_ml_p          REAL,
    entry_features      TEXT,                       -- JSON
    -- Status tracking
    status              TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    -- Namespace
    broker_mode         TEXT,
    webull_environment  TEXT,
    account_id          TEXT
);
CREATE INDEX IF NOT EXISTS ix_trade_ledger_symbol ON trade_ledger(symbol, id);
CREATE INDEX IF NOT EXISTS ix_trade_ledger_proposal ON trade_ledger(proposal_id);
CREATE TABLE IF NOT EXISTS autopsies (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id             INTEGER NOT NULL,           -- FK to trade_ledger
    proposal_id          INTEGER NOT NULL,           -- FK to proposals
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL,
    -- Classification
    outcome_class        TEXT NOT NULL,              -- win | loss | breakeven
    process_class        TEXT NOT NULL,              -- good | bad | mixed
    -- Evidence-based causes (ranked by strength)
    causes               TEXT NOT NULL,              -- JSON array of {cause, evidence, strength, category}
    -- Key metrics at autopsy time
    pnl_pct              REAL,
    mae_pct              REAL,
    mfe_pct              REAL,
    hold_hours           REAL,
    -- LLM narration (not determination)
    llm_narration        TEXT,
    -- Metadata
    created_at           TEXT NOT NULL,
    -- Namespace
    broker_mode          TEXT,
    webull_environment   TEXT,
    account_id           TEXT
);
CREATE INDEX IF NOT EXISTS ix_autopsies_trade ON autopsies(trade_id);
CREATE INDEX IF NOT EXISTS ix_autopsies_proposal ON autopsies(proposal_id);
CREATE TABLE IF NOT EXISTS lessons (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    version               INTEGER NOT NULL DEFAULT 1,
    param_name            TEXT NOT NULL,              -- e.g., "stop_atr_mult", "target_atr_mult"
    param_value           REAL NOT NULL,              -- new proposed value
    param_scope           TEXT NOT NULL,              -- "global" | "symbol:SPY" | "regime:up"
    -- Statistical validation
    sample_size           INTEGER NOT NULL,           -- number of trades supporting this
    min_sample_size       INTEGER NOT NULL DEFAULT 30, -- required minimum
    train_pct             REAL NOT NULL DEFAULT 0.6,  -- time-split train
    val_pct               REAL NOT NULL DEFAULT 0.2,  -- time-split validation
    test_pct              REAL NOT NULL DEFAULT 0.2,  -- time-split test
    baseline_metric       REAL,                       -- current system performance (e.g., win rate)
    proposed_metric       REAL,                       -- performance with this change
    metric_name           TEXT,                       -- "win_rate" | "sharpe" | "expectancy"
    p_value               REAL,                       -- statistical significance
    -- Evidence
    supporting_autopsies  TEXT,                       -- JSON array of autopsy IDs
    evidence_summary      TEXT,                       -- human-readable summary
    -- Status & versioning
    status                TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | active | retired | rejected
    created_by            TEXT NOT NULL,              -- "autopsy" | "human" | "review"
    created_at            TEXT NOT NULL,
    approved_by           TEXT,                       -- human who approved
    approved_at           TEXT,
    activated_at          TEXT,
    retired_at            TEXT,
    retired_reason        TEXT,
    retired_by            TEXT,
    previous_version_id   INTEGER,                  -- for rollback
    -- Namespace
    broker_mode           TEXT,
    webull_environment    TEXT,
    account_id            TEXT
);
CREATE INDEX IF NOT EXISTS ix_lessons_param ON lessons(param_name, status);
CREATE INDEX IF NOT EXISTS ix_lessons_status ON lessons(status);
CREATE TABLE IF NOT EXISTS research_packets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    asset_class   TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    packet_type   TEXT NOT NULL,
    score         REAL,
    evidence      TEXT NOT NULL DEFAULT '[]',
    caveats       TEXT NOT NULL DEFAULT '[]',
    approval_only INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_research_packets_created ON research_packets(created_at DESC);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    status        TEXT NOT NULL,           -- ok | error
    n_outcomes    INTEGER,
    stats         TEXT,
    lessons       TEXT,
    suggestions   TEXT,
    evidence      TEXT,
    error         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    close            REAL,
    atr              REAL,
    trend            TEXT,                 -- up | down | range
    strength         TEXT,                 -- strong | moderate | weak
    vol              TEXT,                 -- high | normal | low
    ta_score         REAL,                 -- -1..+1
    ta_components    TEXT,                 -- JSON list
    features         TEXT,                 -- JSON dict
    sent_score       REAL,                 -- the score the gate uses
    sent_webull      REAL,
    sent_llm          REAL,
    sent_n           INTEGER,
    sent_pos         INTEGER,
    sent_neg         INTEGER,
    sent_neu         INTEGER,
    fear_greed       REAL,
    ml_p             REAL,
    ml_model_id      INTEGER,
    consensus        TEXT,                 -- long | blocked (verdict for a hypothetical BUY)
    consensus_checks TEXT,                 -- JSON list
    sl               REAL,
    tp               REAL,
    ret_1d           REAL,
    ret_5d           REAL,
    spy_5d           REAL,
    evaluated_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_symbol ON signals(symbol, id);
CREATE TABLE IF NOT EXISTS news_items (
    news_id          TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    published_at     TEXT,
    title            TEXT,
    summary          TEXT,
    source           TEXT,
    url              TEXT,
    webull_sentiment TEXT,                 -- POSITIVE | NEGATIVE | NEUTRAL | NULL
    llm_score        REAL,                 -- -1..+1
    llm_relevance    REAL,                 -- 0..1
    llm_label        TEXT,
    llm_reason       TEXT,
    fetched_at       TEXT NOT NULL,
    scored_at        TEXT,
    PRIMARY KEY (news_id, symbol)
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    net_liq       REAL,
    cash          REAL,
    market_value  REAL,
    unrealized    REAL,
    day_pnl       REAL,
    buying_power  REAL
);
-- One row per completed trading day, used for account-vs-SPY performance.
-- ``key`` is "<namespace>:<YYYY-MM-DD>" so multiple broker/account namespaces
-- can share this database without overwriting each other.
CREATE TABLE IF NOT EXISTS benchmark_daily (
    key             TEXT PRIMARY KEY,
    day             TEXT NOT NULL,              -- trading date (ET), YYYY-MM-DD
    net_liq         REAL,
    cash            REAL,
    market_value    REAL,
    spy_close       REAL,
    day_pnl         REAL,                       -- broker day P&L at the close snapshot
    flow            REAL NOT NULL DEFAULT 0,    -- estimated deposit (+) / withdrawal (-)
    core_value      REAL,                       -- populated after the core/satellite phase
    satellite_value REAL,
    updated_at      TEXT,
    broker_mode     TEXT,
    webull_environment TEXT,
    account_id      TEXT
);
CREATE INDEX IF NOT EXISTS ix_benchmark_daily_day ON benchmark_daily(day);
CREATE TABLE IF NOT EXISTS protective_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id      INTEGER,
    symbol           TEXT NOT NULL,
    kind             TEXT NOT NULL,        -- sl | tp
    client_order_id  TEXT NOT NULL,
    quantity         REAL NOT NULL,
    price            REAL NOT NULL,
    -- waiting (entry not filled yet) | active | filled | cancelled | closed | unknown
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT,
    note             TEXT,
    sell_coid        TEXT                  -- the sell this stop was cancelled for, until resolved
);
CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,             -- champion | challenger | rejected | retired | failed
    path        TEXT,
    n_samples   INTEGER,
    n_symbols   INTEGER,
    metrics     TEXT,                      -- JSON: walk-forward results
    params      TEXT,                      -- JSON
    note        TEXT,
    -- Champion/challenger tracking
    shadow_started_at  TEXT,             -- when challenger entered shadow mode
    shadow_trades      INTEGER DEFAULT 0, -- number of trades evaluated in shadow
    shadow_hit_rate    REAL,              -- hit rate in shadow mode
    shadow_avg_return  REAL,              -- avg return in shadow mode
    champion_replaced_id INTEGER         -- ID of champion this replaced (for rollback)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    purpose       TEXT NOT NULL,           -- analysis | review | sentiment
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read    INTEGER DEFAULT 0,
    cache_write   INTEGER DEFAULT 0,
    cost_usd      REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release. Existing databases are upgraded in place.
ADDED_COLUMNS = {
    "signals": {
        "relative_strength": "REAL",
        "market_trend": "TEXT",
        "market_strength": "TEXT",
        "market_adx": "REAL",
        "sent_llm": "REAL",
    },
    "trade_ledger": {
        "broker_mode": "TEXT",
        "webull_environment": "TEXT",
        "account_id": "TEXT",
        "quantity_requested_real": "REAL",
        "quantity_filled_real": "REAL",
        "entrust_type": "TEXT",
        "total_cash_amount": "REAL",
    },
    "autopsies": {
        "broker_mode": "TEXT",
        "webull_environment": "TEXT",
        "account_id": "TEXT",
    },
    "proposals": {
        "features": "TEXT",       # indicator snapshot (JSON) when the proposal was made
        "ret_1d": "REAL",         # % move 1 trading day later, in the proposal's direction
        "ret_5d": "REAL",         # same, 5 trading days later
        "spy_5d": "REAL",         # SPY % move over the same 5 days (benchmark)
        "evaluated_at": "TEXT",
        "signal_id": "INTEGER",   # the signal snapshot the consensus gate used
        "sl": "REAL",
        "tp": "REAL",
        "ml_p": "REAL",
        "consensus": "TEXT",      # JSON list of gate checks
        "risk_checks": "TEXT",    # JSON list of risk checks
        "asset_class": "TEXT NOT NULL DEFAULT 'equity'",
        "instrument_json": "TEXT",
        "strategy_type": "TEXT",
        "legs": "TEXT",
        "collateral_usd": "REAL",
        "evidence": "TEXT",
        "live_eligible": "INTEGER NOT NULL DEFAULT 0",
        "entrust_type": "TEXT",           # "QTY" | "AMOUNT" for fractional
        "total_cash_amount": "REAL",      # dollar amount for AMOUNT orders
        # Options fields
        "option_type": "TEXT",            # CALL | PUT
        "strike_price": "REAL",           # strike price
        "expiration_date": "TEXT",        # YYYY-MM-DD
        "position_intent": "TEXT",        # BUY_TO_OPEN | BUY_TO_CLOSE | SELL_TO_OPEN | SELL_TO_CLOSE
    },
    "runs": {
        "model": "TEXT",
        "cache_read_tokens": "INTEGER DEFAULT 0",
        "cache_write_tokens": "INTEGER DEFAULT 0",
        "cost_usd": "REAL",
        "n_signals": "INTEGER DEFAULT 0",
    },
    "news_items": {
        "llm_score": "REAL", "llm_relevance": "REAL", "llm_label": "TEXT",
        "llm_reason": "TEXT",
        "scorer": "TEXT", "event_type": "TEXT", "llm_event": "TEXT",   # news pipeline (0005)
    },
    "models": {
        "broker_mode": "TEXT",           # "mock" | "webull" - provenance of training data
        "webull_environment": "TEXT",    # "uat" | "prod" - for webull mode
        "account_id": "TEXT",            # Webull account ID for traceability
        "shadow_started_at": "TEXT",     # when challenger entered shadow mode
        "shadow_trades": "INTEGER DEFAULT 0", # trades evaluated in shadow
        "shadow_hit_rate": "REAL",       # hit rate in shadow mode
        "shadow_avg_return": "REAL",     # avg return in shadow mode
        "champion_replaced_id": "INTEGER", # ID of champion this replaced
    },
}

JSON_FIELDS = ("features", "consensus", "risk_checks", "ta_components", "consensus_checks",
               "causes", "evidence", "caveats",
               "metrics", "params", "stats", "instrument_json", "legs", "evidence",
               "caveats")



def _now() -> str:
    return now_utc().isoformat(timespec="seconds")


def decode(row: dict | None) -> dict | None:
    """Parse the JSON text columns of a row for the API."""
    if row is None:
        return None
    out = dict(row)
    for k in JSON_FIELDS:
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    return out



# -- schema drift helpers (used by migration 0001 and the insert self-heal) -------------
def add_column(conn, table: str, col: str, kind: str) -> bool:
    """ALTER TABLE ADD COLUMN, tolerating a second process that added it first."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            return False
        raise


def table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def sync_schema_columns(conn) -> None:
    """Make existing tables match SCHEMA. SCHEMA is the single source of truth, so a
    column added to a CREATE TABLE can never be missing from an upgraded database,
    even if nobody remembered to list it in ADDED_COLUMNS."""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA)
        tables = [r[0] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            have = table_columns(conn, table)
            for _cid, col, ctype, notnull, dflt, pk in ref.execute(f"PRAGMA table_info({table})"):
                if col in have or pk:
                    continue
                # SQLite cannot add a NOT NULL column without a default; drop NOT NULL.
                decl = ctype or ""
                if dflt is not None:
                    decl += f" DEFAULT {dflt}"
                add_column(conn, table, col, decl.strip())
    finally:
        ref.close()


NAMESPACE_TABLES = (
    "runs", "proposals", "signals", "news_items", "equity_snapshots",
    "protective_orders", "llm_usage", "reviews", "events", "research_packets",
    "trade_ledger", "autopsies",
)


def ensure_namespace_columns(conn) -> None:
    """Add namespace columns to tables that may be missing them."""
    for table in NAMESPACE_TABLES:
        cols = table_columns(conn, table)
        for col in ("broker_mode", "webull_environment", "account_id"):
            if col not in cols:
                add_column(conn, table, col, "TEXT")
    if "namespace" not in table_columns(conn, "kv"):
        add_column(conn, "kv", "namespace", "TEXT")
