"""
Does the live ratchet behave like the model every S2 parameter was tuned on?

portfolio2.simulate drives the ratchet off the bar CLOSE and, having raised the
stop at the end of a bar, only tests that stop against the NEXT bar. Its
docstring calls close-driving "the conservative proxy for a level price
actually held long enough for a 20s poll to act on". That is conservative about
WHEN the stop arms. It is not conservative about whether the new stop SURVIVES,
and that is the half that matters.

live._check_trail_s2 polls the mid every ~20s and, the instant price prints
TRAIL_START_R, places the stop at exactly TRAIL_START_R -- i.e. essentially AT
the market. The first live trade to arm proved the point: BTC 2026-08-03 armed
at +0.75R at 13:49:56 and was filled 22 seconds later at +0.706R. The model
would have needed a full 1h bar to close above 0.75R before arming, and could
not have stopped out until the following bar.

So the deployed exit is not the exit that was measured, and the difference cuts
against the tail -- which is where this strategy's entire EV lives.

Because 1h OHLC cannot recover the intrabar path, this reports BOUNDS, not an
estimate, the same discipline sweep_tp.py uses:

  close       -- portfolio2 exactly. Asserted equal in __main__; without that
                 check the rows below would be measuring a different model.
  touch_opt   -- ratchet driven by the bar's FAVOURABLE extreme, stop never
                 tested against the same bar. Upper bound on tick-driving:
                 arms early AND always survives.
  touch_pess  -- ratchet driven by the favourable extreme, then the new stop is
                 tested against the SAME bar's adverse extreme. On the arming
                 bar the lock is pinned to TRAIL_START_R however far the bar
                 ran, because the first touch is what arms it. Lower bound.

Live sits between touch_opt and touch_pess and cannot be outside them. Whether
it sits near the good end is not knowable from 1h bars -- which is the point of
quoting a range instead of a number.
"""
import sys

import pandas as pd

import portfolio2 as p2
import strategy2 as s2
from backtest2 import TAKER_FEE

MODES = ("close", "touch_opt", "touch_pess")


def simulate(bars_by_coin, cand, max_adx=None, max_trades=None,
             trail_start_r=None, trail_step_r=None, rank="stretch",
             mode="close", trail_gap_r=0.0):
    """portfolio2.simulate with a selectable ratchet-timing model.

    Entry selection, the concurrency cap, ranking and the no-lookahead rule are
    byte-for-byte the same; only the exit block below differs.

    trail_gap_r > 0 forbids the stop from ever being placed within that many R
    of the current excursion. It is the direct fix for the at-market placement
    the tick-driven models expose: arming is delayed until the move has run
    trail_start_r + trail_gap_r, and the stop still lands at trail_start_r, so
    it starts life with a real cushion instead of on top of the price.
    """
    if mode not in MODES:
        raise ValueError("mode must be one of " + str(MODES))
    max_adx = s2.MAX_ADX if max_adx is None else max_adx
    max_trades = s2.MAX_TRADES if max_trades is None else max_trades
    tsr = s2.TRAIL_START_R if trail_start_r is None else trail_start_r
    tstep = s2.TRAIL_STEP_R if trail_step_r is None else trail_step_r

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

            def _stopped():
                return (row["low"] <= t["sl"]) if d == 1 else (row["high"] >= t["sl"])

            def _close_out():
                t["total_r"] = (t["locked_r"] if t["scaled"]
                                else (t["sl"] - t["entry"]) * d / R)
                t["result"] = "trail" if t["scaled"] else "sl"
                t["exit"], t["close_ts"] = t["sl"], ts
                closed.append(t)
                del open_p[c]

            # The resting stop is always tested before anything moves it.
            if _stopped():
                _close_out()
                continue

            if mode == "close":
                mfe_r = (row["close"] - t["entry"]) * d / R
            else:
                ext = row["high"] if d == 1 else row["low"]
                mfe_r = (ext - t["entry"]) * d / R

            if mfe_r >= tsr + trail_gap_r:
                first_arm = not t["scaled"]
                if mode == "touch_pess" and first_arm:
                    # The first print of the arming level is what arms it, so
                    # the stop lands there no matter how far the bar ran on.
                    locked = tsr
                else:
                    rung = int((mfe_r - tsr) / tstep)
                    locked = tsr + rung * tstep
                if trail_gap_r:
                    locked = min(locked, mfe_r - trail_gap_r)
                if locked < tsr:
                    continue
                t["scaled"] = True
                new_sl = t["entry"] + d * locked * R
                if (new_sl > t["sl"]) if d == 1 else (new_sl < t["sl"]):
                    t["sl"], t["locked_r"] = new_sl, locked
                    # Tick-driving places the stop at (or within one rung of)
                    # the market, so the same bar can still take it out.
                    if mode == "touch_pess" and _stopped():
                        _close_out()
                        continue

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


def r_profile(trades, label=""):
    """Where the exits land in R. The tail is the whole strategy, so show it."""
    df = pd.DataFrame(trades)
    r = df["total_r"]
    return (label.ljust(26)
            + "n=" + str(len(r)).ljust(5)
            + "sumR=" + format(r.sum(), "+8.2f")
            + "  meanR=" + format(r.mean(), "+.3f")
            + "  >=1.5R=" + str(int((r >= 1.5).sum())).ljust(4)
            + "  >=3R=" + str(int((r >= 3).sum())).ljust(4)
            + "  maxR=" + format(r.max(), "+6.2f"))


if __name__ == "__main__":
    coins = sys.argv[1:] or s2.WATCHLIST
    bars_by_coin, cand = p2.load(coins)

    # Without this the rows below could be measuring a different model.
    a = simulate(bars_by_coin, cand, mode="close",
                 trail_start_r=s2.TRAIL_START_R, trail_step_r=s2.TRAIL_STEP_R)
    b = p2.simulate(bars_by_coin, cand,
                    trail_start_r=s2.TRAIL_START_R, trail_step_r=s2.TRAIL_STEP_R)
    assert len(a) == len(b), (len(a), len(b))
    for x, y in zip(a, b):
        assert x["coin"] == y["coin"] and x["open_ts"] == y["open_ts"], (x, y)
        assert abs(x["total_r"] - y["total_r"]) < 1e-9, (x["coin"], x["total_r"], y["total_r"])
    print("equivalence check vs portfolio2.simulate at mode=close: OK  (n=" + str(len(a)) + ")")
    print()

    print("=== ratchet timing model — " + str(len(bars_by_coin))
          + " coins, cap " + str(s2.MAX_TRADES) + ", TRAIL_START_R="
          + str(s2.TRAIL_START_R) + ", real prices, fees in ===")
    print()
    runs = dict()
    for m in MODES:
        runs[m] = simulate(bars_by_coin, cand, mode=m)
        print(p2.summarize(runs[m], m))
    print()
    for m in MODES:
        print(r_profile(runs[m], m))

    print()
    print("out-of-sample, pessimistic bound:")
    for line in p2.out_of_sample(runs["touch_pess"], "touch_pess"):
        print(line)

    print()
    # The grid has to CONTAIN the deployed value, or this table is sensitivity
    # analysis for a system nobody is running. It was hardcoded 0.50-1.50, which
    # silently stopped covering TRAIL_START_R the moment it went to 2.50 on
    # 2026-08-05 -- the identical failure test_ratchet.py was fixed for on
    # 2026-08-19, where pinned rung prices encoded an old threshold. Derive the
    # range from the live constant so it cannot drift out from under it again.
    live_tsr = s2.TRAIL_START_R
    tsr_grid = sorted(set(round(live_tsr + k * 0.5, 2) for k in (-3, -2, -1, 0, 1))
                      | {live_tsr})
    tsr_grid = [g for g in tsr_grid if g >= 0.25]
    assert live_tsr in tsr_grid, (live_tsr, tsr_grid)

    print("TRAIL_START_R sensitivity under each model (net acct %):")
    print("  (live TRAIL_START_R = " + format(live_tsr, ".2f") + ")")
    print("  tsr    " + "".join(m.ljust(14) for m in MODES))
    for tsr in tsr_grid:
        cells = []
        for m in MODES:
            tr = simulate(bars_by_coin, cand, trail_start_r=tsr, mode=m)
            cells.append(format(p2.frame(tr)["acct"].sum(), "+.2f").ljust(14))
        print("  " + format(tsr, ".2f").ljust(7) + "".join(cells))

    print()
    print("trail_gap_r -- forbid the stop from resting within gap R of price:")
    for tsr in sorted({live_tsr, round(live_tsr - 0.5, 2)}):
        print("  TRAIL_START_R=" + format(tsr, ".2f"))
        for gap in (0.0, 0.125, 0.25, 0.375, 0.50):
            row = "    gap=" + format(gap, ".3f").ljust(8)
            for m in ("touch_opt", "touch_pess"):
                tr = simulate(bars_by_coin, cand, trail_start_r=tsr,
                              mode=m, trail_gap_r=gap)
                f = p2.frame(tr)
                row += (m + "=" + format(f["acct"].sum(), "+7.2f")
                        + "/EV" + format(f["acct"].mean(), "+.3f")
                        + "/WR" + format((f["acct"] > 0).mean() * 100, "4.0f") + "  ")
            print(row)
