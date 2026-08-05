"""Unit test for the strategy-2 stop ratchet.

This is the code path that carries the entire measured edge of strategy 2: on
real prices the fixed-TP variant of the same signal is -2.82% over 121 trades
while the ratcheted one is +29.97% (see the 2026-07-30 nightly entry). It had
never executed live -- the only live trade so far stopped out before reaching
1R -- so it was the largest untested surface in the system.

Run: python3 test_ratchet.py
"""
import sys
sys.path.insert(0, "/root/trade")

import live
import strategy2

# Importing live attaches its bot.log sink, so an unmuted test writes fake
# "[S2] ETH stop -> +1R" and "ratchet failed" lines straight into the real
# trading log -- exactly what the 2026-07-29 watchdog test did. A later session
# grepping bot.log cannot tell those from real events. Drop every sink first.
live.logger.remove()


class FakeSL(object):
    """Stands in for executor.update_sl, recording how it was called."""

    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    def __call__(self, coin, direction, sz, new_sl, entry=None):
        self.calls.append(dict(coin=coin, direction=direction, sz=sz,
                               new_sl=new_sl, entry=entry))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("SL placement rejected")


def _trade(entry=100.0, R=1.0, d=1):
    return dict(strategy="S2", dir=d, entry=entry, R=R, size=10.0,
                sl=entry - d * R, locked_r=0.0)


def _run(price, trade, coin="ETH", fail_times=0):
    live._open_trades.clear()
    live._open_trades[coin] = trade
    fake = FakeSL(fail_times)
    real_sl, real_dm = live.update_sl, live.tg.dm_owner
    live.update_sl = fake
    live.tg.dm_owner = lambda *a, **k: None
    try:
        live._check_trail_s2(dict([(coin, dict())]), mids=dict([(coin, price)]))
    finally:
        live.update_sl, live.tg.dm_owner = real_sl, real_dm
    return fake, live._open_trades[coin]


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("%-4s %s %s" % (mark, name, detail))
    return cond


def main():
    # Derived from the live constants rather than hard-coded, so a future change
    # to the ratchet retunes the test instead of breaking it.
    #
    # This used to say the rung-arithmetic cases below were "pinned to explicit
    # numbers on purpose, since that is the arithmetic being checked". That was
    # wrong, and TRAIL_START_R 1.00 -> 2.50 on 2026-08-05 broke five of them: the
    # arithmetic under test is "0.6R past the threshold buys two 0.25R rungs",
    # which holds at ANY threshold. Pinning the price to 1.6R silently encoded
    # TRAIL_START_R == 1.0 -- the same failure case 2b was already fixed for.
    # Everything below is now expressed as an offset from `start`.
    start, step = strategy2.TRAIL_START_R, strategy2.TRAIL_STEP_R
    assert step == 0.25, "rung cases below encode a 0.25R step"
    ok = True

    # 1. Below the arming threshold the ratchet must not arm -- the original
    #    stop stays, and the resting take-profit must not be cancelled.
    f, t = _run(100.0 + (start - 0.1), _trade())
    ok &= check("no arm below %sR" % start, not f.calls and t["sl"] == 99.0)

    # 2. At exactly the threshold it arms, locks there, and calls update_sl
    #    WITHOUT entry so the TP is cancelled -- leaving it in place would close
    #    the trade at TP_R and the ratchet would never do anything.
    f, t = _run(100.0 + start, _trade())
    ok &= check("arms at %sR" % start, len(f.calls) == 1 and t["locked_r"] == start
                and abs(t["sl"] - (100.0 + start)) < 1e-9)
    ok &= check("cancels TP (entry=None)", f.calls and f.calls[0]["entry"] is None)

    # 2b. A trade that peaks between the arming threshold and TP_R -- i.e. it
    #     runs but never reaches the backstop -- must exit in PROFIT rather than
    #     at a full stop. Derived from the constants, not hard-coded: this case
    #     used to pin the peak at 0.9R, which silently encoded TRAIL_START_R
    #     being below 0.9 and broke the moment it went back to 1.0 on 2026-08-04.
    peak = start + (strategy2.TP_R - start) / 2.0
    runner = _trade()
    _run(100.0 + peak, runner)
    ok &= check("sub-TP peak (%.2fR) leaves a profitable stop" % peak,
                runner["sl"] > 100.0 and start <= runner["locked_r"] <= peak,
                "sl=%.4f locked=%s" % (runner["sl"], runner["locked_r"]))

    # 3. Rung arithmetic: 0.6R past the threshold -> int(0.6/0.25)=2 rungs.
    two_rungs = start + 0.5
    f, t = _run(100.0 + start + 0.6, _trade())
    ok &= check("%.2fR locks %.2fR" % (start + 0.6, two_rungs),
                abs(t["locked_r"] - two_rungs) < 1e-9, "got %s" % t["locked_r"])

    # 4. Short side mirrors it exactly.
    f, t = _run(100.0 - (start + 0.6), _trade(d=-1))
    ok &= check("short %.2fR locks %.2fR" % (start + 0.6, two_rungs),
                abs(t["locked_r"] - two_rungs) < 1e-9
                and abs(t["sl"] - (100.0 - two_rungs)) < 1e-9)

    # 5. The stop must never retreat: arm high, then poll a pullback.
    armed = _trade()
    _run(100.0 + start + 1.0, armed)
    f, t = _run(100.0 + start + 0.2, armed)
    ok &= check("stop never retreats",
                not f.calls and abs(t["sl"] - (100.0 + start + 1.0)) < 1e-9)

    # 6. On a placement failure the recorded stop must NOT advance, so the next
    #    poll retries -- update_sl has already cancelled the TP and the old stop
    #    by then, so believing a failed move succeeded leaves it naked forever.
    f, t = _run(100.0 + start + 0.6, _trade(), fail_times=1)
    ok &= check("failure leaves sl unmoved", t["sl"] == 99.0 and t["locked_r"] == 0.0)
    ok &= check("failure flags for alert", t.get("naked_alerted") is True)

    # 7. And the retry actually re-protects it.
    tr = _trade()
    _run(100.0 + start + 0.6, tr, fail_times=1)
    f, t = _run(100.0 + start + 0.6, tr)
    ok &= check("retry re-protects",
                len(f.calls) == 1 and abs(tr["locked_r"] - two_rungs) < 1e-9
                and "naked_alerted" not in t)

    live._open_trades.clear()
    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
