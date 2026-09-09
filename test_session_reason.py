#!/usr/bin/env python3
"""Guards on how a failed nightly session is EXPLAINED to the owner.

Why this exists: on 2026-09-08 the 02:00 session and both its retries died on
`Your organization has disabled Claude subscription access for Claude Code`.
The SUPERVISION section reported all three as a settings.json permission typo
(02:00) and as our own `MODE: --retry` banner (04:30, 15:00). The bot then
traded unsupervised for three days behind a diagnosis that pointed at a
five-second fix which would have changed nothing.

The cause was `body.splitlines()[0]` -- a guess about WHERE the harness prints
a fatal error. It happened to be right for 14 of 17 historical failures, which
is precisely why nobody looked at it. The replacement classifies on the
invariant instead: a line that also opens a session which exited 0 cannot
explain a different session's non-zero exit.

Method: build synthetic selflearn.log bodies in a temp dir and parse them.
Case 9 sha256s the production log to prove this suite never writes to it --
the lesson of test_ratchet.py case 8, which on 2026-08-24 wrote its fixture
over the live state.json and disabled the ratchet on an open position for 24h.
"""
import hashlib
import pathlib
import sys
import tempfile

sys.path.insert(0, "/root/trade")
import analyze

REAL_LOG = "/root/trade/selflearn.log"
PERM = ("Permission allow rule (../.claude/settings.json): "
        "Write(/opt/vps-backup/**) is not matched by file permission checks.")
ORG = ("Your organization has disabled Claude subscription access for "
       "Claude Code · Use an Anthropic API key instead")

failures = 0


def check(label, expected, actual):
    global failures
    if expected == actual:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n        expected {expected!r}\n        got      {actual!r}")
        failures += 1


def sha(path):
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


LOG_BEFORE = sha(REAL_LOG)
tmp = tempfile.mkdtemp(prefix="sessreason")


def session(date, tm, body_lines, rc):
    """Render one selflearn.log block exactly as self_improve.sh writes it."""
    head = "=" * 40 + f"\nSELF-LEARN: {date} {tm} UTC\n" + "=" * 40 + "\n"
    body = "\n".join(body_lines)
    return head + body + f"\nSession ended: {tm} UTC (exit {rc})\n\n"


def parse(blocks, name):
    p = pathlib.Path(tmp, name)
    p.write_text("".join(blocks))
    return analyze._session_history(path=str(p), limit=50)


print("\n=== 1. benign preamble must not displace the real error ===")
# The 2026-09-08 02:00 shape: harness warning above the fatal error. The
# warning is proven benign because it also opens a session that exited 0.
rows, _ = parse([
    session("2026-09-06", "02:00", [PERM, "Session complete. All six steps done."], 0),
    session("2026-09-08", "02:00", [PERM, ORG], 1),
], "case1.log")
check("real cause chosen over benign preamble", ORG[:60],
      [r for r in rows if r[2]][0][3])

print("\n=== 2. our own MODE banner is never the reason ===")
# The --retry paths put self_improve.sh's own banner on line 1, so ANY
# positional guess lands on it by construction. This was 2/3 of that night.
rows, _ = parse([
    session("2026-09-06", "02:00", [PERM, "Session complete."], 0),
    session("2026-09-08", "04:30", ["MODE: --retry (no successful session yet today)",
                                    PERM, ORG], 1),
], "case2.log")
got = [r for r in rows if r[2]][0][3]
check("MODE banner rejected", ORG[:60], got)
check("banner not merely truncated into the reason", False, got.startswith("MODE:"))

print("\n=== 3. cause beats remediation hint (the 2026-08-20 shape) ===")
# Real body: "Error: claude native binary not installed." followed by six
# lines of install advice. Taking the LAST survivor would report the advice.
rows, _ = parse([
    session("2026-08-20", "02:00", [
        "Error: claude native binary not installed.",
        "Run: npm install -g @anthropic-ai/claude-code",
        "Or reinstall without --ignore-scripts / --omit=optional.",
    ], 1),
], "case3.log")
check("first survivor, not last", "Error: claude native binary not installed.",
      rows[0][3])

print("\n=== 4. usage limits still take their dedicated path ===")
rows, _ = parse([
    session("2026-08-30", "02:00", [PERM, "You've hit your session limit · resets 3:20am (UTC)"], 1),
    session("2026-08-29", "02:00", ["You've hit your weekly limit · resets 2pm (UTC)"], 1),
], "case4.log")
by_date = {r[0]: r[3] for r in rows}
check("session limit, even below a preamble", "session limit · resets 3:20am (UTC)",
      by_date["2026-08-30"])
check("weekly limit", "weekly limit · resets 2pm (UTC)", by_date["2026-08-29"])

print("\n=== 5. exit 124 is the wall clock, not a log line ===")
rows, _ = parse([
    session("2026-07-28", "02:00", ["Execution error"], 124),
], "case5.log")
check("124 labelled as the timeout", "killed at the 60-minute wall clock", rows[0][3])

print("\n=== 6. SELF-POISONING: a report quoting the error must not excuse it ===")
# Tonight's own session report names ORG in its body. If benign harvested
# whole bodies instead of first lines, that quote would mark ORG benign and
# the 09-08 failures would silently revert to being misreported tomorrow.
rows, _ = parse([
    # establishes PERM as benign, so the failure below has two candidates
    session("2026-09-06", "02:00", [PERM, "Session complete."], 0),
    session("2026-09-09", "02:00", [
        "Session complete. Here's what happened.",
        "",
        f"The three 09-08 failures all read: {ORG}",
        "Fixed in analyze._failure_reason.",
    ], 0),
    session("2026-09-08", "02:00", [PERM, ORG], 1),
], "case6.log")
check("mid-body quote did not launder the cause", ORG[:60],
      [r for r in rows if r[2]][0][3])

print("\n=== 7. no successful session to learn from -> degrade, don't crash ===")
rows, _ = parse([
    session("2026-09-08", "02:00", [PERM, ORG], 1),
], "case7.log")
check("still returns a line", True, bool(rows[0][3]))
check("one failure counted", 1, len(rows))

print("\n=== 8. successful sessions carry no reason, and counts are right ===")
rows, st = parse([
    session("2026-09-06", "02:00", [PERM, "Session complete."], 0),
    session("2026-09-07", "02:00", ["Session complete."], 0),
    session("2026-09-08", "02:00", [PERM, ORG], 1),
], "case8.log")
check("n", 3, st["n"])
check("ok", 2, st["ok"])
check("failed", 1, st["failed"])
check("no reason on a clean exit", [""] * 2, [r[3] for r in rows if r[2] == 0])

print("\n=== 9. production selflearn.log untouched, and every real failure is real ===")
check("selflearn.log unchanged", LOG_BEFORE, sha(REAL_LOG))
rows, _ = analyze._session_history(limit=60)
bad = [(d, t, why) for d, t, rc, why in rows
       if rc and (why.startswith(("MODE:", "Session ")) or why.startswith(PERM[:30]))]
check("no live failure explained by a banner or a benign warning", [], bad)
check("no live failure left unexplained", [],
      [(d, t) for d, t, rc, why in rows if rc and (not why or why == "unknown")])

print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
