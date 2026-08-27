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

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
