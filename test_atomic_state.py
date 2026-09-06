#!/usr/bin/env python3
"""state.json must survive a process that dies mid-write.

WHY THIS FILE EXISTS
--------------------
Every state file was written as:

    with open(path, "w") as f:
        json.dump(obj, f)

open(path, "w") truncates before a byte of new content is written, so the window
between truncate and flush is a window in which the file is EMPTY. A crash, an
OOM kill, `systemctl restart` or a full disk inside that window leaves the file
empty or half-written -- and state.json holds every open position, the entire
closed_trades list and the stats.

This has already happened: test_null_record.py records that the OP trade of
2026-08-13 was "rebuilt by an ad-hoc repair script after save_state erased it".

The second half of the failure was tracker.load_state(): a corrupt read fell
through to the empty skeleton, which reads to every caller as "no open positions
and no history", so the bot would open new trades on top of forgotten ones and
rewrite the stats from zero. A file that cannot be read must be loud.
"""
import json, os, shutil, sys, tempfile
sys.path.insert(0, "/root/trade")

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}")


import io_safe

_LIVE = "/root/trade/state.json"
_LIVE_BEFORE = open(_LIVE).read()          # this test must not touch the book
TMP = tempfile.mkdtemp(prefix="atomic_test_")
P = os.path.join(TMP, "state.json")

BOOK = {"closed_trades": [{"coin": "ETH", "pnl_usd": 14.21}] * 40,
        "tracked": {"BTC": {"sl": 99.0}}, "stats": {"total": 19}}

print("\n── the happy path still writes what it was given ──")
io_safe.atomic_write_json(P, BOOK)
check("file parses", json.load(open(P)) == BOOK)
check("no .tmp left behind", not [f for f in os.listdir(TMP) if f.endswith(".tmp")])

print("\n── a write that dies partway leaves the PREVIOUS file intact ──")
real_replace = os.replace
os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("killed mid-write"))
try:
    io_safe.atomic_write_json(P, {"closed_trades": [], "stats": {"total": 0}})
except OSError:
    pass
finally:
    os.replace = real_replace
on_disk = json.load(open(P))
check("state.json still parses after the failed write", isinstance(on_disk, dict))
check("and still holds all 40 trades, not the empty replacement",
      len(on_disk["closed_trades"]) == 40)
check("no .tmp turd left behind", not [f for f in os.listdir(TMP) if f.endswith(".tmp")])

print("\n── an unserialisable object fails BEFORE the target is touched ──")
class Boom:
    pass
try:
    io_safe.atomic_write_json(P, {"x": Boom()}, default=None)
except TypeError:
    pass
check("the old file is untouched by a serialisation failure",
      len(json.load(open(P))["closed_trades"]) == 40)

print("\n── a torn primary is recovered from the .bak generation ──")
io_safe.atomic_write_json(P, BOOK)                  # writes .bak from the previous
open(P, "w").write('{"closed_trades": [{"coin": "ET')   # simulate a torn write
obj, source = io_safe.read_json_with_fallback(P)
check("recovered from backup", source == "backup")
check("and the recovered book has trades in it", len(obj["closed_trades"]) == 40)

print("\n── load_state is LOUD about an unreadable book, never silently empty ──")
import tracker
orig_state_f = tracker.STATE_F
try:
    tracker.STATE_F = P
    open(P, "w").write("{ truncated")
    open(P + ".bak", "w").write("{ also truncated")
    raised = False
    try:
        tracker.load_state()
    except RuntimeError:
        raised = True
    check("both files corrupt -> raises rather than returning a skeleton", raised)

    os.remove(P); os.remove(P + ".bak")
    fresh = tracker.load_state()
    check("a genuinely ABSENT file still yields the empty skeleton",
          fresh.get("closed_trades") == [] and fresh.get("stats", {}).get("total") == 0)

    io_safe.atomic_write_json(P, BOOK)
    check("a healthy file round-trips through load_state",
          tracker.load_state()["stats"]["total"] == 19)
finally:
    tracker.STATE_F = orig_state_f
    shutil.rmtree(TMP, ignore_errors=True)

print("\n── the live book is untouched by this test ──")
check("state.json byte-identical to before the run", open(_LIVE).read() == _LIVE_BEFORE)

print(f"\ntest_atomic_state: {PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
