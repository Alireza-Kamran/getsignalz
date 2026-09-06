"""Pins _stop_fill_quality (2026-09-06): did the ORIGINAL stop deliver -1.00R?

The counterpart to the ratchet-slippage check. A stop placed at 1R from entry
DEFINES the loss, so realised must equal -1.00R and any deviation is fill
quality, not variance. Twelve stops net +0.047R, which is the answer this
section exists to make un-forgettable: the venue is not taxing us, the ratchet
is (it arms at market and rests its stop on the price that just traded).

The assertions that matter are the CLASSIFICATION ones. `locked_r is None and
sl == sl_orig` reads like "untouched stop" and is the obvious way to write this
function, but it also matches the four pre-2026-08-16 winners whose trail moved
without ever writing a lock back to state -- AVAX/ETH 08-01, BTC 08-03, AVAX
08-11, which exited up to 5.8% BEYOND their recorded stop, in profit. Folding
those into a loss-side execution figure would import +5.3R of ratchet profit
and flip the verdict from "unbiased" to "the stop pays us". The disambiguator
is sign, not bookkeeping: the original stop is always adverse.

Run: python3 test_stop_fill.py
"""
import sys

sys.path.insert(0, "/root/trade")

from analyze import _stop_fill_quality, load_state

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def one(**kw):
    """A closed-trade record with the fields the function reads."""
    t = {"coin": "X", "opened_at": "2026-01-01T00:00:00", "dir": 1,
         "entry": 100.0, "sl": 99.0, "sl_orig": 99.0, "exit": 99.0,
         "rr": -1.0, "locked_r": None}
    t.update(kw)
    return t


def run(trades):
    return _stop_fill_quality({"closed_trades": trades})


# ── 1. The clean case: a stop that filled exactly at its trigger ─────────────
m, g = run([one()])
check("exact fill measured", len(m) == 1 and not g)
check("exact fill dev is zero", abs(m[0]["dev_r"]) < 1e-9)
check("exact fill adv_pct is zero", abs(m[0]["adv_pct"]) < 1e-9)
check("width is 1R over entry", abs(m[0]["width_pct"] - 0.01) < 1e-9)

# ── 2. THE CLASSIFICATION TRAP ───────────────────────────────────────────────
# A pre-2026-08-16 winner: no lock recorded, sl untouched in the record, but it
# exited far beyond the stop in profit. It ratcheted. It is not a stop fill.
ratched_no_lock = one(rr=2.632, exit=105.8, locked_r=None)
m, g = run([ratched_no_lock])
check("profitable exit with no lock is NOT a stop fill", not m)
check("...and is not reported as a coverage gap either", not g)

# The same trade with a lock recorded must also stay out.
m, _ = run([one(rr=2.476, exit=102.5, locked_r=2.5)])
check("ratcheted winner excluded", not m)

# A trade whose stop was MOVED but ended negative is still not the original
# stop, even with no lock recorded -- sl != sl_orig is the tell.
m, g = run([one(rr=-0.4, sl=99.6, sl_orig=99.0, exit=99.6)])
check("moved stop excluded from measurement", not m)
check("moved stop named as a gap, not dropped", len(g) == 1 and g[0][0] == "X")

# locked_r == 0.0 means "armed nothing" and must NOT exclude the trade: BTC
# 2026-09-03 carries exactly that and is a genuine original-stop loss.
m, _ = run([one(rr=-1.012, exit=98.988, locked_r=0.0)])
check("locked_r == 0.0 still counts as an original stop", len(m) == 1)

# ── 3. Sign conventions, both directions ─────────────────────────────────────
# LONG: filled BELOW the trigger is worse for us.
m, _ = run([one(dir=1, sl=99.0, sl_orig=99.0, exit=98.5, rr=-1.5)])
check("long adverse fill has positive adv_pct", m[0]["adv_pct"] > 0)
check("long adverse fill has negative dev_r", m[0]["dev_r"] < 0)

m, _ = run([one(dir=1, sl=99.0, sl_orig=99.0, exit=99.5, rr=-0.5)])
check("long favourable fill has negative adv_pct", m[0]["adv_pct"] < 0)
check("long favourable fill has positive dev_r", m[0]["dev_r"] > 0)

# SHORT: entry below the stop; filled ABOVE the trigger is worse for us.
sh = {"dir": -1, "entry": 100.0, "sl": 101.0, "sl_orig": 101.0}
m, _ = run([one(exit=101.5, rr=-1.5, **sh)])
check("short adverse fill has positive adv_pct", m[0]["adv_pct"] > 0)
check("short adverse fill has negative dev_r", m[0]["dev_r"] < 0)

m, _ = run([one(exit=100.5, rr=-0.5, **sh)])
check("short favourable fill has negative adv_pct", m[0]["adv_pct"] < 0)

# ── 4. Bad rows are named, never silently dropped ────────────────────────────
# The lesson of the 2026-09-05 feed-quality split: a slice that discards rows
# must report the remainder, or the absence reads as an absence of the problem.
m, g = run([one(entry=None), one(exit="n/a"), one(rr=-1.0, entry=0.0)])
check("unusable rows all land in the gap list", len(m) == 0 and len(g) == 3)

# rr missing entirely is not a stop fill and not a gap -- there is nothing to
# measure and nothing to explain.
m, g = run([one(rr=None)])
check("rr=None is skipped outright", not m and not g)

# A zero-width stop cannot define an R and must not divide by zero.
m, g = run([one(entry=100.0, sl=100.0, sl_orig=100.0, exit=99.0, rr=-1.0)])
check("zero-width stop does not raise", len(g) == 1)

# ── 5. Width is the conversion factor, and it comes off sl_orig ──────────────
# FIL 08-12 and ARB 09-05 are the live proof that dev_r cannot be read without
# it: FIL lost 0.190R on a 0.092% miss, ARB only 0.147R on a 0.614% miss,
# because their stops were 0.48% and 4.37% wide.
m, _ = run([one(entry=100.0, sl=95.0, sl_orig=95.0, exit=94.0, rr=-1.2)])
check("width uses the original stop distance", abs(m[0]["width_pct"] - 0.05) < 1e-9)

# The identity that makes the two columns commensurable. They are in different
# units -- adv_pct is a fraction of price, dev_r is a fraction of R -- and
# width_pct is the exchange rate between them. Comparing them directly is the
# mistake this assertion exists to prevent.
x = m[0]
implied = -(x["adv_pct"] * 95.0) / (x["width_pct"] * 100.0)
check("dev_r = -(adv_pct x trigger) / (width_pct x entry)",
      abs(x["dev_r"] - implied) < 1e-9)

# And the consequence: the SAME percentage miss costs more R on a tighter stop.
tight, _ = run([one(entry=100.0, sl=99.5, sl_orig=99.5, exit=99.0, rr=-2.0)])
wide,  _ = run([one(entry=100.0, sl=95.0, sl_orig=95.0, exit=94.5, rr=-1.1)])
check("tight and wide stops here missed by a similar %",
      abs(abs(tight[0]["adv_pct"]) - abs(wide[0]["adv_pct"])) < 0.002)
check("the tighter stop loses far more R for it",
      abs(tight[0]["dev_r"]) > 5 * abs(wide[0]["dev_r"]))

# ── 6. Real book: the numbers the report prints ──────────────────────────────
real, real_gap = _stop_fill_quality(load_state())
check(f"real book measures 12 original stops (got {len(real)})", len(real) == 12)
check("no coverage gap in the real book", not real_gap)
check("every measured trade is a loss", all(x["real_r"] < 0 for x in real))
check("none of the four unlocked winners leaked in",
      not any(x["real_r"] > 0 for x in real))

net = sum(x["dev_r"] for x in real)
check(f"book is execution-unbiased (net {net:+.3f}R)", abs(net) < 0.25)
check("both directions of fill are present -- it is noise, not a tax",
      any(x["dev_r"] > 0 for x in real) and any(x["dev_r"] < 0 for x in real))

# The invariant the whole section rests on: dev_r is rr measured against -1.
check("dev_r is exactly rr + 1 for every row",
      all(abs(x["dev_r"] - (x["real_r"] + 1.0)) < 1e-9 for x in real))

# ── 7. The report actually prints it ─────────────────────────────────────────
from analyze import full_report

rep = full_report()
check("report contains the section", "STOP FILL QUALITY" in rep)
check("report states the verdict", "execution-UNBIASED" in rep)
check("report contrasts it with the ratchet", "vs RATCHET:" in rep)
check("report warns that width governs the dev column", "1R width spans" in rep)

# ── Result ───────────────────────────────────────────────────────────────────
if FAILED:
    print(f"test_stop_fill: {PASSED} passed, {len(FAILED)} FAILED")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"test_stop_fill: {PASSED}/{PASSED} passed")
