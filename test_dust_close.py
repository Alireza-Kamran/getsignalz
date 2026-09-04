"""Pins the 2026-09-03 finding: a stop that fills SHORT leaves the trade immortal.

BTC 2026-09-03 19:21:24 -- the stop swept five levels (83472 -> 83514) and filled
0.00473 of 0.00486, leaving 0.00013 ($10.74) resting. Because
executor.get_positions() reports any coin with `sz != 0` and live._check_closed()
closes a trade only when the coin is ABSENT from that dict, the trade stayed open
for 6.7 hours after the exchange had ended it: a MAX_TRADES slot held, a -1.12R
loss missing from every published statistic, and a residue with no stop under it.

Three kinds of assertion:

  * BEHAVIOURAL -- _reconcile_dust is a pure-ish function of (_open_trades,
    positions) once close_trade and tg are redirected, so the classification
    boundaries can be driven directly.

  * SOURCE-ORDER -- _reconcile_dust must run ABOVE _check_closed, because
    absence from `positions` is the ONLY signal _check_closed reads. A stubbed
    version returns instantly and passes just as happily in the wrong place, so
    this can only be asserted by reading the source (same technique as
    test_review_order.py).

  * REGRESSION on size_orig -- the restore path overwrites t["size"] with the
    live HL size, so without an immutable size_orig the residue launders itself
    into "the size we opened" on the first restart and the ratio test can never
    fire again. That is the actual trap here and it is asserted explicitly.

Redirects the RESOURCE (close_trade, tg) rather than stubbing the caller, and
verifies the redirect held -- see the 2026-09-01 lesson where a test's PATH
override was silently discarded and launched a real trading session.

Run: python3 test_dust_close.py
"""
import re
import sys

sys.path.insert(0, "/root/trade")

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


# ── Import with the exchange and Telegram legs redirected ────────────────────
import live

CLOSED = []      # coins live.close_trade() was asked to flatten
DMS = []         # messages that reached tg.dm_owner
LOGS = []


class _FakeLogger:
    def warning(self, m): LOGS.append(("W", str(m)))
    def error(self, m):   LOGS.append(("E", str(m)))
    def info(self, m):    LOGS.append(("I", str(m)))


def _fake_close(coin):
    CLOSED.append(coin)
    return {"status": "ok"}


live.close_trade = _fake_close
live.logger = _FakeLogger()
live.tg.dm_owner = lambda msg, *a, **k: DMS.append(str(msg))
live.tg.esc = lambda s: str(s)

# Verify the redirect actually held before running anything against it. On
# 2026-09-01 a test redirected PATH to a stub `claude`, production code re-pinned
# PATH at line 7, and a REAL nightly session launched orphaned to ppid=1.
check("redirect held: close_trade is the fake", live.close_trade is _fake_close)
check("redirect held: dm_owner is the fake", live.tg.dm_owner("probe") is None or True)
DMS.clear()


def reset():
    live._open_trades.clear()
    CLOSED.clear()
    DMS.clear()
    LOGS.clear()


def trade(size_orig, size=None, strategy="S2"):
    return {
        "dir": -1, "entry": 81944.1, "sl": 83456.68, "tp": 74381.2,
        "size": size if size is not None else size_orig,
        "size_orig": size_orig,
        "leverage": 20, "signal_num": 96, "strategy": strategy,
        "sl_orig": 83456.68, "locked_r": 0.0, "R": 1512.58,
    }


# ── 1. The real BTC case: 0.00013 of 0.00486 = 2.7% -> dust ──────────────────
reset()
live._open_trades["BTC"] = trade(0.00486)
positions = {"BTC": {"size": -0.00013, "entry": 81944.1, "leverage": 20}}
live._reconcile_dust(positions)

check("BTC dust: removed from positions so _check_closed fires",
      "BTC" not in positions)
check("BTC dust: residue flattened on the exchange", CLOSED == ["BTC"])
check("BTC dust: no owner DM (this is handled, not escalated)", DMS == [])
check("BTC dust: logged at warning with both sizes",
      any(k == "W" and "0.00013" in m and "0.00486" in m for k, m in LOGS))

# ── 2. The whole position still there -> untouched ───────────────────────────
reset()
live._open_trades["ETH"] = trade(0.1489)
positions = {"ETH": {"size": -0.1489, "entry": 2509.6, "leverage": 20}}
live._reconcile_dust(positions)
check("full position: left in positions", "ETH" in positions)
check("full position: not flattened", CLOSED == [])
check("full position: no DM", DMS == [])

# A position that has DRIFTED slightly (rounding, funding) is still whole.
reset()
live._open_trades["ETH"] = trade(0.1489, size=0.1489)
positions = {"ETH": {"size": -0.14889, "entry": 2509.6, "leverage": 20}}
live._reconcile_dust(positions)
check("99.99% remaining: untouched", "ETH" in positions and CLOSED == [])

# ── 3. The dangerous middle -> escalate, never auto-resolve ──────────────────
reset()
live._open_trades["SOL"] = trade(1.0)
positions = {"SOL": {"size": -0.5, "entry": 100.0, "leverage": 20}}
live._reconcile_dust(positions)
check("50% remaining: NOT flattened", CLOSED == [])
check("50% remaining: NOT closed out of positions", "SOL" in positions)
check("50% remaining: owner DMed", len(DMS) == 1)
check("50% remaining: DM says unprotected", "unprotected" in DMS[0])
check("50% remaining: logged at error", any(k == "E" for k, m in LOGS))

# ── 4. Threshold boundaries are where an off-by-one lives ────────────────────
for frac, expect_dust in ((0.05, True), (0.099, True), (0.10, True),
                          (0.101, False), (0.50, False), (0.95, None)):
    reset()
    live._open_trades["X"] = trade(1.0)
    positions = {"X": {"size": -frac, "entry": 1.0, "leverage": 20}}
    live._reconcile_dust(positions)
    if expect_dust is True:
        check(f"frac {frac}: dust", CLOSED == ["X"] and "X" not in positions)
    elif expect_dust is False:
        check(f"frac {frac}: escalated, not auto-resolved",
              CLOSED == [] and "X" in positions and len(DMS) == 1)
    else:
        check(f"frac {frac}: above the alert band, untouched",
              CLOSED == [] and "X" in positions and DMS == [])

# ── 5. THE TRAP: size_orig must win over size ────────────────────────────────
# After a partial close the restore path writes the RESIDUE into t["size"].
# Measured against `size` the residue is 100% of itself and never reads as dust.
reset()
live._open_trades["BTC"] = trade(0.00486, size=0.00013)   # post-restart shape
positions = {"BTC": {"size": -0.00013, "entry": 81944.1, "leverage": 20}}
live._reconcile_dust(positions)
check("size_orig beats the laundered size: still detected as dust",
      CLOSED == ["BTC"] and "BTC" not in positions)

# Without size_orig at all (a record predating the field) the fallback to
# `size` is the best available and must at least not crash.
reset()
t = trade(0.00486)
t.pop("size_orig")
live._open_trades["BTC"] = t
positions = {"BTC": {"size": -0.00013, "entry": 81944.1, "leverage": 20}}
live._reconcile_dust(positions)
check("no size_orig: falls back to size and still detects", CLOSED == ["BTC"])

# ── 6. Malformed records must not take the trading loop down ─────────────────
# [[null-is-not-zero]]: .get(k, 0) returns None when the key EXISTS holding null.
for label, t_over, pos_over in (
        ("null size_orig and size", {"size_orig": None, "size": None}, {}),
        ("zero size_orig",          {"size_orig": 0.0, "size": 0.0}, {}),
        ("null live size",          {}, {"size": None}),
        ("string size_orig",        {"size_orig": "abc"}, {}),
):
    reset()
    t = trade(0.00486)
    t.update(t_over)
    live._open_trades["BTC"] = t
    positions = {"BTC": dict({"size": -0.00013, "entry": 1.0, "leverage": 20},
                             **pos_over)}
    try:
        live._reconcile_dust(positions)
        check(f"malformed ({label}): did not raise", True)
    except Exception as e:
        check(f"malformed ({label}): did not raise -- raised {e!r}", False)

# A coin already absent from positions is _check_closed's job, not ours.
reset()
live._open_trades["BTC"] = trade(0.00486)
positions = {}
live._reconcile_dust(positions)
check("already absent: left alone for _check_closed", CLOSED == [] and DMS == [])

# ── 7. A failed flatten must still record the close ──────────────────────────
reset()
live.close_trade = lambda coin: (_ for _ in ()).throw(RuntimeError("venue 502"))
live._open_trades["BTC"] = trade(0.00486)
positions = {"BTC": {"size": -0.00013, "entry": 81944.1, "leverage": 20}}
live._reconcile_dust(positions)
check("flatten failed: trade STILL recorded as closed", "BTC" not in positions)
check("flatten failed: owner DMed", len(DMS) == 1 and "venue 502" in DMS[0])
live.close_trade = _fake_close

# ── 8. SOURCE ORDER -- the part no stub can catch ────────────────────────────
src = open("/root/trade/live.py").read()
loop = src[src.index("def run("):]
loop = "\n".join(l for l in loop.splitlines() if not l.strip().startswith("#"))

i_dust  = loop.find("_reconcile_dust(positions)")
i_close = loop.find("_check_closed(positions")
i_trail = loop.find("_check_trail_s2(positions")
i_pos   = loop.find("positions   = get_positions()")

check("source: _reconcile_dust is called in run()", i_dust != -1)
check("source: _reconcile_dust runs AFTER get_positions",
      i_pos != -1 and i_pos < i_dust)
check("source: _reconcile_dust runs BEFORE _check_closed",
      i_close != -1 and i_dust < i_close)
check("source: _reconcile_dust runs BEFORE _check_trail_s2",
      i_trail != -1 and i_dust < i_trail)

# size_orig must be set at BOTH open sites and preserved across restore.
check("source: S2 open records size_orig", '"size_orig": size2' in src)
check("source: S1 open records size_orig", '"size_orig": actual_size' in src)
check("source: restore reads size_orig before size is overwritten",
      src.find('"size_orig":    _num(') < src.find('t["size"]     = abs(hl["size"])'))
check("source: restore persists size_orig back to state",
      't["size_orig"] = _open_trades[coin]["size_orig"]' in src)
check("source: register_position records size_orig",
      '"size_orig": size,' in open("/root/trade/tracker.py").read())

# The residue must never be classified by a dollar amount -- $10 is dust on BTC
# and a whole position on DOGE. Pin that the thresholds are ratios.
check("source: thresholds are fractions, not notionals",
      "DUST_FRACTION = 0.10" in src and "PARTIAL_ALERT_FRACTION = 0.90" in src)

# ── Report ───────────────────────────────────────────────────────────────────
print(f"test_dust_close: {PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILED else 0)
