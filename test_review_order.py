"""Pins the 2026-09-02 fix: blocking maintenance must not sit above position
management, and the review latches must be windows guarded by a once-per-period
flag set BEFORE the call.

Two kinds of assertion here, deliberately:

  * BEHAVIOURAL -- should_nightly_review / should_weekly_review are pure
    functions of the clock and can be tested directly.

  * SOURCE-ORDER -- the actual defect was an ORDERING one inside run()'s while
    loop, and no amount of stubbing catches that: a stubbed nightly_review()
    returns instantly, so a test with stubs passes just as happily with the
    review above the ratchet as below it. The only way to assert "A runs before
    B" for a blocking call is to read the source. Same technique as
    test_scan_stale.py's marker-precedes-the-continue check (2026-08-31).

Run: python3 test_review_order.py
"""
import re
import sys

sys.path.insert(0, "/root/trade")

from review import should_nightly_review, should_weekly_review

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


# ── Source under test ────────────────────────────────────────────────────────
# Full-line comments are stripped FIRST. The fix being pinned here is documented
# by a comment block that names nightly_review(), _check_trail_s2() and the rest
# in prose -- and that block sits, by design, ABOVE the ratchet it describes. A
# naive .find() therefore matched the explanation instead of the call and the
# ordering assertions failed against correct source. Same trap as the 2026-09-01
# session-failure detector that matched the report discussing the strings it
# grepped for: AN INSTRUMENT MUST NOT MATCH ITS OWN DOCUMENTATION.
# Inline (trailing) comments are left alone -- none of the needles below appear
# in one, and stripping them would risk mangling a '#' inside a string literal.
SRC = open("/root/trade/live.py").read()
LOOP = "\n".join(ln for ln in SRC[SRC.index("def run()"):].splitlines()
                 if not ln.lstrip().startswith("#"))


def pos(needle, label):
    """Index of `needle` within run(); records a failure if absent."""
    i = LOOP.find(needle)
    check(f"{label}: {needle!r} present in run()", i != -1)
    return i if i != -1 else 10 ** 9


# ── 1. Nightly latch is a window, not an instant ─────────────────────────────
for minute in range(20, 30):
    check(f"nightly fires at 23:{minute:02d}", should_nightly_review(23, minute))
for minute in (0, 5, 19, 30, 59):
    check(f"nightly silent at 23:{minute:02d}", not should_nightly_review(23, minute))
for hour in (0, 4, 22, 21):
    check(f"nightly silent at {hour:02d}:20", not should_nightly_review(hour, 20))

# The window is still ten minutes wide. POLL=20s plus a pass's network calls can
# step over any single minute, which is the race the window exists to close; its
# width is the guarantee, its position is not.
width = sum(1 for m in range(60) if should_nightly_review(23, m))
check(f"nightly window is 10 min wide (got {width})", width == 10)

# REPLACES "nightly still fires at exactly 23:00" (2026-09-02..2026-09-06).
# That assertion pinned the window's START on the rule "widening may not move
# the start, only its end" -- correct for a widening, but this is a RELOCATION
# and the start is exactly what had to move. The review is the last blocking
# work in the loop and at 23:00 it blocked the 23:00 candle scan: p90 19.4 min
# late over 38 nights, 14.0 min last night, against ~9s every other hour.
# NOTE (2026-09-16): "since position management moved above it the ratchet is
# safe" was written here and in live.py, and it was wrong -- the reorder runs
# the ratchet once BEFORE the review and not again until it returns. The
# review blocked the loop 125 min across 12 position-crossing nights between
# 09-03 and 09-14 (analyze REVIEW BLOCKING). What makes the ratchet safe is
# the brain-wait keepalive pinned by test_brain_keepalive.py; the ordering
# below is still required, it is just not sufficient.
# The invariant that actually matters is the one below: nightly must clear the
# candle scan it used to block, and must not collide with the weekly.
check("nightly starts after the candle scan can finish (>=23:10)",
      not any(should_nightly_review(23, m) for m in range(0, 10)))

# ── 2. Weekly latch is a window, not an instant ──────────────────────────────
for minute in (30, 31, 45, 59):
    check(f"weekly fires Sun 23:{minute:02d}", should_weekly_review(6, 23, minute))
for minute in (0, 15, 29):
    check(f"weekly silent Sun 23:{minute:02d}", not should_weekly_review(6, 23, minute))
for wd in (0, 3, 5):
    check(f"weekly silent on weekday {wd}", not should_weekly_review(wd, 23, 30))
check("weekly silent at 22:30 Sun", not should_weekly_review(6, 22, 30))
check("weekly still fires at exactly Sun 23:30", should_weekly_review(6, 23, 30))

# ── 3. Nightly and weekly windows do not overlap ─────────────────────────────
# If they did, one pass could trigger both and the weekly would run against a
# loop already blocked by the nightly.
overlap = [m for m in range(60)
           if should_nightly_review(23, m) and should_weekly_review(6, 23, m)]
check(f"nightly/weekly windows disjoint (overlap={overlap})", not overlap)

# ── 4. SOURCE ORDER: position management precedes all blocking maintenance ───
i_fetch   = pos("positions   = get_positions()", "fetch")
i_closed  = pos("_check_closed(positions, account_val)", "closed-detect")
i_trail   = pos("_check_trail_s2(positions, mids=mids)", "ratchet")
i_version = pos("version_push()", "version push")
i_nightly = pos("nightly_review()", "nightly review")
i_weekly  = pos("weekly_review()", "weekly review")

check("prices fetched before closed-trade detection", i_fetch < i_closed)
check("closed-trade detection before the ratchet", i_closed < i_trail)

# THE regression this file exists for. nightly_review() runs ai_brain and
# _self_improve() inline; with it above the ratchet, _check_trail_s2 did not
# execute for a measured 27.7 min on 2026-08-23 with an ETH SHORT open.
check("RATCHET runs before nightly review", i_trail < i_nightly)
check("RATCHET runs before weekly review", i_trail < i_weekly)
check("RATCHET runs before version push", i_trail < i_version)
check("closed-trade detection before nightly review", i_closed < i_nightly)

# ── 5. SOURCE ORDER: the once-per-period latch is set BEFORE the call ────────
# This is what makes the widened window in (1) and (2) safe. If the flag were
# assigned after the review returned, every pass inside the 10-minute window
# would start another review.
i_nflag = pos("_nightly_done = now_utc.date()", "nightly latch")
i_wflag = pos("_weekly_done = now_utc.isocalendar()[1]", "weekly latch")
check("_nightly_done set BEFORE nightly_review()", i_nflag < i_nightly)
check("_weekly_done set BEFORE weekly_review()", i_wflag < i_weekly)

# ── 5b. The latch must SURVIVE A RESTART, not just the loop ─────────────────
# review._self_improve() ends in os.execv() (review.py:410) -- an in-place
# restart fired from INSIDE nightly_review(), during the window that decides
# whether to run it. A module global resets there. With the old minute==0 latch
# that was harmless (the restart landed outside the window); with a 10-minute
# window it is a re-entry bug: review -> exec at 23:03 -> empty latch -> minute 3
# is still inside the window -> review again.
i_persist = pos("_save_review_latches(_nightly_done, _weekly_done)",
                "latch persisted")
i_restore = pos("_nightly_done, _weekly_done = _load_review_latches()",
                "latch restored at startup")
check("latch persisted BEFORE nightly_review() (execv kills it otherwise)",
      i_persist < i_nightly)
check("latch restored at startup before the loop", i_restore < i_nightly)

# The latch file must not live in state.json: tracker.save_state is a non-atomic
# read-modify-write that has already silently erased a tracked position.
check("latch has its own file, not state.json",
      "REVIEW_LATCH_FILE" in SRC and ".review_latch" in SRC)

# Round-trip the real helpers, including the corrupt-file path -- a latch that
# raises on bad input would take the whole loop down at 23:00.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("_livemod", "/root/trade/live.py")
try:
    import datetime as _dtm
    import tempfile, os as _os, json as _json
    _lv = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_lv)
    _tmp = tempfile.mkdtemp()
    _lv.REVIEW_LATCH_FILE = _os.path.join(_tmp, ".review_latch")

    check("missing latch file returns (None, None)",
          _lv._load_review_latches() == (None, None))

    _d = _dtm.date(2026, 9, 2)
    _lv._save_review_latches(_d, 36)
    check("latch round-trips through disk",
          _lv._load_review_latches() == (_d, 36))

    with open(_lv.REVIEW_LATCH_FILE, "w") as _fh:
        _fh.write("{not json")
    check("corrupt latch file degrades to (None, None), does not raise",
          _lv._load_review_latches() == (None, None))

    _lv._save_review_latches(None, None)
    check("null latch round-trips", _lv._load_review_latches() == (None, None))
except Exception as _e:                                   # pragma: no cover
    check(f"latch helpers importable and round-trip (got {_e!r})", False)

# ── 6. The scan still sits below the review (documented, not yet fixed) ──────
# SCAN_STALE_ALERT_SEC=8100 is sized to clear the nightly review as the longest
# legitimate scan-free stretch. If someone moves the scan above the review, that
# budget silently becomes ~70 min too generous and the blind-bot detector goes
# half-blind. Fail loudly here so that change is made deliberately, with the
# threshold re-derived at the same time.
i_scan = pos("now_ts = _candle_ts()", "candle scan")
check("candle scan still BELOW nightly review "
      "(else re-derive SCAN_STALE_ALERT_SEC)", i_nightly < i_scan)

# ── Report ───────────────────────────────────────────────────────────────────
total = PASSED + len(FAILED)
print(f"test_review_order: {PASSED}/{total} passed")
for f in FAILED:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILED else 0)
