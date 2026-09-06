"""
Crash-safe JSON writes.

Every state file in this project was written as `open(path, "w")` followed by
`json.dump`. That truncates the file before a single byte of the new content is
written, so the window between truncate and flush is a window in which the file
is empty. Anything that ends the process in that window -- a crash, an OOM kill,
`systemctl restart`, a full disk -- leaves the file empty or half-written.

state.json is the whole book: open positions, closed_trades, stats. journal.json
is the trade history. Losing either is unrecoverable, and it has already
happened once: test_null_record.py records that the OP trade of 2026-08-13 was
"rebuilt by an ad-hoc repair script after save_state erased it".

os.replace() is atomic on POSIX when source and destination are on the same
filesystem, so a reader sees either the whole old file or the whole new one and
never a torn one. The tmp file is therefore created in the SAME directory as the
target, not in /tmp, which may be a different mount.
"""
import json
import os
import tempfile

__all__ = ["atomic_write_json", "atomic_write_text", "read_json_with_fallback"]


def _atomic_replace(path: str, data: str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    # Keep one generation. This is what read_json_with_fallback recovers from,
    # and it is written BEFORE the replace so it always holds a complete file.
    if os.path.exists(path):
        try:
            with open(path) as src:
                prev = src.read()
            if prev.strip():
                with open(path + ".bak", "w") as bak:
                    bak.write(prev)
                    bak.flush()
                    os.fsync(bak.fileno())
        except OSError:
            pass  # a missing backup must never block the write itself

    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    # mkstemp creates 0600. Without this the atomic write silently tightens the
    # permissions of every file it touches on its first pass -- state.json went
    # 644 -> 600 the first time this shipped. Preserve what was there; fall back
    # to the process umask for a file being created for the first time.
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except OSError:
        cur = os.umask(0); os.umask(cur)
        os.chmod(tmp, 0o666 & ~cur)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())      # data on disk before the rename is visible
        os.replace(tmp, path)         # atomic
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj, indent=2, default=str):
    """Serialise FIRST, write second.

    json.dump() streams straight to the file handle, so an object it cannot
    serialise (a stray datetime, a NaN) raises partway through and leaves a
    partial file behind. Building the whole string first means a serialisation
    failure raises before the target is touched at all.
    """
    atomic_write_text(path, json.dumps(obj, indent=indent, default=default))


def atomic_write_text(path: str, text: str):
    _atomic_replace(path, text)


def read_json_with_fallback(path: str, logger=None):
    """Read `path`, falling back to `path + ".bak"` if it is missing or corrupt.

    Returns (obj, source) where source is "primary", "backup" or None. Callers
    must treat None as "could not read", never as "empty" -- silently returning
    a fresh skeleton on a corrupt read is what turns a torn file into an
    apparently-empty trade history.
    """
    for candidate, tag in ((path, "primary"), (path + ".bak", "backup")):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate) as f:
                obj = json.load(f)
            if tag == "backup" and logger:
                logger(f"{path} was unreadable — recovered from {candidate}")
            return obj, tag
        except (json.JSONDecodeError, OSError) as e:
            if logger:
                logger(f"{candidate} failed to parse ({e})")
    return None, None


# Deliberately NOT routed through this module:
#   live.py  HEARTBEAT_FILE   written every loop tick; an fsync per 20s buys
#                             nothing, and a torn read is treated as "stale".
#   live.py  LOCKFILE         a pid, rewritten on every start.
#   live.py  REVIEW_LATCH     its own docstring says a failure degrades to the
#                             old in-memory behaviour rather than breaking.
#   review.py REVIEW_STATE    a single Telegram message id, regenerated nightly.
# Each is small, high-frequency and trivially regenerable. Adding fsync to them
# would cost I/O on the trading loop for no recoverable data.
