"""
Mean-reversion family: H0 (S2 as deployed) and D (with-trend variant).

The trigger is strategy2.signal re-expressed on arrays: RSI, ADX, ATR and the
EMA stretch are computed on Heikin Ashi (as live does), the entry and the stop
are priced on REAL closes (as live does).
"""
import numpy as np
import pandas as pd

from lab.strats.common import heikin_ashi, htf_state, rsi, atr, adx, ema_line, to_signals
from lab import data


def features(df):
    """Per-bar S2 inputs for one coin."""
    ha = heikin_ashi(df)
    f = pd.DataFrame(index=df.index)
    f["close"] = df["close"]
    f["rsi"] = rsi(ha["close"], 14)
    f["adx"] = adx(ha["high"], ha["low"], ha["close"], 14)[0]
    f["atr"] = atr(ha["high"], ha["low"], ha["close"], 14)
    f["stretch"] = (ha["close"] - ema_line(ha["close"], 100)) / f["atr"]
    return f


def signals(frames_1h, rsi_lo=25, rsi_hi=75, max_adx=25, min_stretch=1.5,
            sl_atr=1.5, min_sl_pct=0.4, trend=None, trend_ema=100,
            feats=None, legacy_lev_filter=False):
    """S2 trigger over every coin.

    trend: None   -> no direction filter (H0)
           "with" -> longs only above the daily EMA, shorts only below (D)
           "against" -> the control: the opposite
    max_adx: None disables the ADX gate.
    feats: optional precomputed {coin: features(df)} to avoid recomputation.
    legacy_lev_filter: reproduce a rounding artefact in strategy2.signal.
        Live computes leverage = round(20 / sl_pct, 1) and then rejects the
        setup if sl_pct * leverage > 20 -- which is true whenever the rounding
        went UP. So live discards roughly half of all otherwise-valid signals,
        chosen by the second decimal of the stop distance, i.e. at random.
        Found 2026-11-12 by this port disagreeing with the legacy signal count
        (407 vs 215 on the same bars). Needed for H0 to be "as deployed".
    """
    out = []
    for coin, df in frames_1h.items():
        f = feats[coin] if feats else features(df)
        long_ = (f["rsi"] <= rsi_lo) & (f["stretch"] <= -min_stretch)
        short_ = (f["rsi"] >= rsi_hi) & (f["stretch"] >= min_stretch)
        d = pd.Series(0, index=f.index)
        d[long_] = 1
        d[short_] = -1
        if max_adx is not None:
            d[f["adx"] >= max_adx] = 0
        dist = sl_atr * f["atr"]
        d[(100 * dist / f["close"]) < min_sl_pct] = 0
        if legacy_lev_filter:
            sl_pct = 100 * dist / f["close"]
            lev = (20.0 / sl_pct).round(1).clip(upper=25.0)
            d[sl_pct * lev > 20.0] = 0
        if trend:
            day = data.resample(df, "1d")
            up = (day["close"] > ema_line(day["close"], trend_ema)).astype(float)
            up[ema_line(day["close"], trend_ema).isna()] = np.nan
            # warm-up: an EMA needs ~its own length before it means anything
            up.iloc[:trend_ema] = np.nan
            st = htf_state(df, up)
            want = d.map({1: 1.0, -1: 0.0, 0: np.nan})   # state a WITH-trend trade needs
            ok = st.eq(want) if trend == "with" else st.eq(1 - want)
            d[~ok] = 0
        m = d != 0
        if m.any():
            out.append(pd.DataFrame({
                "ts": f.index[m], "coin": coin, "dir": d[m].values,
                "stop_dist": dist[m].values,
                "rank": f["stretch"][m].abs().values}))
    s = to_signals(out)
    return s.dropna(subset=["stop_dist", "rank"])
