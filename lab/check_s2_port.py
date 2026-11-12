"""Is lab.strats.meanrev the same rule as strategy2.signal?

Feeds both the SAME Hyperliquid mainnet bars and compares the signal sets, then
shows what each fill assumption does to the result on that window. This reads
bars from the holdout period, for PORT VALIDATION OF THE RETIRED BASELINE ONLY
-- no candidate is selected on it -- and says so in holdout_log.txt.
"""
import sys
import pandas as pd
sys.path.insert(0, "/root/trade")
import indicators
indicators.BASE_URL = indicators.MAINNET_URL
import portfolio2, strategy2 as s2
from lab import engine, validate as v
from lab.strats import meanrev

with open(v.HOLDOUT_LOG, "a") as f:
    f.write("%s  check_s2_port: S2 port validation vs legacy on HL bars (baseline only, no selection)\n"
            % pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

bars, cand = portfolio2.load()
legacy = {(c, sg["ts"], sg["direction"]) for c, l in cand.items() for sg in l if sg["adx_v"] < s2.MAX_ADX}
frames = {c: d[["open", "high", "low", "close"]] for c, d in bars.items()}
sig = meanrev.signals(frames, legacy_lev_filter=True)
sig_all = meanrev.signals(frames)
# skip each coin's first 400 bars: the legacy frame starts its EMA/RSI recursion
# from a longer history than the trimmed frame handed to the lab features
cut = {c: d.index[400] for c, d in frames.items()}
lab = {(r.coin, r.ts, r.dir) for r in sig.itertuples() if r.ts >= cut[r.coin]}
leg = {k for k in legacy if k[1] >= cut[k[0]]}
print("signals after warm-up: legacy=%d lab=%d both=%d only_legacy=%d only_lab=%d" % (
    len(leg), len(lab), len(leg & lab), len(leg - lab), len(lab - leg)))

mkt = engine.Market(frames)
EXIT = {"ratchet": {"start_r": 2.5, "step_r": 0.25}, "tp_r": 5.0}
print(v.HEAD)
runs = [("legacy model (close,no gap,no cost)", dict(entry="close", gap_fill=False, fee=0, slip_in=0, slip_out=0)),
        ("+ next-open entry", dict(gap_fill=False, fee=0, slip_in=0, slip_out=0)),
        ("+ gap fills", dict(fee=0, slip_in=0, slip_out=0)),
        ("+ costs (lab standard)", dict())]
for label, kw in runs:
    tr = engine.simulate(mkt, sig, EXIT, max_open=2, fill="pess", use_funding=False, **kw)
    print(v.line(label, v.summarize(tr)))
tr = engine.simulate(mkt, sig_all, EXIT, max_open=2, fill="pess", use_funding=False)
print(v.line("all signals (no rounding bug)", v.summarize(tr)))
