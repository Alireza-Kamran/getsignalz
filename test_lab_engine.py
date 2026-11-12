"""Research-lab engine: the fill rules every backtest number depends on.

Synthetic bars only -- no network, no cache. Each check pins one rule that a
past backtest in this project got wrong and paid for.
"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/trade")
from lab import engine, validate as v
from lab.strats import common, trend

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", name)


def bars(rows, start="2024-01-01", freq="1h"):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def one(df, i, d, dist, exit, **kw):
    sig = pd.DataFrame([{"ts": df.index[i], "coin": "X", "dir": d, "stop_dist": dist}])
    kw.setdefault("fee", 0); kw.setdefault("slip_in", 0); kw.setdefault("slip_out", 0)
    return engine.simulate(engine.Market({"X": df}), sig, exit, use_funding=False, **kw)


flat = [100, 100.5, 99.5, 100]

# 1. entry is the NEXT bar's open, never the signal bar's close
df = bars([flat, [100, 100.5, 99.5, 100], [103, 103.5, 102.5, 103], [103, 111, 102.5, 110], flat])
t = one(df, 1, 1, 2.0, {"tp_r": 3.0})
check("entry at next open", t.iloc[0]["entry"] == 103.0)
check("entry timestamp is the next bar", t.iloc[0]["entry_ts"] == df.index[2])
check("target fills at the target", abs(t.iloc[0]["r_gross"] - 3.0) < 1e-9 and t.iloc[0]["reason"] == "tp")

# 2. a bar that touches stop AND target is a loss
df = bars([flat, flat, [100, 110, 97, 105], flat])
t = one(df, 0, 1, 2.0, {"tp_r": 2.0})          # entry bar 1 @100, stop 98, tp 104
t2 = one(df, 0, 1, 2.0, {"tp_r": 2.0})
check("stop tested before target", t.iloc[0]["reason"] == "sl" and abs(t.iloc[0]["r_gross"] + 1.0) < 1e-9)

# 3. a gap through the stop fills at the open, not at the stop
df = bars([flat, flat, [95, 96, 94, 95], flat])
t = one(df, 0, 1, 2.0, {})
check("gap fills at the open", t.iloc[0]["reason"] == "gap" and abs(t.iloc[0]["r_gross"] + 2.5) < 1e-9)
t = one(df, 0, 1, 2.0, {}, gap_fill=False)
check("gap_fill=False reproduces the legacy stop fill", abs(t.iloc[0]["r_gross"] + 1.0) < 1e-9)

# 4. short side mirrors
df = bars([flat, flat, [100, 103, 99, 102], flat])
t = one(df, 0, -1, 2.0, {"tp_r": 2.0})
check("short stop", t.iloc[0]["reason"] == "sl" and abs(t.iloc[0]["r_gross"] + 1.0) < 1e-9)

# 5. costs: charged in R on the actual fill, doubled by cost_mult
df = bars([flat, flat, [100, 100.5, 97, 98], flat])
t = one(df, 0, 1, 2.0, {}, fee=0.001, slip_in=0.0005, slip_out=0.0005)
want = (2 * 0.001 + 0.001) * 100 / 2.0
check("cost in R", abs(t.iloc[0]["cost_r"] - want) < 1e-12)
check("net = gross - cost", abs(t.iloc[0]["r"] - (t.iloc[0]["r_gross"] - want)) < 1e-12)
t2 = one(df, 0, 1, 2.0, {}, fee=0.001, slip_in=0.0005, slip_out=0.0005, cost_mult=2)
check("cost_mult doubles cost", abs(t2.iloc[0]["cost_r"] - 2 * want) < 1e-12)

# 6. a freshly raised ratchet stop has no immunity under pess
#    entry 100, R=2. bar runs to +2.6R (105.2) and its low is 100.5; arms at 2.5R -> stop 105
df = bars([flat, [100, 105.2, 100.5, 104.0], [104, 104.5, 103.5, 104], flat, flat])
rat = {"ratchet": {"start_r": 2.5, "step_r": 0.25}}
tp = one(df, 0, 1, 2.0, rat, fill="pess")
tm = one(df, 0, 1, 2.0, rat, fill="mid")
to = one(df, 0, 1, 2.0, rat, fill="opt")
check("pess: stopped on the arming bar at 2.5R", tp.iloc[0]["bars"] == 1 and abs(tp.iloc[0]["r_gross"] - 2.5) < 1e-9)
check("mid: close below the new stop also exits", tm.iloc[0]["bars"] == 1)
check("opt: survives to the next bar", to.iloc[0]["bars"] == 2)

# 7. concurrency cap, and a freed slot is reusable
a = bars([flat] * 3 + [[100, 100.5, 97, 98]] + [flat] * 4)       # A stops out on bar 3
b = bars([flat] * 8)
mkt = engine.Market({"A": a, "B": b})
sig = pd.DataFrame([{"ts": a.index[0], "coin": "A", "dir": 1, "stop_dist": 2.0, "rank": 2},
                    {"ts": a.index[0], "coin": "B", "dir": 1, "stop_dist": 20.0, "rank": 1},
                    {"ts": a.index[1], "coin": "B", "dir": 1, "stop_dist": 20.0, "rank": 1},
                    {"ts": a.index[3], "coin": "B", "dir": 1, "stop_dist": 20.0, "rank": 1}])
t = engine.simulate(mkt, sig, {"max_bars": 2}, max_open=1, fee=0, slip_in=0, slip_out=0, use_funding=False)
check("cap=1 takes the higher-ranked signal", list(t["coin"]) == ["A", "B"])
check("cap frees when the first trade exits", t.iloc[1]["signal_ts"] == a.index[3])
check("skips are counted, not silent", t.attrs["skipped"]["book_full"] == 2)

# 8. funding: a long pays positive funding, a short receives it
df = bars([flat] * 6)
f = pd.Series([0.001, 0.001], index=[df.index[2], df.index[3]])
mk = engine.Market({"X": df}, {"X": f})
sg = lambda d: pd.DataFrame([{"ts": df.index[0], "coin": "X", "dir": d, "stop_dist": 2.0}])
tl = engine.simulate(mk, sg(1), {"max_bars": 4}, fee=0, slip_in=0, slip_out=0)
ts_ = engine.simulate(mk, sg(-1), {"max_bars": 4}, fee=0, slip_in=0, slip_out=0)
check("long pays funding", abs(tl.iloc[0]["fund_r"] + 0.002 * 100 / 2.0) < 1e-12)
check("short receives funding", abs(ts_.iloc[0]["fund_r"] - 0.002 * 100 / 2.0) < 1e-12)

# 9. no look-ahead in signals: cutting the future must not change the past
rng = np.random.default_rng(3)
c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 600)))
o = np.r_[c[0], c[:-1]]
full = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995,
                     "close": c}, index=pd.date_range("2022-01-01", periods=600, freq="1D"))
cut = full.iloc[:400]
s_full = trend.signals({"X": full}, lookback=40)
s_cut = trend.signals({"X": cut}, lookback=40)
s_full = s_full[s_full["ts"] <= cut.index[-1]].reset_index(drop=True)
check("trend signals unchanged by future data",
      len(s_full) == len(s_cut) and len(s_cut) > 0 and
      (s_full["ts"].values == s_cut["ts"].values).all() and
      np.allclose(s_full["stop_dist"], s_cut["stop_dist"]))

# 10. a higher-timeframe value is not visible before its bar has closed
h = pd.DataFrame(index=pd.date_range("2024-01-01", periods=72, freq="1h"))
daily = pd.Series([1.0, 2.0, 3.0], index=pd.date_range("2024-01-01", periods=3, freq="1D"))
st = common.htf_state(h, daily)
check("day 1 value invisible during day 1", st.iloc[:24].isna().all())
check("day 1 value visible from day 2 open", st.iloc[24] == 1.0 and st.iloc[47] == 1.0)
check("day 2 value visible from day 3 open", st.iloc[48] == 2.0)

# 11. the holdout cannot be read by accident
tr = pd.DataFrame({"entry_ts": pd.to_datetime(["2023-05-01", "2025-05-01", "2026-05-01"]), "r": [1, 1, 1]})
check("train split", len(v.split(tr, "train")) == 1)
check("validate split", len(v.split(tr, "validate")) == 1)
for bad, kw in (("all", {}), ("holdout", {})):
    try:
        v.split(tr, bad, **kw)
        check("%s is refused" % bad, False)
    except ValueError:
        check("%s is refused" % bad, True)
old = v.HOLDOUT_LOG
v.HOLDOUT_LOG = os.path.join(tempfile.mkdtemp(), "log.txt")     # redirect the resource, not the caller
check("holdout with a reason is allowed and logged",
      len(v.split(tr, "holdout", reason="unit test")) == 1 and "unit test" in open(v.HOLDOUT_LOG).read())
v.HOLDOUT_LOG = old

# 12. stats
check("max drawdown of +1,-2,-1,+5", v.max_dd([1, -2, -1, 5]) == -3.0)
check("deflated t shrinks with trials", v.deflated_t(3.0, 40) < 3.0 and v.deflated_t(3.0, 1) == 3.0)

print("test_lab_engine: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
