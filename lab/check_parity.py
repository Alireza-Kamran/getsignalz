"""
Parity check: lab.engine must reproduce sweep_trail_mode.simulate exactly.

sweep_trail_mode.py is the model this project's deployed exit parameters were
derived on. Before the lab's engine is trusted with anything new it has to
return the SAME trades, with the SAME R, when configured the legacy way
(fill at the signal close, no gap fills, no costs). If the two disagree, one
of them is wrong, and that has to be known before either is quoted.

Run:  /opt/bots-venv/bin/python -m lab.check_parity
Needs the network (Hyperliquid mainnet candles). Not part of run_tests.py.
"""
import sys

import pandas as pd

sys.path.insert(0, "/root/trade")

import indicators
indicators.BASE_URL = indicators.MAINNET_URL      # real prices, before any fetch

import portfolio2
import strategy2 as s2
import sweep_trail_mode as stm

from lab import engine


def main(coins=None):
    bars_by_coin, cand = portfolio2.load(coins=coins)
    ok = True
    for mode, fill, drive in (("touch_pess", "pess", None),
                              ("touch_opt", "opt", None),
                              ("close", "opt", "close")):
        ref = stm.simulate(bars_by_coin, cand, mode=mode)
        ref = pd.DataFrame([{"coin": t["coin"], "ts": t["open_ts"],
                             "r": t["total_r"]} for t in ref])

        sig = pd.DataFrame([{"ts": sg["ts"], "coin": c, "dir": sg["direction"],
                             "stop": sg["sl"], "rank": abs(sg["stretch"])}
                            for c, lst in cand.items() for sg in lst
                            if sg["adx_v"] < s2.MAX_ADX])
        mkt = engine.Market(bars_by_coin)
        rat = {"start_r": s2.TRAIL_START_R, "step_r": s2.TRAIL_STEP_R}
        if drive:
            rat["drive"] = drive
        got = engine.simulate(mkt, sig, {"ratchet": rat}, max_open=s2.MAX_TRADES,
                              fill=fill, entry="close", gap_fill=False,
                              fee=0, slip_in=0, slip_out=0, use_funding=False)
        got = got[got["reason"] != "eod"]

        j = ref.merge(got[["coin", "signal_ts", "r_gross"]], how="outer",
                      left_on=["coin", "ts"], right_on=["coin", "signal_ts"],
                      indicator=True)
        only_ref = (j["_merge"] == "left_only").sum()
        only_lab = (j["_merge"] == "right_only").sum()
        both = j[j["_merge"] == "both"]
        worst = (both["r"] - both["r_gross"]).abs().max() if len(both) else 0.0
        good = only_ref == 0 and only_lab == 0 and worst < 1e-9
        ok &= good
        print("%-10s legacy n=%d sumR=%+.3f | lab n=%d sumR=%+.3f | "
              "only_legacy=%d only_lab=%d max|dR|=%.2e  %s" % (
                  mode, len(ref), ref["r"].sum(), len(got), got["r_gross"].sum(),
                  only_ref, only_lab, worst, "OK" if good else "MISMATCH"))
        if not good:
            print(j[(j["_merge"] != "both") |
                    ((j["r"] - j["r_gross"]).abs() > 1e-9)].head(12).to_string())
    print("PARITY", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
