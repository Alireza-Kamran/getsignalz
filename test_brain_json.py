#!/usr/bin/env python3
"""A prose reply is a recoverable format error, not a discarded review.

WHY THIS FILE EXISTS
--------------------
The nightly loop has TWO Claude sessions: the outer one (self_improve.sh ->
claude -p) and the inner `ai_brain` that review.py calls at 23:20. Only the
outer one had a health record (SUPERVISION, from selflearn.log). The inner one
failed on two consecutive nights and no report showed it:

  2026-09-20  claude CLI error (rc=1): a malformed permission rule in
              ~/.claude/settings.json -- the CLI refused to start at all.
  2026-09-21  ran for 772 SECONDS, answered in prose ("The `_mfe_seen` helper
              and the excursion floor fix are already fully in place..."), and
              `json.loads(raw)` threw at char 0. The entire review -- thirteen
              minutes of analysis over a 37K-token prompt -- was converted into
              a 300-character truncated error string and thrown away.

The parser was `json.loads(raw)` behind a fence-stripper that only fired when
the reply STARTED with ```. Every other shape was fatal. Three things were
wrong with that, and they are three different bugs:

  1. NO LAYERED EXTRACTION. A fence after a prose preamble, or a bare object
     after a sentence, is trivially recoverable and was not recovered.
  2. THE ANALYSIS WAS DISCARDED. The expensive half of a brain run is the
     reasoning, and it had already been produced and paid for. It was dropped
     on the floor because its WRAPPER was wrong.
  3. NON-DICT JSON WAS ACCEPTED. `json.loads` returns an int for "5", None for
     "null" and a list for "[1,2]". Mutation-checked against the old parser:
     all three were accepted, then raised AttributeError inside
     `parsed.get(...)` and reached the owner as `Brain error: 'int' object has
     no attribute 'get'` -- a schema violation reported as an internal crash,
     which sends the next session hunting in the wrong file.

So the tests below are shaped around the two replies that really arrived, plus
the shapes that would let a bad parse look like a good night. The
`_brain_history` half asserts the reporting invariant that matters: a night the
brain FAILED must never be counted among the nights it ran, and a night whose
wording cannot distinguish the two must be counted as NEITHER.
"""
import sys, json, subprocess

sys.path.insert(0, "/root/trade")
import ai_brain
import analyze

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


# The 2026-09-21 reply, verbatim from the CHANGELOG entry it produced.
PROSE_0921 = (
    "The `_mfe_seen` helper and the excursion floor fix are already fully in "
    "place. The primary improvement for tonight has been the step-lag "
    "decomposition, which is already committed. No further change is warranted."
)

print("\n── _extract_json: shapes that must parse ──")

ok, how = ai_brain._extract_json('{"changes": [], "analysis": "fine"}')
check("bare object parses", ok == {"changes": [], "analysis": "fine"}, how)
check("bare object reports its source", how == "whole reply", how)

ok, how = ai_brain._extract_json('```json\n{"changes": []}\n```')
check("leading fence parses", ok == {"changes": []}, how)

ok, how = ai_brain._extract_json('```\n{"changes": []}\n```')
check("leading fence, no language tag", ok == {"changes": []}, how)

ok, how = ai_brain._extract_json(
    'Here is my analysis for tonight.\n\n```json\n{"changes": [], "n": 1}\n```\n\nDone.')
check("fence AFTER prose parses", ok == {"changes": [], "n": 1}, how)
check("fence after prose is labelled", how == "fenced block", how)

ok, how = ai_brain._extract_json('Sure thing:\n{"changes": [], "n": 2}')
check("prose preamble then bare object", ok == {"changes": [], "n": 2}, how)
check("preamble case is labelled embedded", how == "embedded object", how)

# The brain's own schema puts free prose in "reason"/"summary", and the prompt
# it reads is full of `{{` literals -- so a brace inside a string is the
# EXPECTED shape of a correct reply, not an exotic one.
brace_in_str = '{"reason": "replace {a} with {b}", "changes": []}'
ok, how = ai_brain._extract_json("prose\n" + brace_in_str)
check("brace inside a string does not truncate the span",
      ok == {"reason": "replace {a} with {b}", "changes": []}, ok)

ok, how = ai_brain._extract_json('{"bad": }\n{"changes": [], "good": true}')
check("an unparseable first object does not mask a parseable second",
      ok.get("good") is True, ok)

nested = '{"a": {"b": {"c": 1}}, "changes": []}'
ok, how = ai_brain._extract_json("note:\n" + nested)
check("nested objects close at the right depth", ok["a"]["b"]["c"] == 1, ok)

print("\n── _extract_json: shapes that must NOT be accepted ──")

for bad, label in [
    ("5",     "a bare int"),
    ("null",  "a bare null"),
    ("[1,2]", "a bare array"),
    ('"hi"',  "a bare string"),
]:
    try:
        got, _ = ai_brain._extract_json(bad)
        check(f"{label} is rejected", False, f"accepted {got!r}")
    except ai_brain._NoJSON:
        check(f"{label} is rejected", True)

try:
    ai_brain._extract_json(PROSE_0921)
    check("the real 09-21 prose raises _NoJSON", False, "it parsed")
except ai_brain._NoJSON as e:
    check("the real 09-21 prose raises _NoJSON", True)
    check("_NoJSON carries the text forward, not a truncation",
          e.text == PROSE_0921, e.text[:60])

try:
    ai_brain._extract_json("   \n  ")
    check("empty reply raises _NoJSON", False)
except ai_brain._NoJSON as e:
    check("empty reply raises _NoJSON", True)
    check("empty reply is distinguishable from prose (text is empty)",
          e.text == "")

print("\n── run_ai_brain: the analysis survives a prose reply ──")

_real_run = ai_brain._run_claude


def _fake(out, rc=0, err=""):
    def f(cmd, prompt, keepalive=None, slice_s=None, timeout=None):
        return rc, out, err, 0
    return f


# Case 1: prose, and the repair pass cannot fix it either. This is the 09-21
# night. The point of the fix is that the reasoning is NOT lost.
try:
    ai_brain._run_claude = _fake(PROSE_0921)
    _real_repair = ai_brain._repair_json
    ai_brain._repair_json = lambda t, keepalive=None: (_ for _ in ()).throw(
        ai_brain._NoJSON(t))
    r = ai_brain.run_ai_brain()
    check("prose + failed repair still reports an error", bool(r["error"]))
    check("the error names the format, not a JSONDecodeError line number",
          "prose" in (r["error"] or "").lower(), r["error"])
    check("THE ANALYSIS IS PRESERVED (was discarded before this fix)",
          PROSE_0921[:40] in r["analysis"], repr(r["analysis"])[:80])
    check("no change is applied off an unparsed reply",
          r["changes_applied"] == [])
finally:
    ai_brain._repair_json = _real_repair
    ai_brain._run_claude = _real_run

# Case 2: prose, and the repair pass converts it. The night becomes a normal
# "brain ran, proposed nothing" -- which is the correct reading of the 09-21
# reply, whose content was "nothing further is warranted".
try:
    ai_brain._run_claude = _fake(PROSE_0921)
    _real_repair = ai_brain._repair_json
    ai_brain._repair_json = lambda t, keepalive=None: (
        {"analysis": "nothing to change", "changes": [], "summary": "quiet night"},
        "whole reply")
    r = ai_brain.run_ai_brain()
    check("a repaired prose reply is NOT an error", r["error"] is None, r["error"])
    check("a repaired reply records that it was repaired",
          "repaired" in r.get("json_source", ""), r.get("json_source"))
    check("the repaired summary is used", r["summary"] == "quiet night")
finally:
    ai_brain._repair_json = _real_repair
    ai_brain._run_claude = _real_run

# Case 3: a repair that returns an EMPTY analysis must not erase the prose it
# was built from. `.get(k, "")` would have; `or` does not.
try:
    ai_brain._run_claude = _fake(PROSE_0921)
    _real_repair = ai_brain._repair_json
    ai_brain._repair_json = lambda t, keepalive=None: (
        {"analysis": "", "changes": []}, "whole reply")
    r = ai_brain.run_ai_brain()
    check("an empty repaired analysis falls back to the original prose",
          PROSE_0921[:40] in r["analysis"], repr(r["analysis"])[:80])
finally:
    ai_brain._repair_json = _real_repair
    ai_brain._run_claude = _real_run

# Case 4: the 09-20 night. rc != 0 means the CLI never started, so there is no
# prose to rescue and no repair pass should be attempted.
try:
    ai_brain._run_claude = _fake("", rc=1, err="Permission allow rule ... Write(/opt/vps-backup/**)")
    _real_repair = ai_brain._repair_json
    _called = []
    ai_brain._repair_json = lambda t, keepalive=None: _called.append(1) or (
        {"changes": []}, "x")
    r = ai_brain.run_ai_brain()
    check("a CLI failure is still reported as a CLI failure",
          "rc=1" in (r["error"] or ""), r["error"])
    check("no repair pass is wasted on a CLI that never started", not _called)
finally:
    ai_brain._repair_json = _real_repair
    ai_brain._run_claude = _real_run

print("\n── _brain_history: the reporting invariant ──")

CL = """# Changelog

---

## v1.53.5 — 2026-09-21

⚠️ **Review incomplete — the nightly AI brain did not run.** Error: `answered in prose`

**Files changed this session (1):**
- analyze.py

---

## v1.53.4 — 2026-09-20

No changes — all parameters within target bounds.

---

## v1.53.3 — 2026-09-19

No changes — all parameters within target bounds.

---

## v1.53.0 — 2026-09-16

**Code improvements (2):**
- a
- b

---
"""


def _hist(text, tmp="/tmp/_test_cl.md"):
    with open(tmp, "w") as f:
        f.write(text)
    return analyze._brain_history(path=tmp, limit=99)


rows, st = _hist(CL)
by_date = {r[0]: r for r in rows}

check("a failed night is classified failed", by_date["2026-09-21"][2] == "failed")
check("the failure detail is the recorded error",
      by_date["2026-09-21"][3] == "answered in prose", by_date["2026-09-21"][3])
check("v1.53.4 'No changes' is trustworthy -> clean",
      by_date["2026-09-20"][2] == "clean", by_date["2026-09-20"][2])
check("v1.53.3 'No changes' PREDATES the truth fix -> ambiguous",
      by_date["2026-09-19"][2] == "ambiguous", by_date["2026-09-19"][2])
check("an applied night is classified applied",
      by_date["2026-09-16"][2] == "applied")
check("applied counts its changes", by_date["2026-09-16"][3] == "2 changes",
      by_date["2026-09-16"][3])

check("scored excludes the ambiguous night", st["scored"] == 3, st)
check("ran counts only nights that produced a verdict", st["ran"] == 2, st)
check("failed is counted", st["failed"] == 1, st)
check("ambiguous is reported separately, not as a success",
      st["ambiguous"] == 1, st)
# The invariant: every scored night is exactly one of ran/failed. If a future
# status slips through unclassified into `ran`, this is what catches it.
check("INVARIANT ran + failed == scored", st["ran"] + st["failed"] == st["scored"], st)
check("INVARIANT no night is counted twice",
      st["scored"] + st["ambiguous"] + st["nomark"] == st["n"], st)

# Newest-first must come from the data, not from file position. v1.47.0 is
# physically the FIRST entry in the real CHANGELOG while being dated three
# weeks before its neighbours -- one hand edit is enough to make "first entry
# == latest night" false, and spotting a stale latest night is this section's job.
rows2, _ = _hist(CL.replace("---\n\n## v1.53.5",
                            "---\n\n## v1.40.0 — 2026-08-01\n\nNo changes — all parameters within target bounds.\n\n---\n\n## v1.53.5"))
check("newest-first survives an out-of-order entry at the top of the file",
      rows2[0][0] == "2026-09-21", rows2[0])
check("the out-of-order old entry is still parsed",
      any(r[0] == "2026-08-01" for r in rows2))

# A hand-written owner entry has no brain verdict at all. It must not be scored
# in either direction -- it was not a nightly brain run.
rows3, st3 = _hist(CL + "\n## v1.47.0 — 2026-09-04\n\nOwner-driven session, three pieces of work.\n\n---\n")
check("a hand-written entry is 'nomark', not a success",
      any(r[2] == "nomark" for r in rows3))
check("a hand-written entry does not inflate `ran`", st3["ran"] == st["ran"], st3)

rows4, st4 = _hist("# Changelog\n\n---\n")
check("an empty changelog yields no rows, not a crash", rows4 == [] and st4["n"] == 0)
check("a missing changelog yields no rows, not a crash",
      analyze._brain_history(path="/tmp/_nope_missing.md") == ([], {}))

print("\n── live data sanity ──")
rows5, st5 = analyze._brain_history(limit=5)
check("the real changelog parses", st5.get("n", 0) > 50, st5)
check("the two known failures are found",
      st5["failed"] >= 2, st5)
check("the newest real entry is the newest date",
      rows5[0][0] == max(r[0] for r in rows5), rows5[0])

print(f"\n{'='*52}\n  {passed} passed, {failed} failed\n{'='*52}")
sys.exit(1 if failed else 0)
