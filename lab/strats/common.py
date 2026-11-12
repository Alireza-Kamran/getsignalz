"""Shared building blocks for lab strategies.

Indicator maths is imported from the live `indicators.py` rather than
re-implemented, so a rule researched here is computed by the same code that
would run it live.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/trade")
from indicators import rsi, atr, adx, ema_line  # noqa: E402,F401


def heikin_ashi(df):
    """Heikin Ashi OHLC as a NEW frame. The real prices in `df` are untouched.

    Same recurrence as indicators.fetch_candles. HA is only ever an input to
    indicators here; no fill is simulated on it.
    """
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    hc = (o + h + l + c) / 4.0
    ho = np.empty(len(df))
    ho[0] = (o[0] + c[0]) / 2.0
    for i in range(1, len(df)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
    return pd.DataFrame({"open": ho, "close": hc,
                         "high": np.maximum.reduce([h, ho, hc]),
                         "low": np.minimum.reduce([l, ho, hc])}, index=df.index)


def htf_state(df_1h, series_htf):
    """Align a higher-timeframe series to 1h bars WITHOUT look-ahead.

    A daily bar stamped 00:00 is not known until 24:00. Its value is therefore
    made available only from its close time onward: the series is shifted by
    one of its own bars before being forward-filled onto the hourly index.
    Getting this wrong hands every hourly bar its own day's close.
    """
    step = series_htf.index.to_series().diff().median()
    avail = series_htf.copy()
    avail.index = avail.index + step
    return avail.reindex(df_1h.index.union(avail.index)).ffill().reindex(df_1h.index)


def to_signals(frames):
    """Concatenate per-coin signal frames into one, sorted by time."""
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return pd.DataFrame(columns=["ts", "coin", "dir", "stop_dist", "rank"])
    return pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)
