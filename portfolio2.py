"""
Portfolio-level simulator for strategy 2.

backtest2.py walks ONE coin at a time and lets that coin hold a position
whenever its own signal fires. Live does not work that way: live.py scans the
whole watchlist each candle, holds at most strategy2.MAX_TRADES positions
across ALL coins at once, and when several coins qualify on the same bar it
takes the most stretched one (strategy2.find_setup ranks by abs(stretch)).

At the deployed setting that difference is small. Measured 2026-07-31, 20
coins, 5000 bars: MAX_ADX=25 gives n=116 net +36.49% maxDD -12.49% with the
cap, against n=121 net +33.43% maxDD -14.57% without it. That confirms by
measurement the 2026-07-26 claim (previously only asserted) that backtested
concurrency never exceeded 2, and so validates backtest2 AT THE DEPLOYED
SIGNAL RATE.

It stops being valid the moment anything raises that rate. With the ADX gate
removed the unlimited-concurrency model reports maxDD -74.33%, while the real
capped book over the same period reports -10.59% -- the cap is what bounds
risk, and a model without it answers a question nobody asked. Every question
about signal FREQUENCY (MAX_ADX, RSI thresholds, stretch, extra coins) must
therefore be answered here, not in backtest2.

Exit rules are a faithful copy of backtest2.backtest_coin under the live
ratchet semantics (partial_at_r=1.0, partial_pct=0.0, trail_step_r=0.25): the
fixed take-profit is dropped, the stop locks at 1R and ratchets one rung every
0.25R of further favourable excursion, and the trade exits only when that stop
is taken out. The ratchet is driven by the bar CLOSE.

WARNING (2026-08-04): close-driving was described here as "the conservative
proxy for a level price actually held long enough for a 20s poll to act on".
That was wrong, and it is the reason TRAIL_START_R was set to 0.75 on 2026-08-02
and had to be reverted. It is conservative about WHEN the stop arms and silently
optimistic about whether the stop SURVIVES: raising the stop at the end of a bar
and not testing it until the next one grants every freshly-raised stop a full
bar of immunity that no real stop has. live._check_trail_s2 places the stop AT
the market the instant price prints the level (BTC 2026-08-03: armed +0.75R at
13:49:56, filled 22s later at +0.706R).

This model therefore scores ABOVE the optimistic bound on live behaviour -- it
is outside the achievable range, not inside it. It remains useful for
apples-to-apples comparison of entry-side questions (MAX_ADX, RSI, stretch,
extra coins, the concurrency cap), which is what it was built for. Do NOT use it
to choose any exit-side parameter; use sweep_trail_mode.py, which reports both
achievable bounds and asserts equivalence with this file at mode="close".

Prices are the REAL exchange OHLC. indicators.fetch_candles leaves Heikin Ashi
in the open/high/low/close columns and the true prices in real_*; walking a
simulated fill over the HA path is the 2026-07-30 bug that overstated this
strategy by more than 2x. Indicators still read HA, which is the strategy
basis and is deliberately untouched.
"""
import sys

import pandas as pd

import strategy2 as s2
from backtest2 import TAKER_FEE

RISK_DIVISOR = 20.0
# A trade is sized so a full stop costs MAX_LEV_LOSS (20%) of the leveraged
# notional, i.e. 1% of the account. Dividing leveraged % by 20 converts to
# account %, matching backtest2.summarize so numbers stay comparable.


def load(coins=None, bars=5000, tf="1h"):
    """Fetch every coin once and pre-compute its signals with NO ADX gate.

    ADX is a pure independent veto inside strategy2.signal, so collecting the
    candidates once with the gate open and filtering by the stored adx per run
    is exactly equivalent to re-running signal() per threshold, and far faster.
    """
    coins = coins or s2.WATCHLIST
    bars_by_coin, cand = dict(), dict()
    saved = s2.MAX_ADX
    s2.MAX_ADX = 10 ** 9
    try:
        for c in coins:
            d = s2.build_df(c, tf, bars=bars)
            if d is None:
                continue
            d = d.copy()
            for col in ("open", "high", "low", "close"):
                if "real_" + col in d.columns:
                    d[col] = d["real_" + col]
            bars_by_coin[c] = d
            lst = []
            for i in range(len(d)):
                sg = s2.signal(d, i)
                if sg:
                    sg["adx_v"] = float(d.iloc[i]["adx"])
                    sg["ts"] = d.index[i]
                    lst.append(sg)
            cand[c] = lst
    finally:
        s2.MAX_ADX = saved
    return bars_by_coin, cand


def simulate(bars_by_coin, cand, max_adx=None, max_trades=None,
             trail_start_r=1.0, trail_step_r=0.25, rank="stretch"):
    """Run the whole book on one shared clock. Returns closed trades."""
    max_adx = s2.MAX_ADX if max_adx is None else max_adx
    max_trades = s2.MAX_TRADES if max_trades is None else max_trades

    by_ts = dict()
    for c, lst in cand.items():
        for sg in lst:
            if sg["adx_v"] < max_adx:
                by_ts.setdefault(sg["ts"], []).append((c, sg))

    timeline = sorted(set().union(*[set(d.index) for d in bars_by_coin.values()]))
    posmap = dict((c, dict((t, i) for i, t in enumerate(d.index)))
                  for c, d in bars_by_coin.items())

    open_p, closed = dict(), []
    for ts in timeline:
        # Exits are processed before entries so a position opened on this bar
        # is first judged on the NEXT one -- same no-lookahead rule as
        # backtest2, where the open_t branch continues past the signal check.
        for c in list(open_p):
            t = open_p[c]
            pm = posmap[c].get(ts)
            if pm is None:
                continue
            row = bars_by_coin[c].iloc[pm]
            d, R = t["direction"], t["R"]
            hit = (row["low"] <= t["sl"]) if d == 1 else (row["high"] >= t["sl"])
            if hit:
                t["total_r"] = (t["locked_r"] if t["scaled"]
                                else (t["sl"] - t["entry"]) * d / R)
                t["result"] = "trail" if t["scaled"] else "sl"
                t["exit"], t["close_ts"] = t["sl"], ts
                closed.append(t)
                del open_p[c]
                continue
            mfe_r = (row["close"] - t["entry"]) * d / R
            if mfe_r >= trail_start_r:
                t["scaled"] = True
                rung = int((mfe_r - trail_start_r) / trail_step_r)
                locked = trail_start_r + rung * trail_step_r
                new_sl = t["entry"] + d * locked * R
                if (new_sl > t["sl"]) if d == 1 else (new_sl < t["sl"]):
                    t["sl"], t["locked_r"] = new_sl, locked

        pool = by_ts.get(ts, [])
        pool = (sorted(pool, key=lambda x: -abs(x[1]["stretch"])) if rank == "stretch"
                else sorted(pool, key=lambda x: x[0]))
        for c, sg in pool:
            if len(open_p) >= max_trades or c in open_p:
                continue
            t = dict(sg)
            t["coin"], t["open_ts"] = c, ts
            t["sl_orig"], t["R"] = sg["sl"], abs(sg["entry"] - sg["sl"])
            t["scaled"], t["locked_r"] = False, 0.0
            open_p[c] = t
    return closed


def frame(trades):
    """Closed trades to a DataFrame with a net account-% column."""
    df = pd.DataFrame(trades).sort_values("open_ts").reset_index(drop=True)
    r_as_pct = abs(df["entry"] - df["sl_orig"]) / df["entry"]
    gross = df["total_r"] * r_as_pct * 100 * df["leverage"]
    df["acct"] = (gross - TAKER_FEE * 2 * df["leverage"] * 100) / RISK_DIVISOR
    return df


def summarize(trades, label=""):
    if len(trades) < 3:
        return label.ljust(26) + "n=" + str(len(trades)) + " (too few)"
    df = frame(trades)
    a = df["acct"]
    eq = a.cumsum()
    dd = (eq - eq.cummax()).min()
    months = max((df["open_ts"].max() - df["open_ts"].min()).days / 30.44, 0.1)
    t_stat = a.mean() / (a.std() / len(a) ** 0.5) if a.std() else 0.0
    ranked = a.sort_values(ascending=False)
    return (label.ljust(26)
            + "n=" + str(len(df)).ljust(5)
            + "WR=" + format((a > 0).mean() * 100, "5.1f") + "%"
            + "  /mo=" + format(len(df) / months, "5.1f")
            + "  net=" + format(a.sum(), "+8.2f") + "%"
            + "  maxDD=" + format(dd, "6.2f") + "%"
            + "  EV=" + format(a.mean(), "+.3f") + "%"
            + "  t=" + format(t_stat, "4.2f")
            + "  ex-top5=" + format(ranked.iloc[5:].sum(), "+8.2f") + "%")


def out_of_sample(trades, label=""):
    """Older half vs newer half. A rule that only works in-sample is rejected."""
    df = frame(trades)
    mid = df["open_ts"].min() + (df["open_ts"].max() - df["open_ts"].min()) / 2
    return [summarize(trades, label + " ALL"),
            summarize([t for t in trades if t["open_ts"] < mid], label + "  in-sample"),
            summarize([t for t in trades if t["open_ts"] >= mid], label + "  OUT-OF-SAMPLE")]


if __name__ == "__main__":
    coins = sys.argv[1:] or s2.WATCHLIST
    bars_by_coin, cand = load(coins)
    print("=== STRATEGY 2 portfolio sim — " + str(len(bars_by_coin))
          + " coins, cap " + str(s2.MAX_TRADES) + ", real prices, fees in ===")
    print()
    for line in out_of_sample(simulate(bars_by_coin, cand), "deployed"):
        print(line)
    print()
    print("MAX_ADX sensitivity (deployed = " + str(s2.MAX_ADX) + "):")
    for v in (20, 25, 30, 35, 40, 10 ** 9):
        print(summarize(simulate(bars_by_coin, cand, max_adx=v),
                        "  MAX_ADX=" + str(v)))
