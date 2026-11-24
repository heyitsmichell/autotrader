import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from app.ml_candles import load_bars, build_dataset, evaluate

OUT = "data/ml_research/candles_2026-11-24"
os.makedirs(OUT, exist_ok=True)
bars = load_bars()
pd.set_option("display.width", 250)
for tm in (1.5, 3.0):
    D, feat = build_dataset(bars, tm)
    print(f"\n=== target {tm}xATR  rows={len(D)} base hit={D.win.mean():.3f} allExpR={D.r_net.mean():.3f}")
    R = evaluate(D, feat)
    R.to_csv(f"{OUT}/folds_tgt{tm}.csv", index=False)
    print(R.round(3).to_string(index=False))
    g = R.groupby("model").agg(auc=("auc", "mean"), trades=("trades", "sum"), hit=("hit", "mean"),
                               expR=("expR_taken", "mean"), pos_folds=("expR_taken", lambda x: (x > 0).sum()),
                               maxdd=("maxdd", "min"), rand=("rand_expR", "mean"), all_=("all_expR", "mean"))
    print(g.round(3))
    g.to_csv(f"{OUT}/summary_tgt{tm}.csv")
