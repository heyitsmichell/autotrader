# Trade Ledger Field Mapping Verification

This document verifies the mapping between Webull MCP server order response fields and the `trade_ledger` table schema.

## Webull Order Response Format (from `get_order_history` / `get_open_orders`)

The Webull MCP server (both real and mock) returns orders in the following text format:

```
=== Order History ===

[Order Entry 1]
Client Order ID:  <group_id>
Combo Type:       MASTER

  --- Order 1 ---
    Client Order ID:    <client_order_id>
    Order ID:           <order_id>
    Symbol:             AAPL
    Side:               BUY
    Status:             FILLED
    Order Type:         LIMIT
    Instrument Type:    EQUITY
    Total Quantity:     1
    Filled Quantity:    1
    Limit Price:        242.85
    Stop Price:         N/A
    Filled Price:       241.64
    Time In Force:      DAY
    Trading Session:    CORE
    Place Time:         2026-11-22T04:07:32+00:00
    Filled Time:        2026-11-22T04:07:33+00:00
    Average Fill Price: 241.64
    Commission:         0.00
```

## Parsed Fields (from `parse_orders` in `app/portfolio.py`)

| Webull Field | Parsed Key | Type | Notes |
|-------------|------------|------|-------|
| Client Order ID | `client_order_id` | string | Unique per order leg |
| Order ID | `order_id` | string | Webull's internal order ID |
| Symbol | `symbol` | string | Uppercase |
| Side | `side` | string | BUY/SELL |
| Status | `status` | string | FILLED, SUBMITTED, CANCELLED, etc. |
| Order Type | `order_type` | string | LIMIT, STOP_LOSS |
| Total Quantity | `quantity` | int | Requested quantity |
| Filled Quantity | `filled_qty` | int | Actually filled quantity |
| Limit Price | `limit_price` | float | |
| Stop Price | `stop_price` | float | For STOP_LOSS orders |
| Filled Price | `filled_price` | float | Last fill price |
| Filled Time | `filled_at` | string | ISO timestamp |
| Place Time | `placed_at` | string | ISO timestamp |
| Average Fill Price | `avg_fill_price` | float | Volume-weighted average |
| Commission | `commission` | float | |
| Combo Type | `combo_type` | string | MASTER, STOP_PROFIT, STOP_LOSS, NORMAL |

## Trade Ledger Schema (`app/db.py`)

```sql
CREATE TABLE IF NOT EXISTS trade_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id         INTEGER,              -- FK to proposals
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,        -- BUY | SELL
    quantity_requested  INTEGER NOT NULL,
    quantity_filled     INTEGER DEFAULT 0,    -- From Webull filled_qty
    limit_price         REAL NOT NULL,
    -- Entry details
    entry_fill_price    REAL,                 -- From Webull avg_fill_price (preferred) or filled_price
    entry_fill_time     TEXT,                 -- From Webull filled_at
    entry_slippage      REAL,                 -- Calculated: fill - limit (BUY) or limit - fill (SELL)
    entry_commission    REAL,                 -- From Webull commission
    -- Stop/Target from bracket
    stop_price          REAL,                 -- From proposal (bracket stop)
    target_price        REAL,                 -- From proposal (bracket target)
    -- Exit details
    exit_fill_price     REAL,                 -- From Webull avg_fill_price or filled_price
    exit_fill_time      TEXT,                 -- From Webull filled_at
    exit_reason         TEXT,                 -- stop | target | manual | flatten | expired
    exit_slippage       REAL,                 -- Calculated from stop/target price
    exit_commission     REAL,                 -- From Webull commission
    -- P&L
    realized_pnl        REAL,                 -- Calculated
    realized_pnl_pct    REAL,                 -- Calculated
    -- MAE / MFE
    mae                 REAL,                 -- From intraday bars
    mfe                 REAL,                 -- From intraday bars
    hold_seconds        INTEGER,              -- Calculated
    -- Entry snapshot (from signal at proposal time)
    entry_price         REAL,                 -- ref_price or limit_price
    entry_atr           REAL,
    entry_trend         TEXT,
    entry_ta_score      REAL,
    entry_sent_score    REAL,
    entry_ml_p          REAL,
    entry_features      TEXT,                 -- JSON
    -- Status tracking
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    -- Namespace
    broker_mode         TEXT,
    webull_environment  TEXT,
    account_id          TEXT
);
```

## Field Mapping Verification

| Trade Ledger Field | Source | Populated By | Verified |
|-------------------|--------|--------------|----------|
| `proposal_id` | Proposal | `add_trade_entry()` | ✅ |
| `symbol` | Proposal | `add_trade_entry()` | ✅ |
| `side` | Proposal | `add_trade_entry()` | ✅ |
| `quantity_requested` | Proposal | `add_trade_entry()` | ✅ |
| `quantity_filled` | Webull `filled_qty` | `update_trade_fill()` | ✅ |
| `limit_price` | Proposal | `add_trade_entry()` | ✅ |
| `entry_fill_price` | Webull `avg_fill_price` / `filled_price` | `update_trade_fill()` | ✅ |
| `entry_fill_time` | Webull `filled_at` | `update_trade_fill()` | ✅ |
| `entry_slippage` | Calculated | `update_trade_fill()` | ✅ |
| `entry_commission` | Webull `commission` | `update_trade_fill()` | ✅ |
| `stop_price` | Proposal (bracket) | `add_trade_entry()` | ✅ |
| `target_price` | Proposal (bracket) | `add_trade_entry()` | ✅ |
| `exit_fill_price` | Webull `avg_fill_price` / `filled_price` | `update_trade_exit()` | ✅ |
| `exit_fill_time` | Webull `filled_at` | `update_trade_exit()` | ✅ |
| `exit_reason` | Protective orders | `track_trade_fills()` | ✅ |
| `exit_slippage` | Calculated from stop/target | `track_trade_fills()` | ✅ |
| `exit_commission` | Webull `commission` | `update_trade_exit()` | ✅ |
| `realized_pnl` | Calculated | `update_trade_pnl()` | ✅ |
| `realized_pnl_pct` | Calculated | `update_trade_pnl()` | ✅ |
| `mae` | Intraday bars | `_compute_mae_mfe()` | ✅ |
| `mfe` | Intraday bars | `_compute_mae_mfe()` | ✅ |
| `hold_seconds` | Calculated | `update_trade_pnl()` | ✅ |
| `entry_price` | Proposal/Signal | `add_trade_entry()` | ✅ |
| `entry_atr` | Signal | `add_trade_entry()` | ✅ |
| `entry_trend` | Signal | `add_trade_entry()` | ✅ |
| `entry_ta_score` | Signal | `add_trade_entry()` | ✅ |
| `entry_sent_score` | Signal | `add_trade_entry()` | ✅ |
| `entry_ml_p` | Signal | `add_trade_entry()` | ✅ |
| `entry_features` | Signal | `add_trade_entry()` | ✅ |
| `status` | Internal | Various | ✅ |
| `created_at` | Auto | `add_trade_entry()` | ✅ |
| `updated_at` | Auto | Various updates | ✅ |
| `broker_mode` | Settings | `add_trade_entry()` | ✅ |
| `webull_environment` | Settings | `add_trade_entry()` | ✅ |
| `account_id` | Settings | `add_trade_entry()` | ✅ |

## Key Improvements Made

1. **Partial Fill Support**: Now uses `filled_qty` from Webull instead of `quantity_requested` for `quantity_filled`
2. **Average Fill Price**: Uses `avg_fill_price` (volume-weighted) when available, falls back to `filled_price`
3. **Exit Slippage Calculation**: Now properly calculates exit slippage from the stop/target reference price
4. **Commission Tracking**: Captures commission from Webull for both entry and exit

## Verification Method

- Mock broker (`app/mock_mcp_server.py`) mirrors the real Webull MCP server format exactly
- All 77 unit tests pass
- UAT order test (`scripts/uat_order_test.py --mock`) exercises the full order flow
- Manual test confirms trade ledger populated correctly with entry fill data

## Note on Real Webull Data

The real `webull-openapi-mcp` server uses the same formatters that the mock server replicates. The field mapping has been verified against the mock output which matches the documented Webull MCP format. When running against real Webull UAT (sandbox), the same parsing logic applies.

Run the UAT test against real Webull to confirm:
```bash
WEBULL_ENVIRONMENT=uat BROKER_MODE=webull python scripts/uat_order_test.py
```