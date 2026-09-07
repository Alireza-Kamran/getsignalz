"""Pin the trade->signal join so a schema date can never again masquerade as a
market fact.

journal["signals"] does not exist before 2026-08-10T17:01. Six closed trades
were opened before that instant, so they join to nothing. Two readers treated
"no signal row" as "did not qualify":

  1. ENTRY CONDITION BANDS did `if not sig or r is None: continue` with no
     counter, silently computing the whole table on 14 of 20 closed trades. The
     kept and dropped cohorts differ (WR 36% vs 50%, meanR +0.266 vs -0.050),
     so the table's baseline is neither of the two figures printed in the
     report's headline.
  2. _edge_confidence scored only trades with a signal row as qualifying, so
     the Trust Score sample component read 14/20 instead of 20/20 -- a 7.5
     point understatement on the owner's dashboard.

The invariant that settles it: under S2 the only code path that opens a
position is strategy2.signal returning a setup, so THE EXISTENCE OF A CLOSED
TRADE IS THE EVIDENCE THAT A SIGNAL FIRED. The journal row records that fact;
it is not the fact. See [[reference_classify_by_invariant]].

Run: python3 test_signal_join.py
"""
import sys
sys.path.insert(0, "/root/trade")

import analyze
from analyze import _entry_bands, _edge_confidence, SIGNAL_LOG_FROM

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL:", label)


def trade(coin, opened, r=-1.0, entry=100.0, sl=99.0, result="sl"):
    """A closed LONG whose realised R is exactly `r`.

    _r_of measures off the ORIGINAL stop: (exit-entry)*dir / |entry-sl|. With
    entry 100 and sl 99 the risk unit is 1.0, so exit = 100 + r.
    """
    return {"coin": coin, "open_time": opened, "entry": entry, "sl": sl,
            "sl_orig": sl, "exit": entry + r * abs(entry - sl), "direction": 1,
            "leverage": 10, "lev_pct": r * 10.0, "result": result}


def signal(coin, t, rsi=22.0, adx=22.0, direction=1, stretch=3.2):
    return {"coin": coin, "time": t, "rsi": rsi, "adx": adx,
            "direction": direction, "fired": True, "score": 0,
            "reasons": [f"RSI {rsi} (oversold)", f"{stretch} ATR from mean",
                        f"ADX {adx} (ranging, not trending)"]}


# ── 1. _entry_bands returns its leftovers rather than dropping them ──────
print("1. _entry_bands reports what it could not band")

trades = [
    trade("ARB",  "2026-07-27T16:01:40+00:00", r=-0.87),  # pre-signal-log
    trade("ETH",  "2026-08-01T20:01:00+00:00", r=+0.98),  # pre-signal-log
    trade("BTC",  "2026-08-10T17:01:38+00:00", r=-0.99),  # joins
    trade("SOL",  "2026-08-16T23:09:00+00:00", r=+2.48),  # joins
]
sigs = [signal("BTC", "2026-08-10T17:01:38+00:00", rsi=22.8, adx=22.8),
        signal("SOL", "2026-08-16T23:09:00+00:00", rsi=21.3, adx=19.9)]

banded, dropped = _entry_bands(trades, sigs)
ok(len(dropped) == 2, "two unjoinable trades are returned, not swallowed")
ok({d[0] for d in dropped} == {"ARB", "ETH"}, "the right two are returned")
ok(all(d[3] == "predates the signal log" for d in dropped),
   "reason names the schema date, not a market condition")
ok(all(d[2] is not None for d in dropped),
   "a dropped trade still carries its R, so the caller can state the bias")

# The banded cells must cover exactly the two joinable trades.
adx_cells = {k: v for k, v in banded.items() if k.startswith("ADX")}
ok(sum(v["n"] for v in adx_cells.values()) == 2,
   "ADX cells total the JOINED trades only")

# ── 2. the drop is by DATE, and the date is the discriminator ────────────
print("2. the cut is a date, and the two cohorts differ")

ok(SIGNAL_LOG_FROM == "2026-08-10T17:01", "signal-log epoch is pinned")
# A trade opened AFTER the epoch that still fails to join gets a different
# reason -- that one really is a join failure and must not be blamed on the
# schema.
late = [trade("DOGE", "2026-08-19T15:01:00+00:00", r=-0.95)]
_b, d2 = _entry_bands(late, sigs)
ok(len(d2) == 1 and d2[0][3] == "no signal within 2h of entry",
   "a post-epoch miss is reported as a join failure, not as a schema date")

# A trade with no derivable R is reported separately from an unjoined one.
no_r = [{"coin": "X", "open_time": "2026-08-20T10:00:00+00:00", "result": "sl"}]
_b3, d3 = _entry_bands(no_r, sigs)
ok(len(d3) == 1 and d3[0][2] is None and "no R" in d3[0][3],
   "a missing entry stop is its own reason, distinct from a missing signal")

# ── 3. nothing is silently discarded: kept + dropped == input ────────────
print("3. conservation -- every input trade is accounted for exactly once")

for pop in (trades, trades + late + no_r, [], late):
    b, d = _entry_bands(pop, sigs)
    joined = len([t for t in pop
                  if analyze._sig_for(t, sigs) and analyze._r_of(t) is not None])
    ok(joined + len(d) == len(pop),
       f"banded + dropped == input for n={len(pop)}")

# ── 4. _edge_confidence classifies on the invariant, not the record ──────
print("4. Trust Score counts every S2 trade, signal row or not")

journal = {"trades": trades, "signals": sigs}
s2_cfg = {"engine": "S1_DISABLED — live engine is strategy2.py", "min_score": 7}

pts, meta = _edge_confidence(journal, s2_cfg)
ok(meta["n"] == 4,
   "all 4 closed S2 trades qualify — a trade exists only because a signal fired")

# The regression this guards: scoring on the presence of a signal ROW.
by_row = sum(1 for t in trades if analyze._sig_for(t, sigs))
ok(by_row == 2, "the old record-based test would have counted only 2")
ok(meta["n"] > by_row,
   "invariant-based count strictly exceeds record-based count on this book")

# Sample component is min(n,20)/20*25. 4 -> 5.0 pts; the old count 2 -> 2.5.
ok(abs(pts - (min(meta["n"], 20) / 20 * 25 + (15.0 if meta["n"] >= 5
        and meta["avg_pct"] > 0 else 0.0))) < 1e-9,
   "points follow the documented formula")

# ── 5. the S1 branch is untouched -- the fix is confined to S2 ───────────
print("5. S1 scoring path is not affected")

s1_cfg = {"engine": "S1 active", "min_score": 7}
s1_sigs = [dict(signal("BTC", "2026-08-10T17:01:38+00:00"), score=8),
           dict(signal("SOL", "2026-08-16T23:09:00+00:00"), score=3)]
_p1, m1 = _edge_confidence({"trades": trades, "signals": s1_sigs}, s1_cfg)
ok(m1["n"] == 1,
   "under S1 only the score>=min_score trade qualifies (invariant does not apply)")

# And an S2 book with zero signal rows at all must still count its trades.
_p2, m2 = _edge_confidence({"trades": trades, "signals": []}, s2_cfg)
ok(m2["n"] == 4, "S2 count survives a completely empty signal log")

# ── 6. the real book: the numbers this test was written for ──────────────
print("6. against the live journal")

try:
    real = analyze.load_journal()
    rt = [t for t in real.get("trades", []) if t.get("result")]
    rs = real.get("signals", [])
    if rt:
        rb, rd = _entry_bands(rt, rs)
        ok(len(rd) + len([t for t in rt if analyze._sig_for(t, rs)
                          and analyze._r_of(t) is not None]) == len(rt),
           "live book: every closed trade is banded or named as dropped")
        _rp, rm = _edge_confidence(real, analyze.load_config())
        ok(rm["n"] == len(rt),
           f"live book: Trust Score sample is all {len(rt)} closed trades")
    else:
        print("  (no closed trades in journal; skipped)")
except Exception as e:  # pragma: no cover
    print("  (live journal unreadable, skipped):", e)

# ── 7. the report prints coverage before the cells ───────────────────────
print("7. the report states coverage where a reader will see it")

try:
    rep = analyze.full_report()
    i = rep.index("ENTRY CONDITION BANDS")
    seg = rep[i:i + 2500]
    ok("COVERAGE:" in seg, "COVERAGE line is present")
    ok("NOT BANDED" in seg, "the dropped cohort is printed")
    ok(seg.index("COVERAGE:") < seg.index("ADX "),
       "coverage appears BEFORE the first cell, not in a footnote")
    ok("CONTINUATION" in rep, "the conditional excursion curve is present")
    ok("censored by our own exit" in rep,
       "levels at/above TRAIL_START_R are marked as censored")
except Exception as e:  # pragma: no cover
    print("  (full_report failed):", e)
    FAIL += 1

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
