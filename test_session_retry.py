#!/usr/bin/env python3
"""Guards on self_improve.sh's retry scheduling.

Why this exists: between 2026-07-24 and 08-31 the 02:00 nightly session failed
14 times and nothing ever re-ran it, so the bot traded unsupervised for four
consecutive days (08-28..08-31). Seven of those failures were usage limits that
reset within hours -- recoverable by simply trying again. The retry that fixes
that is only safe because of ONE guard: a retry must be a no-op when today's
session already succeeded. If that guard breaks, every day runs three full
sessions -- triple usage burn, triple commits, triple owner DMs.

Method: redirect the RESOURCES, don't stub the caller. PATH points at stub
`claude` and `python3` binaries; the log, marker and lock paths come from env
vars. This is the lesson of test_ratchet.py case 8 -- on 2026-08-24 that suite
stubbed call sites, missed a third writer one level down, and wrote its fixture
over the live state.json, disabling the ratchet on an open position for 24h.
Case 7 below sha256s the production files to prove this suite never touches them.
"""
import hashlib
import os
import pathlib
import signal
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

SCRIPT = "/root/trade/self_improve.sh"
REAL_LOG = "/root/trade/selflearn.log"
REAL_MARKER = "/root/trade/.last_session"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

failures = 0


def sha(path):
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def check(label, expected, actual):
    global failures
    if expected == actual:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} — expected {expected!r} got {actual!r}")
        failures += 1


tmp = tempfile.mkdtemp(prefix="sessretry")
binp = os.path.join(tmp, "bin")
os.makedirs(binp)

# Stub claude: records that it ran, exits with $FAKE_RC.
pathlib.Path(binp, "claude").write_text(
    '#!/bin/bash\necho invoked >> "$SENTINEL"\n'
    'echo "stub session transcript"\nexit ${FAKE_RC:-0}\n')
# Stub python3 so the notification branches record themselves instead of
# sending real Telegram DMs. Records ALL args on one line: the script invokes
# `python3 - "$HEAD" "$SNIPPET"`, so argv[1] is the literal "-" for stdin and
# the message text is argv[2..].
# Records the whole invocation -- args AND the heredoc on stdin -- because the
# success branch passes only $MODE as an arg and puts its message in the Python
# source itself.
pathlib.Path(binp, "python3").write_text(
    '#!/bin/bash\n{ echo "$*"; cat; } | tr "\\n" " " >> "$PYCALLS"\n'
    'echo >> "$PYCALLS"\nexit 0\n')
for f in ("claude", "python3"):
    os.chmod(os.path.join(binp, f), 0o755)

LOG_BEFORE, MARKER_BEFORE = sha(REAL_LOG), sha(REAL_MARKER)

# Pre-flight: refuse to run at all unless the script still honours the stub
# seam. If someone reverts that line, every case below would silently launch a
# real nightly session instead of failing. Read the source -- no amount of
# stubbing catches a missing seam (same reasoning as test_scan_stale.py's
# source-order assertion).
if "SELF_IMPROVE_BIN_PREFIX" not in pathlib.Path(SCRIPT).read_text():
    raise SystemExit(f"ABORT: {SCRIPT} no longer honours SELF_IMPROVE_BIN_PREFIX; "
                     "running this suite would launch real claude sessions.")


def run(mode, marker, n, fake_rc=0):
    """Invoke self_improve.sh with every resource redirected into tmp."""
    env = dict(os.environ)
    # NOT env["PATH"] — self_improve.sh pins PATH and would discard it, then run
    # the REAL claude. Use the script's own seam instead.
    env["SELF_IMPROVE_BIN_PREFIX"] = binp
    env["FAKE_RC"] = str(fake_rc)
    paths = {k: os.path.join(tmp, f"{k.lower()}.{n}")
             for k in ("SENTINEL", "PYCALLS")}
    paths["SELF_IMPROVE_LOG"] = os.path.join(tmp, f"log.{n}")
    paths["SELF_IMPROVE_MARKER"] = os.path.join(tmp, f"marker.{n}")
    paths["SELF_IMPROVE_LOCK"] = os.path.join(tmp, f"lock.{n}")
    env.update(paths)
    for k in ("SENTINEL", "PYCALLS", "SELF_IMPROVE_LOG"):
        pathlib.Path(paths[k]).write_text("")
    mk = pathlib.Path(paths["SELF_IMPROVE_MARKER"])
    if marker is None:
        mk.unlink(missing_ok=True)
    else:
        mk.write_text(marker + "\n")
    cmd = ["bash", SCRIPT] + ([mode] if mode else [])
    # start_new_session so a timeout can kill the whole group. subprocess.run's
    # timeout only kills the bash it spawned, leaving `timeout 3600 claude`
    # orphaned to ppid=1 — an unsupervised acceptEdits session running against
    # the live repo. That happened once; it must not be possible again.
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
        raise SystemExit(
            "ABORT: self_improve.sh did not return within 30s. The stub was "
            "bypassed and a real claude session was launched (process group "
            "killed). Check that SELF_IMPROVE_BIN_PREFIX is still honoured.")
    read = lambda k: pathlib.Path(paths[k]).read_text() if os.path.exists(paths[k]) else ""
    return {
        "sentinel": read("SENTINEL").strip(),
        "pycalls": read("PYCALLS"),
        "log": read("SELF_IMPROVE_LOG"),
        "marker": read("SELF_IMPROVE_MARKER").strip(),
    }


print("1. retry is a NO-OP when today's session already succeeded")
r = run("--retry", f"{TODAY} 02:08 scheduled", 1)
check("claude not invoked", "", r["sentinel"])
check("nothing appended to log", "", r["log"])
check("marker left alone", f"{TODAY} 02:08 scheduled", r["marker"])

print("2. retry RUNS when the marker is stale (yesterday)")
r = run("--retry", f"{YDAY} 02:08 scheduled", 2)
check("claude invoked", "invoked", r["sentinel"])
check("marker advanced to today", TODAY, r["marker"].split()[0])
check("mode recorded in marker", "--retry", r["marker"].split()[2])

print("3. retry RUNS when no marker exists at all (02:00 never fired)")
r = run("--retry", None, 3)
check("claude invoked", "invoked", r["sentinel"])

print("4. the scheduled 02:00 run ignores the marker and always runs")
r = run("", f"{TODAY} 02:08 scheduled", 4)
check("claude invoked", "invoked", r["sentinel"])

print("5. notification routing by mode (the alarm-fatigue budget)")
r = run("--retry", f"{YDAY} 02:08 scheduled", 5, fake_rc=1)
check("failed retry sends NO dm", "", r["pycalls"].strip())
check("failed retry writes no marker", YDAY, r["marker"].split()[0])
r = run("--retry-last", f"{YDAY} 02:08 scheduled", 6, fake_rc=1)
check("failed retry-last escalates", 1, r["pycalls"].count("No nightly session completed"))
r = run("", None, 7, fake_rc=1)
check("failed 02:00 run still alerts", 1, r["pycalls"].count("likely failed"))
r = run("--retry", f"{YDAY} 02:08 scheduled", 8)
check("recovered retry DMs success", 1, r["pycalls"].count("recovered on"))

print("6. a held lock makes a concurrent run skip instead of doubling up")
import fcntl
lock_path = os.path.join(tmp, "lock.9")
held = open(lock_path, "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
env = dict(os.environ)
env["SELF_IMPROVE_BIN_PREFIX"] = binp
env.update({"SENTINEL": os.path.join(tmp, "sentinel.9"),
            "PYCALLS": os.path.join(tmp, "pycalls.9"),
            "SELF_IMPROVE_LOG": os.path.join(tmp, "log.9"),
            "SELF_IMPROVE_MARKER": os.path.join(tmp, "marker.9"),
            "SELF_IMPROVE_LOCK": lock_path})
for k in ("SENTINEL", "PYCALLS", "SELF_IMPROVE_LOG"):
    pathlib.Path(env[k]).write_text("")
pathlib.Path(env["SELF_IMPROVE_MARKER"]).unlink(missing_ok=True)
subprocess.run(["bash", SCRIPT, "--retry"], env=env, capture_output=True, timeout=30)
held.close()
check("claude not invoked while locked", "",
      pathlib.Path(env["SENTINEL"]).read_text().strip())
check("skip is logged", 1,
      pathlib.Path(env["SELF_IMPROVE_LOG"]).read_text().count("holds the lock"))

print("7. production files untouched by this suite")
check("selflearn.log unchanged", LOG_BEFORE, sha(REAL_LOG))
check("marker unchanged", MARKER_BEFORE, sha(REAL_MARKER))

print(f"\nFAILURES: {failures}")
raise SystemExit(1 if failures else 0)
