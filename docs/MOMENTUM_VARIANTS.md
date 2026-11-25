# Momentum variants (lab: app/rotation_lab/momentum_variants.py)

Common rules: long-only, equal weight top-N (N=10 stocks, 3 ETFs; also N=5 on app87 for 4 configs), monthly rebalance at last trading day, trade next open, rotation-lab costs, cash = BIL. Run: `scripts/momentum_variants.py`. Results: data/experiments/momentum_variants/REPORT.md.

| # | Variant | Rule as implemented | Source |
|---|---|---|---|
| 1 | Vol-scaled momentum | plain 12-1; exposure = min(cap, 12% / realized 126d vol of own unscaled returns, lagged one day); rest cash. Caps 1.0, 1.5 (leverage) | Barroso & Santa-Clara 2015, JFE 116(1) - https://doi.org/10.1016/j.jfineco.2014.11.010 |
| 2 | Crash-protected momentum | bear = SPY 24m return < 0 AND SPY 126d vol > trailing 5y median -> 100% IEF, else plain 12-1 | Daniel & Moskowitz 2016, JFE 122(2) - https://doi.org/10.1016/j.jfineco.2015.12.002 |
| 3 | Industry/sector momentum | 11 SPDR sector ETFs, top 3 by 6m return (12-1 = benchmark row) | Moskowitz & Grinblatt 1999, JF 54(4) - https://doi.org/10.1111/0022-1082.00146 |
| 4 | Frog-in-the-pan | ID = sign(PRET 12-1) x (%neg days - %pos days) over 12-1 window; top 2N by 12-1, hold N lowest ID | Da, Gurun & Warachka 2014, RFS 27(7) - https://doi.org/10.1093/rfs/hhu003 |
| 5 | Quantitative Momentum | top decile by 12-1, best half by FIP (lowest ID); quarterly, last day of Feb/May/Aug/Nov | Gray & Vogel, Alpha Architect, "Quantitative Momentum" (2016 book) - https://alphaarchitect.com/ |
| 6 | Intermediate momentum | return t-12 to t-7; also rank average with 6-1 | Novy-Marx 2012, JFE 103(3) - https://doi.org/10.1016/j.jfineco.2011.11.003 |
| 7 | Seasonal momentum | mean return of the upcoming calendar month over prior <=10 years (>=5); evaluated from 2016 | Heston & Sadka 2008, JFE 87(2) - https://doi.org/10.1016/j.jfineco.2007.03.004 |
| 8 | Momentum acceleration | 6m return minus prior 6m (t-12..t-6); also rank average with 12-1 | Gettleman & Marks 2006 (SSRN 889995) - https://ssrn.com/abstract=889995 |
| 9 | Skip variants | 12-1 (benchmark) vs 12-0 vs 12m to t-5d | Jegadeesh 1990 / Jegadeesh & Titman 1993 (reversal skip) - https://doi.org/10.1111/j.1540-6261.1993.tb04702.x |
| 10 | Clenow Stocks on the Move | 90d log-regression slope annualised x R2; >100DMA, no >15% gap in 90d, new buys only if SPY>200DMA; weekly; weights ~ price/ATR20 (ATR = close-to-close proxy), cap 2/N | Clenow 2015, "Stocks on the Move" (book) - https://www.followingthetrend.com/stocks-on-the-move/ |
| 11 | Momentum + low vol | rank average of 12-1 and -252d vol | Blitz & van Vliet 2007, JPM; van Vliet & Blitz 2018 "Conservative Formula" - https://ssrn.com/abstract=980236 |
| 12 | Breadth crash filter | IEF when <40% of universe above SMA200, else plain 12-1 | practitioner rule (no academic source) |

Links are given from memory and were not re-verified online. Seasonal and DM signals need extra history (see REPORT caveats).
