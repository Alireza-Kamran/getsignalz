"""H0: S2 exactly as deployed, on the multi-year data. See HYPOTHESES.md."""
import time
from lab import data, engine, validate as v
from lab.strats import meanrev

t0 = time.time()
frames = data.load_all(tf="1h", end="2026-01-01")        # dev only: no holdout bars loaded
fund = {c: data.load_funding(c) for c in frames}
mkt = engine.Market(frames, fund)
feats = {c: meanrev.features(df) for c, df in frames.items()}
print("loaded %d coins, features in %.0fs" % (len(frames), time.time() - t0))

sig = v.clip_signals(meanrev.signals(frames, feats=feats, legacy_lev_filter=True), "dev")
sig_all = v.clip_signals(meanrev.signals(frames, feats=feats), "dev")
EXIT = {"ratchet": {"start_r": 2.5, "step_r": 0.25}, "tp_r": 5.0}
print("signals:", len(sig))
print(v.HEAD)
for fill in ("pess", "mid"):
    tr = engine.simulate(mkt, sig, EXIT, max_open=2, fill=fill)
    v.show("H0 train %s" % fill, v.split(tr, "train"))
    v.show("H0 validate %s" % fill, v.split(tr, "validate"))
tr = engine.simulate(mkt, sig, EXIT, max_open=2, fill="pess")
print("skipped:", tr.attrs["skipped"])
v.by_year(tr, "H0 pess")
v.by_side(tr, "H0 pess")
print("exit reasons:", tr["reason"].value_counts().to_dict())
print("mean cost R %.3f | mean funding R %+.4f | median stop %.2f%%" % (
    tr["cost_r"].mean(), tr["fund_r"].mean(), tr["r_dist_pct"].median()))
v.show("H0 pess cost x2", engine.simulate(mkt, sig, EXIT, max_open=2, fill="pess", cost_mult=2))
v.show("H0 pess no cap", engine.simulate(mkt, sig, EXIT, fill="pess"))
v.show("H0 pess no costs", engine.simulate(mkt, sig, EXIT, max_open=2, fill="pess", fee=0, slip_in=0, slip_out=0, use_funding=False))
v.show("H0 pess, rounding bug off", engine.simulate(mkt, sig_all, EXIT, max_open=2, fill="pess"))
w20 = "BTC ETH SOL AVAX SUI DOGE BNB AAVE ARB ADA WLD TIA INJ NEAR APT OP ATOM XLM FIL LDO".split()
v.show("H0 pess, live 20 coins", engine.simulate(mkt, sig[sig.coin.isin(w20)], EXIT, max_open=2, fill="pess"))
print("done in %.0fs" % (time.time() - t0))
