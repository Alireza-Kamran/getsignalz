"""Behavioural test for the blind-bot detector (live._check_scan_stale).

Importing live.py attaches a FileHandler to the real bot.log, and on the first
run this test wrote four phantom ERROR lines straight into the operational log
-- the same class of mistake as the 2026-08-24 test suite that corrupted live
state. Detach every handler before touching anything, and stub tg so no
Telegram message is ever sent.
"""
import sys
import time

sys.path.insert(0, "/root/trade")
import live

live.logger.remove()      # loguru: drops the stdout sink AND the bot.log sink

sent = []
live.tg.dm_owner = lambda m: sent.append(m)


def reset(stale_sec, quiet_hour=False, open_trades=None):
    sent.clear()
    live._scan_alerted = False
    live._last_scan_ok = time.time() - stale_sec
    live._open_trades = open_trades or {}
    live.should_quiet = (lambda h: True) if quiet_hour else (lambda h: False)


fails = 0


def check(label, cond):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails += 1


print("1. fresh scan -> silent")
reset(60)
live._check_scan_stale()
check("no DM", not sent)
check("not latched", live._scan_alerted is False)

print("2. just under threshold -> silent")
reset(live.SCAN_STALE_ALERT_SEC - 30)
live._check_scan_stale()
check("no DM", not sent)

print("3. over threshold, flat book -> one DM, latched")
reset(live.SCAN_STALE_ALERT_SEC + 60)
live._check_scan_stale()
check("one DM", len(sent) == 1)
check("says blind", "blind" in sent[0].lower())
check("says flat", "flat" in sent[0].lower())
check("latched", live._scan_alerted is True)

print("4. still stale -> does NOT re-alert (once per episode)")
live._check_scan_stale()
live._check_scan_stale()
check("still one DM", len(sent) == 1)

print("5. recovery -> clears latch and DMs once")
sent.clear()
live._mark_scan_ok()
check("recovery DM", len(sent) == 1 and "recovered" in sent[0].lower())
check("unlatched", live._scan_alerted is False)
sent.clear()
live._mark_scan_ok()
check("no repeat recovery DM", not sent)

print("6. over threshold with open position -> names it, warns ratchet frozen")
reset(live.SCAN_STALE_ALERT_SEC + 3600, open_trades={"ETH": {}, "SOL": {}})
live._check_scan_stale()
check("one DM", len(sent) == 1)
check("names ETH", "ETH" in sent[0])
check("names SOL", "SOL" in sent[0])
check("mentions ratchet", "ratchet" in sent[0].lower())

print("7. quiet hours -> suppressed even when very stale")
reset(live.SCAN_STALE_ALERT_SEC + 7200, quiet_hour=True)
live._check_scan_stale()
check("no DM", not sent)
check("not latched", live._scan_alerted is False)

print("8. tg failure must not propagate")
reset(live.SCAN_STALE_ALERT_SEC + 60)


def boom(m):
    raise RuntimeError("telegram down")


live.tg.dm_owner = boom
try:
    live._check_scan_stale()
    check("swallowed", True)
except Exception as e:
    check(f"swallowed (raised {e})", False)


# ── Scheduled scan-free windows must not age into alerts ────────────────────
# Cases 9-12 lock the 2026-08-31 regression: the detector counted quiet hours
# and the off-session `continue` as staleness, so it cried wolf every night.
from datetime import datetime, timezone

import review

live.tg.dm_owner = lambda m: sent.append(m)  # case 8 left this raising
live.should_quiet = review.should_quiet      # the real 02:00-04:00 UTC gate
_real_time = live.time


class _FakeTime:
    """Only `live`'s own time lookups are redirected; the module is restored."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, _s):
        pass


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


print("9. quiet-hours discount is exact")
span = live._unscheduled_stale_seconds(at(2026, 8, 30, 1, 0), at(2026, 8, 30, 4, 30))
check(f"3.5h span minus 2h quiet = 1.5h (got {span/3600:.3f}h)", abs(span - 5400) < 1)
none = live._unscheduled_stale_seconds(at(2026, 8, 30, 2, 10), at(2026, 8, 30, 3, 50))
check(f"fully inside quiet -> 0 (got {none:.0f}s)", abs(none) < 1)
plain = live._unscheduled_stale_seconds(at(2026, 8, 30, 12, 0), at(2026, 8, 30, 15, 0))
check(f"no quiet overlap -> unchanged (got {plain/3600:.2f}h)", abs(plain - 10800) < 1)
check("reversed window -> 0", live._unscheduled_stale_seconds(at(2026, 8, 30, 5, 0),
                                                              at(2026, 8, 30, 4, 0)) == 0.0)

print("10. the nightly 04:00 false alarm is gone")
# Exactly the 2026-08-30 log: scanned 01:00:20, checked 04:00:46, raw stale 3.0h.
try:
    live.time = _FakeTime(at(2026, 8, 30, 4, 0, 46))
    sent.clear()
    live._scan_alerted = False
    live._open_trades = {}
    live._last_scan_ok = at(2026, 8, 30, 1, 0, 20)
    raw = (at(2026, 8, 30, 4, 0, 46) - live._last_scan_ok)
    check(f"raw stale really does exceed the limit ({raw/3600:.2f}h)",
          raw > live.SCAN_STALE_ALERT_SEC)
    live._check_scan_stale()
    check("no DM after discounting quiet hours", not sent)
    check("not latched", live._scan_alerted is False)

    print("11. a REAL outage spanning quiet hours still alerts")
    # 2026-08-27: last scan 00:01, API dead through the morning.
    live.time = _FakeTime(at(2026, 8, 27, 4, 30))
    sent.clear()
    live._scan_alerted = False
    live._last_scan_ok = at(2026, 8, 27, 0, 1)
    live._check_scan_stale()
    check("one DM", len(sent) == 1)
    check("says blind", sent and "blind" in sent[0].lower())
finally:
    live.time = _real_time

print("12. the scan marker sits ABOVE the off-session gate in run()")
# Source-order check, not a behavioural one: the bug was that `_mark_scan_ok()`
# lived only below `if not in_session(h): continue`, which returns to the top of
# the loop for 11h a night (00:00-11:00 UTC). No amount of stubbing catches an
# ordering mistake, so assert the ordering.
src = open("/root/trade/live.py").read()
i_mark = src.find("if states:\n                _mark_scan_ok()")
i_gate = src.find("if not in_session(h):")
check("marker present after the scan loop", i_mark != -1)
check("session gate present", i_gate != -1)
check("marker precedes the off-session continue", -1 < i_mark < i_gate)

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
