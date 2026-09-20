"""Pins _concurrency: overlap detection, concordance, and the SOLO/CONCURRENT split.

Added 2026-09-20 to answer a question flagged qualitatively on 2026-08-23 and
never measured: MAX_TRADES=2 caps the NUMBER of positions, but nothing caps their
correlation, and correlation is what sets the variance of the book.

The arithmetic is easy to get subtly wrong in ways that flatter the finding, so
the cases below are the ones that would do it:

  * touching intervals (one closes exactly as the next opens) are NOT an overlap;
  * containment (a long-lived position swallowing a short one) IS;
  * a trade appearing in several pairs must be counted ONCE in the SOLO/
    CONCURRENT split, or the concurrent bucket inflates itself;
  * the independence yardstick is p^2+(1-p)^2 at the book's own win rate, never
    50% -- at WR 36% two independent coin flips already agree 54% of the time,
    so a naive 50% baseline would manufacture an effect out of nothing;
  * one unparseable timestamp must drop its own row, not the section
    ([[reference_null_not_zero]] -- the OP 2026-08-13 record with its "+00:00"
    offset aborted two whole sections on 2026-09-03).

Run: python3 test_concurrency.py
"""
import sys

sys.path.insert(0, "/root/trade")

from analyze import _concurrency

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def T(coin, o, c, r, direction=1):
    """A journal-shaped trade. entry/sl/exit are chosen to yield exactly r."""
    entry, sl = 100.0, 90.0                     # 1R = 10.0
    return {"coin": coin, "direction": direction, "result": "sl",
            "open_time": o, "close_time": c,
            "entry": entry, "sl_orig": sl, "exit": entry + r * 10.0 * direction}


# ── 1. Touching is not overlapping ───────────────────────────────────────────
res = _concurrency([
    T("A", "2026-09-01T00:00:00", "2026-09-01T04:00:00", +1.0),
    T("B", "2026-09-01T04:00:00", "2026-09-01T08:00:00", +1.0),
])
check("1a back-to-back trades produce no pair", res is None or res["n_pairs"] == 0)

# ── 2. Containment is overlapping ────────────────────────────────────────────
res = _concurrency([
    T("LONGHOLD", "2026-09-01T00:00:00", "2026-09-03T00:00:00", -1.0),
    T("INSIDE",   "2026-09-01T06:00:00", "2026-09-01T09:00:00", -1.0),
])
check("2a a contained trade overlaps", res and res["n_pairs"] == 1)
check("2b overlap is the inner trade's own length",
      res and abs(res["pairs"][0][2] - 3.0) < 1e-6)
check("2c both losers are concordant", res and res["concordant"] == 1)

# ── 3. Concordance is about sign, not direction ──────────────────────────────
res = _concurrency([
    T("L", "2026-09-01T00:00:00", "2026-09-01T10:00:00", -1.0, direction=1),
    T("S", "2026-09-01T02:00:00", "2026-09-01T06:00:00", -1.0, direction=-1),
])
check("3a a long and a short both losing is CONCORDANT",
      res and res["concordant"] == 1)
check("3b and is counted as opposite-direction",
      res and res["opp_dir_concordant"] == 1)

res = _concurrency([
    T("W", "2026-09-01T00:00:00", "2026-09-01T10:00:00", +2.5),
    T("L", "2026-09-01T02:00:00", "2026-09-01T06:00:00", -1.0),
])
check("3c one win and one loss is a split", res and res["concordant"] == 0)

# ── 4. A trade in several pairs is counted once in the split ─────────────────
res = _concurrency([
    T("HUB", "2026-09-01T00:00:00", "2026-09-05T00:00:00", -1.0),
    T("X",   "2026-09-01T01:00:00", "2026-09-01T02:00:00", -1.0),
    T("Y",   "2026-09-02T01:00:00", "2026-09-02T02:00:00", -1.0),
    T("Z",   "2026-09-03T01:00:00", "2026-09-03T02:00:00", -1.0),
    T("ALONE", "2026-09-09T00:00:00", "2026-09-09T02:00:00", +2.0),
])
check("4a hub pairs with each of the three", res and res["n_pairs"] == 3)
check("4b concurrent bucket holds 4 trades, not 6",
      res and res["conc"]["n"] == 4)
check("4c the untouched trade is solo", res and res["solo"]["n"] == 1)
check("4d buckets partition the book",
      res and res["conc"]["n"] + res["solo"]["n"] == 5)
check("4e max_pairs_per_trade exposes the hub", res and res["max_pairs_per_trade"] == 3)
check("4f solo meanR is that trade's own R",
      res and abs(res["solo"]["mean"] - 2.0) < 1e-6)

# ── 5. The independence yardstick uses the book's WR, not 50% ────────────────
# 5 trades, 1 winner -> p=0.2 -> 0.2^2 + 0.8^2 = 0.68, NOT 0.50.
check("5a independent baseline is p^2+(1-p)^2",
      res and abs(res["independent"] - (0.2 ** 2 + 0.8 ** 2)) < 1e-9)

# ── 6. A bad row drops itself, not the section ───────────────────────────────
rows = [
    T("GOOD1", "2026-09-01T00:00:00", "2026-09-01T10:00:00", -1.0),
    T("GOOD2", "2026-09-01T02:00:00", "2026-09-01T06:00:00", -1.0),
    T("NOTIME", None, "2026-09-01T06:00:00", -1.0),
    T("JUNK", "not-a-date", "also-not-a-date", -1.0),
]
rows.append({"coin": "NOR", "direction": 1, "result": "sl",
             "open_time": "2026-09-01T03:00:00", "close_time": "2026-09-01T04:00:00",
             "entry": 100.0, "sl_orig": 100.0, "exit": 101.0})   # entry==sl -> R is None
res = _concurrency(rows)
check("6a unparseable rows do not raise", res is not None)
check("6b only the two good rows survive", res and res["n_pairs"] == 1)

# Mixed tz-aware and naive timestamps must not raise: the repaired OP record
# carries "+00:00" while every live record is naive.
res = _concurrency([
    T("AWARE", "2026-09-01T00:00:00+00:00", "2026-09-01T10:00:00+00:00", -1.0),
    T("NAIVE", "2026-09-01T02:00:00", "2026-09-01T06:00:00", -1.0),
])
check("6c aware and naive timestamps compare cleanly", res and res["n_pairs"] == 1)

# ── 7. Degenerate inputs ─────────────────────────────────────────────────────
check("7a empty book returns None", _concurrency([]) is None)
check("7b single trade returns None",
      _concurrency([T("A", "2026-09-01T00:00:00", "2026-09-01T01:00:00", 1.0)]) is None)
res = _concurrency([
    T("A", "2026-09-01T00:00:00", "2026-09-01T01:00:00", +1.0),
    T("B", "2026-09-05T00:00:00", "2026-09-05T01:00:00", -1.0),
])
check("7c a book with no overlap reports zero pairs",
      res and res["n_pairs"] == 0 and res["solo"]["n"] == 2 and res["conc"] is None)

# A close before its open is corrupt, not a negative overlap.
res = _concurrency([
    T("BACKWARDS", "2026-09-01T10:00:00", "2026-09-01T00:00:00", -1.0),
    T("OK", "2026-09-01T02:00:00", "2026-09-01T06:00:00", -1.0),
])
check("7d a close-before-open row is dropped", res is None or res["n_pairs"] == 0)

# ── 8. Reproduces the live book (regression guard on the real journal) ───────
# Computed independently on 2026-09-20 before the section existed: 11 pairs,
# 9 concordant, SOLO n=12 meanR +0.330, CONCURRENT n=16 meanR -0.085.
try:
    from analyze import load_journal
    live = [t for t in load_journal().get("trades", []) if t.get("result")]
    res = _concurrency(live)
    if res and len(live) == 28:
        check("8a live book still yields 11 overlapping pairs", res["n_pairs"] == 11)
        check("8b 9 of them concordant", res["concordant"] == 9)
        check("8c solo/concurrent partition is 12/16",
              res["solo"]["n"] == 12 and res["conc"]["n"] == 16)
    else:
        PASSED += 1   # book has moved on; the synthetic cases above still bind
except Exception as e:
    FAILED.append(f"8 live-book check raised: {e}")

print(f"\n{'=' * 60}")
if FAILED:
    print(f"FAILED {len(FAILED)}/{PASSED + len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"test_concurrency: {PASSED}/{PASSED} checks passed")
