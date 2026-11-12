"""Family F (delta-neutral funding carry). See HYPOTHESES.md.

Usage: python -m lab.run_f train | validate
"""
import math
import sys
import numpy as np
import pandas as pd
from lab import data, validate as v

PERIOD = sys.argv[1] if len(sys.argv) > 1 else "train"
a, b = (pd.Timestamp(x) for x in v.PERIODS[PERIOD])
if PERIOD == "holdout":
    raise SystemExit("holdout is opened once, for the final book only")

# daily funding RECEIVED by a short = sum of the day's settlements
F = pd.DataFrame({c: data.load_funding(c).resample("1D").sum() for c in data.UNIVERSE})
F = F[F.index < b]

COST = (0.00045 + 0.0003) + (0.0007 + 0.0003)   # perp leg + spot leg, per unit notional traded
CAP = 1.5                                       # capital per unit of perp notional


def run(label, thr=0.10, top=5, win=7, rebalance=7, cost_mult=1.0, show=True):
    # trailing funding known at the close of day t (shifted: decided on past days only)
    trail = F.rolling(win).mean().shift(1) * 365
    w = pd.DataFrame(0.0, index=F.index, columns=F.columns)
    cur = pd.Series(0.0, index=F.columns)
    for i in range(len(F)):
        if i % rebalance == 0:
            row = trail.iloc[i].dropna()
            pick = row[row > thr].sort_values().index[-top:]
            cur = pd.Series(0.0, index=F.columns)
            cur[pick] = 1.0 / top           # unfilled slots stay in cash
        w.iloc[i] = cur
    income = (w * F.fillna(0.0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1)
    turnover.iloc[0] = w.iloc[0].sum()
    r = (income - turnover * COST * cost_mult) / CAP
    r = r[(r.index >= a) & (r.index < b)]
    m = r.resample("MS").sum()
    eq = r.cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()
    used = w[(w.index >= a) & (w.index < b)].sum(axis=1).mean()
    if show:
        yr = " ".join("%d:%+.1f%%" % (y, 100 * g.sum()) for y, g in r.groupby(r.index.year))
        print("%-24s ann %+6.2f%%  mo+ %3.0f%%  worstMo %+5.2f%%  maxDD %5.2f%%  deployed %3.0f%% | %s" % (
            label, 100 * r.mean() * 365, 100 * (m > 0).mean(), 100 * m.min(), 100 * dd, 100 * used, yr))
    return r


print("=== %s   returns are on CAPITAL (1.5x perp notional), basis moves not marked" % PERIOD.upper())
r = run("F PRIMARY thr10% top5 7d")
run("  cost x2", cost_mult=2)
if PERIOD == "train":
    for thr in (0.05, 0.20):
        run("thr %d%%" % (thr * 100), thr=thr)
    for top in (3, 8):
        run("top %d" % top, top=top)
    for win in (3, 14):
        run("window %dd" % win, win=win)
m = r.resample("MS").sum() * 100
print("monthly %:")
for y, g in m.groupby(m.index.year):
    print("  %d  %s" % (y, " ".join("%+5.2f" % x for x in g.values)))
