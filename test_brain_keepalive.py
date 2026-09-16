"""Pins the 2026-09-16 fix: the Claude brain wait inside the nightly review
must keep handing control back to position management.

Background. review._self_improve() ran the brain with subprocess.run() on the
main thread, so for the 5-36 minutes it took, _check_trail_s2 did not execute
-- 2026-09-14 23:20-23:34 with OP and ETH shorts open. The 2026-09-02 reorder
(ratchet above the review) guarantees ONE ratchet pass before the wait and none
during it. ai_brain._run_claude now waits in KEEPALIVE_SLICE_S slices and calls
`keepalive()` between them; live.py installs _manage_book there.

Three things are pinned:
  1. _run_claude calls keepalive repeatedly while the child runs, and still
     returns the child's full stdout/rc (no output lost across retries).
  2. A keepalive that raises does not kill the wait.
  3. The overall timeout still kills the child and raises TimeoutExpired.
  4. The plumbing: review passes its KEEPALIVE hook, live.py installs
     _manage_book on it, and _manage_book performs the loop's four steps in
     the loop's order.

Run: python3 test_brain_keepalive.py
"""
import re
import subprocess
import sys
import time

sys.path.insert(0, "/root/trade")

import ai_brain

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


# A stand-in for `claude`: read all of stdin, hold for a while, echo a JSON doc.
CHILD = [sys.executable, "-u", "-c",
         "import sys,time; d=sys.stdin.read(); time.sleep(1.2); "
         "print('{\"echo\": %d}' % len(d))"]

# ── 1. keepalive runs during the wait; output survives the retries ───────────
calls = []
rc, out, err, passes = ai_brain._run_claude(
    CHILD, "x" * 50000, keepalive=lambda: calls.append(time.monotonic()),
    slice_s=0.25, timeout=30)
check("child exits 0", rc == 0)
check(f"stdout intact across retries (got {out.strip()!r})",
      out.strip() == '{"echo": 50000}')
check(f"keepalive called at least 3 times during a 1.2s child (got {len(calls)})",
      len(calls) >= 3)
check("passes reported equals calls made", passes == len(calls))
if len(calls) >= 2:
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    check(f"keepalive cadence ~slice_s (max gap {max(gaps):.2f}s)",
          max(gaps) < 1.0)

# ── 1b. no keepalive: behaves like a plain blocking run ──────────────────────
rc, out, err, passes = ai_brain._run_claude(CHILD, "abc", keepalive=None,
                                            slice_s=0.25, timeout=30)
check("no-keepalive run returns rc 0", rc == 0)
check("no-keepalive run returns stdout", out.strip() == '{"echo": 3}')
check("no-keepalive run reports 0 passes", passes == 0)


# ── 2. a keepalive that raises does not kill the wait ────────────────────────
def _boom():
    raise RuntimeError("poll failed")


rc, out, err, passes = ai_brain._run_claude(CHILD, "abc", keepalive=_boom,
                                            slice_s=0.25, timeout=30)
check("raising keepalive: child still completes", rc == 0)
check("raising keepalive: stdout still returned", out.strip() == '{"echo": 3}')
check("raising keepalive: passes still counted", passes >= 3)

# ── 3. overall timeout still kills the child ─────────────────────────────────
SLOW = [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"]
t0 = time.monotonic()
raised = False
try:
    ai_brain._run_claude(SLOW, "abc", keepalive=lambda: None, slice_s=0.2,
                         timeout=0.8)
except subprocess.TimeoutExpired:
    raised = True
check("TimeoutExpired raised on overall timeout", raised)
check(f"timeout honoured promptly ({time.monotonic()-t0:.1f}s)",
      time.monotonic() - t0 < 5)

# ── 4. plumbing: review hook -> live._manage_book, in the loop's order ───────
REV = open("/root/trade/review.py").read()
LIVE = open("/root/trade/live.py").read()
AIB = open("/root/trade/ai_brain.py").read()


def _code(src):
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


check("review.py declares KEEPALIVE hook",
      re.search(r"^KEEPALIVE\s*=\s*None", REV, re.M) is not None)
check("review passes KEEPALIVE into run_ai_brain",
      "run_ai_brain(keepalive=KEEPALIVE)" in _code(REV))
check("ai_brain.run_ai_brain accepts keepalive",
      "def run_ai_brain(keepalive=None)" in AIB)
# The needle is the CALL form, not the name: _run_claude's docstring says
# "subprocess.run() blocks..." in prose, and an instrument must not match its
# own documentation (test_review_order.py, 2026-09-02).
check("ai_brain no longer assigns a subprocess.run() result for the brain",
      "= subprocess.run(" not in _code(AIB))
check("ai_brain waits via Popen", "subprocess.Popen(" in _code(AIB))
check("live.py installs _manage_book on review.KEEPALIVE",
      "_review.KEEPALIVE = _manage_book" in _code(LIVE))

run_src = _code(LIVE[LIVE.index("def run()"):])
check("hook installed before the loop starts",
      run_src.find("_review.KEEPALIVE = _manage_book") < run_src.find("while True:"))

# _manage_book must perform the same four steps in the same order the loop does.
mb_start = LIVE.index("def _manage_book()")
mb_end = LIVE.index("\ndef ", mb_start + 10)
mb = _code(LIVE[mb_start:mb_end])
STEPS = ["get_positions()", "_reconcile_dust(positions)",
         "_check_closed(positions, account_val)",
         "_check_trail_s2(positions, mids=mids)", "_verify_stops(positions)"]
idx_mb = [mb.find(s) for s in STEPS]
idx_loop = [run_src.find(s) for s in STEPS]
check(f"_manage_book contains every step {idx_mb}", all(i != -1 for i in idx_mb))
check("_manage_book steps in loop order", idx_mb == sorted(idx_mb))
check("loop steps in the same order", idx_loop == sorted(idx_loop))
check("_manage_book does NOT scan or open trades",
      "find_best_setup" not in mb and "open_trade(" not in mb
      and "strategy2.scan" not in mb and "quick_state" not in mb)
check("_manage_book keeps the review-width heartbeat", "_beat(4800)" in mb)
check("_manage_book swallows errors (brain wait must outlive a bad poll)",
      "except Exception" in mb)

print(f"test_brain_keepalive: {PASSED}/{PASSED + len(FAILED)} passed")
for f in FAILED:
    print("  FAIL:", f)
sys.exit(1 if FAILED else 0)
