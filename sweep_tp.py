"""
Re-derivation of strategy2.TP_R on real prices — the last constant whose
justification was Heikin-Ashi-path and void (see the 2026-07-30 bug and the
2026-08-02 TRAIL_START_R re-derivation).

WHY THIS NEEDED A NEW TOOL RATHER THAN A portfolio2 SWEEP
---------------------------------------------------------
portfolio2.simulate does not model the take-profit AT ALL. It only models the
stop and the ratchet, i.e. it assumes the resting TP is always cancelled before
it can fill. That assumption is why TP_R could not simply be swept there: the
parameter is invisible to that model.

Live it is not invisible. The resting TP sits at TP_R (1.0R) and the ratchet
arms at TRAIL_START_R (0.75R) and only then cancels it. The ratchet is driven by
a ~20s poll, so it almost always wins the race -- but a move that travels from
0.75R to 1.0R inside a single poll window fills the TP first and caps the trade
at exactly 1R. That is a real leak, and it lands on precisely the fast, strongly
trending trades the ratchet exists to harvest.

BOUNDS, NOT AN ESTIMATE
-----------------------
The true outcome depends on the intrabar path, which 1h OHLC cannot recover. So
this measures both ends and reports the interval:

  optimistic  (tp_r=None) -- the ratchet always wins the race, TP never fills.
                             This is exactly portfolio2's model, and the number
                             every TRAIL_START_R decision was made against.
  pessimistic (tp_r=X)    -- any bar that touches the TP level while the trade
                             is not yet armed AT A PRIOR BAR CLOSE fills the TP.

A 1h bar contains ~180 polls, so reality sits very close to the optimistic end.
The pessimistic run is a worst case, and its job is to bound the damage.

Same conventions as portfolio2, deliberately: real exchange OHLC (never the HA
path), fees in, capped book, ratchet on bar close, stop-before-TP on same-bar
ambiguity.

Run:  python3 sweep_tp.py
"""
import pandas as pd

import portfolio2 as p2
import strategy2 as s2
from portfolio2 import summarize, frame


def simulate_tp(bars_by_coin, cand, tp_r=None, max_adx=None, max_trades=None,
                trail_start_r=None, trail_step_r=None, rank="stretch"):
    """portfolio2.simulate plus an optional resting take-profit.

    tp_r=None reproduces portfolio2.simulate exactly (verified in __main__).
    Any float models a resting TP at that many R which is cancelled the moment
    the ratchet arms -- the live semantics of _check_trail_s2 calling update_sl
    without `entry`.
    """
    max_adx = s2.MAX_ADX if max_adx is None else max_adx
    max_trades = s2.MAX_TRADES if max_trades is None else max_trades
    trail_start_r = s2.TRAIL_START_R if trail_start_r is None else trail_start_r
    trail_step_r = s2.TRAIL_STEP_R if trail_step_r is None else trail_step_r

    by_ts = dict()
    for c, lst in cand.items():
        for sg in lst:
            if sg["adx_v"] < max_adx:
                by_ts.setdefault(sg["ts"], []).append((c, sg))

    timeline = sorted(set().union(*[set(d.index) for d in bars_by_coin.values()]))
    posmap = dict((c, dict((t, i) for i, t in enumerate(d.index)))
                  for c, d in bars_by_coin.items())

    open_p, closed = dict(), []
    for ts in timeline:
        for c in list(open_p):
            t = open_p[c]
            pm = posmap[c].get(ts)
            if pm is None:
                continue
            row = bars_by_coin[c].iloc[pm]
            d, R = t["direction"], t["R"]

            # Stop first: same-bar ambiguity resolves against us, as everywhere
            # else in this project's simulators.
            hit = (row["low"] <= t["sl"]) if d == 1 else (row["high"] >= t["sl"])
            if hit:
                t["total_r"] = (t["locked_r"] if t["scaled"]
                                else (t["sl"] - t["entry"]) * d / R)
                t["result"] = "trail" if t["scaled"] else "sl"
                t["exit"], t["close_ts"] = t["sl"], ts
                closed.append(t)
                del open_p[c]
                continue

            # The resting TP only exists while the ratchet has not yet armed.
            if tp_r is not None and not t["scaled"]:
                tp_px = t["entry"] + d * tp_r * R
                got = (row["high"] >= tp_px) if d == 1 else (row["low"] <= tp_px)
                if got:
                    t["total_r"], t["result"] = tp_r, "tp"
                    t["exit"], t["close_ts"] = tp_px, ts
                    closed.append(t)
                    del open_p[c]
                    continue

            mfe_r = (row["close"] - t["entry"]) * d / R
            if mfe_r >= trail_start_r:
                t["scaled"] = True
                rung = int((mfe_r - trail_start_r) / trail_step_r)
                locked = trail_start_r + rung * trail_step_r
                new_sl = t["entry"] + d * locked * R
                if (new_sl > t["sl"]) if d == 1 else (new_sl < t["sl"]):
                    t["sl"], t["locked_r"] = new_sl, locked

        pool = by_ts.get(ts, [])
        pool = (sorted(pool, key=lambda x: -abs(x[1]["stretch"])) if rank == "stretch"
                else sorted(pool, key=lambda x: x[0]))
        for c, sg in pool:
            if len(open_p) >= max_trades or c in open_p:
                continue
            t = dict(sg)
            t["coin"], t["open_ts"] = c, ts
            t["sl_orig"], t["R"] = sg["sl"], abs(sg["entry"] - sg["sl"])
            t["scaled"], t["locked_r"] = False, 0.0
            open_p[c] = t
    return closed


def leak_report(bars_by_coin, cand, tp_r, **kw):
    """How much the resting TP costs, trade by trade, in the worst case.

    Pairs each TP-capped trade with what the same trade did when the TP was
    absent, which is the counterfactual the deployed model assumes.
    """
    with_tp = simulate_tp(bars_by_coin, cand, tp_r=tp_r, **kw)
    without = simulate_tp(bars_by_coin, cand, tp_r=None, **kw)
    base = dict(((t["coin"], t["open_ts"]), t) for t in without)

    rows = []
    for t in with_tp:
        if t.get("result") != "tp":
            continue
        b = base.get((t["coin"], t["open_ts"]))
        if b is not None:
            rows.append(dict(coin=t["coin"], opened=t["open_ts"],
                             capped_r=t["total_r"], would_have_r=b["total_r"],
                             give_up_r=b["total_r"] - t["total_r"]))
    return pd.DataFrame(rows), with_tp, without


if __name__ == "__main__":
    bars_by_coin, cand = p2.load()
    print("=== TP_R re-derivation — " + str(len(bars_by_coin))
          + " coins, cap " + str(s2.MAX_TRADES) + ", real prices, fees in ===")
    print("    TRAIL_START_R=" + str(s2.TRAIL_START_R)
          + "  TRAIL_STEP_R=" + str(s2.TRAIL_STEP_R)
          + "  MAX_ADX=" + str(s2.MAX_ADX))
    print()

    # Equivalence check: tp_r=None must reproduce portfolio2.simulate exactly,
    # otherwise this tool is measuring something else and nothing below counts.
    a = simulate_tp(bars_by_coin, cand, tp_r=None,
                    trail_start_r=s2.TRAIL_START_R)
    b = p2.simulate(bars_by_coin, cand, trail_start_r=s2.TRAIL_START_R,
                    trail_step_r=s2.TRAIL_STEP_R)
    same = (len(a) == len(b)
            and all(abs(x["total_r"] - y["total_r"]) < 1e-9
                    for x, y in zip(a, b)))
    print("equivalence vs portfolio2.simulate (tp_r=None): "
          + ("OK" if same else "MISMATCH — STOP, tool is wrong"))
    print()

    print("WORST CASE — every touch of the TP level fills before the ratchet arms:")
    for v in (0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0):
        print(summarize(simulate_tp(bars_by_coin, cand, tp_r=v),
                        "  TP_R=" + str(v)))
    print(summarize(simulate_tp(bars_by_coin, cand, tp_r=None),
                    "  TP_R=none (best case)"))
    print()

    print("Leakage detail at the deployed TP_R=1.0:")
    lk, _, _ = leak_report(bars_by_coin, cand, 1.0)
    if len(lk):
        print("  trades capped by the resting TP: " + str(len(lk)))
        print("  total R given up (worst case):   "
              + format(lk["give_up_r"].sum(), "+.2f") + "R")
        print(lk.sort_values("give_up_r", ascending=False).head(12).to_string(index=False))
    else:
        print("  none")
    print()

    print("Out-of-sample split, worst case, at a few settings:")
    for v in (1.0, 2.0, None):
        for line in p2.out_of_sample(simulate_tp(bars_by_coin, cand, tp_r=v),
                                     "TP_R=" + str(v)):
            print("  " + line)
        print()
