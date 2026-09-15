"""The single-instance lock must survive an in-place (execv) restart.

review._self_improve() restarts the bot with os.execv, which keeps the PID
and skips atexit. The lockfile therefore still names the restarting process
itself, and a guard that only asks "is that pid alive?" answers yes -- about
itself -- and exits. Observed 2026-09-13 23:29:35: "Another instance already
running (PID 727691). Exiting." followed 32 s later by a systemd restart.

Pins: own pid -> proceed; live foreign pid -> exit; dead pid -> proceed;
corrupt file -> proceed. Runs against a temp lockfile, never the real one.
"""
import os, sys, tempfile
sys.path.insert(0, "/root/trade")
import live

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")

_tmp = tempfile.NamedTemporaryFile(prefix="getsignalz-test-", suffix=".pid", delete=False)
_tmp.close()
live.LOCKFILE = _tmp.name
me = os.getpid()

def _acquire_raises():
    try:
        live._acquire_lock()
        return False
    except SystemExit:
        return True

print("── own pid (the execv case) ──")
open(live.LOCKFILE, "w").write(str(me))
check("own pid in the lockfile does not exit", not _acquire_raises())
check("lockfile still names us afterwards", open(live.LOCKFILE).read().strip() == str(me))

print("── a live foreign process ──")
# PID 1 is always alive; it is not us, so the guard must refuse.
open(live.LOCKFILE, "w").write("1")
check("live foreign pid exits", _acquire_raises())
check("lockfile left untouched on refusal", open(live.LOCKFILE).read().strip() == "1")

print("── a dead process ──")
# Find a pid that is not running: probe upward from a large number.
dead = 2**22 - 7
while True:
    try:
        os.kill(dead, 0)
        dead -= 1
    except ProcessLookupError:
        break
    except PermissionError:
        dead -= 1
open(live.LOCKFILE, "w").write(str(dead))
check("dead pid is stale -> proceed", not _acquire_raises())
check("lockfile now names us", open(live.LOCKFILE).read().strip() == str(me))

print("── a corrupt lockfile ──")
open(live.LOCKFILE, "w").write("not-a-pid")
check("corrupt lockfile -> proceed", not _acquire_raises())
check("lockfile now names us", open(live.LOCKFILE).read().strip() == str(me))

print("── release only removes our own lock ──")
open(live.LOCKFILE, "w").write("1")
live._release_lock()
check("a newer owner's lock is left alone", os.path.exists(live.LOCKFILE))
open(live.LOCKFILE, "w").write(str(me))
live._release_lock()
check("our own lock is removed", not os.path.exists(live.LOCKFILE))

try:
    os.unlink(live.LOCKFILE)
except FileNotFoundError:
    pass
print(f"\ntest_lock: {passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
