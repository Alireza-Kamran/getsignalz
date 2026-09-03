#!/usr/bin/env python3
"""One malformed trade record must not take down an instrument.

WHY THIS FILE EXISTS
--------------------
On 2026-09-03 a SINGLE record -- OP 2026-08-13, rebuilt by an ad-hoc repair
script after save_state erased it, and flagged `reconstructed_from_journal` --
was simultaneously:

  1. aborting the AVAILABILITY open-position cross-reference,
  2. killing the ENTIRE LOOP LATENCY section (added the night before,
     specifically to answer that night's primary question),
  3. printing a FALSE "INVARIANT VIOLATED" against a textbook stop-out, and
  4. holding the owner's pinned Telegram dashboard dead for 15 hours,

because it differed from every production record in exactly two ways: its
timestamps carried "+00:00" while all 34 others are naive, and its excursion
fields were null rather than absent.

The defect was never in the analysis. It was that four readers each assumed the
data could only take the shape the happy path writes. The repair script left no
trace in the CHANGELOG, so nothing connected the four symptoms to one cause.

These tests are shaped around the DATA, not the functions: a record with the
awkward shape goes in, and every reader must survive it and say something true.
"""
import sys, json, copy
sys.path.insert(0, "/root/trade")

from datetime import datetime
import analyze

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


# ── The awkward record, in the exact shape the repair script wrote ──────────
POISON = {
    "coin": "OP", "dir": 1, "entry": 0.08591, "exit": 0.08405,
    "sl": 0.0842099, "sl_orig": 0.0842099, "tp": 0.0944105,
    "size": 4018.1, "leverage": 20, "result": "sl", "strategy": "S2",
    "lev_pct": -43.301, "raw_pct": -2.1651, "pnl_usd": -7.47, "rr": -1.094,
    "opened_at": "2026-08-13T00:01:48.976252+00:00",   # tz-AWARE
    "closed_at": "2026-08-13T02:40:17.943248+00:00",   # tz-AWARE
    "max_adverse_pct": None,                           # NULL, not absent
    "peak_roe_pct": None,
    "reconstructed_from_journal": True,
}
HEALTHY = {
    "coin": "AAVE", "dir": -1, "entry": 134.6454, "exit": 126.0,
    "sl": 137.2533, "sl_orig": 137.2533, "tp": 118.9936,
    "size": 2.62, "leverage": 20, "result": "trail", "strategy": "S2",
    "lev_pct": 128.4, "raw_pct": 6.42, "pnl_usd": 40.0, "rr": 2.974,
    "opened_at": "2026-09-02T06:01:32.041814",         # naive
    "closed_at": "2026-09-02T09:47:34.569686",
    "max_adverse_pct": -12.0, "peak_roe_pct": 135.0, "locked_r": 3.0,
}
STATE = {"closed_trades": [HEALTHY, POISON], "tracked": {}}


print("── _naive_utc: every shape this system writes ──")
cases = [
    ("naive",        "2026-08-13T00:01:48.976252",        datetime(2026, 8, 13, 0, 1, 48, 976252)),
    ("offset +00:00","2026-08-13T00:01:48.976252+00:00",  datetime(2026, 8, 13, 0, 1, 48, 976252)),
    ("Z suffix",     "2026-08-13T00:01:48.976252Z",       datetime(2026, 8, 13, 0, 1, 48, 976252)),
    ("date only",    "2026-08-13",                        datetime(2026, 8, 13, 0, 0)),
]
for lab, raw, want in cases:
    got = analyze._naive_utc(raw)
    check(f"{lab:<14} -> naive UTC", got == want)
    check(f"{lab:<14} carries no tzinfo", got is not None and got.tzinfo is None)
for lab, raw in [("None", None), ("empty", ""), ("garbage", "not-a-date"),
                 ("wrong type", 12345.6)]:
    check(f"{lab:<14} -> None, not a raise", analyze._naive_utc(raw) is None)

# The precise regression: the OLD code did .replace("Z",""), which strips a Z
# suffix but leaves "+00:00" intact, so fromisoformat returned an AWARE object
# and any comparison against a naive gap raised TypeError.
print("\n── the exact comparison that used to raise ──")
try:
    gap_a = datetime(2026, 8, 13, 0, 0)
    gap_b = datetime(2026, 8, 13, 3, 0)
    out = analyze._open_during(gap_a, gap_b, STATE)
    check("_open_during survives an aware-timestamp record", True)
    check("and still FINDS the position that was open in the gap",
          any(c == "OP" for c, _s, _o in out))
except TypeError as e:
    check(f"_open_during survives an aware-timestamp record ({e})", False)
    check("and still FINDS the position that was open in the gap", False)

check("mixed naive+aware book does not lose the healthy record",
      any(c == "AAVE" for c, _s, _o in
          analyze._open_during(datetime(2026, 9, 2, 6, 0),
                               datetime(2026, 9, 2, 10, 0), STATE)))

# A record so broken it cannot be parsed must be SKIPPED, never fatal --
# the whole point is that one row cannot decide what the report may measure.
print("\n── an unparseable row is skipped, not fatal ──")
junk = dict(POISON, opened_at="???", closed_at="???")
try:
    analyze._open_during(datetime(2026, 8, 13), datetime(2026, 8, 14),
                         {"closed_trades": [HEALTHY, junk], "tracked": {}})
    check("unparseable opened_at is skipped", True)
except Exception as e:
    check(f"unparseable opened_at is skipped ({e})", False)


print("\n── null excursion is NOT zero excursion ──")
exc = analyze._excursion_stats(STATE)
by = {x["coin"]: x for x in exc}
check("the null-excursion trade is still PRESENT (not dropped)", "OP" in by)
check("its MFE is None, not 0.0", by.get("OP", {}).get("mfe_r") is None)
check("its MAE is None, not 0.0", by.get("OP", {}).get("mae_r") is None)
check("its realised R survives intact (-1.09R)",
      abs(by.get("OP", {}).get("real_r", 0) - (-1.094)) < 0.02)
check("the healthy trade still measures normally",
      by.get("AAVE", {}).get("mfe_r") is not None)

# The false positive this produced: MAE 0.00 <= realised -1.09 is FALSE, so a
# routine stop-out was reported as a broken invariant for weeks.
print("\n── the false invariant violation does not come back ──")
rep = analyze.full_report()
check("report mentions OP's excursion was never recorded",
      "excursion never recorded" in rep or "NOT RECORDED" in rep)
check("no INVARIANT VIOLATED banner on a clean book",
      "INVARIANT VIOLATED" not in rep)
check("the excursion section did not silently drop a trade",
      "MFE   n/a" in rep)
check("LOOP LATENCY section actually rendered",
      "LOOP LATENCY (candle start" in rep)
check("LOOP LATENCY did not fall over",
      "latency check failed" not in rep)
check("AVAILABILITY did not fall over",
      "availability check failed" not in rep)


print("\n── stall cause: the venue's fault vs ours ──")
due  = datetime(2026, 9, 2, 7, 0)
wall = datetime(2026, 9, 2, 7, 28)
errs = [datetime(2026, 9, 2, 7, 5), datetime(2026, 9, 2, 7, 20)]
cause, n = analyze._stall_cause(due, wall, errs)
check("API errors inside the stall  -> venue down", cause == "venue down")
check("and the error count is reported", n == 2)
cause, n = analyze._stall_cause(due, wall, [datetime(2026, 9, 1, 3, 0)])
check("errors OUTSIDE the stall     -> self-blocked", cause == "self-blocked")
check("no errors at all             -> self-blocked",
      analyze._stall_cause(due, wall, [])[0] == "self-blocked")
# Boundary: an error exactly at either edge still belongs to the stall.
check("error exactly at the stall start counts",
      analyze._stall_cause(due, wall, [due])[0] == "venue down")
check("error exactly at the stall end counts",
      analyze._stall_cause(due, wall, [wall])[0] == "venue down")


print("\n── the pinned dashboard renders on the awkward book ──")
import tracker
try:
    txt = tracker._dashboard_text(STATE, {}, current_balance=689.28)
    check("_dashboard_text survives a null max_adverse_pct", True)
    check("and produced a real message", isinstance(txt, str) and len(txt) > 200)
except TypeError as e:
    check(f"_dashboard_text survives a null max_adverse_pct ({e})", False)
    check("and produced a real message", False)

# The averaged drawdown must come from the record that HAS one, and must not be
# diluted by counting the null as a zero.
try:
    txt = tracker._dashboard_text(STATE, {}, current_balance=689.28)
    check("avg adverse uses only the measured record (-12.0)", "12.0" in txt)
except Exception:
    check("avg adverse uses only the measured record (-12.0)", False)


print("\n── the live book is untouched by this test ──")
live = json.load(open("/root/trade/state.json"))
check("state.json still parses", isinstance(live.get("closed_trades"), list))
check("state.json trade count unchanged", len(live["closed_trades"]) == 18)

print(f"\ntest_null_record: {PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
