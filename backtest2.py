"""
Backtest harness for strategy2 (mean reversion).

Same simplifications and conservative choices as backtest.py -- SL assumed to
fill first when a single bar covers both SL and TP, one position per coin, fees
modelled explicitly -- so the two strategies' numbers are directly comparable.
"""
import sys
import pandas as pd

import strategy2 as s2

# Verified against the live account 2026-08-05 via info.user_fees: userCrossRate
# is 0.00045 on BOTH networks (userAddRate 0.00015). This was 0.00035, a 29%
# understatement, on every result this project has quoted.
#
# Both legs are taker: entries go in via exchange.market_open (executor.py:179)
# and exits fire market-trigger stops (executor.py isMarket=True), so neither
# side earns the maker rate.
TAKER_FEE = 0.00045

# Per-side slippage, previously modelled as exactly zero. Entries are market
# orders sent with slippage=0.01 tolerance; exits are stop-markets, which fill
# at whatever is there when triggered. 1bp/side is a deliberately mild opening
# assumption -- the real figure is measurable from live fills (signal price vs
# entry fill, stop trigger vs stop fill) and is not yet measured, so treat this
# as a placeholder to be replaced with data, not as a calibrated number.
SLIPPAGE_PER_SIDE = 0.0001


def round_trip_cost(leverage):
    """Total taker cost of one round trip, as a % of margin.

    Scales with leverage because the fee is charged on notional while returns
    here are denominated in margin.
    """
    return (TAKER_FEE + SLIPPAGE_PER_SIDE) * 2 * leverage * 100


def backtest_coin(coin, tf="1h", bars=5000, start=None, end=None,
                  trail_start_r=None, trail_step_r=0.5,
                  partial_at_r=None, partial_pct=0.5,
                  breakeven_at_r=None, ratchet_on="close", path="real"):
    """Walk history through strategy2.signal(), simulating fills.

    path: which price series the simulated trade walks. "real" uses the actual
    exchange OHLC; "ha" uses the Heikin Ashi series indicators.fetch_candles
    overwrites the OHLC columns with. HA is what every number in this file was
    measured on before 2026-07-30, and it is wrong for path simulation: HA high
    is by construction max(real_high, ha_open, ha_close) and HA low the matching
    min, so HA bars are ~14% wider than the real ones, and ha_close differs from
    the real close by ~0.21% on average. The strategy READS HA (that is the
    indicator basis and stays untouched) but it FILLS on the exchange, and
    strategy2.signal already prices entry from real_close. Walking the stop and
    the ratchet over HA mixed a real entry with a synthetic path.

    trail_start_r: when set, the fixed take-profit is replaced by a progressive
    stop. Reaching trail_start_r moves the stop to breakeven; every further
    trail_step_r of favourable excursion ratchets it up by one step, and the
    position exits only when that stop is taken out -- so a runner is never
    capped. Default None keeps the fixed-TP behaviour every earlier number in
    this file was measured with.

    partial_at_r: scale out instead. On reaching this level, partial_pct of the
    position is banked there and the REMAINDER's stop is locked at the same
    level, then ratcheted up every trail_step_r. Because the remainder can only
    ever exit at or above that lock, the trade's total return is bounded below
    by partial_at_r once reached -- it is a free option on further upside, not
    a trade of certain profit for uncertain profit (which is what trail_start_r
    does, and which measured worse: same net, far lower win rate).
    Fees are unchanged: two partial exits sum to the same notional as one.
    """
    df = s2.build_df(coin, tf, bars=bars)
    if df is None or len(df) < 100:
        return []
    if path == "real" and "real_close" in df.columns:
        df = df.copy()
        for c in ("open", "high", "low", "close"):
            df[c] = df["real_" + c]
    if start is not None:
        df = df[df.index >= start]
    if end is not None:
        df = df[df.index < end]
    if len(df) < 50:
        return []

    trades, open_t = [], None
    for i in range(len(df)):
        row = df.iloc[i]
        if open_t:
            d = open_t["direction"]

            # Max adverse excursion: how far the trade went against us before it
            # resolved. Recorded per trade because the aggregate win rate hides
            # it entirely -- a winner that first ran 0.9R against the stop is a
            # very different trade from one that never dipped.
            worst = row["low"] if d == 1 else row["high"]
            adverse = (open_t["entry"] - worst) * d
            if adverse > open_t["mae_abs"]:
                open_t["mae_abs"] = adverse

            # Stop is always tested against where it stood at the start of the
            # bar, before this bar's high is allowed to ratchet it -- otherwise
            # the same candle both raises the stop and is judged against the
            # raised value, which is lookahead.
            hit_sl = (row["low"] <= open_t["sl"]) if d == 1 else (row["high"] >= open_t["sl"])
            if hit_sl:                      # SL first on ambiguity, as in backtest.py
                res = "sl" if open_t["sl"] == open_t["sl_orig"] else "trail"
                if open_t.get("moved_be") and not open_t.get("scaled"):
                    total_r = 0.0                 # stopped out at entry
                    res = "breakeven"
                elif open_t.get("scaled"):
                    # Blended outcome: partial_pct banked at the scale-out level,
                    # the rest exiting at wherever the ratchet had reached.
                    total_r = (partial_pct * partial_at_r
                               + (1 - partial_pct) * open_t["locked_r"])
                    res = "partial+trail"
                else:
                    total_r = (open_t["sl"] - open_t["entry"]) * d / open_t["R"]
                trades.append({**open_t, "exit": open_t["sl"], "result": res,
                               "total_r": round(total_r, 4), "close_time": row.name})
                open_t = None
                continue

            if partial_at_r:
                R = open_t["R"]
                # The ratchet must be driven by a price the LIVE bot could
                # actually have acted on. live.py polls the mid every ~20s, so a
                # one-minute wick inside an hourly bar is invisible to it -- but
                # the bar's high records it, and using that let a spurious spike
                # lock the stop at +5R and then "fill" there. Measured on 80
                # coins: thin alts showed avgR +1.81 vs +0.77 for the majors,
                # 90 trades above 3R, one at 13R, and 89% of all profit came
                # from wicks on the illiquid names. Close is the conservative
                # proxy for "a level price actually held long enough to trade".
                extreme = row["close"] if ratchet_on == "close" else (
                    row["high"] if d == 1 else row["low"])
                mfe_r = (extreme - open_t["entry"]) * d / R
                # Optional early move to breakeven, BELOW the ratchet's own
                # trigger. Cuts a loss to zero when a trade runs part-way and
                # reverses -- but only ever helps if the trade would otherwise
                # have gone on to lose, and turns a winner into a scratch every
                # time price dips back through entry before running.
                if (breakeven_at_r and not open_t["scaled"]
                        and mfe_r >= breakeven_at_r):
                    be_sl = open_t["entry"]
                    if (be_sl > open_t["sl"]) if d == 1 else (be_sl < open_t["sl"]):
                        open_t["sl"] = be_sl
                        open_t["moved_be"] = True
                if mfe_r >= partial_at_r:
                    if not open_t["scaled"]:
                        open_t["scaled"] = True          # bank partial_pct at the level
                    # Remainder's stop: locked at partial_at_r, then ratcheted.
                    rung   = int((mfe_r - partial_at_r) / trail_step_r)
                    locked = partial_at_r + rung * trail_step_r
                    new_sl = open_t["entry"] + d * locked * R
                    if (new_sl > open_t["sl"]) if d == 1 else (new_sl < open_t["sl"]):
                        open_t["sl"] = new_sl
                        open_t["locked_r"] = locked
                continue

            if trail_start_r:
                R = open_t["R"]
                extreme = row["high"] if d == 1 else row["low"]
                mfe_r = (extreme - open_t["entry"]) * d / R
                if mfe_r >= trail_start_r:
                    # rung 0 = breakeven at trail_start_r, then one step per
                    # trail_step_r beyond it.
                    rung = int((mfe_r - trail_start_r) / trail_step_r)
                    locked = rung * trail_step_r
                    new_sl = open_t["entry"] + d * locked * R
                    if (new_sl > open_t["sl"]) if d == 1 else (new_sl < open_t["sl"]):
                        open_t["sl"] = new_sl
                        open_t["max_rung"] = rung
                continue

            hit_tp = (row["high"] >= open_t["tp"]) if d == 1 else (row["low"] <= open_t["tp"])
            if hit_tp:
                total_r = (open_t["tp"] - open_t["entry"]) * d / open_t["R"]
                trades.append({**open_t, "exit": open_t["tp"], "result": "tp",
                               "total_r": round(total_r, 4), "close_time": row.name})
                open_t = None
            continue

        sig = s2.signal(df, i)
        if sig:
            open_t = {**sig, "coin": coin, "tf": tf, "open_time": row.name,
                      "mae_abs": 0.0, "sl_orig": sig["sl"],
                      "R": abs(sig["entry"] - sig["sl"]), "max_rung": 0,
                      "scaled": False, "locked_r": 0.0, "moved_be": False}
    return trades


def r_pct(t, fee=0.0):
    """Leveraged % for a trade. Uses total_r when present so a scaled-out trade
    (part banked at the target, part exiting on the ratchet) is measured as the
    blend of its two legs rather than as a single fill."""
    if t.get("total_r") is not None:
        r_as_pct = abs(t["entry"] - t["sl_orig"]) / t["entry"]
        gross = t["total_r"] * r_as_pct * 100 * t["leverage"]
    else:
        raw = (t["exit"] - t["entry"]) / t["entry"]
        gross = raw * 100 * t["direction"] * t["leverage"]
    if not fee:
        return gross
    # Slippage rides along with the fee: callers pass TAKER_FEE to mean "net of
    # costs", and pricing the fee while leaving slippage at zero was how the
    # cost model stayed optimistic. round_trip_cost() carries both.
    per_side = fee + (SLIPPAGE_PER_SIDE if fee == TAKER_FEE else 0.0)
    return gross - per_side * 2 * t["leverage"] * 100


def summarize(trades, label=""):
    if not trades:
        return f"{label:<28} no trades"
    df = pd.DataFrame(trades)
    df["gross"] = df.apply(lambda r: r_pct(r), axis=1)
    df["net"]   = df.apply(lambda r: r_pct(r, TAKER_FEE), axis=1)
    df["acct"]  = df["net"] / 20
    df = df.sort_values("open_time")
    eq = df["acct"].cumsum()
    dd = (eq - eq.cummax()).min()
    months = max((df["open_time"].max() - df["open_time"].min()).days / 30.44, 0.1)
    wr = (df["net"] > 0).mean() * 100
    s = df["acct"].sort_values(ascending=False)
    return (f"{label:<28}n={len(df):<5}WR={wr:>5.1f}%  /mo={len(df)/months:>5.1f}  "
            f"net={df['acct'].sum():+7.2f}%  maxDD={dd:>6.2f}%  "
            f"ex-top5={s.iloc[5:].sum():+7.2f}%")


if __name__ == "__main__":
    coins = sys.argv[1:] or ["BTC", "ETH", "SOL", "AVAX", "SUI", "DOGE", "BNB",
                             "AAVE", "ARB", "ADA", "WLD", "TIA", "INJ", "NEAR",
                             "APT", "OP", "ATOM", "XLM", "FIL", "LDO"]
    tf = "1h"
    allt = []
    for c in coins:
        try:
            allt += backtest_coin(c, tf, bars=5000)
        except Exception as e:
            print(f"ERR {c}: {str(e)[:60]}", flush=True)
    if not allt:
        print("no trades at all")
        sys.exit()

    df = pd.DataFrame(allt)
    mid = df["open_time"].min() + (df["open_time"].max() - df["open_time"].min()) / 2
    print(f"=== STRATEGY 2 (mean reversion) — {len(coins)} coins, {tf}, fees included ===")
    print(f"span {df['open_time'].min()} .. {df['open_time'].max()}   split at {mid}")
    print()
    print(summarize(allt, "ALL"))
    print(summarize([t for t in allt if t["open_time"] < mid],  "  in-sample (older half)"))
    print(summarize([t for t in allt if t["open_time"] >= mid], "  OUT-OF-SAMPLE (newer)"))
