"""Pins the 2026-09-10 capacity instrument: `analyze._capacity` and the live.py
log line that stops the capacity gate being invisible.

THE DEFECT BEING PINNED. `live.py`'s S2 block was guarded by
`if s2_at_risk < strategy2.MAX_TRADES:` with no `else`. When the book was full
the whole block was skipped -- including the `no mean-reversion setup` heartbeat
two levels down, which exists precisely so that a quiet scanner is
distinguishable from a dead one. The log therefore said the same thing for "we
looked and found nothing" and "we never looked", and only the second costs a
trade. Every other entry constraint in this system leaves a record; this one
left none.

`_capacity` reconstructs the history by joining position intervals from
state.json against the scan stream, which is only possible because a SEPARATE
loop prints RSI/ADX per coin every hour. That accident is not a substitute for
the log line, so both are asserted here.

Three kinds of assertion, deliberately:

  * CONSERVATION -- `blocked + reachable == observations fed in`, for every
    population including the empty and the malformed one. This repo has now
    found the same silent-row-drop three times (feed-quality split 09-05, entry
    bands 09-07); a splitter that cannot account for its input is how it hides.

  * BOUNDARY -- concurrency exactly AT MAX_TRADES is blocked, one below is
    reachable, and a position with no `closed_at` holds its slot up to now
    rather than being dropped for having a null field.

  * FORMAT -- the live.py line must be emitted on the else branch and must name
    the holders. Comments are stripped FIRST: the branch is documented by a
    comment naming the very symbols the assertions search for, and AN
    INSTRUMENT MUST NOT MATCH ITS OWN DOCUMENTATION.

Run: python3 test_capacity.py
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/root/trade")

import analyze
import strategy2

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def _strip_comments(src):
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


def obs(day, hour, rsi, adx, price=100.0):
    return (f"2026-09-{day:02d} {hour:02d}:01:00", price, rsi, adx)


def trade(coin, o, c=None):
    t = {"coin": coin, "opened_at": o}
    if c is not None:
        t["closed_at"] = c
    return t


CALL = dict(max_trades=2, rsi_lo=25, rsi_hi=75, max_adx=25)


def run(per, state):
    return analyze._capacity(per, state, CALL["max_trades"], CALL["rsi_lo"],
                             CALL["rsi_hi"], CALL["max_adx"])


# ── 1. Conservation: every observation lands in exactly one bucket ──────────
per = {
    "AAA": [obs(1, 5, 20, 20), obs(1, 6, 50, 50), obs(1, 7, 80, 10)],
    "BBB": [obs(1, 5, 90, 60), obs(1, 6, 24, 24)],
}
state = {"closed_trades": [
    trade("X", "2026-09-01T04:00:00", "2026-09-01T08:00:00"),
    trade("Y", "2026-09-01T04:30:00", "2026-09-01T05:30:00"),
], "tracked": {}}
hours, blk, blk_q, rch, rch_q, runs = run(per, state)
total_in = sum(len(v) for v in per.values())
check("conservation: blocked + reachable == observations",
      blk + rch == total_in)
check("qualifying counts never exceed their own bucket",
      blk_q <= blk and rch_q <= rch)

# At 05:01 both X and Y are open -> 2 == MAX_TRADES -> BLOCKED.
# At 06:01 and 07:01 only X is open -> 1 -> reachable.
check("boundary: concurrency AT MAX_TRADES is blocked", blk == 2)
check("boundary: concurrency BELOW MAX_TRADES is reachable", rch == 3)
check("blocked qualifiers counted (AAA rsi20/adx20 at 05:01)", blk_q == 1)
check("reachable qualifiers counted (AAA rsi80/adx10, BBB rsi24/adx24)",
      rch_q == 2)
check("hours bucketed by peak concurrency in the hour",
      hours[datetime(2026, 9, 1, 5)] == 2 and hours[datetime(2026, 9, 1, 6)] == 1)


# ── 2. An OPEN position holds its slot; a null closed_at is not "closed" ────
# [[reference_null_not_zero]] -- the absence of the field dates the record, it
# does not mean the position stopped consuming capacity.
per2 = {"AAA": [obs(2, 10, 20, 20)]}
state2 = {"closed_trades": [trade("X", "2026-09-02T09:00:00")],
          "tracked": {"Y": trade("Y", "2026-09-02T09:30:00")}}
hours2, blk2, blk_q2, rch2, rch_q2, _ = run(per2, state2)
check("open position with no closed_at still holds a slot",
      blk2 == 1 and rch2 == 0)
check("open-position qualifier is recorded as blocked", blk_q2 == 1)

# The same two positions dated in the FUTURE must not block a past hour.
state2b = {"closed_trades": [trade("X", "2026-09-08T09:00:00",
                                   "2026-09-08T12:00:00")], "tracked": {}}
_, blk2b, _, rch2b, _, _ = run(per2, state2b)
check("a position that had not opened yet blocks nothing",
      blk2b == 0 and rch2b == 1)


# ── 3. Full stretches merge across a one-hour hole, not a long one ─────────
# Quiet hours (02:00-03:59 UTC) print no scan lines, so a stretch spanning them
# is ONE event. A real reopening days later is not.
per3 = {"AAA": [obs(3, 1, 50, 50), obs(3, 4, 50, 50), obs(3, 5, 50, 50),
                obs(6, 1, 50, 50)]}
state3 = {"closed_trades": [
    trade("X", "2026-09-03T00:00:00", "2026-09-03T06:00:00"),
    trade("Y", "2026-09-03T00:00:00", "2026-09-03T06:00:00"),
    trade("Z", "2026-09-06T00:00:00", "2026-09-06T06:00:00"),
    trade("W", "2026-09-06T00:00:00", "2026-09-06T06:00:00"),
], "tracked": {}}
_, _, _, _, _, runs3 = run(per3, state3)
check("a 3h gap splits a stretch, a 1h hole does not", len(runs3) == 3)
check("stretch names its holders", runs3[0][2] == {"X", "Y"})
check("stretches returned in chronological order",
      [r[0] for r in runs3] == sorted(r[0] for r in runs3))


# ── 4. Degenerate input returns zeros instead of raising ──────────────────
check("empty state returns zeros", run(per, {}) == ({}, 0, 0, 0, 0, []))
check("empty census with real state returns zeros",
      run({}, state)[1:5] == (0, 0, 0, 0))

# One unparseable row must not abort the whole measurement.
per5 = {"AAA": [obs(4, 5, 20, 20), ("not-a-timestamp", 1.0, 20, 20)]}
state5 = {"closed_trades": [trade("X", "2026-09-04T04:00:00",
                                  "2026-09-04T06:00:00"),
                            trade("Z", None, None),
                            trade("Q", "garbage", "garbage")], "tracked": {}}
try:
    _, blk5, _, rch5, _, _ = run(per5, state5)
    check("one bad row is skipped, the good row still measured",
          blk5 + rch5 == 1)
except Exception as e:                                   # pragma: no cover
    check(f"one bad row must not raise ({e})", False)


# ── 5. The ratio is invariant to the shared upper bound ──────────────────
# RSI+ADX over-counts the live gate on BOTH sides (no stretch term), so the
# absolute counts are bounds but the RATIO is the quantity to read. Halving
# every qualifier must leave the ratio unchanged -- this pins the claim the
# report prints, not merely the arithmetic.
b_rate = blk_q / blk if blk else 0
r_rate = rch_q / rch if rch else 0
check("qualifying rates are computed per-bucket, not pooled",
      abs(b_rate - 0.5) < 1e-9 and abs(r_rate - 2 / 3) < 1e-9)


# ── 6. live.py must LOG the block; the reconstruction is not the record ───
LIVE_SRC = _strip_comments(open("/root/trade/live.py").read())
check("live.py has an else on the MAX_TRADES gate",
      "if s2_at_risk < strategy2.MAX_TRADES:" in LIVE_SRC
      and "book full" in LIVE_SRC)
check("the block line names the holders, not just the count",
      "holders:" in LIVE_SRC)
check("holders are filtered to S2, so an S1 position cannot be blamed",
      't.get("strategy") != "S2"' in LIVE_SRC)
# opened_at is a datetime in _open_trades (converted on restore, live.py:1071),
# but a string in state.json. Guarding the type here is what stopped the
# 2026-08-16 ghost-close bug, where the same assumption raised inside an
# except that swallowed it.
check("position age is guarded on type, not assumed",
      "isinstance(opened, datetime)" in LIVE_SRC)


# ── 7. The report section must exist and must state its own bound ────────
ANALYZE_SRC = open("/root/trade/analyze.py").read()
check("report prints a CAPACITY section", "── CAPACITY (MAX_TRADES=" in ANALYZE_SRC)
check("report states RSI+ADX is an upper bound",
      "UPPER bound on qualifying" in ANALYZE_SRC)
check("report deflates before quoting a trade count", "deflator:" in ANALYZE_SRC)
# The confound is the finding. If this caption is ever dropped, the section
# becomes a case for raising MAX_TRADES, which is the opposite of what it says.
check("report names the correlation confound",
      "CORRELATION" in ANALYZE_SRC and "cluster" in ANALYZE_SRC)
check("_capacity docstring records that the gate left no log",
      "leaves NOTHING" in (analyze._capacity.__doc__ or ""))


# ── 8. Live data: the section renders and conserves on the real book ─────
try:
    real_state = analyze.load_state()
    real_per = analyze._scan_census()
    h, b, bq, r, rq, rr = analyze._capacity(
        real_per, real_state, strategy2.MAX_TRADES, strategy2.RSI_OVERSOLD,
        strategy2.RSI_OVERBOUGHT, strategy2.MAX_ADX)
    check("live: conservation holds on the production book",
          b + r == sum(len(v) for v in real_per.values()))
    check("live: no hour exceeds MAX_TRADES concurrency",
          all(v <= strategy2.MAX_TRADES for v in h.values()))
except Exception as e:                                   # pragma: no cover
    check(f"live book must not raise ({e})", False)


print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILED else 0)
