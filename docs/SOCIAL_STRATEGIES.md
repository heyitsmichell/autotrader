# Social-media strategy catalog (research only)

Found 25 distinct strategies; 14 families implemented (83 trials incl. variants), 11 skipped (reason given). Results in
`data/experiments/social/REPORT.md` and `results.csv`. Code: `app/rotation_lab/social.py`, `app/strategy_lab/social_families.py`,
`scripts/run_social.py`, `scripts/fetch_social_bars.py`. Execution: signal at close t, fill open t+1, 5 bps + SEC + TAF.
Selection-bias warning: posts are winners that survived being posted; nearly all claims below are backtest-only, in-sample, often
quoted before costs and with next-close fills.

| # | Strategy | Source | Claimed | Rules (as tested) | Data | Credibility | Status |
|---|---|---|---|---|---|---|---|
| 1 | Hedgefundie UPRO/TMF 55/45 | https://www.bogleheads.org/forum/viewtopic.php?t=288192 (orig t=272007); https://www.optimizedportfolio.com/hedgefundie-adventure/ | huge CAGR 2010-2021; -64% in 2022 | 55% UPRO 45% TMF, quarterly rebalance | UPRO,TMF (synthetic pre-2009) | backtest + live followers (many lost money 2022); regime bet on stock/bond correlation | tested (6 variants) |
| 2 | TQQQ 200-SMA 5%/-3% buffer | https://www.tradingview.com/script/VfrwgjA6-TQQQ-200-SMA-5-Entry-3-Exit-since-2010-Metrics-by-DE | max DD ~53% | QQQ close >200SMA*1.05 -> TQQQ; exit < SMA*0.97; else BIL | TQQQ,QQQ | backtest, buffers tuned | tested |
| 3 | SPY 200-SMA +4/-3 -> TQQQ / QQQ | r/LETFs 1nhye66 via https://www.tradingview.com/script/bznVflR1-SPY200SMA-4-3-TQQQ-QQQ-STRATEGY | long-term outperformance | SPY >SMA*1.04 -> TQQQ; <SMA*0.97 -> QQQ (original: DCA into QQQ over 6-12 mo, simplified) | TQQQ,QQQ,SPY | backtest only, bubble guard not tested | tested |
| 4 | TQQQ/UPRO/SSO/QLD 200-SMA (plain) | r/LETFs common pattern | - | index >SMA200 -> LETF else BIL | LETFs | folklore | tested |
| 5 | Composer "TQQQ For The Long Term" (FTLT) clones | https://www.composer.trade/trading-strategies/tqqq-for-the-long-term-v4-no-better-qqqsinglestocks-pietros-maneos-10-yr-annualized-return-2694-644-calmar-ratio--sWVH0gg6XDilOgcPElW6 | 26.9% 10y ann., Calmar 0.64 (platform claim) | SPY>200SMA: TQQQ unless QQQ RSI(10)>hot -> BIL; else TQQQ only if TQQQ RSI(10)<cold else BIL/TLT. Exact public thresholds not published; APPROXIMATION | TQQQ,QQQ,SPY,TLT | backtest only, heavily mod-chained/overfit | tested (6) |
| 6 | Kelly 9Sig (TQQQ/bond) | https://jasonkelly.com/ ; https://azqato.com/invests/leveraged/9sig ; https://bestfolio.app/blog/kelly-signal-danger | 35% CAGR since 2017 (vendor) | quarterly: line = TQQQ value after last quarter x1.09; sell surplus to bond / buy shortfall (90% throttle). Spike-reset and 30-Down NOT implemented (paywalled) | TQQQ,IEF | vendor claim; sim 3x QQQ through 2000 = -99.7% | tested (5) |
| 7 | Connors RSI(2) on SPY/QQQ | https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2 ; https://empiricalab.substack.com/p/connors-rsi2-spy | Sharpe 0.97 pre-publication, 0.68 after | RSI(2)<10 & close>SMA200 buy; exit close>SMA5 (or RSI>70) | SPY,QQQ | published 2008; decays | tested (6) |
| 8 | IBS mean reversion | https://jonathankinlay.com/2019/07/the-internal-bar-strength-indicator/ ; https://empiricalab.substack.com/p/ibs-qqq | 13.9% QQQ, DD -55% | IBS<0.2 buy, >0.8 sell (+/- SMA200) | SPY,QQQ OHLC | backtest, same-close fills overstate | tested (6) |
| 9 | "3 days down" overnight | https://tradinginvestingstrategies.substack.com/p/the-famous-3-down-days-trading-strategy-backtest | +1056% (headline) | 3 consecutive down closes -> hold 1 day | SPY,QQQ | tiny edge per trade, fill-sensitive | tested (6) |
| 10 | Turn-of-month | https://algoedgeinsights.beehiiv.com/p/the-turn-of-the-month-effect-is-real-i-tested-it-on-26-years-of-spy-to-prove-it | 42% of return, 29% exposure | last N + first M trading days | SPY,QQQ | academic effect, decayed | tested (6) |
| 11 | VIX/VIX3M term-structure switch (SPY) | https://www.quantconnect.com/forum/discussion/2657 | - | hold SPY while VIX/VIX3M < th | ^VIX,^VIX3M | folklore | tested (3) |
| 12 | Short-vol SVXY on contango / VIX9D<VIX | https://www.quantconnect.com/forum/discussion/7984 ; TradingView "Trading the VIX Part 2" | thresholds +/-1% change returns >35% (reply: overfit) | SVXY while VIX/VIX3M<th or VIX9D<VIX | SVXY (-1x pre 2018-02, -0.5x after) | short-vol tail risk, XIV died 2018 | tested (3), window from 2012-10 |
| 13 | VIX-based leverage scaling | https://www.quantconnect.com/research/20886/the-max-effect-with-vix-based-leverage-scaling/ | - | L=1.5 at VIX<=15 -> 1x at VIX>=30, via SPY+SSO | SPY,SSO,^VIX | research post | tested (5) |
| 14 | LETF momentum rotation (top-1 of TQQQ/UPRO/TMF[/SOXL]) | Composer / r-LETFs pattern; QC forum 19360 | - | best N-day return if >0 else BIL, monthly | LETFs | no canonical post | tested (5) |
| 15 | Top-3 sector ETF rotation | https://paperswithbacktest.com/strategies/sector-rotation-strategy ; https://github.com/stevejames115/sector-momentum-backtest | SR 0.98 vs 1.00 SPY (careful backtests) | top-N SPDR sectors by return, monthly, SPY200 filter opt. | 9 SPDRs | honest backtests, no edge | tested (6) |
| 16 | RSI(2) / IBS stock baskets | Connors-Alvarez 2008; Pagonidis 2014 | - | up to 10/20 slots, most oversold, same exits | S&P500 (today's) | survivorship-biased | tested (8) |
| 17 | 12-1 sector/stock momentum | (already tested earlier: rotation_lab) | - | - | - | - | benchmark only |
| 18 | Concretum eVRP + VIX/VIX3M 4-regime | https://concretumgroup.substack.com/p/automating-a-volatility-strategy | none given | needs expected-VRP model | VIX, realized vol | unverifiable | skipped (no rule detail) |
| 19 | Short VXX after +6.67%/5d | https://valuelytica.substack.com/p/less-exposure-same-edge | - | needs shorting | VXX | - | skipped (short) |
| 20 | QC "Regime-Based Tactical Rotation" #19360 | https://www.quantconnect.com/forum/discussion/19360/regime-based-tactical-rotation/ | - | full rule tree not public | LETFs,UVXY | - | skipped |
| 21 | QC #20785 bull/bear LETF + UVXY | https://www.quantconnect.com/forum/discussion/20785 | - | rule tree not public; critics: SVIX blow-up | - | - | skipped |
| 22 | Wheel / covered calls (r/thetagang) | reddit | - | needs options data | - | - | skipped |
| 23 | Opening-range breakout | various | - | needs intraday history | - | - | skipped |
| 24 | Crypto strategies | various | - | out of scope | - | - | skipped |
| 25 | Composer RSI/UVXY/SQQQ defensive legs | composer "TQQQ Moving Averages" | - | needs SQQQ/UVXY synthetics (path-dependent decay) | - | - | skipped (defense leg replaced by BIL/TLT in #5) |

Also noted but not run: bestfolio/"four dip rules" (https://empiricalab.substack.com/p/four-dip-rules-one-account, rules not reproducible from the free text),
Risk-parity LETF basket (QC research 17879, weights not given).

## Results summary (full numbers: data/experiments/social/results.csv, yearly_returns.csv; reproduce `python scripts/run_social.py`)
Windows: long 2006-01..2025-11-21 (LETFs synthetic before inception), common 2011-01-03..2025-11-21. Holdout never loaded. Deflation N = 252 registry
rows + 83 social + 304 prior rotation = 639 (daily-Sharpe variance 1.728e-04). Zero of 83 trials reach DSR > 0.95.

Benchmarks (common): SPY 13.7% / Sharpe 0.86 / DD -32%; 60/40 9.4% / 0.99 / -22.5%; 12-1 momentum top-10 monthly (sp500) 40.5% / 1.16 / -45.6%,
weekly 43.9% / 1.23 / -44.1% (survivorship-biased). Variant d (reference, 2022-07..2025-11): 14.1% / 0.87.

Top social strategies, common window (CAGR / Sharpe / maxDD / worst year / DSR):
FTLT approx hot79 cold31 44.5% / 0.98 / -55% / -24% / 0.89; 9Sig 6% 26.1% / 0.97 / -52% / -48% / 0.89; 9Sig 9% 34.7% / 0.95 / -65% / -61% / 0.88;
FTLT hot85 cold25 TLT 39.9% / 0.93 / -67% / -63% / 0.86; FTLT hot80 cold30 40.6% / 0.93 / -57% / -24% / 0.86; SPY200 +4/-3 -> TQQQ/QQQ 37.7% / 0.91 / -65% / -59% / 0.83;
TQQQ own SMA200 33.1% / 0.87 / -48% / -34% / 0.80; TQQQ buy&hold 38.4% / 0.84 / -82% / -79% / 0.76; VIX leverage 1.25x 14.6% / 0.84 / -33% / -23% / 0.76;
IBS stock basket 13.9% / 0.81 / -34% / -17% / 0.73.

Crisis behaviour, long window (2008 / 2020 / 2022 return, partly synthetic): SPY -37 / +18 / -18; TQQQ B&H -88 / +108 / -79 (maxDD -94%);
HFEA 55/45 -28 / +77 / -62 (DD -68%); FTLT hot79 +26 / +123 / -24; SPY200 +4/-3 -> QQQ -42 / +92 / -59; QQQ200 +5/-3 -> BIL -22 / +78 / -35.

Findings: no social strategy beats 12-1 momentum on Sharpe; only ~8 beat SPY Sharpe and all of those carry -47..-67% drawdowns from 3x leverage; 60/40 matches
the best of them on Sharpe with -22% DD. Hyped failures: HFEA (Sharpe 0.81 common, -62% 2022), TQQQ/9Sig (DD -65..-94%), UPRO/SSO/QLD SMA (no better than 1x SMA),
VIX/VIX3M switches (Sharpe 0.67-0.76, CAGR 7-9%), SVXY (DD -52..-62%), LETF momentum rotation (Sharpe 0.33-0.65), sector rotation (no better than EW),
RSI2/IBS/3-down/TOM on SPY/QQQ (Sharpe 0.0-0.6, CAGR 1-7%; RSI2 decay confirmed). Plausible after deflation: none; FTLT has the best DSR (0.89, worst year -24%)
but is a leveraged-beta overlay built from unpublished/overfit clone parameters.

Caveats: posted strategies are survivors/in-sample with same-close fills (we fill next open); synthetic LETFs are slightly optimistic and pre-inception;
SVXY is -1x before 2018-02 and -0.5x after; FTLT/9Sig/#3 are approximations; SHY proxies BIL before 2007-05; S&P 500 universe is today's membership;
registry `notes` text "synthetic leveraged pre-inception" was applied to every social row including unleveraged ones (append-only file not rewritten);
9Sig variants collapse to one path after 2010 (bond sleeve drained by the 90% throttle).

## Data and synthetic leverage
`scripts/fetch_social_bars.py [--symbols ...]` writes `data/bars_yahoo_ext/social/` (kept separate so the 560-symbol universe globs of
other labs are unchanged): TQQQ (2010-02), UPRO (2009-06), SSO/QLD (2006-06), TMF (2009-04), UBT, SOXL (2010-03), TECL, SPXL, SVXY (2011-10),
VIXY (2011-01), VXX (2018-01), UVXY, SQQQ, SH, SPXU, TNA, EDV, SHY, SMH, XLK, ^VIX, ^VIX3M, ^VIX9D, ^VXN, ^GSPC, ^NDX. Yahoo series are adjusted.
Synthetic = L x daily underlying return - (L-1) x BIL (SHY before 2007-05) - 1%/yr, spliced to the real ETF at inception. Validation on the overlap:
daily-return correlation TQQQ 0.9989 (synthetic CAGR 42.9% vs real 41.2%), UPRO 0.9982 (33.6 vs 31.8), TMF 0.9967 (-4.0 vs -5.4):
synthetic is slightly optimistic (~1.5 pt/yr). Everything before the inception dates is SYNTHETIC (2008 crash in the long window).
