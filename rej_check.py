"""Rejection tests for the TRAIL_START_R change. One-off, safe to delete."""
import pandas as pd

import portfolio2 as p2

import time

for attempt in range(5):
    try:
        b, c = p2.load(bars=5000)
        break
    except Exception as e:
        print("load failed (" + str(e)[:60] + ") — retry " + str(attempt + 1))
        time.sleep(15)
print("=== REJECTION TESTS: TRAIL_START_R 0.75 vs deployed 1.0 ===")
print()
print("-- independence from the |stretch| ranking (alphabetical selection) --")
for st in (0.75, 1.0):
    print(p2.summarize(p2.simulate(b, c, trail_start_r=st, rank="alpha"),
                       "  alpha start=" + str(st)))
print()
print("-- independence from the concurrency cap --")
for cap in (1, 2, 4):
    for st in (0.75, 1.0):
        print(p2.summarize(p2.simulate(b, c, trail_start_r=st, max_trades=cap),
                           "  cap=" + str(cap) + " start=" + str(st)))
print()
print("-- fine grid around the candidate --")
for st in (0.65, 0.7, 0.75, 0.8, 0.85):
    print(p2.summarize(p2.simulate(b, c, trail_start_r=st), "  start=" + str(st)))
print()
print("-- monthly, deployed vs candidate --")
for st in (1.0, 0.75):
    d = p2.frame(p2.simulate(b, c, trail_start_r=st))
    m = d.groupby(pd.to_datetime(d["open_ts"]).dt.to_period("M"))["acct"].agg(["sum", "count"])
    print(" start=" + str(st) + ":  " + "  ".join(
        str(k) + " " + format(v["sum"], "+.1f") + "%(" + str(int(v["count"])) + ")"
        for k, v in m.iterrows()))
    print("   positive months: " + str(int((m["sum"] > 0).sum())) + "/" + str(len(m)))
