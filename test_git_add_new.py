"""Pins the 2026-09-06 fix: version_push must commit NEW files, not just changed ones.

`git add -u` stages only files git already tracks. For as long as version_push
has existed, every new module a nightly session wrote was therefore left out of
every commit -- silently, because the commit itself succeeds. On 2026-09-06
`git ls-files` showed brand.py and io_safe.py absent from the repository while
tg.py, tracker.py and review.py were all importing them (a fresh clone could not
start the bot), together with five test files -- the safety net missing from the
backup of the thing it protects.

The two assertions that matter pull in opposite directions:

  * it must pick up new source, or the bug is back;
  * it must NEVER pick up config.py, which holds the API keys and is ignored on
    line 2 of .gitignore. --exclude-standard is the only thing standing between
    this function and a secret in a public repo, so it is tested against a real
    scratch repo rather than a stub.

Run: python3 test_git_add_new.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/root/trade")

from review import _untracked_source, version_push

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


# ── A real scratch repo: --exclude-standard behaviour cannot be stubbed ───────
tmp = tempfile.mkdtemp(prefix="gitaddnew_")


def git_in(repo):
    def _g(*args):
        return subprocess.run(["git", "-C", repo] + list(args),
                              capture_output=True, text=True, timeout=60)
    return _g


try:
    g = git_in(tmp)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")

    def write(rel, body="x = 1\n"):
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True) if "/" in rel else None
        with open(path, "w") as fh:
            fh.write(body)

    write(".gitignore", "config.py\n*.log\n__pycache__/\n")
    write("live.py")
    g("add", ".gitignore", "live.py")
    g("commit", "-qm", "base")

    # The four cases, side by side.
    write("brand.py")                     # new module  -> MUST be staged
    write("test_brand.py")                # new test    -> MUST be staged
    write("config.py", "API_KEY = 'sk-secret'\n")   # ignored -> MUST NOT
    write("bot.log", "noise\n")           # ignored     -> MUST NOT
    write("avatar/compose.py")            # nested      -> MUST NOT
    write("avatar/out.png", "binary")     # nested asset-> MUST NOT
    with open(os.path.join(tmp, "live.py"), "a") as fh:
        fh.write("y = 2\n")               # modified tracked -> add -u's job

    got = _untracked_source(g)

    check("new module is picked up", "brand.py" in got)
    check("new test is picked up", "test_brand.py" in got)
    check("SECRET config.py is NOT picked up", "config.py" not in got)
    check("ignored *.log is NOT picked up", "bot.log" not in got)
    check("nested avatar/compose.py is NOT picked up",
          not any(f.startswith("avatar/") for f in got))
    check("no binaries", not any(f.endswith(".png") for f in got))
    check("already-tracked live.py is not re-listed", "live.py" not in got)
    check("result is sorted and deduped", got == sorted(set(got)))
    check(f"exactly the two new .py files (got {got})",
          got == ["brand.py", "test_brand.py"])

    # End to end: staging that list actually puts them in the commit.
    g("add", "-u")
    g("add", *got)
    g("commit", "-qm", "v1")
    tracked = g("ls-files").stdout.split()
    check("brand.py is in the repo after the push", "brand.py" in tracked)
    check("config.py is STILL not in the repo", "config.py" not in tracked)

    # ── Failure mode: git unavailable must return [], never raise ────────────
    def broken(*a):
        raise OSError("git missing")

    check("git failure returns [] rather than raising",
          _untracked_source(broken) == [])

    def nonzero(*a):
        return subprocess.CompletedProcess(a, 128, "", "fatal: not a git repo")

    check("git non-zero returns []", _untracked_source(nonzero) == [])

    # An empty result must not become a bare `git add` (which exits non-zero).
    empty_repo = tempfile.mkdtemp(prefix="gitaddempty_")
    try:
        g2 = git_in(empty_repo)
        g2("init", "-q")
        check("clean tree yields no new source", _untracked_source(g2) == [])
    finally:
        shutil.rmtree(empty_repo, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── SOURCE ORDER: the guard against a bare `git add` ─────────────────────────
# _git("add") with no paths exits non-zero. The call must be conditional.
SRC = open("/root/trade/review.py").read()
# Match the `def` line without its parameter list. version_push gained
# (repo, do_push) on 2026-09-20 so it could be exercised against a scratch
# repo, and a needle pinned to the empty-parens spelling took this whole
# suite down with a bare ValueError -- [[reference_stale_instruments]]: the
# needle must describe what it is looking for, not how it was written once.
body = SRC[SRC.index("def version_push("):]
body_nc = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))

check("version_push calls _untracked_source", "_untracked_source(_git)" in body_nc)
check("the add is guarded by a truthiness check", "if _new:" in body_nc)
check("--exclude-standard is still present in the helper",
      '"--exclude-standard"' in SRC)
check("add -u is still there (this supplements, not replaces)",
      '_git("add", "-u")' in body_nc)
check("NOT switched to the unsafe blanket add -A", '"add", "-A"' not in SRC)

# ── The live repo: the files that prompted this must now be committable ──────
real = git_in("/root/trade")
live_new = _untracked_source(real)
check("live repo: config.py never listed", "config.py" not in live_new)
check("live repo: no nested paths", not any("/" in f for f in live_new))
check("live repo: every entry is a .py", all(f.endswith(".py") for f in live_new))

# ── Result ───────────────────────────────────────────────────────────────────
if FAILED:
    print(f"test_git_add_new: {PASSED} passed, {len(FAILED)} FAILED")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"test_git_add_new: {PASSED}/{PASSED} passed")
