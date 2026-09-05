"""Pins the 2026-09-05 fixes: the observability log must cover the coins the
LIVE engine trades, the report must not discard trades silently, and a process
restart must not be reported as a loop stall.

The defect being pinned is one instance of a pattern this repo keeps finding
(see the `stale_instruments` note): an instrument silently drifts off the live
constants and then reports confidently about a universe it cannot see.

  live.py's scan loop iterated `WATCHLIST`, imported from trader.py -- the
  RETIRED S1 list of 12 -- while the engine trades strategy2.WATCHLIST (20).
  Eight live coins never printed an RSI/ADX line, so ENTRY GATE CENSUS and
  FEED HEALTH were blind to them, and REALISED R BY FEED QUALITY dropped 6 of
  19 closed trades on a criterion unrelated to feed quality.

Three kinds of assertion here, deliberately:

  * BEHAVIOURAL -- _feed_quality_split, _scan_census and _loop_latency are pure
    functions of their inputs and are driven directly with synthetic data.

  * FORMAT -- the log line live.py emits must be parseable by the regex
    analyze.py mines it with. These live in two files and drifted before; a
    round-trip assertion is the only thing that keeps them in step.

  * SOURCE-ORDER -- "the observability pass must run BELOW the entry decision"
    cannot be observed from outside: both orderings produce identical output,
    they differ only in how many seconds of signal-to-fill drift they add. As
    in test_review_order.py, the only way to assert it is to read the source.
    Comments are stripped FIRST -- the block being pinned is documented by a
    comment that names the very symbols the assertions search for, and AN
    INSTRUMENT MUST NOT MATCH ITS OWN DOCUMENTATION.

Run: python3 test_scan_coverage.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, "/root/trade")

import analyze
import strategy2
import trader

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def _strip_comments(src):
    """Full-line comments and docstring prose removed, so source-order
    assertions match CODE and never the explanation above it."""
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


LIVE_SRC = _strip_comments(open("/root/trade/live.py").read())


# ── 1. The watchlists genuinely differ (the premise of the whole fix) ────────
s1, s2 = set(trader.WATCHLIST), set(strategy2.WATCHLIST)
check("S2 watchlist is strictly larger than S1's", s2 > s1)
check("there are S2 coins absent from S1", bool(s2 - s1))
check("BTC is one of the coins S1's list omits", "BTC" in (s2 - s1))


# ── 2. live.py logs an observation for every coin the ENGINE trades ─────────
# The regression: iterating the dead list while trading the live one.
check("live.py's supplementary pass iterates strategy2.WATCHLIST",
      "for _c in strategy2.WATCHLIST:" in LIVE_SRC)
check("it skips the coins the S1 scan already covered",
      "_covered = set(WATCHLIST)" in LIVE_SRC and "if _c in _covered:" in LIVE_SRC)

# It must NOT route through strategy2.build_df: that returns None when its own
# freshness guard trips, so the coins with the WORST feeds would emit no row --
# the feed-health monitor would go blind exactly where it needs to see.
obs = LIVE_SRC[LIVE_SRC.find("_covered = set(WATCHLIST)"):]
obs = obs[:obs.find("Strategy 2 runs BEFORE")] if "Strategy 2 runs BEFORE" in obs else obs
check("the observability pass does not call build_df (it hides stale coins)",
      "build_df" not in obs)
check("it fetches candles directly instead", "_fc(" in obs)
check("it reports real_close, not the polled mid",
      'real_close' in obs)


# ── 3. SOURCE-ORDER: observability must sit BELOW the entry decision ────────
# Above it, every candle fetch is signal-to-fill drift on a live order -- the
# failure mode the FIL sizing incident is made of.
i_entry = LIVE_SRC.find("res2  = open_trade(")
i_obs = LIVE_SRC.find("for _c in strategy2.WATCHLIST:")
check("the S2 entry call was located", i_entry > 0)
check("the observability pass was located", i_obs > 0)
check("observability runs AFTER the S2 entry decision", i_entry < i_obs)

# ...and position management must still precede both, unchanged.
i_ratchet = LIVE_SRC.find("_check_trail_s2(positions")
check("position management still precedes observability", 0 < i_ratchet < i_obs)


# ── 4. FORMAT: what live.py writes, analyze.py must be able to read ────────
# Mirrors the emit in live.py exactly, including the [S2-only] suffix.
for coin, px, rsi_v, adx_v in (("BTC", 80218.0, 38, 27),
                               ("NEAR", 2.0011, 59, 39),
                               ("XLM", 0.1788, 39, 30)):
    line = (f"2026-09-05 03:00:11 | INFO | {coin:<6} ${px:.4f}  "
            f"RSI {rsi_v:.0f}  ADX {adx_v:.0f}  [S2-only]")
    m = analyze._SCAN_RE.match(line)
    check(f"analyze._SCAN_RE parses the {coin} observability line", bool(m))
    if m:
        check(f"{coin}: coin parsed", m.group(2) == coin)
        check(f"{coin}: price parsed", abs(float(m.group(3)) - px) < 1e-4)
        check(f"{coin}: rsi parsed", int(m.group(4)) == rsi_v)
        check(f"{coin}: adx parsed", int(m.group(5)) == adx_v)


# ── 5. BEHAVIOURAL: the feed-quality split reports what it cannot measure ──
# The bug: `stale.get(coin)` -> None made the trade vanish with no counter.
stale = {"ETH": (100, 2, 0.02, 3), "DOGE": (100, 20, 0.20, 5)}
trades = [
    {"coin": "ETH", "result": "sl", "entry": 100.0, "exit": 102.0,
     "sl_orig": 99.0, "direction": 1},
    {"coin": "DOGE", "result": "sl", "entry": 100.0, "exit": 99.0,
     "sl_orig": 99.0, "direction": 1},
    {"coin": "BTC", "result": "sl", "entry": 100.0, "exit": 99.0,
     "sl_orig": 99.0, "direction": 1},
    {"coin": "FIL", "result": "sl", "entry": 100.0, "exit": 99.0,
     "sl_orig": 99.0, "direction": 1},
]
clean, dirty, unmeasured = analyze._feed_quality_split(trades, stale)
check("clean bucket holds the low-frozen coin", len(clean) == 1)
check("dirty bucket holds the high-frozen coin", len(dirty) == 1)
check("unlogged coins are RETURNED, not dropped", len(unmeasured) == 2)
check("unmeasured names its coins",
      {c for c, _ in unmeasured} == {"BTC", "FIL"})
check("no trade is lost by the split",
      len(clean) + len(dirty) + len(unmeasured) == len(trades))

# The whole point: a coin missing from `stale` must never be silently
# reclassified into one of the two reported buckets.
check("an unlogged coin is not counted as clean", len(clean) == 1)
check("an unlogged coin is not counted as dirty", len(dirty) == 1)


# ── 6. BEHAVIOURAL: a restart is not a stall ──────────────────────────────
# A restart re-prints the header for the hour it starts in. Before the fix that
# second header read as "the loop was blocked for 49 minutes"; on 2026-09-04
# nine such phantoms inflated "OURS to prevent" to 494 min against a true ~28.
LOG = """2026-09-04 15:00:15 | INFO | ━━━ Candle 15:00 UTC ━━━
2026-09-04 15:49:22 | INFO | ━━━ Candle 15:00 UTC ━━━
2026-09-04 15:53:23 | INFO | ━━━ Candle 15:00 UTC ━━━
2026-09-04 16:00:06 | INFO | ━━━ Candle 16:00 UTC ━━━
2026-09-04 16:51:08 | INFO | ━━━ Candle 16:00 UTC ━━━
"""
fd, path = tempfile.mkstemp(suffix=".log")
with os.fdopen(fd, "w") as fh:
    fh.write(LOG)
try:
    by_hour, worst, frozen = analyze._loop_latency(logs=[path], state=None)
    check("both candle hours are still counted", set(by_hour) == {15, 16})
    check("hour 15 is counted ONCE despite two restarts", by_hour[15][0] == 1)
    check("hour 16 is counted ONCE despite one restart", by_hour[16][0] == 1)
    check("hour 15 latency is the first arrival (15s), not 49 min",
          by_hour[15][3] == 15)
    check("hour 16 latency is the first arrival (6s), not 51 min",
          by_hour[16][3] == 6)
    check("no hour is reported as blocked >5min", by_hour[15][4] == 0
          and by_hour[16][4] == 0)

    # A genuinely late first arrival must still be caught -- the dedupe must
    # not become a way to hide real downtime (2026-08-22 17:50, post-blackout).
    LATE = ("2026-08-22 17:50:36 | INFO | ━ Candle 17:00 UTC ━\n")
    fd2, path2 = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd2, "w") as fh:
        fh.write(LATE)
    try:
        bh2, _, _ = analyze._loop_latency(logs=[path2], state=None)
        check("a real 50-min-late first arrival is still flagged",
              bh2[17][4] == 1 and bh2[17][3] == 3036)
    finally:
        os.unlink(path2)

    # Restart counting: the same log, read as what it actually is.
    per = analyze._restarts(logs=[path])
    check("restart counter ignores candle headers", per == {})
finally:
    os.unlink(path)

RESTART_LOG = """2026-09-04 15:49:17 | INFO |   GETSIGNAL AI — ONLINE
2026-09-04 15:50:38 | INFO |   GETSIGNAL AI — ONLINE
2026-09-04 15:53:17 | INFO |   GETSIGNAL AI — ONLINE
2026-09-03 02:09:52 | INFO |   GETSIGNAL AI — ONLINE
"""
fd3, path3 = tempfile.mkstemp(suffix=".log")
with os.fdopen(fd3, "w") as fh:
    fh.write(RESTART_LOG)
try:
    per = analyze._restarts(logs=[path3])
    check("restarts counted per day", per == {"2026-09-04": 3, "2026-09-03": 1})
finally:
    os.unlink(path3)


# ── 7. The census docstring must not claim coverage it does not have ──────
# It asserted "prints RSI/ADX for all 20 coins" while the loop printed 12.
doc = analyze._scan_census.__doc__ or ""
check("census docstring no longer claims all-20 coverage",
      "all 20 coins" not in doc)
check("census docstring records the pre-2026-09-05 coverage gap",
      "2026-09-05" in doc and "BTC" in doc)


# ── Report ────────────────────────────────────────────────────────────────
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILED else 0)
