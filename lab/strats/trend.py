"""
Family A: time-series trend following (Donchian breakout + chandelier trail).

All inputs are REAL prices. A breakout is an EVENT -- the first close beyond
the prior N-bar extreme -- not a state, so one move produces one signal rather
than one per bar for as long as it lasts.
"""
import numpy as np
import pandas as pd

from lab.strats.common import atr, to_signals


def prepare(frames, atr_len=20):
    """ATR per coin, as {coin: array}, for Market.add('atr', ...)."""
    return {c: atr(df["high"], df["low"], df["close"], atr_len).values
            for c, df in frames.items()}


def signals(frames, lookback=40, stop_atr=3.0, atr_len=20, long_only=False,
            regime=None):
    """Breakout signals.

    regime: optional Series (indexed like the bars, already lag-safe) that is
        True when LONGS are allowed; shorts are then allowed only when it is
        False. NaN allows neither.
    """
    out = []
    for coin, df in frames.items():
        if len(df) < lookback + atr_len + 5:
            continue
        a = atr(df["high"], df["low"], df["close"], atr_len)
        # shift(1): the channel a bar is compared against excludes that bar
        hh = df["high"].rolling(lookback).max().shift(1)
        ll = df["low"].rolling(lookback).min().shift(1)
        up = df["close"] > hh
        dn = df["close"] < ll
        d = pd.Series(0, index=df.index)
        d[up & ~up.shift(1, fill_value=False)] = 1
        if not long_only:
            d[dn & ~dn.shift(1, fill_value=False)] = -1
        if regime is not None:
            g = regime.reindex(df.index)
            d[(d == 1) & ~(g == True)] = 0      # noqa: E712  (NaN must fail)
            d[(d == -1) & ~(g == False)] = 0    # noqa: E712
        strength = np.where(d == 1, (df["close"] - hh) / a, (ll - df["close"]) / a)
        m = (d != 0) & a.notna() & (a > 0)
        if m.any():
            out.append(pd.DataFrame({
                "ts": df.index[m], "coin": coin, "dir": d[m].values,
                "stop_dist": (stop_atr * a)[m].values,
                "rank": pd.Series(strength, index=df.index)[m].values}))
    return to_signals(out)


def btc_regime(btc_daily, length=200):
    """True while BTC's daily close is above its `length`-day average. Shifted
    one bar so a day's regime is the one known at that day's OPEN."""
    ma = btc_daily["close"].rolling(length).mean()
    r = (btc_daily["close"] > ma).where(ma.notna())
    return r.shift(1)
