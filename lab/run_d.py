"""Family D (mean reversion WITH the daily trend) on TRAIN. See HYPOTHESES.md."""
from lab import data, engine, validate as v
from lab.strats import meanrev

frames = data.load_all(tf="1h", end="2025-01-01")
fund = {c: data.load_funding(c) for c in frames}
mkt = engine.Market(frames, fund)
feats = {c: meanrev.features(df) for c, df in frames.items()}


def run(label, exit, cap=4, **kw):
    sig = v.clip_signals(meanrev.signals(frames, feats=feats, max_adx=None, **kw), "train")
    tr = engine.simulate(mkt, sig, exit, max_open=cap, fill="pess")
    v.show(label, tr)
    return sig, tr


TP2 = {"tp_r": 2.0}
print(v.HEAD)
print("-- primary: with-trend, EMA100d, 2R target")
sig, tr = run("D PRIMARY", TP2, trend="with")
v.by_year(tr, " ")
v.by_side(tr, " ")
v.show("  cost x2", engine.simulate(mkt, sig, TP2, max_open=4, fill="pess", cost_mult=2))
print("  exits:", tr["reason"].value_counts().to_dict(), "| mean cost %.3fR" % tr["cost_r"].mean())
print("-- controls")
run("AGAINST trend (control)", TP2, trend="against")
run("no trend filter", TP2)
print("-- neighbourhood: exit")
run("with, tp 1.5R", {"tp_r": 1.5}, trend="with")
run("with, tp 3R", {"tp_r": 3.0}, trend="with")
run("with, ratchet 2.5R", {"ratchet": {"start_r": 2.5, "step_r": 0.25}, "tp_r": 5.0}, trend="with")
print("-- neighbourhood: filter / trigger")
run("with, EMA50d", TP2, trend="with", trend_ema=50)
run("with, EMA200d", TP2, trend="with", trend_ema=200)
run("with, RSI 20/80", TP2, trend="with", rsi_lo=20, rsi_hi=80)
run("with, RSI 30/70", TP2, trend="with", rsi_lo=30, rsi_hi=70)
