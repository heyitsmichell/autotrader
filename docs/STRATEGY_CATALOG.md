# Strategy catalog (published rules, prices-only)

Implemented in `app/rotation_lab/catalog.py`; results in `data/experiments/rotation/REPORT_CATALOG.md`.
Conventions: month-end signals = last trading day close, trade next open; "month" = 21 trading days for lagged returns
(13612W etc. are daily-shift approximations of the monthly definitions); cash = BIL; long-only, no leverage.
Source verification: [V] = rules checked against a web result fetched this session; [M] = rules from memory of the paper
(link given, not re-read) - treat exact parameters of [M] items as approximate. Links for [M] items were not opened.

## A. Tactical asset allocation
| # | Strategy | Exact rule as implemented | Source |
|---|---|---|---|
| 1 | Faber GTAA-5 (10-mo SMA) [V] | Equal-weight SPY, EFA, IEF, DBC, VNQ (ETF proxies for S&P, EAFE, 10y, GSCI, REIT); hold each if month-end close > 10-month SMA of month-end closes, else cash; monthly. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461 |
| 2 | Faber top-6 of 13 (2013 update) [V/M] | 13 ETF classes (approximation, no intl REIT); rank by avg of 1,3,6,12m return; hold top 6 EW if also > 10m SMA. | https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf |
| 3 | Antonacci GEM [M] | 12m return: if SPY > T-bill (BIL) hold better of SPY/EFA (ACWX proxied by EFA), else AGG; monthly. | https://www.optimalmomentum.com/ ; book Dual Momentum Investing (2014) |
| 4 | Accelerating Dual Momentum [M] | score = avg(1,3,6m return); SPY vs EFA; winner if score > 0 else TLT. | https://engineeredportfolio.com/2018/05/21/accelerating-dual-momentum-investing/ |
| 5 | VAA-G4 [V/M] | Offense SPY,EFA,EEM,AGG; defense LQD,IEF,SHY; 13612W = 12r1+4r3+2r6+r12; b = #offense with 13612W<=0; CF=min(1,b/B), B=1; (1-CF) in top-1 offense, CF in best defense. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3002624 |
| 6 | VAA-G12 [M] | Offense 12 ETFs (SPY,IWM,QQQ,VGK,EWJ,VWO,VNQ,DBC,GLD,TLT,HYG,LQD), top 2, B=4, same defense. | same |
| 7 | DAA-G12 [V] | Canary VWO,BND (13612W); CF=b/2; offense top 6 of G12; defense best of SHY,IEF,LQD (cash if its momentum<=0). | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3212862 |
| 8 | BAA-G12 [V/M] | Canary SPY,VEA,VWO,BND (13612W): all >0 -> top 6 of offense by SMA12 relative momentum (p/SMA-1); else top 3 defensive (TLT,IEF,LQD,TIP,BND,DBC,BIL), each replaced by BIL if worse than BIL. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4166845 |
| 9 | BAA-G4 [M] | As 8, offense QQQ,VEA,VWO,AGG, top 1. | same |
| 10 | HAA [V] | Canary TIP equal-weight 13612>0: top 4 of (SPY,IWM,VEA,VWO,DBC(for PDBC),VNQ,TLT,IEF) at 25% each, negative ones -> IEF/BIL; else 100% IEF or BIL. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4346906 |
| 11 | PAA (Protective AA) [M] | 12 ETFs, MOM=p/SMA12-1; n=#MOM>0; BF=(N-n)/(N-n1), n1=aN/4 (a=2); top 6 weight (1-BF)/6, BF in IEF/BIL. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2759734 |
| 12 | Permanent Portfolio [M] | 25% SPY/TLT/GLD/cash, monthly rebalance. | https://www.investopedia.com/terms/p/permanent-portfolio.asp |
| 13 | All-Weather (ETF approx) [M] | 30 VTI,40 TLT,15 IEF,7.5 GLD,7.5 DBC. | https://www.optimizedportfolio.com/all-weather-portfolio/ |
| 14 | Risk parity (naive inverse-vol) [M] | SPY,TLT,GLD,DBC weights ~ 1/vol(252d), sum to 1 (unlevered, so below levered RP returns). | https://www.quantpedia.com/strategies/risk-parity-asset-allocation/ |
| 15 | Golden Butterfly [M] | 20% each VTI, VBR, TLT, SHY, GLD. | https://portfoliocharts.com/portfolio/golden-butterfly/ |
| 16 | 60/40 trend overlay [M] | 60% SPY/40% IEF, each leg held only if > 10-month SMA else cash. | Faber SSRN 962461 applied to 60/40 |

## B. Equity factors / anomalies (today's S&P 500 list: survivorship-biased; long leg only)
| # | Strategy | Rule | Source |
|---|---|---|---|
| 17 | 12-1 momentum [M] | Rank by return t-252..t-21; top decile EW, monthly. | Jegadeesh-Titman 1993 https://doi.org/10.1111/j.1540-6261.1993.tb04702.x |
| 18 | Short-term reversal [M] | Long worst-decile 1-month return, monthly (variants weekly 5/10d). | Jegadeesh 1990 https://doi.org/10.1111/j.1540-6261.1990.tb05110.x |
| 19 | Low volatility [M] | Lowest-quintile 252d daily vol, monthly. | https://www.quantpedia.com/strategies/low-volatility-factor-effect-in-stocks/ |
| 20 | Low beta / BAB long leg [M] | Lowest-quintile 252d beta vs SPY. Published BAB is levered long/short (not implementable). | Frazzini-Pedersen 2014 https://doi.org/10.1016/j.jfineco.2013.10.005 |
| 21 | 52-week high [M] | Top 30% by close / 252d high, monthly. | George-Hwang 2004 https://doi.org/10.1111/j.1540-6261.2004.00695.x |
| 22 | Residual momentum [M] | 12-1 return net of SPY beta (252d), divided by residual vol; top decile. Shorter window than the 36-month original. | Blitz-Huij-Martens 2011 https://doi.org/10.1016/j.jempfin.2011.01.003 |
| 23 | Time-series momentum [M] | Per asset: long if 12m return > 0, size 10%/EWMA-vol/N (no leverage, no shorting), 8 ETFs. | Moskowitz-Ooi-Pedersen 2012 https://doi.org/10.1016/j.jfineco.2011.11.003 |

## C. Calendar effects (SPY; hold-days approximated by open-to-open)
| # | Strategy | Rule | Source |
|---|---|---|---|
| 24 | Turn of month [M] | Hold last trading day + first 3 of next month. | Lakonishok-Smidt 1988; Ariel 1987 |
| 25 | End-of-month window dressing [M] | Hold last 3 trading days of month only. | https://www.quantpedia.com/strategies/turn-of-the-month-in-equity-indexes/ |
| 26 | Pre-holiday [M] | Hold only the trading day before each market holiday (weekday gaps in index). | https://www.quantpedia.com/strategies/pre-holiday-effect-in-equity-markets/ |
| 27 | Overnight vs intraday [M] | Buy close/sell next open (overnight) or buy open/sell close (intraday) every day; two trades a day. | Cooper-Cliff-Gulen 2008 |
| 28 | Halloween / sell-in-May [M] | SPY Nov-Apr, cash (or IEF/TLT) May-Oct. | Bouman-Jacobsen 2002 https://doi.org/10.1257/000282802762024683 |
| 29 | Volatility-managed portfolio [V] | w = target_vol / trailing 21d realised vol, capped at 1 (published is uncapped/levered), monthly. | Moreira-Muir 2017 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2659431 |

## D. Trend / CTA style
| # | Strategy | Rule | Source |
|---|---|---|---|
| 30 | Dual MA golden cross [M] | SPY long when SMA50 > SMA200 (daily), else cash. | classic |
| 31 | Donchian breakout (Turtle) [M] | Long when close > prior 55-day max close; exit < prior 20-day min close (close-based channel). Also 20/10 and 8-ETF basket. | Faith, Way of the Turtle |
| 32 | Vol-targeted trend on ETFs [M] | 8 ETFs; hold if > SMA200; weight 10%/vol63/N, gross <= 1. | Hurst-Ooi-Pedersen 2017 https://doi.org/10.3905/jpm.2017.44.1.015 |

## E. Metals / currencies (separate result table)
33 Faber 10m-SMA on GLD / SLV / GDX; 34 GLD+SLV+UUP trend; 35 GEM extended with GLD (and SLV/UUP); 36 gold/silver ratio switch (ratio < 12m SMA -> SLV else GLD, held only if above its own 10m SMA; my definition, no canonical source); 37 FX-ETF (FXE,FXY,FXB,FXA,FXC,FXF) 10m-SMA trend; 38 momentum rank of GLD/SLV/UUP/GDX (6m, abs>0; exploratory). Sources: Faber SSRN 962461, Antonacci.

## Skipped (data we do not have, or not implementable)
- Fundamentals-based: value (B/M), quality/profitability, post-earnings drift, accruals, F-score, shareholder yield.
- Futures/options/rates-based: variance risk premium, carry, roll yield, VIX term structure, FX carry.
- Intraday/order-book (opening range, VWAP) beyond daily bars; sentiment, analyst revisions.
- Long/short legs (BAB, WML): cash account, no shorting or leverage; only long legs tested.
- ETFs with short history (MTUM 2013-04, QUAL 2013-07, PDBC 2014-11, USMV 2011-10) as standalone rules; pre-2005 index series (Faber 1973-, Keller 1970s) replaced by ETF proxies.
- Point-in-time index membership (survivorship fix): declined by the user.

## Data fetched (data/bars_yahoo_ext/, cut at 2025-11-21)
VEA 2007-07, EFA 2005, VWO 2005-03, EEM 2005, BND 2007-04, AGG 2005, LQD 2005, HYG 2007-04, TIP 2005, SHY 2005, VNQ 2005, IWD/IWF 2005, MTUM 2013-04, USMV 2011-10, QUAL 2013-07, RSP 2005, DBC 2006-02, PDBC 2014-11, SPLV 2011-05, VTI/VGK/EWJ/IJR/VBR/VUG/VTV 2005, SLV 2006-04, SIVR 2009-07, GDX 2006-05, GDXJ 2009-11, UUP/UDN 2007-03, FXE 2005-12, FXY 2007-02, FXB/FXA/FXC/FXF 2006-06. `long/` holds 2005-start SPY, QQQ, IWM, TLT, IEF, GLD, BIL (BIL 2007-05).
