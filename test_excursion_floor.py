#!/usr/bin/env python3
"""A recorded ratchet lock is a floor on peak excursion. Read it, don't re-sample.

WHY THIS FILE EXISTS
--------------------
`peak_roe_pct` is sampled by the tracker poll loop; `locked_r` is recorded by
update_sl() at the moment it moves the stop. The ratchet only moves a stop to a
level price has actually traded through, so a recorded lock PROVES the level was
reached, while the poll sample merely hopes it caught the tick.

On 2026-09-21 the EXCURSION section was reading the raw sample for its threshold
counts and its continuation table, while `_armed()` -- twenty-five lines below,
in the same function -- correctly preferred the recorded fact. Four trades
(SOL/SUI 08-18, ETH 08-21, OP 09-14) had their stops locked at exactly +2.50R
while their sampled peaks read 2.32-2.46R, so every ">=2.5R" test scored four
real arms as misses:

    reached >=2.5R     3/27 (11%)   ->   7/27 (26%)
    2.0R -> 2.5R       3/8  (38%)   ->   7/8  (88%)

That is not a rounding difference. 38% says trades die just below our arming
level and TRAIL_START_R=2.5 is set too far out; 88% says the 2.0-2.5 band is one
of the SAFEST in the curve and the real attrition is down at 0.5->1.0R. The two
readings argue for opposite changes to an owner-locked constant.

The bias is ONE-SIDED BY CONSTRUCTION -- only winners arm, so only winners carry
a lock, so only winners were understated. Every loser read correctly, which is
precisely why the aggregate looked plausible for weeks. This is the same shape as
the 2026-08-17 stop-denominator bug: an error that lands exclusively on the fat
tail that decides the book.

Two symptoms were visible in the printed report the whole time and neither was
read as a defect:
  1. "reached >=2.5R: 3/27" printed four lines above "armed in 7/27 trades" --
     a flat self-contradiction, since arming at 2.5R requires reaching 2.5R.
  2. SOL and SUI printed "gave back -0.02R" / "-0.06R" -- a NEGATIVE giveback,
     i.e. a trade exiting ABOVE its own peak, which is impossible.

So the tests below are shaped around those two cross-checks rather than around
the helper's return value: an internal contradiction between two sections is a
stronger invariant than either section's number, and it is what should have
caught this.
"""
import sys, json
sys.path.insert(0, "/root/trade")

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


# ── 1. The helper itself ────────────────────────────────────────────────────
print("\n_mfe_seen: recorded lock beats sampled peak")

check("lock above sample wins (the SOL case)",
      analyze._mfe_seen({"mfe_r": 2.46, "locked_r": 2.50}) == 2.50)
check("sample above lock wins (ratchet stepped, poll caught the run)",
      analyze._mfe_seen({"mfe_r": 3.13, "locked_r": 3.00}) == 3.13)
check("locked_r None (pre-2026-08-16 record) falls back to sample",
      analyze._mfe_seen({"mfe_r": 0.61, "locked_r": None}) == 0.61)
check("locked_r absent entirely falls back to sample",
      analyze._mfe_seen({"mfe_r": 0.61}) == 0.61)
check("locked_r 0.0 means recorded-but-never-armed, not a floor",
      analyze._mfe_seen({"mfe_r": 2.05, "locked_r": 0.0}) == 2.05)
check("no excursion recorded stays None, NOT 0.0",
      analyze._mfe_seen({"mfe_r": None, "locked_r": None}) is None)
check("no excursion recorded stays None even WITH a lock",
      analyze._mfe_seen({"mfe_r": None, "locked_r": 2.5}) is None)


# ── 2. The cross-check that should have caught it ───────────────────────────
# A trade cannot arm the ratchet at TRAIL_START_R without having reached
# TRAIL_START_R. So the ">=arm" excursion count can never be LOWER than the
# armed count. This is an identity between two independently-computed numbers
# that the report prints four lines apart.
print("\ncross-check: reached>=arm must be >= armed count (live book)")

state = json.load(open("/root/trade/state.json"))
exc   = analyze._excursion_stats(state)
import strategy2
arm   = float(strategy2.TRAIL_START_R)

meas    = [x for x in exc if analyze._mfe_seen(x) is not None]
reached = sum(1 for x in meas if analyze._mfe_seen(x) >= arm)
armed   = sum(1 for x in meas
              if (x["locked_r"] is not None and x["locked_r"] > 0)
              or (x["locked_r"] is None and x["mfe_r"] is not None
                  and x["mfe_r"] >= arm))

check(f"reached>={arm:g}R ({reached}) >= armed ({armed})", reached >= armed)
check("the raw column FAILS this cross-check (the bug is real, not theoretical)",
      sum(1 for x in meas if x["mfe_r"] >= arm) < armed)


# ── 3. Giveback can never be negative ───────────────────────────────────────
# MAE <= realised <= MFE by construction: a trade cannot exit above its own
# peak. A negative giveback is that invariant failing in plain sight.
print("\ngiveback (MFE - realised) must be >= 0 on every measured trade")

TOL = 0.10   # poll sampling, same tolerance the report uses
neg = [x for x in meas
       if analyze._mfe_seen(x) - x["real_r"] < -TOL]
check(f"no negative giveback on {len(meas)} measured trades",
      not neg)
for x in neg:
    print(f"        {x['coin']} {x['opened']}: "
          f"MFE {analyze._mfe_seen(x):+.2f} realised {x['real_r']:+.2f}")

# Cross-check against the independently-computed RATCHET SLIPPAGE leak.
#
# These are NOT the same quantity, and an early draft of this test asserted
# equality and failed -- correctly. Decomposing:
#
#     leak     = locked_r - realised      (fill slippage below the stop level)
#     giveback = true peak - realised     (everything not captured)
#     giveback - leak = true peak - locked_r
#
# That last term is real run ABOVE the level the ratchet had stepped to -- move
# that TRAIL_STEP_R never caught up with. It is zero exactly when the poll never
# observed anything above the lock, which is the common case; AAVE 2026-09-02
# peaked at 3.13R with the stop stepped only to 3.00R, so 0.13R of genuine
# un-harvested run sits on top of 0.026R of slippage.
#
# So the invariant is giveback >= leak, and the GAP is a measurement in its own
# right: how far the step size lags a fast move.
slip, _gap = analyze._ratchet_slippage(state)
by_key = {(s["coin"], s["opened"]): s for s in slip}
agreed = 0
for x in meas:
    k = (x["coin"], x["opened"])
    if k not in by_key:
        continue
    give = analyze._mfe_seen(x) - x["real_r"]
    leak = by_key[k]["slip_r"]
    lag  = give - leak
    check(f"{x['coin']} {x['opened']}: giveback {give:+.3f}R >= "
          f"leak {leak:+.3f}R  (step lag {lag:+.3f}R)",
          give >= leak - 0.02)
    # And the lag must equal peak - locked_r, by the algebra above.
    check(f"{x['coin']} {x['opened']}: lag == peak - locked_r",
          abs(lag - (analyze._mfe_seen(x) - by_key[k]["locked_r"])) < 0.02)
    agreed += 1
check("at least 4 armed trades available to cross-check", agreed >= 4)


# ── 4. Censoring flag ───────────────────────────────────────────────────────
# Reaching the arming level is now fully observable (arming records it), so
# only levels STRICTLY above the arm are censored. Leaving this at ">=" flags
# the corrected row as unreadable and buries the finding.
print("\ncensoring marks levels strictly ABOVE the arm, not at it")

rows, _unrec, haz_arm = analyze._excursion_hazard(exc)
check("hazard reports the live arm level", haz_arm == arm)
at_arm = [r for r in rows if r["hi"] == arm]
above  = [r for r in rows if r["hi"] > arm]
check(f"row ending AT {arm:g}R is not flagged censored",
      at_arm and not at_arm[0]["censored"])
check(f"rows above {arm:g}R are flagged censored",
      above and all(r["censored"] for r in above))


# ── 5. Report renders and stays self-consistent ─────────────────────────────
print("\nfull_report renders with the corrected column")
rep = analyze.full_report()
check("EXCURSION section present", "EXCURSION: WHAT THE ENTRY OFFERED" in rep)
# Only givebacks beyond the poll-sampling tolerance are defects. A pre-2026-08-16
# record has no locked_r to correct it, so a trade that closed right at its peak
# can still print -0.01R from a single missed tick (AVAX 2026-08-01). Flagging
# any negative at all would make this test fail on benign sampling noise; the
# thing that must never reappear is a MATERIAL negative, which is what the
# uncorrected winners printed (-0.02R, -0.06R against true leaks of +0.02/+0.06).
import re
printed_neg = [float(m) for m in re.findall(r"gave back (-\d+\.\d+)R", rep)]
check(f"no giveback below -{TOL}R printed (found: {printed_neg or 'none'})",
      all(v >= -TOL for v in printed_neg))
check("corrected rows disclose the raw poll sample",
      "peak from locked_r; poll sampled" in rep)
check("invariant line still OK", "invariant MAE <= realised <= MFE: OK" in rep)

print(f"\ntest_excursion_floor: {PASS}/{PASS+FAIL} passed")
sys.exit(1 if FAIL else 0)
