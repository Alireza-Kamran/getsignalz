"""Family B (cross-sectional momentum) on TRAIN. See HYPOTHESES.md."""
import pandas as pd
from lab import data, engine, validate as v
from lab.strats import xsmom

END = "2025-01-01"
fr = data.load_all(tf="1d", end=END)
closes = pd.DataFrame({c: d["close"] for c, d in fr.items()})
# funding paid by a long over each day = sum of that day's settlements
fund = pd.DataFrame({c: data.load_funding(c).resample("1D").sum() for c in fr}).reindex(closes.index).fillna(0.0)
a, b = v.PERIODS["train"]


def run(label, **kw):
    w = xsmom.weights(closes, **kw)
    r = engine.weights_backtest(closes, w, funding=fund)
    r = r[(r.index >= a) & (r.index < b)]
    s = v.returns_summary(r, 365)
    r2 = engine.weights_backtest(closes, w, funding=fund, cost_mult=2)
    s2 = v.returns_summary(r2[(r2.index >= a) & (r2.index < b)], 365)
    yr = " ".join("%d:%+.0f%%" % (y, 100 * ((1 + g).prod() - 1)) for y, g in r.groupby(r.index.year))
    print("%-22s ann %+6.1f%% vol %5.1f%% sharpe %+5.2f t %+5.2f maxDD %6.1f%% mo+ %3.0f%% worstMo %+6.1f%% | x2cost sharpe %+5.2f | %s" % (
        label, s["ann_ret"], s["ann_vol"], s["sharpe"], s["t"], s["max_dd"], s["pos_months"], s["worst_month"], s2["sharpe"], yr))
    return r


print("coins:", closes.shape[1], "days:", len(closes))
run("B PRIMARY 28d top20%")
for lb in (14, 56):
    run("lookback %dd" % lb, lookback=lb)
run("28d top/bottom third", frac=1 / 3)
run("28d no skip", skip=0)
run("7d (reversal check)", lookback=7)
run("28d rebalance daily", rebalance=1)
