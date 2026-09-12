"""Pins the 2026-09-12 stall-attribution fix in `analyze._stall_cause` and the
LOOP LATENCY totals that read it.

THE DEFECT BEING PINNED. `_stall_cause` had two branches: if a Hyperliquid
error landed inside the stall window it returned "venue down", and OTHERWISE it
returned "self-blocked" -- our own code held the loop, ours to fix. That makes
"self-blocked" a RESIDUAL BUCKET wearing a diagnosis: a cause assigned from the
absence of one alternative rather than from evidence, which is the failure mode
[[reference_cause_by_elimination]] exists to name.

The alternative it never tested is the one that matters most: THE PROCESS WAS
NOT RUNNING. A bot that is down cannot be blocked by its own code, and when it
comes back the first thing it does is print the candle header for the hour it
woke up in -- which reads as an enormous stall. Two of the nine rows were this:

  * 2026-08-22 17:00, ETH SHORT, 50.9 min -- banner at 17:50:45, the tail of
    the 19h48m host blackout already reported by AVAILABILITY.
  * 2026-09-10 12:00, DOGE + ETH, 52.9 min -- banner at 12:51:56, after a 1.9h
    outage the heartbeat itself logged as
    "Bot was not managing the book for 1.9h before this start".

Note the shape. The 2026-09-04 first-arrival dedup in `_loop_latency` guards
RESTART-AFTER-SERVING (an hour served on time, then a second header from a
restart). This is RESTART-BEFORE-SERVING, the mirror image: the dedup correctly
keeps the row, and the row is still wrong about cause.

The second defect is arithmetic. `_frozen` holds one row PER OPEN POSITION, and
the totals summed the rows, so the two positions open across the single 52.9
min window on 2026-09-10 contributed 105.8 min to a wall-clock figure. Totals
are now taken over distinct stall events.

Together: "total 268 min frozen, of which 241 min was OURS to prevent" against
a true 216 min wall-clock and 84 min ours -- the actionable number overstated
2.9x. Per the 2026-08-31 session, a health alarm that cries wolf gets read
past, and this one sits directly above the numbers a session is meant to act on.

Three kinds of assertion:

  * PRECEDENCE -- process-down beats venue-down beats self-blocked, including
    when venue errors and a restart both fall in the same window. A process
    that was not running cannot have been blocked by its own code, whatever
    else the log says during the gap.

  * BOUNDARY -- the window is half-open `(due, wall]`. A banner exactly at
    `due` belongs to the previous hour's stall, not this one; a banner exactly
    at `wall` is this restart printing this header.

  * CONSERVATION -- the three buckets must sum to the wall-clock total, and the
    total must never exceed the sum over DISTINCT events. A splitter that
    cannot account for its own input is how a miscount hides
    ([[reference_silent_row_drops]], found four times now).

Run: python3 test_stall_cause.py
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/root/trade")

import analyze

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
        out.append(line)
    return "\n".join(out)


D = datetime(2026, 9, 10, 12, 0, 0)
W = D + timedelta(minutes=52, seconds=54)


# ── PRECEDENCE ────────────────────────────────────────────────────────
check("no evidence at all -> self-blocked",
      analyze._stall_cause(D, W, [], [])[0] == "self-blocked")

check("venue error inside window -> venue down",
      analyze._stall_cause(D, W, [D + timedelta(minutes=5)], [])[0]
      == "venue down")

check("venue down reports how many errors it saw",
      analyze._stall_cause(
          D, W, [D + timedelta(minutes=5), D + timedelta(minutes=6)], [])[1] == 2)

check("restart inside window -> process down",
      analyze._stall_cause(D, W, [], [D + timedelta(minutes=51)])[0]
      == "process down")

check("restart WINS over venue errors in the same window",
      analyze._stall_cause(D, W, [D + timedelta(minutes=5)],
                           [D + timedelta(minutes=51)])[0] == "process down")

check("process down claims no error count",
      analyze._stall_cause(D, W, [D + timedelta(minutes=5)],
                           [D + timedelta(minutes=51)])[1] == 0)

check("restart OUTSIDE the window does not excuse a self-block",
      analyze._stall_cause(D, W, [], [D - timedelta(hours=3)])[0]
      == "self-blocked")


# ── BOUNDARY ──────────────────────────────────────────────────────────
check("banner exactly AT due belongs to the previous hour, not this stall",
      analyze._stall_cause(D, W, [], [D])[0] == "self-blocked")

check("banner exactly AT wall is this restart printing this header",
      analyze._stall_cause(D, W, [], [W])[0] == "process down")

check("banner one second past wall is a later restart",
      analyze._stall_cause(D, W, [], [W + timedelta(seconds=1)])[0]
      == "self-blocked")

check("empty starts is not an error",
      analyze._stall_cause(D, W, [], ())[0] == "self-blocked")


# ── THE TWO REAL EVENTS, by their logged timestamps ───────────────────
check("2026-08-22 17:00 ETH stall is the host blackout, not a self-block",
      analyze._stall_cause(
          datetime(2026, 8, 22, 17, 0), datetime(2026, 8, 22, 17, 50, 54),
          [], [datetime(2026, 8, 22, 17, 50, 45)])[0] == "process down")

check("2026-09-10 12:00 stall is the 1.9h outage, not a self-block",
      analyze._stall_cause(
          datetime(2026, 9, 10, 12, 0), datetime(2026, 9, 10, 12, 52, 54),
          [], [datetime(2026, 9, 10, 12, 51, 56)])[0] == "process down")

check("2026-09-02 07:00 AAVE stall is still venue down (no restart that hour)",
      analyze._stall_cause(
          datetime(2026, 9, 2, 7, 0), datetime(2026, 9, 2, 7, 27, 54),
          [datetime(2026, 9, 2, 7, 5)], [datetime(2026, 9, 2, 23, 6, 50)])[0]
      == "venue down")


# ── ONE PARSE, TWO CONSUMERS ──────────────────────────────────────────
_starts = analyze._restart_times()
check("_restart_times returns datetimes", all(isinstance(t, datetime)
                                              for t in _starts))
check("_restart_times is sorted", list(_starts) == sorted(_starts))
check("_restart_times is deduped", len(set(_starts)) == len(_starts))
check("_restarts agrees with _restart_times on the total",
      sum(analyze._restarts(starts=_starts).values()) == len(_starts))
check("_restarts derived from the shared parse matches the standalone call",
      analyze._restarts() == analyze._restarts(starts=_starts))
check("the production log carries restart banners at all", len(_starts) > 0)


# ── CONSERVATION on the live book ─────────────────────────────────────
try:
    _bh, _worst, _frozen = analyze._loop_latency(state=analyze.load_state())

    by_ev = {}
    for due, coin, side, lag, cause, nerr in _frozen:
        by_ev[due] = (lag, cause)
    tot = sum(l for l, _ in by_ev.values())
    buckets = {}
    for l, c in by_ev.values():
        buckets[c] = buckets.get(c, 0.0) + l

    check("buckets sum to the wall-clock total",
          abs(sum(buckets.values()) - tot) < 1e-6)

    check("every cause is one of the three known kinds",
          set(buckets) <= {"self-blocked", "venue down", "process down"})

    check("event total never exceeds the naive per-row sum",
          tot <= sum(r[3] for r in _frozen) + 1e-6)

    check("distinct events <= rows (rows are per position)",
          len(by_ev) <= len(_frozen))

    check("a single stall has ONE cause across all its positions",
          all(len({c for d, _c, _s, _l, c, _n in _frozen if d == due}) == 1
              for due in by_ev))

    # The regression itself: the live book must no longer bill the two known
    # outages to us. If either is ever reclassified as self-blocked, the
    # residual-bucket bug is back.
    _mis = [(d, c) for d, _c, _s, _l, c, _n in _frozen
            if d in (datetime(2026, 8, 22, 17, 0), datetime(2026, 9, 10, 12, 0))
            and c == "self-blocked"]
    check(f"known outages are not billed as self-blocked ({_mis})", not _mis)

    _ours = buckets.get("self-blocked", 0.0) / 60
    check(f"'ours to prevent' is the blocking-maintenance figure, not the "
          f"residual (got {_ours:.0f} min, pre-fix was 241)", _ours < 150)
except Exception as e:                                   # pragma: no cover
    check(f"live latency must not raise ({e})", False)


# ── FORMAT: the report must not re-derive the totals by summing rows ──
_src = _strip_comments(open("/root/trade/analyze.py").read())
check("report buckets stall time by cause",
      "_bucket[_c] += _l" in _src)
check("report keys the totals on the stall event, not the row",
      "_by_ev[_due]" in _src)
check("report still labels a process-down row distinctly",
      "PROCESS DOWN" in _src)
check("_stall_cause takes the restart list",
      "def _stall_cause(due, wall, venue_events, starts=())" in _src)
check("_loop_latency passes restarts into the classifier",
      "_stall_cause(due, wall, venue, starts)" in _src)


print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILED else 0)
