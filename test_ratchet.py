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
    # to the ratchet retunes the test instead of breaking it. Only the arming
    # threshold is parametrised; the rung-arithmetic cases below are pinned to
    # explicit numbers on purpose, since that is the arithmetic being checked.
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

    # 2b. The point of arming below TP_R (2026-08-02): a trade that runs most of
    #     the way to target and then reverses must exit in PROFIT rather than at
    #     a full stop. Peak 0.9R, then back through the entry.
    runner = _trade()
    _run(100.9, runner)
    ok &= check("sub-TP peak leaves a profitable stop",
                runner["sl"] > 100.0 and runner["locked_r"] >= start,
                "sl=%.4f locked=%s" % (runner["sl"], runner["locked_r"]))

    # 3. Rung arithmetic: 1.6R -> int(0.6/0.25)=2 rungs -> locked 1.5R.
    f, t = _run(101.6, _trade())
    ok &= check("1.6R locks 1.5R", t["locked_r"] == 1.5, "got %s" % t["locked_r"])

    # 4. Short side mirrors: entry 100, R 1, price 98.4 = 1.6R -> stop 98.5.
    f, t = _run(98.4, _trade(d=-1))
    ok &= check("short 1.6R locks 1.5R",
                t["locked_r"] == 1.5 and abs(t["sl"] - 98.5) < 1e-9)

    # 5. The stop must never retreat. Arm at 2R, then poll a pullback to 1.2R.
    armed = _trade()
    _run(102.0, armed)
    f, t = _run(101.2, armed)
    ok &= check("stop never retreats", not f.calls and abs(t["sl"] - 102.0) < 1e-9)

    # 6. On a placement failure the recorded stop must NOT advance, so the next
    #    poll retries -- update_sl has already cancelled the TP and the old stop
    #    by then, so believing a failed move succeeded leaves it naked forever.
    f, t = _run(101.6, _trade(), fail_times=1)
    ok &= check("failure leaves sl unmoved", t["sl"] == 99.0 and t["locked_r"] == 0.0)
    ok &= check("failure flags for alert", t.get("naked_alerted") is True)

    # 7. And the retry actually re-protects it.
    tr = _trade()
    _run(101.6, tr, fail_times=1)
    f, t = _run(101.6, tr)
    ok &= check("retry re-protects", len(f.calls) == 1 and t["locked_r"] == 1.5
                and "naked_alerted" not in t)

    live._open_trades.clear()
    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
