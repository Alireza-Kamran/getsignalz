"""
Family C: rules taken from the owner's own indicator (ind.txt).

Each function is a vectorised equivalent of the block in ind.txt / its port in
indicators.py, checked against that port in lab/run_c.py before use. As on the
owner's chart (and in live fetch_candles), the indicator maths runs on Heikin
Ashi candles; entries, stops and fills are priced on REAL candles.
"""
import numpy as np
import pandas as pd

from lab.strats.common import heikin_ashi, htf_state, rsi, atr, to_signals
from lab import data


def ssl_state(ha, length=200):
    """SSL channel state: +1 once the close is above SMA(high), -1 once it is
    below SMA(low), unchanged in between. NaN until the first decision."""
    mh = ha["high"].rolling(length).mean()
    ml = ha["low"].rolling(length).mean()
    s = pd.Series(np.where(ha["close"] > mh, 1.0,
                           np.where(ha["close"] < ml, -1.0, np.nan)), index=ha.index)
    s[mh.isna()] = np.nan
    return s.ffill()


def order_blocks(ha, volume, length=5):
    """(bull_ob, bear_ob) booleans, true on the bar the block is CONFIRMED --
    `length` bars after the volume pivot, which is when the indicator draws it."""
    upper = ha["high"].rolling(length).max()
    lower = ha["low"].rolling(length).min()
    hp, lp = ha["high"].shift(length), ha["low"].shift(length)
    os_ = pd.Series(np.where(hp > upper, 0.0, np.where(lp < lower, 1.0, np.nan)),
                    index=ha.index).ffill().fillna(0.0)
    pivot = volume.shift(length) == volume.rolling(2 * length + 1).max()
    return pivot & (os_ == 1), pivot & (os_ == 0)


def ssl_flip_signals(frames, length=200, stop_atr=3.0):
    """C1. Returns (signals, exit_long, exit_short) -- the exits are aux arrays
    for the engine: leave a long once the state is -1, a short once it is +1."""
    out, xl, xs = [], {}, {}
    for coin, df in frames.items():
        ha = heikin_ashi(df)
        st = ssl_state(ha, length)
        a = atr(df["high"], df["low"], df["close"], 20)
        prev = st.shift(1)
        d = pd.Series(0, index=df.index)
        d[(st == 1) & (prev == -1)] = 1
        d[(st == -1) & (prev == 1)] = -1
        xl[coin] = (st == -1).values
        xs[coin] = (st == 1).values
        m = (d != 0) & a.notna()
        if m.any():
            out.append(pd.DataFrame({"ts": df.index[m], "coin": coin,
                                     "dir": d[m].values,
                                     "stop_dist": (stop_atr * a)[m].values,
                                     "rank": 0.0}))
    return to_signals(out), xl, xs


def ob_signals(frames_1h, rsi_lo=30, rsi_hi=70, use_rsi=True, ssl_tf=None,
               ssl_len=200, stop_atr=1.5, ob_len=5):
    """C2 (use_rsi=True): the indicator's combined alert -- bullish OB with
    RSI <= 30 is a long, bearish OB with RSI >= 70 a short.
    C3 (use_rsi=False, ssl_tf='4h'): an OB taken only with the SSL trend."""
    out = []
    for coin, df in frames_1h.items():
        ha = heikin_ashi(df)
        bull, bear = order_blocks(ha, df["volume"], ob_len)
        a = atr(ha["high"], ha["low"], ha["close"], 14)
        d = pd.Series(0, index=df.index)
        if use_rsi:
            r = rsi(ha["close"], 14)
            d[bull & (r <= rsi_lo)] = 1
            d[bear & (r >= rsi_hi)] = -1
        else:
            d[bull] = 1
            d[bear] = -1
        if ssl_tf:
            h = data.resample(df, ssl_tf)
            st = htf_state(df, ssl_state(heikin_ashi(h), ssl_len))
            d[(d == 1) & ~(st == 1)] = 0
            d[(d == -1) & ~(st == -1)] = 0
        m = (d != 0) & a.notna() & (a > 0)
        if m.any():
            out.append(pd.DataFrame({"ts": df.index[m], "coin": coin,
                                     "dir": d[m].values,
                                     "stop_dist": (stop_atr * a)[m].values,
                                     "rank": 0.0}))
    return to_signals(out)
