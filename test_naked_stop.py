#!/usr/bin/env python3
"""An entry that fills must never outlive its stop.

WHY THIS FILE EXISTS
--------------------
open_trade placed the protective stop, and when the exchange rejected it, said:

    logger.warning(f"SL placement failed: {sl_result}")

and then fell through to `return {...}`. live.py:1245 and live.py:1337 both read
that dict as proof the trade is protected -- they copy res["sl"] straight into
_open_trades -- so the bot went on believing in a stop that no order backed,
on a position whose loss was now unbounded. Nothing re-checked it afterwards:
get_stop_price() was called only on restart, and there a failed read and an
absent order were both None, so a MISSING stop was undetectable by construction.

update_sl() had been hardened against this exact condition on the ratchet path
and raises. The entry path never was. These tests pin both halves: open_trade
refuses to return a position it could not protect, and _verify_stops notices one
that loses its stop later.
"""
import sys, types
sys.path.insert(0, "/root/trade")

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}")


import executor

# ── Fakes ────────────────────────────────────────────────────────────────────
OK_FILL = {"status": "ok", "response": {"data": {"statuses": [
    {"filled": {"avgPx": "100.0", "totalSz": "1.0"}}]}}}
OK_REST = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
REJECT  = {"status": "ok", "response": {"data": {"statuses": [
    {"error": "Order could not immediately match against any resting orders."}]}}}


class FakeExchange:
    def __init__(self, sl_results):
        self.sl_results = list(sl_results)
        self.orders, self.closed = [], []
    def market_open(self, coin, is_buy, sz, px, slippage):
        return OK_FILL
    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only):
        tpsl = order_type["trigger"]["tpsl"]
        self.orders.append(tpsl)
        if tpsl == "sl":
            return self.sl_results.pop(0) if self.sl_results else REJECT
        return OK_REST
    def market_close(self, coin):
        self.closed.append(coin)
        return {"status": "ok"}


def install(sl_results):
    ex = FakeExchange(sl_results)
    executor._clients = lambda: (types.SimpleNamespace(), ex)
    executor.get_price = lambda c: 100.0
    executor.get_account_value = lambda: 1000.0
    executor._sz_decimals = lambda c: 2
    executor.close_trade = lambda c: ex.market_close(c)
    sys.modules["tg"] = types.SimpleNamespace(
        dm_owner=lambda *a, **k: dms.append(a[0] if a else ""),
        esc=lambda x: str(x))
    return ex


dms = []
OPEN = dict(coin="BTC", direction=1, risk_usd=10.0, sl_price=99.0,
            tp_price=105.0, leverage=10, tp_ratio=2.0)

print("\n── _order_ok tells an inner rejection from a success ──")
check("outer ok + inner error  -> not ok", executor._order_ok(REJECT)[0] is False)
check("outer ok + resting      -> ok",     executor._order_ok(OK_REST)[0] is True)
check("outer error             -> not ok", executor._order_ok({"status": "err"})[0] is False)
check("garbage                 -> not ok", executor._order_ok(None)[0] is False)

print("\n── the stop rests first time: normal trade ──")
dms.clear(); ex = install([OK_REST])
res = executor.open_trade(**OPEN)
check("returns the trade dict", isinstance(res, dict))
check("stop was placed",        "sl" in ex.orders)
check("take-profit was placed", "tp" in ex.orders)
check("position NOT closed",    ex.closed == [])

print("\n── the stop is rejected once, then rests ──")
dms.clear(); ex = install([REJECT, OK_REST])
res = executor.open_trade(**OPEN)
check("retried and succeeded",   isinstance(res, dict))
check("two stop attempts made",  ex.orders.count("sl") == 2)
check("position NOT closed",     ex.closed == [])

print("\n── the stop is rejected every time ──")
dms.clear(); ex = install([REJECT, REJECT, REJECT])
res = executor.open_trade(**OPEN)
check("open_trade returns None (caller registers nothing)", res is None)
check("exactly 3 stop attempts", ex.orders.count("sl") == 3)
check("position was CLOSED",     ex.closed == ["BTC"])
check("owner was DM'd",          any("استاپ" in d for d in dms))
check("no take-profit left resting on a closed position", "tp" not in ex.orders)

print("\n── a naked position is detected while it is open ──")
import live
live._open_trades.clear()
live._stop_verified_at.clear()
live._open_trades["BTC"] = {"dir": 1, "entry": 100.0, "sl": 99.0, "size": 1.0,
                            "R": 1.0, "strategy": "S2"}
live.tg = types.SimpleNamespace(dm_owner=lambda *a, **k: dms.append(a[0] if a else ""),
                                esc=lambda x: str(x))
replaced = []

live.get_stop_price = lambda c: None                 # read OK, nothing resting
live.update_sl = lambda *a, **k: replaced.append(a)
dms.clear()
live._verify_stops({"BTC": {}})
check("missing stop triggers a replacement", len(replaced) == 1)
check("and the owner is told",               any("استاپ" in d for d in dms))

live._stop_verified_at.clear(); dms.clear(); replaced.clear()
def _boom(c): raise RuntimeError("API down")
live.get_stop_price = _boom
live._verify_stops({"BTC": {}})
check("an UNREADABLE book is not mistaken for a missing stop", replaced == [])
check("and raises no alert",                                   dms == [])

live._stop_verified_at.clear(); dms.clear()
live.get_stop_price = lambda c: 95.0                 # resting, but wrong price
live._verify_stops({"BTC": {}})
check("a stop at the wrong price is adopted from the exchange",
      live._open_trades["BTC"]["sl"] == 95.0)

live._stop_verified_at.clear(); dms.clear()
live.get_stop_price = lambda c: 95.2                 # inside the 0.5% band
live._verify_stops({"BTC": {}})
check("tick rounding inside 0.5% is not treated as a disagreement",
      live._open_trades["BTC"]["sl"] == 95.0 and dms == [])

live._open_trades.clear()
print(f"\ntest_naked_stop: {PASS}/{PASS + FAIL} passed")
sys.exit(1 if FAIL else 0)
