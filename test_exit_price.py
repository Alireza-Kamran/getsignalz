"""Regression test: _exit_price must reach the fills lookup for BOTH shapes.

_open_trades holds opened_at as a datetime; state.json round-trips it as an ISO
string, and the ghost-close path on startup reads straight from state. The
string form used to raise AttributeError on .tzinfo, which _exit_price's own
except-clause swallowed into a silent mid-price fallback -- so the caller that
most needs a real fill (a position closed while the bot was DOWN) quietly never
got one. These assertions pin the since_ms actually handed to get_close_fill.
"""
import types
from datetime import datetime, timezone

SRC = "/root/trade/live.py"


def _load_exit_price(calls):
    src = open(SRC).read()
    body = src[src.index("def _exit_price"):src.index("def _check_closed")]
    ns = {
        "datetime": datetime,
        "timezone": timezone,
        "get_close_fill": lambda c, s: (calls.append(s), 1.234)[1],
        "get_price": lambda c: 9.999,
        "logger": types.SimpleNamespace(warning=lambda m: None),
    }
    exec(body, ns)
    return ns["_exit_price"]


def main():
    calls = []
    exit_price = _load_exit_price(calls)
    dt = datetime(2026, 8, 12, 22, 1, tzinfo=timezone.utc)
    want = int(dt.timestamp() * 1000)

    cases = [
        ("datetime (aware)", {"opened_at": dt}, want),
        ("ISO string", {"opened_at": dt.isoformat()}, want),
        ("naive ISO string", {"opened_at": "2026-08-12T22:01:00"}, want),
        ("naive datetime", {"opened_at": dt.replace(tzinfo=None)}, want),
        ("missing", {}, 0),
    ]

    failed = 0
    for name, trade, expect in cases:
        px = exit_price("FIL", trade)
        got = calls[-1]
        ok = got == expect and px == 1.234
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<18} since={got} px={px}")

    print("FAILED" if failed else "all _exit_price shapes reach the fills lookup")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
