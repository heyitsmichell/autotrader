# My strategy (example: edit this to your own rules)

Goal: aggressive swing trades in large, liquid US stocks and ETFs. Let winners run (no fixed target) and cut losers at the stop.
Pyramiding into winners is planned (not built yet). Averaging down (DCA) was rejected.
Protecting capital comes before making money.

## Research standard
- Start with the broad market regime and scheduled macro events, then review each
  candidate's price and volume, company news, earnings date, and account exposure.
- Prefer company filings and issuer releases for material claims when available.
  Treat headlines, social posts, analyst targets, and unusual option activity as
  leads to verify, not standalone reasons to trade.
- Cite source and observation time in the rationale. Separate observed facts from
  interpretation, call out stale or missing data, and include evidence that conflicts
  with the thesis.
- Do not treat one indicator, sentiment score, model probability, or social post as
  a forecast. A model score is a filter, not evidence of expected return.
- Consider spread, fees, and likely slippage. If the available data cannot support
  an execution-quality check, skip the trade.
- Options and crypto spot may be researched, but this stock-share strategy does not
  authorize orders in either market. Dedicated product risk rules must be approved.

## Buying
- Only buy when the daily trend is up: the last close is above the average of
  the last 20 daily closes (get_stock_bars, timespan D, count 30).
- Prefer buying pullbacks: price is 2-5% below its 10-day high, and today's drop
  is not caused by bad company news (check get_stock_news).
- Skip a symbol that reports earnings in the next 3 trading days
  (get_stock_earnings_calendar).
- Open one starter position per symbol. Add to it at most once.
- **Entry orders:**
  - They are marketable limits (`ENTRY_ORDER_TYPE=marketable`): last price + `MARKETABLE_OFFSET_BPS=10`, capped by the price band.
  - An entry unfilled after `ENTRY_CHASE_SECONDS=45` in regular hours is re-sent once.
  - Entries never use true MARKET orders.
- **No BUY on a stale price.** Outside regular hours the price comes from the extended-hours or overnight print. A BUY priced only off yesterday's close is refused, and so is a BUY whose price is at or below its stop.
- **Skip a symbol with negative hard news in the last 48 h:**
  - which news counts: downgrades, guidance cuts, share offerings, investigations or lawsuits, FDA rejections, bankruptcy, being acquired, delisting;
  - how it's matched: the event must be tied to the clause that names the symbol, and resolved cases (dismissed, dropped, cleared) don't count;
  - the LLM's reading overrides the keyword match.
- **News sentiment:** it must be above `CONSENSUS_SENT_MIN=-0.30`. With fewer than `CONSENSUS_MIN_NEWS=3` relevant headlines, news counts as neutral, not as a block.

## Selling
Exits are managed by code, so they are the same in live trading and in the backtester.
Do not propose your own stop or target for a BUY; the code sets them.

**Current exit style: "let winners run" (variant d, live since 2026-11-24 10:06 ET).**
- **Stop loss:** set from ATR when the BUY is placed: `SL_ATR_MULT=2.0`, clamped by `MIN_SL_PCT` / `MAX_SL_PCT`.
  - It is held at the broker as a GTC stop for the CORE session (09:30-16:00 ET).
  - When an entry fills, the stop is placed straight away.
- **No take-profit:** `BRACKET_TAKE_PROFIT=false`, so entries go out alone. There are also no scale-outs (`PROFIT_SCALE_OUT=false`), no break-even stop (`PROFIT_BREAKEVEN_R=0`), no RSI-exhaustion exit (`PROFIT_RSI_EXHAUSTION=0`) and no give-back exit (`PROFIT_GIVEBACK_PCT=0`).
  - The backtest showed the give-back exit and the tight 1.5-ATR target cut winners. With real Webull costs, the old settings lost about 36% over the full period. Variant d made +0.14R per trade (+81% over the full period, +15% in the last 12 months, max drawdown 16%), with survivorship bias.
  - Report: HANDOFF.md §7 item 4a. Data: `data/backtests/swing_2026-11-24_v2/`.
- **Time stop:** sell after `EXIT_MAX_HOLD_DAYS=20`.
- **Trend break:** off (`EXIT_ON_TREND_BREAK=false`).
- **ML drop exit:** only when a promoted model exists, and none does today.
- **Extended hours:** the soft stop (`EXTENDED_HOURS_SOFT_STOP`) protects a position outside 09:30-16:00.
  - **When it fires:** the price must be at or below the stop on a fresh, non-stale quote, on 2 readings at least 30 s apart.
  - **What it does:** it cancels the CORE stop and sends one limit sell per symbol at 0.5% under the price.
  - **If that sell is unfilled at 09:30:** it is cancelled, and the CORE stop is restored, or the position is exited if the price is at or below the stop.
- **A fill below its planned stop** (stale-price entry): the stop is re-anchored from the fill, never upward. The old stop is replaced only after Webull confirms its cancel.
- **In regular hours, a stop that would sit above the market** becomes an exit order instead.
- **Order types:** exits are true MARKET orders in regular hours (`EXIT_ORDER_TYPE=market`), with a limit fallback only on a clear rejection. Outside regular hours they are limits.
- **Selling is always allowed.** The consensus gate and the kill switch never block an exit, stop, soft stop or flatten.

## Never
- Buy more of a position that is losing money.
- Trade in the first 15 minutes after the open.
- Chase a gap, rely on unverified rumors, or treat a broker screener as a trade signal.
- Propose a trade when key sources disagree or the thesis cannot be stated in a
  falsifiable way.

## Review discipline
- Prefer no trade when the evidence is mixed or risk/reward is unclear.
- State the thesis, invalidation level, catalysts, and strongest contrary evidence
  in each proposal rationale.
- Evaluate strategy changes with chronological out-of-sample walk-forward results
  and realistic costs. Do not optimize to one recent period.

## What the code actually enforces
This file is a set of notes for you and the learning reviewer. No code reads it to make a
trade decision, so editing it changes nothing about trading. The rules above are enforced like this:

| Rule | Enforced where | Default |
|---|---|---|
| Buy only when the daily trend is up | `consensus.py` "TA regime" (ML override allowed above 70%) | on |
| Prefer pullbacks 2-5% below the 10-day high | `consensus.py` "Pullback" check, `ENTRY_PULLBACK_GATE` (range `ENTRY_PULLBACK_MIN_PCT` / `_MAX_PCT`) | **off** (opt-in; the TA score also gives a small bonus for it) |
| Skip earnings within 3 days | `eligibility.py`, `ELIGIBILITY_EARNINGS_BLACKOUT_DAYS=3`. Fails closed when no calendar can be read (`ELIGIBILITY_EARNINGS_FAIL_CLOSED`); in mock mode a missing calendar passes | on |
| One starter per symbol | `eligibility.py` `ELIGIBILITY_ONE_STARTER_PER_SYMBOL` (blocks any BUY of a held symbol) | on |
| Add at most once | `risk.py` "Max adds", `MAX_ADDS_PER_SYMBOL=1`. Only reachable when the one-starter rule above is switched off | on |
| Never add to a loser | `risk.py` "Add to loser", `BLOCK_ADD_TO_LOSER`: refuses a BUY of a held symbol when last price is below average cost, or when the cost cannot be determined | on |
| No trades in the first 15 minutes | `eligibility.py` `ELIGIBILITY_FIRST_15_MIN` | on |
| Daily loss limit | `risk.py` "Daily loss": refuses new BUYs; fails closed when day P&L is unavailable | when `MAX_DAILY_LOSS_USD > 0` |
| Stop, time stop (no target, scale-out, break-even, RSI or give-back exit) | `exits.py`, the stop is placed on fill (`portfolio._protect_filled_entry`), and `reconcile` re-places a missing stop | on (variant d) |
| Event veto (negative hard news within 48 h) | `eligibility.py` + `news_events.py` | on |
| Earnings blackout fails closed when the lookup returns nothing for a stock | `yfinance_service.py` | on in live |
| No BUY on a stale or at-or-below-stop price | `risk.py`, `proposals.py`, `broker/parsers.py` | on |
| Extended-hours soft stop | `risk_loop.py` | `EXTENDED_HOURS_SOFT_STOP=true` |
| ML downtrend override needs p > 0.70 | `consensus.py` | on |
| Kill switch blocks only opening orders | `broker` guard, `proposals.py` | on |

Not code-enforced (judgement for you, or just text): the research standard, "verify rumours",
"state the thesis", "no broker screener signals", and "do not chase a gap" beyond the gap filter
(`ELIGIBILITY_MAX_GAP_PCT`). The backtester cannot reproduce sentiment, the first-15-minutes rule
or earnings; its report says so.
