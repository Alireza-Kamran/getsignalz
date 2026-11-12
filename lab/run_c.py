"""Family C (the owner's indicator) on TRAIN. See HYPOTHESES.md."""
import sys
import numpy as np
from lab import data, engine, validate as v
from lab.strats import kamran, trend
from lab.strats.common import heikin_ashi
sys.path.insert(0, "/root/trade")
import indicators as live_ind

END = "2025-01-01"

# --- port check: the vectorised blocks must equal the live port, bar for bar
df = data.load("ETH", "1h", start="2024-06-01", end="2024-09-01")
ha = heikin_ashi(df)
b1, s1 = kamran.order_blocks(ha, df["volume"])
ref = live_ind.order_blocks(ha["high"], ha["low"], ha["close"], df["volume"])
st_ref, _, _ = live_ind.ssl_channel(ha["high"], ha["low"], ha["close"], 200)
st = kamran.ssl_state(ha, 200)
print("port check  OB bull %s  OB bear %s  SSL %s   (n OB: %d bull, %d bear)" % (
    bool((b1 == ref[0]).all()), bool((s1 == ref[1]).all()),
    bool((st.fillna(0).values[200:] == st_ref.fillna(0).values[200:]).all()),
    int(b1.sum()), int(s1.sum())))

print(v.HEAD)
print("-- C1: SSL-200 flips as a trend engine")
for tf, L in (("4h", 200), ("1h", 200), ("1d", 200), ("4h", 100)):
    fr = data.load_all(tf=tf, end=END)
    mkt = engine.Market(fr, {c: data.load_funding(c) for c in fr})
    sig, xl, xs = kamran.ssl_flip_signals(fr, L)
    mkt.add("exit_long", xl).add("exit_short", xs)
    tr = engine.simulate(mkt, v.clip_signals(sig, "train"), {"exit_signal": True},
                         max_open=8, max_per_dir=6, fill="pess")
    v.show("C1 %s SSL%d%s" % (tf, L, " PRIMARY" if (tf, L) == ("4h", 200) else ""), tr)
    if (tf, L) == ("4h", 200):
        v.by_year(tr, " ")
        v.by_side(tr, " ")

fr = data.load_all(tf="1h", end=END)
mkt = engine.Market(fr, {c: data.load_funding(c) for c in fr})
print("-- C2: indicator's combined alert (OB + RSI 30/70), 1h")
sig = v.clip_signals(kamran.ob_signals(fr), "train")
tr = engine.simulate(mkt, sig, {"tp_r": 2.0}, max_open=4, fill="pess")
v.show("C2 PRIMARY tp 2R", tr)
v.by_year(tr, " ")
v.by_side(tr, " ")
v.show("C2 ratchet 2.5R", engine.simulate(mkt, sig, {"ratchet": {"start_r": 2.5, "step_r": 0.25}, "tp_r": 5.0}, max_open=4, fill="pess"))
v.show("C2 no costs", engine.simulate(mkt, sig, {"tp_r": 2.0}, max_open=4, fill="pess", fee=0, slip_in=0, slip_out=0, use_funding=False))
print("-- C3: OB with the 4h SSL-200 trend, 1h")
sig = v.clip_signals(kamran.ob_signals(fr, use_rsi=False, ssl_tf="4h"), "train")
tr = engine.simulate(mkt, sig, {"tp_r": 2.0}, max_open=4, fill="pess")
v.show("C3 PRIMARY tp 2R", tr)
v.by_side(tr, " ")
v.show("C3 no costs", engine.simulate(mkt, sig, {"tp_r": 2.0}, max_open=4, fill="pess", fee=0, slip_in=0, slip_out=0, use_funding=False))
