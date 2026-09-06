#!/usr/bin/env python3
"""A signal that cannot become a trade must not reach the channel.

WHY THIS FILE EXISTS
--------------------
On 2026-09-04 NEAR signalled at 21:01 and again at 22:01 with byte-identical
rsi=78.0 adx=22.7 stretch=5.03, and both entries died with

    Entry error: Order could not immediately match against any resting orders.

Four independent defects lined up to produce that:

  1. NEAR's testnet book had a 4.02% spread. market_open posts an IOC limit 1%
     from the mid, so it could never cross -- the order was unfillable by
     construction, and nothing checked before spending it.
  2. tg.send_signal ran BEFORE `if res2:` was ever evaluated, so the channel got
     a signal post for a trade that did not exist. Twice. Each burned a signal
     number.
  3. The only cooldown fired on a stop-out. A coin that could not be ENTERED
     stayed eligible and re-signalled every candle, indefinitely.
  4. build_df's staleness test needed THREE identical closes; NEAR had two, and
     the real tell was that its newest bar was a whole candle behind the clock.
"""
import sys, types, time
sys.path.insert(0, "/root/trade")

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}")


import executor

print("\n── 1. an uncrossable book is refused before an order is spent ──")


def fake_book(bid, ask):
    lv = [[{"px": str(bid)}], [{"px": str(ask)}]]
    executor._clients = lambda: (types.SimpleNamespace(l2_snapshot=lambda c: {"levels": lv}), None)
    executor._hl_call = lambda fn, *a, **k: fn(*a, **k)


fake_book(1.9222, 2.0011)          # NEAR as it actually was: 4.02% spread
ok, detail = executor.book_crossable("NEAR", -1)
check(f"4% spread rejected for a SHORT ({detail})", ok is False)
check("4% spread rejected for a LONG too", executor.book_crossable("NEAR", 1)[0] is False)

fake_book(2499.5, 2500.5)          # a normal book, 0.04%
check("a tight book passes", executor.book_crossable("ETH", -1)[0] is True)

fake_book(100.0, 101.0)            # 1.0% spread -> each side must cross 0.5%
check("a spread inside the cap passes", executor.book_crossable("X", -1)[0] is True)

# An unreadable book is NOT evidence of a bad book. Refusing to trade on a
# failed snapshot would be a worse failure than the one being guarded against.
def boom(*a, **k):
    raise RuntimeError("snapshot down")
executor._clients = lambda: (types.SimpleNamespace(l2_snapshot=boom), None)
executor._hl_call = lambda fn, *a, **k: fn(*a, **k)
check("an unreadable book proceeds rather than blocking", executor.book_crossable("Z", -1)[0] is True)

print("\n── 2. a failed entry cools the coin down ──")
import live
live._cooldown_until.clear()
live.tracker = types.SimpleNamespace(load_state=lambda: {}, save_state=lambda s: None)
live._mark_entry_failed("NEAR", "book too wide")
check("the coin is now on cooldown", "NEAR" in live._cooldown_until)
remaining = live._cooldown_until["NEAR"] - int(time.time())
check(f"cooldown is hours, not minutes ({remaining//3600}h)", remaining > 3600)
check("cooldown matches the declared constant",
      abs(remaining - live.ENTRY_FAIL_COOLDOWN_S) < 5)

print("\n── 3. the channel post is gated on the order, not the signal ──")
src = open("/root/trade/live.py").read()
s2 = src[src.index("res2  = open_trade("):src.index("log_trade_open(c2")]
check("S2: open_trade is called before send_signal",
      s2.index("open_trade(") < s2.index("tg.send_signal("))
check("S2: send_signal sits inside an `if res2:` block",
      s2.index("if res2:") < s2.index("tg.send_signal("))
check("S2: a failed entry marks a cooldown", "_mark_entry_failed(c2)" in s2)
s1 = src[src.index("result = open_trade("):src.index("log_trade_open(coin,")]
check("S1: open_trade is called before send_signal",
      s1.index("open_trade(") < s1.index("tg.send_signal("))
check("S1: send_signal sits inside an `if result:` block",
      s1.index("if result:") < s1.index("tg.send_signal("))

print("\n── 4. a feed a candle behind the clock is stale ──")
import pandas as pd, numpy as np, strategy2

def frame(last_ts, n=400):
    idx = pd.date_range(end=last_ts, periods=n, freq="1h")
    px = pd.Series(np.linspace(100, 120, n) + np.random.RandomState(0).randn(n) * 0.4)
    return pd.DataFrame({"open": px.values, "high": px.values + .5,
                         "low": px.values - .5, "close": px.values,
                         "volume": np.full(n, 50.0)}, index=idx)

now = pd.Timestamp.utcnow().tz_localize(None)
real_fetch = strategy2.fetch_candles
try:
    strategy2.fetch_candles = lambda c, tf, lookback_bars=0: frame(now.floor("h") - pd.Timedelta(hours=1))
    check("a current feed is accepted", strategy2.build_df("X", "1h") is not None)
    strategy2.fetch_candles = lambda c, tf, lookback_bars=0: frame(now.floor("h") - pd.Timedelta(hours=4))
    check("a feed 4 candles behind is REJECTED", strategy2.build_df("X", "1h") is None)
    # the pre-existing rule must still hold on its own
    def flat(c, tf, lookback_bars=0):
        f = frame(now.floor("h") - pd.Timedelta(hours=1))
        f.iloc[-3:, f.columns.get_loc("close")] = 111.0
        return f
    strategy2.fetch_candles = flat
    check("three identical closes still rejected", strategy2.build_df("X", "1h") is None)
finally:
    strategy2.fetch_candles = real_fetch

print(f"\ntest_entry_gate: {PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
