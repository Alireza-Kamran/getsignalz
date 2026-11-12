"""Family A on the TRAIN period only. See HYPOTHESES.md."""
import sys
from lab import data, engine, validate as v
from lab.strats import trend

PERIOD = sys.argv[1] if len(sys.argv) > 1 else "train"
END = {"train": "2025-01-01", "dev": "2026-01-01"}[PERIOD]


def run(tf, lookback, trail, stop=3.0, label=None, cap=8, per_dir=6, **kw):
    frames = data.load_all(tf=tf, end=END)
    fund = {c: data.load_funding(c) for c in frames}
    mkt = engine.Market(frames, fund).add("atr", trend.prepare(frames))
    sig = v.clip_signals(trend.signals(frames, lookback=lookback, stop_atr=stop, **kw), PERIOD)
    ex = {"chandelier": {"mult": trail}}
    tr = engine.simulate(mkt, sig, ex, max_open=cap, max_per_dir=per_dir, fill="pess")
    v.show(label or "%s N=%d trail=%.1f" % (tf, lookback, trail), tr)
    return mkt, sig, ex, tr


print(v.HEAD)
print("-- primary")
mkt, sig, ex, tr = run("1d", 40, 3.0, label="A PRIMARY 1d N=40 tr=3")
v.by_year(tr, " ")
v.by_side(tr, " ")
v.show("  cost x2", engine.simulate(mkt, sig, ex, max_open=8, max_per_dir=6, fill="pess", cost_mult=2))
v.show("  no cap", engine.simulate(mkt, sig, ex, fill="pess"))
print("  exits:", tr["reason"].value_counts().to_dict(), "| median hold %d bars | mean cost %.3fR | funding %+.3fR"
      % (tr["bars"].median(), tr["cost_r"].mean(), tr["fund_r"].mean()))
print("-- neighbourhood (1d)")
for n in (20, 40, 80):
    for t in (2.5, 3.0, 4.0):
        run("1d", n, t)
print("-- neighbourhood (4h, same horizons)")
for n in (120, 240, 480):
    run("4h", n, 3.0)
print("-- variants")
run("1d", 40, 3.0, label="long only", long_only=True)
btc = data.load("BTC", "1d", end=END)
run("1d", 40, 3.0, label="BTC 200d regime", regime=trend.btc_regime(btc))
