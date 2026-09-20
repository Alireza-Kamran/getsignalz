"""Pins the 2026-09-20 fix: the CHANGELOG entry must describe the commit it ships.

`version_push` staged the working tree with `git add -u` but built its entry text
from `.night_report.json`, which only the INNER ai_brain writes to. The OUTER
nightly session (self_improve.sh -> claude -p) edits files directly, so its work
was committed and then described as nothing at all.

Audited on 2026-09-20 across the whole history: **18 nightly commits shipped 46
changed .py files under an entry reading "No changes — all parameters within
target bounds."** v1.53.1 committed 112 changed lines of analyze.py that way.

Worse, `run_ai_brain` returns its failures in `result["error"]` rather than
raising, so review.py's `except` never fired on a dead brain. On 2026-09-18 the
brain died in 8 seconds on a usage-credit error and v1.53.2 published a clean
bill of health for a review that never looked at anything. That is
[[reference_silent_early_return]] promoted to the permanent public record: "found
nothing" and "never looked" were byte-identical.

The three states must stay distinguishable:
  * nothing needed changing        -> "No changes — all parameters within..."
  * the reviewer never ran         -> "Review incomplete", and the commit says so
  * code changed without a declare -> the file list, taken from the staged diff

Tested against a real scratch repo with do_push=False, because the bug was
invisible to source reading -- both halves looked correct in isolation.

Run: python3 test_changelog_truth.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/root/trade")

import review

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


class _StubTg:
    """version_push DMs the owner on several paths; none of them may fire here."""
    sent = []

    @staticmethod
    def dm_owner(msg):
        _StubTg.sent.append(msg)

    @staticmethod
    def esc(x):
        return str(x)

    @staticmethod
    def send_version_update(*a, **k):
        raise AssertionError("send_version_update must not run with do_push=False")


def make_repo():
    tmp = tempfile.mkdtemp(prefix="chglog_")

    def g(*args):
        return subprocess.run(["git", "-C", tmp] + list(args),
                              capture_output=True, text=True, timeout=60)

    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    with open(os.path.join(tmp, "VERSION"), "w") as f:
        f.write("1.53.3\n")
    with open(os.path.join(tmp, "CHANGELOG.md"), "w") as f:
        f.write("# Changelog\n\n---\n")
    with open(os.path.join(tmp, "analyze.py"), "w") as f:
        f.write("x = 1\n")
    with open(os.path.join(tmp, "strategy_config.json"), "w") as f:
        json.dump({"last_updated": "2026-09-19"}, f)
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    return tmp, g


def run_push(tmp, report):
    with open(os.path.join(tmp, ".night_report.json"), "w") as f:
        json.dump(report, f)
    return review.version_push(repo=tmp, do_push=False)


BASE_STATS = {"trades": 28, "win_rate": 35.7, "total_pnl": 144.7,
              "wins": 10, "losses": 18}

_real_tg = review.tg
review.tg = _StubTg
repos = []

try:
    # ── 1. Quiet night: nothing changed, brain ran fine ──────────────────────
    tmp, g = make_repo(); repos.append(tmp)
    title = run_push(tmp, {"date": "2026-09-20", "rule_changes": [],
                           "code_edits": [], "brain_error": None,
                           "stats": BASE_STATS})
    body = open(os.path.join(tmp, "CHANGELOG.md")).read()
    check("1a quiet night keeps the all-clear",
          "No changes — all parameters within target bounds." in body)
    check("1b quiet night adds no file list", "Files changed this session" not in body)
    check("1c quiet night title is bare", title == "v1.53.4 — nightly 2026-09-20")

    # A `last_updated` bump must NOT count as a change, or the signal dies.
    tmp, g = make_repo(); repos.append(tmp)
    with open(os.path.join(tmp, "strategy_config.json"), "w") as f:
        json.dump({"last_updated": "2026-09-20"}, f)
    run_push(tmp, {"date": "2026-09-20", "rule_changes": [], "code_edits": [],
                   "brain_error": None, "stats": BASE_STATS})
    body = open(os.path.join(tmp, "CHANGELOG.md")).read()
    check("1d strategy_config date bump alone is still a quiet night",
          "No changes — all parameters within target bounds." in body
          and "Files changed this session" not in body)

    # ── 2. THE BUG: session edited code, declared nothing ───────────────────
    tmp, g = make_repo(); repos.append(tmp)
    with open(os.path.join(tmp, "analyze.py"), "w") as f:
        f.write("x = 2  # the outer session's work\n")
    with open(os.path.join(tmp, "test_new_thing.py"), "w") as f:
        f.write("y = 1\n")          # untracked: must be picked up too
    title = run_push(tmp, {"date": "2026-09-20", "rule_changes": [],
                           "code_edits": [], "brain_error": None,
                           "stats": BASE_STATS})
    body = open(os.path.join(tmp, "CHANGELOG.md")).read()
    check("2a undeclared code change does NOT claim the all-clear",
          "No changes — all parameters within target bounds." not in body)
    check("2b it lists the changed files", "**Files changed this session (2):**" in body)
    check("2c the modified tracked file is named", "- analyze.py" in body)
    check("2d the new untracked file is named", "- test_new_thing.py" in body)
    check("2e the commit title carries the count",
          title == "v1.53.4 — nightly 2026-09-20: 2 files changed")
    # The entry must never list the two files it is itself writing.
    check("2f VERSION/CHANGELOG excluded from their own list",
          "- VERSION" not in body and "- CHANGELOG.md" not in body)
    # ...and the commit must really contain them.
    shipped = g("diff", "--name-only", "HEAD~1", "HEAD").stdout.split()
    check("2g commit actually contains what the entry claims",
          "analyze.py" in shipped and "test_new_thing.py" in shipped
          and "CHANGELOG.md" in shipped)

    # ── 3. The brain never ran ──────────────────────────────────────────────
    tmp, g = make_repo(); repos.append(tmp)
    title = run_push(tmp, {"date": "2026-09-20", "rule_changes": [], "code_edits": [],
                           "brain_error": "claude CLI error (rc=1): requires usage credits",
                           "stats": BASE_STATS})
    body = open(os.path.join(tmp, "CHANGELOG.md")).read()
    check("3a a dead brain never publishes an all-clear",
          "No changes — all parameters within target bounds." not in body)
    check("3b it says the review did not run", "Review incomplete" in body)
    check("3c it quotes the reason", "requires usage credits" in body)
    check("3d the commit subject says so too",
          title == "v1.53.4 — nightly 2026-09-20: review did not run")

    # ── 4. Declared changes still win (no regression) ───────────────────────
    tmp, g = make_repo(); repos.append(tmp)
    title = run_push(tmp, {"date": "2026-09-20",
                           "rule_changes": ["MIN_SCORE 6→7 (score-6 EV negative)",
                                            "[AI] live.py: fixed the thing"],
                           "code_edits": [{"file": "live.py", "reason": "fixed the thing"}],
                           "brain_error": None, "stats": BASE_STATS})
    body = open(os.path.join(tmp, "CHANGELOG.md")).read()
    check("4a param changes still render", "**Parameter changes (1):**" in body)
    check("4b code improvements still render", "**Code improvements (1):**" in body)
    check("4c code_edits still bump the MINOR version", title.startswith("v1.54.0 "))
    check("4d improvement count preserved", title.endswith(": 2 improvements"))

    # ── 5. Source order: stage BEFORE describing ────────────────────────────
    # The whole fix is an ordering. If a later edit moves the staging back below
    # the entry build, every behavioural test above still passes on stale data.
    src = open("/root/trade/review.py").read()
    i_add   = src.find('_git("add", "-u")')
    i_query = src.find('"--cached", "--name-only"')
    i_entry = src.find('No changes — all parameters within target bounds.')
    i_ver   = src.find('_git("add", "VERSION", "CHANGELOG.md")')
    check("5a staging happens before the staged-diff query", 0 < i_add < i_query)
    check("5b the query happens before the entry text", 0 < i_query < i_entry)
    check("5c VERSION/CHANGELOG staged after the query", i_query < i_ver)

    # ── 6. review.py carries the brain error into the report ────────────────
    # run_ai_brain RETURNS errors; it does not raise. The value must be read off
    # the result dict, not left to the `except`, or state 3 is unreachable.
    check("6a brain_error read from the result dict",
          re.search(r'brain_error\s*=\s*brain_result\.get\("error"\)', src) is not None)
    check("6b brain_error set in the except path too",
          re.search(r'except Exception as brain_err:\s*\n\s*import traceback\s*\n\s*brain_error\s*=', src)
          is not None)
    check("6c brain_error written into .night_report.json",
          re.search(r'"brain_error":\s*brain_error', src) is not None)
    check("6d version_push reads it back",
          re.search(r'brain_error\s*=\s*report\.get\("brain_error"\)', src) is not None)

    # ── 7. No Telegram traffic escaped during the tests ─────────────────────
    check("7a do_push=False stays silent", _StubTg.sent == [])

finally:
    review.tg = _real_tg
    for t in repos:
        shutil.rmtree(t, ignore_errors=True)

print(f"\n{'=' * 60}")
if FAILED:
    print(f"FAILED {len(FAILED)}/{PASSED + len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"test_changelog_truth: {PASSED}/{PASSED} checks passed")
