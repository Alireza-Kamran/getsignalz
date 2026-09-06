#!/usr/bin/env python3
"""Channel messages must be bilingual without breaking Persian rendering.

WHY THIS FILE EXISTS
--------------------
Telegram applies the Unicode bidi algorithm PER LINE. A line that mixes Persian
and Latin script -- "سود: +20.32 دلار" -- is reordered at render time, and the
sign routinely lands at the wrong end of the number. The reader sees a loss
where there was a profit. That is why every message here keeps Persian and
English on separate lines and pushes all figures into a <pre> block, which is
unambiguously left-to-right.

ZWNJ (U+200C) is the other trap: invisible in the editor, renders as a stray gap
on many Android fonts, and survives copy-paste into places that then break.

These tests are shaped around the OUTPUT, not the functions: whatever a message
site builds, it has to survive being read on a phone.
"""
import re, sys
sys.path.insert(0, "/root/trade")

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}")


import brand

TAG = re.compile(r"<[^>]+>")
PERSIAN = re.compile(r"[؀-ۿ]")
LATIN   = re.compile(r"[A-Za-z]")


def mixed_lines(msg):
    """Lines carrying both scripts. Digits and punctuation are script-neutral
    and do not count -- '1.47.0' inside a Persian title is safe."""
    bad = []
    for line in TAG.sub("", msg).split("\n"):
        if PERSIAN.search(line) and LATIN.search(line):
            bad.append(line)
    return bad


SAMPLES = {
    "version_update": brand.version_update("1.47.0", ["result_card.py", "executor.py"]),
    "header":         brand.header("🌙", "گزارش روزانه", "Daily Report"),
    "bilingual":      brand.bilingual("🚀", "عنوان", "Title", ["یک", "دو"], ["One", "Two"]),
    "numeric":        brand.numeric_block([("Win rate", "44.4%"), ("Result", "+2.97R")]),
}

print("\n── no invisible characters ──")
for name, msg in SAMPLES.items():
    check(f"{name}: no ZWNJ", brand.ZWNJ not in msg)
    check(f"{name}: no other zero-width chars",
          not any(c in msg for c in "​‍‎‏"))

print("\n── no line mixes Persian and Latin script ──")
for name, msg in SAMPLES.items():
    bad = mixed_lines(msg)
    check(f"{name}: every line is single-script", not bad)
    if bad:
        for b in bad:
            print(f"          offending: {b!r}")

print("\n── the two language blocks stay in step ──")
m = brand.version_update("1.47.0", ["result_card.py", "executor.py", "strategy2.py"])
fa_block, en_block = m.split(brand.rule())
check("Persian block comes first",  PERSIAN.search(fa_block) is not None)
check("English block comes second", LATIN.search(en_block) is not None)
check("equal bullet counts",
      fa_block.count(brand.BULLET) == en_block.count(brand.BULLET))
check("version number appears in both", "1.47.0" in fa_block and "1.47.0" in en_block)

print("\n── highlights are safe to publish unattended ──")
h = brand.highlights_for
check("empty input still yields a bullet",      len(h([])) == 1)
check("None input still yields a bullet",       len(h(None)) == 1)
check("unknown files fall back",                h(["some_new_file.py"]) == [brand._FALLBACK])
check("deterministic across calls",             h(["executor.py"]) == h(["executor.py"]))
check("order independent",
      h(["executor.py", "tg.py"]) == h(["tg.py", "executor.py"]))
check("deduplicated (two files, one category)",
      len(h(["result_card.py", "tracker.py"])) == 1)
check("capped at 4",
      len(h(["strategy2.py", "executor.py", "tg.py", "io_safe.py", "trader.py",
             "live.py", "review.py"])) <= brand.MAX_HIGHLIGHTS)
check("a test-only change reads as reliability, not as its own bullet",
      h(["test_ratchet.py"]) == [("پایداری و ایمنی داده بهتر شد",
                                  "Reliability and data safety improved")])
check("full paths are handled, not just basenames",
      h(["/root/trade/executor.py"]) == h(["executor.py"]))
check("every category line is bilingual and non-empty",
      all(fa and en and PERSIAN.search(fa) and LATIN.search(en)
          for fa, en in [c[1:] for c in brand._CATEGORIES] + [brand._FALLBACK]))

print("\n── numeric_block ──")
check("drops None values",
      "Missing" not in brand.numeric_block([("Win", "1"), ("Missing", None)]))
check("empty input renders nothing", brand.numeric_block([]) == "")
check("wraps in <pre> so Telegram keeps it LTR and monospaced",
      brand.numeric_block([("a", "1")]).startswith("<pre>"))

print("\n── the real message sites produce clean output ──")
import json, tracker, review, tg
state = json.load(open("/root/trade/state.json"))
dash = tracker._dashboard_text(state, {}, current_balance=689.28)
check("dashboard: no ZWNJ", brand.ZWNJ not in dash)
bad = mixed_lines(dash)
check("dashboard: every line is single-script", not bad)
if bad:
    for b in bad[:5]:
        print(f"          offending: {b!r}")


print("\n── signal messages: the three states a subscriber actually sees ──")
import json as _json, types
state = _json.load(open("/root/trade/state.json"))
_t = dict(state["tracked"]["ETH"])
_t.setdefault("strategy", "S2")
_t.setdefault("sl_orig", _t["sl"])

OPEN_UNARMED = tracker._live_text(_t, 2444.0, hl_roe=0.35, hl_pnl_usd=6.57,
                                  hl_leverage=20, hl_entry=2509.6)
_armed = dict(_t); _armed["locked_r"] = 2.5; _armed["sl"] = 2400.0
OPEN_ARMED = tracker._live_text(_armed, 2380.0, hl_roe=1.03, hl_pnl_usd=19.4,
                                hl_leverage=20, hl_entry=2509.6)
_win  = dict(next(c for c in state["closed_trades"] if c.get("signal_num") == 95))
_loss = dict(next(c for c in state["closed_trades"] if c.get("signal_num") == 87))
_dust = dict(next(c for c in state["closed_trades"] if c.get("signal_num") == 96))
CLOSED_WIN  = tracker._live_text(_win,  _win["exit"],  closed=True,
                                 close_result="sl", final_pct=_win["lev_pct"])
CLOSED_LOSS = tracker._live_text(_loss, _loss["exit"], closed=True,
                                 close_result="sl", final_pct=_loss["lev_pct"])
CLOSED_DUST = tracker._live_text(_dust, _dust["exit"], closed=True,
                                 close_result="sl", final_pct=_dust["lev_pct"])

_sent = {}
_real_send = tg.send
tg.send = lambda m, **k: (_sent.__setitem__("m", m), 1)[1]
_real_num = tg._next_signal_num
tg._next_signal_num = lambda: 98
tg.send_signal(coin="SOL", direction=-1, score=0, price=101.27, sl=103.1, tp=92.3,
               reasons=["RSI 78 (overbought)"], account_val=689.28, risk_usd=6.89,
               tf="1h", leverage=10, strategy="S2", trail_start_r=2.5)
ENTRY = _sent["m"]
tg.send, tg._next_signal_num = _real_send, _real_num

STATES = {"entry": ENTRY, "open (unarmed)": OPEN_UNARMED, "open (armed)": OPEN_ARMED,
          "closed win": CLOSED_WIN, "closed loss": CLOSED_LOSS, "closed dust": CLOSED_DUST}

for name, msg in STATES.items():
    check(f"{name}: no ZWNJ", brand.ZWNJ not in msg)
    bad = mixed_lines(msg)
    check(f"{name}: every line is single-script", not bad)
    for b in bad:
        print(f"          offending: {b!r}")

print("\n── the phantom target: a cancelled TP must never be advertised ──")
# The S2 ratchet cancels the resting take-profit the moment it arms (live.py
# calls update_sl without `entry`, dropping every reduce-only order). This
# renderer used to print the TP unconditionally, so past that point it showed a
# target no order could fill. tg.send_signal already refused to print it.
check("unarmed S2 shows the target (it really is resting)", "Target" in OPEN_UNARMED)
check("ARMED S2 does NOT show a target", "Target" not in OPEN_ARMED)
check("armed S2 says the profit is locked instead", "locked" in OPEN_ARMED)
check("entry message never advertises the S2 TP as a target",
      "Target" not in ENTRY and "Backstop" in ENTRY)

print("\n── R is present everywhere, since the ratchet is described in R ──")
for name in ("open (unarmed)", "open (armed)", "closed win", "closed loss"):
    check(f"{name}: carries an R figure", "R" in STATES[name] and
          re.search(r"[-+]\d+\.\d+R", STATES[name]) is not None)

print("\n── awkward records must still render ──")
# The OP trade was rebuilt from journal.json during the stats audit and carries
# no signal_num. _live_text used to index t["signal_num"] directly and raised.
_op = dict(next(c for c in state["closed_trades"] if c["coin"] == "OP"))
_op.pop("signal_num", None)
try:
    _m = tracker._live_text(_op, _op["exit"], closed=True,
                            close_result="sl", final_pct=_op.get("lev_pct") or 0)
    check("a record with no signal_num still renders", bool(_m))
    check("and does not print '#SignalNone'", "None" not in _m)
except Exception as _e:
    check(f"a record with no signal_num still renders ({_e})", False)
    check("and does not print '#SignalNone'", False)

print("\n── the bar must MEASURE something, not just exist ──")
# The original bar was min(int(abs(lev_pnl) / 3), 10) on a leveraged figure, so
# at 20x a 1.5% price move filled all ten cells and it read the same on every
# trade. Deleting it was a regression; the fix is a bar that moves. It now
# tracks progress toward arming the risk-free stop.
def _bar_line(msg):
    return next((l for l in msg.split("\n") if "░" in l or ("█" in l and "%" in l)), "")

_near = dict(_t); _near["price_series"] = [2509.6]
_a = tracker._live_text(_near, 2500.0, hl_leverage=20, hl_entry=2509.6)   # ~0.2R
_b = tracker._live_text(_near, 2420.0, hl_leverage=20, hl_entry=2509.6)   # ~1.8R
check("a bar is present", bool(_bar_line(_a)))
check("the bar differs between an early and a late trade",
      _bar_line(_a) != _bar_line(_b))
check("an early trade does not render a full bar", "░" in _bar_line(_a))

print("\n── prices are formatted, not %.5g ──")
check("thousands separator on ETH", "2,509.60" in OPEN_UNARMED)
check("small-price coin keeps its precision", "0.66933" in CLOSED_LOSS)
check("a day-old trade reads as days", re.search(r"\dd \d+h", OPEN_UNARMED) is not None)

print("\n── the instrument panel: bar / spark / track ──")
# The old bar was min(int(abs(lev_pnl)/3), 10) on a LEVERAGED figure: at 20x a
# 1.5% price move filled all ten cells, so it read [██████████] on essentially
# every trade. It was removed, which was a regression -- these pin the replacement.
for _f in (0.0, 0.001, 0.07, 0.5, 0.874, 0.999, 1.0):
    check(f"bar({_f}) is exactly 20 cells", len(brand.bar(_f, 20)) == 20)
check("bar clamps below zero",   len(brand.bar(-5, 20)) == 20 and "█" not in brand.bar(-5, 20))
check("bar clamps above one",    brand.bar(9.0, 20) == "█" * 20)
check("bar has sub-cell resolution (not just 10 states)",
      len({brand.bar(i / 160, 20) for i in range(160)}) > 100)

check("spark of an empty series is empty",  brand.spark([]) == "")
check("spark of one point is empty",        brand.spark([5]) == "")
check("spark of two points renders",        len(brand.spark([1, 2])) == 2)
check("spark downsamples to width",         len(brand.spark(list(range(500)), 24)) == 24)
check("spark of a flat series does not divide by zero",
      brand.spark([7] * 30, 24) == "▁" * 24)
check("spark never emits an out-of-range glyph",
      set(brand.spark([1, 9, 3, 7, 2, 8], 6)) <= set(brand.SPARK))

check("track is exactly the requested width", len(brand.track(1, 9, {5: "●"}, 24)) == 24)
check("track survives lo == hi",              len(brand.track(5, 5, {5: "●"}, 24)) == 24)
check("track clamps a marker outside the range",
      len(brand.track(1, 9, {-100: "●", 900: "┃"}, 24)) == 24)
check("track puts the LOW anchor left and the HIGH anchor right",
      brand.track(0, 10, {0: "┃", 10: "◆"}, 24)[0] == "┃"
      and brand.track(0, 10, {0: "┃", 10: "◆"}, 24)[-1] == "◆")
# A SHORT's target is BELOW its stop. Passing them in trade order (not min/max)
# is what keeps the stop on the left, matching the caption underneath.
check("a reversed span still anchors stop-left, target-right",
      brand.track(2558.7, 2264.3, {2558.7: "┃", 2264.3: "◆"}, 24)[0] == "┃"
      and brand.track(2558.7, 2264.3, {2558.7: "┃", 2264.3: "◆"}, 24)[-1] == "◆")

print("\n── the panel appears in the live message and degrades cleanly ──")
_series = [2509.6 - i * 1.1 for i in range(70)]
_with = dict(_t); _with["price_series"] = _series
_M = tracker._live_text(_with, 2444.0, hl_roe=0.35, hl_pnl_usd=6.57,
                        hl_leverage=20, hl_entry=2509.6)
# "█" is in BOTH the sparkline and the bar alphabets, so presence of the
# sparkline has to be tested on the glyphs unique to it.
SPARK_ONLY = set(brand.SPARK) - set(brand.EIGHTHS)
check("sparkline present when history exists", any(c in _M for c in SPARK_ONLY))
check("progress bar present",                  "█" in _M or "░" in _M)
check("price rail present",                    "●" in _M)
check("the bar is labelled by what it measures",
      "risk-free" in _M or "next lock" in _M)

_without = dict(_t); _without.pop("price_series", None)
_M0 = tracker._live_text(_without, 2444.0, hl_roe=0.35, hl_pnl_usd=6.57,
                         hl_leverage=20, hl_entry=2509.6)
check("no history -> no sparkline, and no broken stub",
      not any(c in _M0 for c in SPARK_ONLY) and bool(_M0))
check("panel characters do not break the single-script rule", not mixed_lines(_M))
check("panel adds no ZWNJ", brand.ZWNJ not in _M)

print("\n── the entry method must not be advertised ──")
# "Mean reversion" is a textbook term: printing it on every signal tells any
# reader what the entry looks for. The exit is public on purpose (the pitch IS
# the risk-free ratchet); the entry condition is not.
LEAKS = ("mean-reversion", "mean reversion", "liquidity-pool", "liquidity pool",
         "rsi", "oversold", "overbought", "adx")
for name, msg in {**STATES, "dashboard": dash}.items():
    low = TAG.sub("", msg).lower()
    hit = [w for w in LEAKS if w in low]
    # The entry message lists its confluence reasons by design -- that is the
    # signal's justification and the owner wants it shown. Everything else must
    # be silent about method.
    if name == "entry":
        continue
    check(f"{name}: does not name the entry method", not hit)
    if hit:
        print(f"          leaked: {hit}")

check("the live strategy has a product name", brand.strategy_name("S2") == "Keystone")
check("the retired one does too",             brand.strategy_name("S1") == "Bedrock")
check("an unknown code degrades to itself",   brand.strategy_name("S9") == "S9")
check("the product name reaches the messages",
      "Keystone" in STATES["open (unarmed)"] and "Keystone" in dash)

print(f"\ntest_brand: {PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
