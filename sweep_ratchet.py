"""One-off analysis: re-derive TRAIL_START_R / TRAIL_STEP_R on real prices.

strategy2.py's ratchet comment cites +628.5% / +1358.6% / maxDD -4.23% / 8-of-8
positive months. Those are Heikin-Ashi-path numbers and are void (2026-07-30).
2026-07-30 also established that the entry has no edge at a fixed 1R exit
(-2.82%) and the ratchet is the entire product -- so these two constants are the
highest-stakes unvalidated numbers in the system.

Same rejection tests the project applies to everything else: out-of-sample
split, robustness to neighbouring values, and ex-top5 (is it a tail artifact).
Run in the capped portfolio model, not backtest2 -- the arming threshold changes
holding time and therefore concurrency.
"""
import portfolio2 as p2
import strategy2 as s2

BARS = 5000

print("loading " + str(len(s2.WATCHLIST)) + " coins x " + str(BARS) + " bars ...")
bars_by_coin, cand = p2.load(bars=BARS)
print("loaded " + str(len(bars_by_coin)) + " coins")
print()

print("=== DRIFT CHECK: deployed row (expect n~116 net ~+36% t~2.1) ===")
base = p2.simulate(bars_by_coin, cand)
print(p2.summarize(base, "deployed 1.0/0.25"))
print()

print("=== TRAIL_START_R sweep (step fixed at 0.25) ===")
for st in (0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.25, 1.5, 2.0):
    tr = p2.simulate(bars_by_coin, cand, trail_start_r=st)
    print(p2.summarize(tr, "  start=" + str(st)))
print()

print("=== TRAIL_STEP_R sweep (start fixed at 1.0) ===")
for sp in (0.1, 0.25, 0.5, 0.75, 1.0):
    tr = p2.simulate(bars_by_coin, cand, trail_step_r=sp)
    print(p2.summarize(tr, "  step=" + str(sp)))
print()

print("=== 2-D grid: net acct % / EV per trade ===")
starts = (0.5, 0.6, 0.75, 1.0, 1.25)
steps = (0.1, 0.25, 0.5)
hdr = "start\\step".ljust(12)
for sp in steps:
    hdr += str(sp).rjust(18)
print(hdr)
for st in starts:
    line = str(st).ljust(12)
    for sp in steps:
        tr = p2.simulate(bars_by_coin, cand, trail_start_r=st, trail_step_r=sp)
        d = p2.frame(tr)
        line += (format(d["acct"].sum(), "+8.1f") + "/"
                 + format(d["acct"].mean(), "+.3f")).rjust(18)
    print(line)
print()

print("=== OUT-OF-SAMPLE on the candidates ===")
for st in (0.5, 0.6, 0.75, 1.0):
    for line in p2.out_of_sample(
            p2.simulate(bars_by_coin, cand, trail_start_r=st), "start=" + str(st)):
        print(line)
    print()

print("=== MFE distribution: how many trades die between 0.5R and 1.0R? ===")
# Re-run the deployed config and record peak favourable excursion per trade on
# the same bar-close basis the ratchet uses.
import pandas as pd

by_ts = dict()
for c, lst in cand.items():
    for sg in lst:
        if sg["adx_v"] < s2.MAX_ADX:
            by_ts.setdefault(sg["ts"], []).append((c, sg))
timeline = sorted(set().union(*[set(d.index) for d in bars_by_coin.values()]))
posmap = dict((c, dict((t, i) for i, t in enumerate(d.index)))
              for c, d in bars_by_coin.items())
open_p, mfes = dict(), []
for ts in timeline:
    for c in list(open_p):
        t = open_p[c]
        pm = posmap[c].get(ts)
        if pm is None:
            continue
        row = bars_by_coin[c].iloc[pm]
        d, R = t["direction"], t["R"]
        hit = (row["low"] <= t["sl"]) if d == 1 else (row["high"] >= t["sl"])
        best_r = (row["high"] - t["entry"]) * d / R if d == 1 else (t["entry"] - row["low"]) / R
        t["mfe"] = max(t["mfe"], best_r)
        if hit:
            mfes.append(dict(coin=c, mfe=t["mfe"], scaled=t["scaled"]))
            del open_p[c]
            continue
        mfe_r = (row["close"] - t["entry"]) * d / R
        if mfe_r >= 1.0:
            t["scaled"] = True
            rung = int((mfe_r - 1.0) / 0.25)
            locked = 1.0 + rung * 0.25
            new_sl = t["entry"] + d * locked * R
            if (new_sl > t["sl"]) if d == 1 else (new_sl < t["sl"]):
                t["sl"], t["locked_r"] = new_sl, locked
    for c, sg in by_ts.get(ts, []):
        if len(open_p) >= s2.MAX_TRADES or c in open_p:
            continue
        t = dict(sg)
        t["coin"], t["R"] = c, abs(sg["entry"] - sg["sl"])
        t["scaled"], t["locked_r"], t["mfe"] = False, 0.0, 0.0
        open_p[c] = t
m = pd.DataFrame(mfes)
print("n=" + str(len(m)))
for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 99)):
    sub = m[(m["mfe"] >= lo) & (m["mfe"] < hi)]
    print("  MFE " + str(lo) + "-" + str(hi) + "R: n=" + str(len(sub)).ljust(5)
          + " (" + format(len(sub) / len(m) * 100, "4.1f") + "% of trades)")
losers = m[~m["scaled"]]
print("  of the " + str(len(losers)) + " that never armed, peak MFE reached:")
print("    median " + format(losers["mfe"].median(), ".2f") + "R"
      + "   >=0.5R: " + str(int((losers["mfe"] >= 0.5).sum()))
      + "   >=0.75R: " + str(int((losers["mfe"] >= 0.75).sum())))
