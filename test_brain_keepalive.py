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

# ── 5. the count must be logged BEFORE anything that can end the process ─────
# 2026-09-16 23:20->23:28 was the first review after the keepalive shipped. It
# ended in os.execv (flat book, changes), live.py's "review complete" line was
# never reached, and the report had no count to read -- the fix could not be
# verified by the instrument built to verify it. review.py now logs the count
# the moment the brain returns; analyze reads it for any window ending.
rev_code = _code(REV)
i_call = rev_code.find("run_ai_brain(keepalive=KEEPALIVE)")
i_log = rev_code.find("Brain wait done in")
i_execv = rev_code.find("os.execv(")
check("review.py logs the brain-wait count", i_log != -1)
check("count logged after the brain returns", i_call != -1 and i_log > i_call)
check("count logged before os.execv", i_execv != -1 and i_log < i_execv)
# Needles are the two f-string fragments as written (the line is split across
# two literals), not the rendered text -- the rendered text is what (b)-(e)
# feed the parser below.
check("count line carries the wall time (0 passes on a 5s failure is not a missing hook)",
      "Brain wait done in {_time.monotonic() - _t0:.0f}s" in rev_code
      and "(book managed {brain_result['keepalive_passes']}x" in rev_code)

import os
import tempfile
import analyze

BANNER = "{d} {t} | INFO |   GETSIGNAL AI — ONLINE\n"
START = "{d} {t} | INFO | {k} review starting (blocks the loop)\n"
BRAIN = "{d} {t} | INFO | Brain wait done in {s}s (book managed {p}x during the brain wait)\n"
DONE = "{d} {t} | INFO | {k} review complete{extra}\n"


def _windows(text):
    fd, path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    try:
        with open(path, "w") as fh:
            fh.write(text)
        return analyze._review_windows(logs=[path])
    finally:
        os.unlink(path)


# (b) execv night: starting -> brain line -> banner. No "complete" ever prints.
w = _windows(START.format(d="2026-09-16", t="23:20:13", k="Nightly")
             + BRAIN.format(d="2026-09-16", t="23:28:20", s=487, p=24)
             + BANNER.format(d="2026-09-16", t="23:28:31"))
check("execv night parsed as one window", len(w) == 1)
check("execv night ends by restart", w and w[0][3] == "restart")
check("execv night passes read from the brain line", w and w[0][4] == 24)
check("execv night brain_secs read from the brain line", w and w[0][5] == 487)

# (c) the pre-09-17 shape: count only on the loop's completion line.
w = _windows(START.format(d="2026-09-15", t="23:20:16", k="Nightly")
             + DONE.format(d="2026-09-15", t="23:38:55", k="Nightly",
                           extra=" (book managed 3x during the brain wait)"))
check("completion-line count still read", w and w[0][3] == "complete" and w[0][4] == 3)
check("no brain line -> brain_secs None", w and w[0][5] is None)

# (d) the brain failing fast: 0 passes on a 4-second wait is not a missing hook.
w = _windows(START.format(d="2026-09-18", t="23:20:00", k="Nightly")
             + BRAIN.format(d="2026-09-18", t="23:20:05", s=4, p=0)
             + DONE.format(d="2026-09-18", t="23:20:40", k="Nightly",
                           extra=" (book managed 0x during the brain wait)"))
check("fast brain failure keeps passes=0 and secs=4", w and w[0][4] == 0 and w[0][5] == 4)

# (e) nightly then weekly: the nightly's brain line must not leak into the weekly.
w = _windows(START.format(d="2026-09-20", t="23:20:00", k="Nightly")
             + BRAIN.format(d="2026-09-20", t="23:29:00", s=540, p=27)
             + BANNER.format(d="2026-09-20", t="23:29:10")
             + START.format(d="2026-09-20", t="23:30:19", k="Weekly")
             + DONE.format(d="2026-09-20", t="23:30:41", k="Weekly", extra=""))
check("nightly+weekly -> two windows", len(w) == 2)
check("nightly keeps its brain count", w and w[0][2] == "nightly" and w[0][4] == 27)
check("weekly does not inherit the nightly's brain line",
      len(w) == 2 and w[1][2] == "weekly" and w[1][4] is None and w[1][5] is None)

print(f"test_brain_keepalive: {PASSED}/{PASSED + len(FAILED)} passed")
for f in FAILED:
    print("  FAIL:", f)
sys.exit(1 if FAILED else 0)
