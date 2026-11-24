# Fractional Shares & Options Trading - Discovery Notes

## Phase 0: Discovery Complete (2025-11-23)

### Webull MCP Server (Mock & Real) - Fractional Share Support

**Tool: `place_stock_order`**
- Supports fractional via `entrust_type=AMOUNT` with `total_cash_amount` (USD notional)
- When `entrust_type=QTY`: `quantity` is shares (float allowed for fractional)
- Order types for fractional: **MARKET or LIMIT only** (no STOP_LOSS, STOP_LOSS_LIMIT, TRAILING_STOP_LOSS)
- Trading session: **CORE only** (no ALL/NIGHT)
- TIF: DAY, GTC (tested in mock)
- **Fractional NOT supported in combo orders** (`place_stock_combo_order` rejects fractional quantities)

**Tool: `preview_stock_order`**
- Same fractional parameters as `place_stock_order`
- Returns estimated cost and fees

**Constants from webull_openapi_mcp:**
```python
VALID_ENTRUST_TYPES = {"QTY", "AMOUNT"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT", "TRAILING_STOP_LOSS"}
VALID_TRADING_SESSIONS = {"ALL", "CORE", "NIGHT"}
```

**Current App Support (app/broker.py):**
- `preview_order()` and `place_order()` support `entrust_type=AMOUNT` with `total_cash_amount`
- `parse_positions()` skips non-EQUITY/STOCK/ETF (options filtered out)
- `place_bracket()` for OTOCO brackets - currently only for equities

---

### Webull MCP Server - Options Trading Support

#### Single-Leg Options (`place_option_single_order`)

**Required Parameters:**
- `symbol`: OCC format (e.g., `AAPL240119C00150000`)
- `side`: `BUY` or `SELL`
- `quantity`: **Integer** (number of contracts, multiplier = 100)
- `option_type`: `CALL` or `PUT`
- `strike_price`: float (e.g., 150.0)
- `expiration_date`: `YYYY-MM-DD` format
- `order_type`: `MARKET`, `LIMIT`, `STOP_LOSS`, `STOP_LOSS_LIMIT`
- `time_in_force`: `DAY` or `GTC`
- `trading_session`: CORE only (hardcoded in Webull)

**Optional Parameters:**
- `account_id`: auto-resolved if not provided
- `client_order_id`: auto-generated if not provided
- `limit_price`: required for LIMIT/STOP_LOSS_LIMIT
- `stop_price`: required for STOP_LOSS/STOP_LOSS_LIMIT
- `position_intent`: `BUY_TO_OPEN`, `BUY_TO_CLOSE`, `SELL_TO_OPEN`, `SELL_TO_CLOSE`
- `account_tax_type`: JP only (`GENERAL`, `SPECIFIC`)
- `leg_in_or_out`: US only (`LEG_IN`, `LEG_OUT`)
- `position_id`: required for leg-in/leg-out
- `leg_in_strategy`: required for LEG_IN (e.g., `COVERED_STOCK`, `VERTICAL`)

**Returns:** `{client_order_id, order_id}`

#### Multi-Leg Option Strategies (`place_option_strategy_order`)

**Required:**
- `account_id`
- `legs`: array of leg objects (min 2)

**Optional:**
- `client_combo_order_id`

**Valid Strategies & Leg Counts:**
| Strategy | Min Legs | Max Legs |
|----------|----------|----------|
| SINGLE | 1 | 1 |
| COVERED_STOCK | 2 | 2 |
| STRADDLE | 2 | 2 |
| STRANGLE | 2 | 2 |
| VERTICAL | 2 | 2 |
| CALENDAR | 2 | 2 |
| BUTTERFLY | 3 | 4 |
| CONDOR | 4 | 4 |
| COLLAR_WITH_STOCK | 3 | 3 |
| IRON_BUTTERFLY | 4 | 4 |
| IRON_CONDOR | 4 | 4 |
| DIAGONAL | 2 | 2 |

**Leg Fields:**
- `symbol`, `side`, `quantity`, `option_type`, `strike_price`, `expiration_date`
- `order_type` (default: LIMIT), `limit_price`, `stop_price`
- `time_in_force` (default: DAY), `position_intent`
- `option_strategy` (default: VERTICAL), `combo_type` (default: NORMAL)

**Returns:** `{client_order_id, combo_order_id, order_id}`

#### Option Data Tools

**`get_option_contracts`:**
- `underlying`: required (e.g., `AAPL`)
- `expiration`: optional filter (YYYY-MM-DD)
- `option_type`: optional filter (CALL/PUT)
- `strike_min`/`strike_max`: optional range filters
- `pagination_key`: for paging
- Returns: symbol (OCC), strike, expiration, type, bid, ask, volume, open_interest

**`get_option_snapshot`:**
- `symbols`: comma-separated OCC symbols (max 20)
- Returns: bid, ask, last, volume, open_interest, IV, Greeks

**`preview_option_order`:**
- Same params as `place_option_single_order` + `account_id` required
- Returns estimated premium, fees, total cost

**`replace_option_order`:**
- Modify limit_price, stop_price, quantity, time_in_force

---

### Mock Broker Implementation (app/mock_mcp_server.py)

**Full Option Support:**
- `get_option_contracts`: generates mock chain around current price (±30% ATM), weekly/monthly expirations
- `get_option_snapshot`: parses OCC symbols, returns mock bid/ask/last/Greeks
- `preview_option_order`: validates, calculates premium (quantity × 100 × price)
- `place_option_single_order`: creates order with legs format, fills immediately
- `place_option_strategy_order`: validates leg count per strategy, places each leg
- `replace_option_order`: modifies open orders

**Position Tracking:**
- Equity positions: `Type: EQUITY` (or STOCK/ETF)
- Option positions: `Type: OPTION` with `Leg: CALL/PUT SYMBOL STRIKE EXP` line
- Option P&L uses multiplier of 100: `(mark - cost) * qty * 100`

**OCC Symbol Format:** `AAPL240119C00150000`
- Underlying: `AAPL` (variable length, typically 1-4 chars)
- Expiration: `240119` = YYMMDD (2024-01-19)
- Type: `C` = CALL, `P` = PUT
- Strike: `00150000` = 150.000 (8 digits, implied 3 decimal places)

---

### Current App Integration Gaps

**app/broker.py:**
- Has `option_contracts()` and `option_snapshots()` read-only methods
- **Missing**: `place_option_single_order()`, `place_option_strategy_order()`, `preview_option_order()`, `replace_option_order()`
- **Missing**: Option position parsing (currently skipped in `parse_positions()`)

**app/risk.py:**
- Has `_is_fractional()` and `_validate_fractional()` for fractional shares
- **Missing**: Any option risk validation (position limits, notional limits, strategy limits)

**app/agent.py:**
- `ANALYST_TOOLS` includes `get_option_contracts`, `get_option_snapshot`
- `PROPOSE_SCHEMA` has `quantity` as number with fractional description
- **Missing**: Option proposal schema (option_type, strike, expiration, strategy, legs)
- **Missing**: System prompt guidance for options

**app/engine.py:**
- Handles fractional in `_flatten_symbol()` via AMOUNT orders
- **Missing**: Option order lifecycle (entry, monitoring, exit)
- **Missing**: Option consensus gate logic
- **Missing**: Option bracket/order management

**app/db.py:**
- `trade_ledger` has `quantity_requested_real`, `quantity_filled_real` (REAL for fractional)
- `proposals` and `trade_ledger` have `entrust_type`, `total_cash_amount`
- **Missing**: Option-specific columns (option_symbol, strategy, legs, multiplier)

**web/src/lib/types.ts:**
- `Proposal` has `quantity: number` (fractional support)
- `Position` has `qty: number`
- **Missing**: Option-specific types (OptionContract, OptionPosition, OptionProposal)

---

### Configuration (app/config.py)

**Existing Fractional Settings:**
```python
fractional_enabled: bool = False
fractional_min_usd: float = 1.0
fractional_decimals: int = 4
fractional_whitelist: list[str] = ["AAPL","MSFT","NVDA","SPY","QQQ","AMZN","GOOGL","META","TSLA"]
```

**Missing Options Settings (need to add):**
- `options_enabled: bool = False`
- `options_max_contracts_per_order: int = 10`
- `options_max_notional_usd: float = 5000.0`
- `options_allowed_strategies: list[str] = ["SINGLE", "VERTICAL", "STRADDLE", "STRANGLE"]`
- `options_require_approval: bool = True` (account option level check)
- `options_min_dte: int = 7` (days to expiration minimum)
- `options_max_dte: int = 45` (days to expiration maximum for entries)

---

### Webull Account Restrictions (Real Broker)

**Options Trading Levels (US):**
- Level 1: Covered calls, cash-secured puts
- Level 2: Long calls/puts, straddles/strangles
- Level 3: Spreads (vertical, calendar, diagonal)
- Level 4: Naked calls/puts, uncovered spreads

**Requirements for Live Trading:**
- Account must have options approval
- `WEBULL_ENVIRONMENT=prod` AND `ALLOW_LIVE_AUTO_TRADING=true` AND `OPTIONS_ENABLED=true`
- Fractional: same but `FRACTIONAL_ENABLED=true`

---

### Implementation Priority (Phase 1 → Phase 2)

**Phase 1: Fractional Shares**
1. ✅ Config flags exist
2. ✅ DB schema supports REAL quantities
3. ✅ Mock broker supports AMOUNT entrust_type
4. ✅ Broker place_order/preview_order support fractional
5. ✅ Sizing returns float, rounds to fractional_decimals
6. ✅ Risk validates fractional qty, rejects brackets for fractional
7. ✅ Engine handles fractional remainder in flatten
8. ⬜ Add whitelist check in risk/order placement
9. ⬜ Add tests for fractional buy/sell/flatten
10. ⬜ Update dashboard for fractional display

**Phase 2: Options Trading**
1. Add config flags for options
2. Add option order methods to Broker class
3. Add option risk validation (position limits, notional, strategy limits)
4. Add option proposal schema to agent
5. Add option consensus gate (IV rank, delta, liquidity filters)
6. Add option order lifecycle in engine (entry, stop-loss, take-profit, assignment risk)
7. Add option position parsing and P&L tracking
8. Add option DB columns (option_symbol, strategy, legs, multiplier=100)
9. Add dashboard option chain, positions, proposals panels
10. Add tests for single-leg and multi-leg strategies

---

### Key Parameter Names for Implementation

**Stock Fractional:**
```python
# place_stock_order / preview_stock_order
entrust_type="AMOUNT"      # or "QTY"
total_cash_amount=100.0    # USD notional (required for AMOUNT)
quantity=0.5               # float shares (for QTY)
order_type="MARKET"        # MARKET or LIMIT only
trading_session="CORE"     # CORE only
time_in_force="DAY"        # DAY or GTC
```

**Option Single-Leg:**
```python
# place_option_single_order
symbol="AAPL240119C00150000"
side="BUY"
quantity=1                 # integer contracts
option_type="CALL"
strike_price=150.0
expiration_date="2024-01-19"
order_type="LIMIT"
time_in_force="DAY"
limit_price=2.50
position_intent="BUY_TO_OPEN"
```

**Option Multi-Leg:**
```python
# place_option_strategy_order
legs=[{
    "symbol": "AAPL240119C00150000",
    "side": "BUY",
    "quantity": 1,
    "option_type": "CALL",
    "strike_price": 150.0,
    "expiration_date": "2024-01-19",
    "order_type": "LIMIT",
    "limit_price": 2.50,
    "time_in_force": "DAY",
    "position_intent": "BUY_TO_OPEN",
    "option_strategy": "VERTICAL"
}, {
    "symbol": "AAPL240119C00155000",
    "side": "SELL",
    "quantity": 1,
    "option_type": "CALL",
    "strike_price": 155.0,
    "expiration_date": "2024-01-19",
    "order_type": "LIMIT",
    "limit_price": 1.00,
    "time_in_force": "DAY",
    "position_intent": "SELL_TO_OPEN",
    "option_strategy": "VERTICAL"
}]
```

---

### Testing Strategy

**Mock Broker Tests:**
- Fractional: place AMOUNT order, verify quantity calculated correctly
- Fractional: place QTY order with float shares
- Fractional: reject bracket order for fractional
- Option single-leg: BUY_TO_OPEN CALL, verify position created
- Option single-leg: SELL_TO_CLOSE, verify position closed
- Option multi-leg: VERTICAL spread, verify both legs placed
- Option multi-leg: STRADDLE, verify 2 legs
- Option expiration: verify DTE filtering works
- Option Greeks: verify snapshot returns IV, delta, gamma, theta, vega

**Integration Tests:**
- Fractional + Risk: max_position_usd respected with fractional qty
- Fractional + Sizing: max_allowed_qty returns float, rounded to decimals
- Option + Risk: max contracts per order enforced
- Option + Risk: strategy leg count validated
- Option + Engine: full lifecycle (proposal → order → fill → exit)

---

### Files to Modify (Phase 1 → Phase 2)

**Phase 1 - Fractional:**
- `app/risk.py`: Add whitelist check in `_validate_fractional()`
- `app/broker.py`: Add whitelist check in `place_order()`/`preview_order()`
- `app/engine.py`: Verify fractional handling in all paths
- `tests/test_fractional.py`: New test file

**Phase 2 - Options:**
- `app/config.py`: Add options settings
- `app/broker.py`: Add option order methods, position parsing
- `app/risk.py`: Add `OptionRiskManager` class
- `app/agent.py`: Add option proposal schema, system prompt
- `app/engine.py`: Add option lifecycle methods
- `app/db.py`: Add option columns, migrations
- `app/sizing.py`: Add option sizing (contracts × 100 multiplier)
- `web/src/lib/types.ts`: Add option types
- `web/src/components/`: Add option chain, positions, proposals panels
- `tests/test_options.py`: New test file