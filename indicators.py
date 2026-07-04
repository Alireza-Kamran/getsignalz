import numpy as np
import pandas as pd
from hyperliquid.info import Info
import time

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


def fetch_candles(symbol="ETH", interval="1h", lookback_bars=350):
    info = Info(TESTNET_URL, skip_ws=True)
    end_ms = int(time.time() * 1000)
    interval_ms = {"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000,"1d":86400000}
    start_ms = end_ms - lookback_bars * interval_ms[interval]
    candles = info.candles_snapshot(symbol, interval, start_ms, end_ms)
    if not candles:
        return None
    df = pd.DataFrame(candles)
    df = df.rename(columns={"t":"time","o":"open","h":"high","l":"low","c":"close","v":"volume"})
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df = df.set_index("time").sort_index()

    # Signals must confirm on CLOSED candles only (manual strategy reads closed
    # HA bars; intra-bar EMA crossings repaint). Drop the still-forming last bar.
    if len(df) and int(df.index[-1].value // 1_000_000) + interval_ms[interval] > end_ms:
        df = df.iloc[:-1]

    # Keep real prices for execution
    df["real_close"] = df["close"]
    df["real_high"]  = df["high"]
    df["real_low"]   = df["low"]
    df["real_open"]  = df["open"]

    # Convert to Heikin Ashi (user always uses HA — filters noise)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open  = ha_close.copy()
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
    ha_high = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low  = pd.concat([df["low"],  ha_open, ha_close], axis=1).min(axis=1)

    df["open"]  = ha_open
    df["high"]  = ha_high
    df["low"]   = ha_low
    df["close"] = ha_close
    return df


def rsi(close, length=14):
    delta = close.diff()
    alpha = 1.0 / length
    avg_gain = delta.clip(lower=0).ewm(alpha=alpha, adjust=False).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=alpha, adjust=False).mean()
    return 100 - (100 / (1 + avg_gain / avg_loss))


def atr(high, low, close, length=14):
    tr = pd.concat([high-low, (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/length, adjust=False).mean()


def adx(high, low, close, length=14):
    """Average Directional Index — measures trend strength. >25 = trending."""
    up   = high.diff()
    down = -low.diff()
    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    a = atr(high, low, close, length)
    plus_di  = 100 * plus_dm.ewm(alpha=1.0/length, adjust=False).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1.0/length, adjust=False).mean() / a
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1.0/length, adjust=False).mean(), plus_di, minus_di


def ut_bot(close, high, low, key_value=1.0, atr_period=10):
    n_loss = key_value * atr(high, low, close, atr_period)
    trail  = pd.Series(0.0, index=close.index)
    for i in range(1, len(close)):
        p, c, c1, nl = trail.iloc[i-1], close.iloc[i], close.iloc[i-1], n_loss.iloc[i]
        if c > p and c1 > p:   trail.iloc[i] = max(p, c - nl)
        elif c < p and c1 < p: trail.iloc[i] = min(p, c + nl)
        elif c > p:            trail.iloc[i] = c - nl
        else:                  trail.iloc[i] = c + nl
    buy  = (close > trail) & (close.shift(1) <= trail.shift(1))
    sell = (close < trail) & (close.shift(1) >= trail.shift(1))
    return buy, sell, trail


def ssl_channel(high, low, close, length=200):
    ma_high = high.rolling(length).mean()
    ma_low  = low.rolling(length).mean()
    hlv = pd.Series(np.nan, index=close.index)
    for i in range(length, len(close)):
        if close.iloc[i] > ma_high.iloc[i]:    hlv.iloc[i] = 1
        elif close.iloc[i] < ma_low.iloc[i]:   hlv.iloc[i] = -1
        else: hlv.iloc[i] = hlv.iloc[i-1] if not pd.isna(hlv.iloc[i-1]) else 0
    return hlv, ma_high, ma_low


def order_blocks(high, low, close, volume, length=5):
    n       = len(close)
    upper   = high.rolling(length).max()
    lower   = low.rolling(length).min()
    hl2     = (high + low) / 2
    os      = pd.Series(0, index=close.index)
    for i in range(length, n):
        hp, lp = high.iloc[i-length], low.iloc[i-length]
        if hp > upper.iloc[i]:     os.iloc[i] = 0
        elif lp < lower.iloc[i]:   os.iloc[i] = 1
        else:                      os.iloc[i] = os.iloc[i-1]
    phv = pd.Series(np.nan, index=volume.index)
    for i in range(2*length, n):
        c = i - length
        if volume.iloc[c] == volume.iloc[i-2*length:i+1].max():
            phv.iloc[i] = volume.iloc[c]
    bull_ob = ~phv.isna() & (os == 1)
    bear_ob = ~phv.isna() & (os == 0)
    return bull_ob, bear_ob, hl2.shift(length).where(bull_ob), low.shift(length).where(bull_ob), \
           high.shift(length).where(bear_ob), hl2.shift(length).where(bear_ob), os


def fvg(high, low, close):
    bull = (low > high.shift(2)) & (close.shift(1) > high.shift(2))
    bear = (high < low.shift(2)) & (close.shift(1) < low.shift(2))
    return bull, bear


def trendline_breaks(high, low, close, length=14, mult=1.0):
    """Detect trendline breakouts — strong directional signals."""
    n      = len(close)
    slope  = atr(high, low, close, length) / length * mult

    ph = pd.Series(np.nan, index=close.index)
    pl = pd.Series(np.nan, index=close.index)
    for i in range(length, n-length):
        window_h = high.iloc[i-length:i+length+1]
        window_l = low.iloc[i-length:i+length+1]
        if high.iloc[i] == window_h.max(): ph.iloc[i+length] = high.iloc[i]
        if low.iloc[i]  == window_l.min(): pl.iloc[i+length] = low.iloc[i]

    slope_ph = slope.copy()
    slope_pl = slope.copy()
    upper_tl = pd.Series(0.0, index=close.index)
    lower_tl = pd.Series(0.0, index=close.index)
    for i in range(1, n):
        slope_ph.iloc[i] = slope.iloc[i] if not pd.isna(ph.iloc[i]) else slope_ph.iloc[i-1]
        slope_pl.iloc[i] = slope.iloc[i] if not pd.isna(pl.iloc[i]) else slope_pl.iloc[i-1]
        upper_tl.iloc[i] = ph.iloc[i] if not pd.isna(ph.iloc[i]) else upper_tl.iloc[i-1] - slope_ph.iloc[i]
        lower_tl.iloc[i] = pl.iloc[i] if not pd.isna(pl.iloc[i]) else lower_tl.iloc[i-1] + slope_pl.iloc[i]

    upos = pd.Series(0, index=close.index)
    dnos = pd.Series(0, index=close.index)
    for i in range(1, n):
        adj_upper = upper_tl.iloc[i] - slope_ph.iloc[i] * length
        adj_lower = lower_tl.iloc[i] + slope_pl.iloc[i] * length
        upos.iloc[i] = 0 if not pd.isna(ph.iloc[i]) else (1 if close.iloc[i] > adj_upper else upos.iloc[i-1])
        dnos.iloc[i] = 0 if not pd.isna(pl.iloc[i]) else (1 if close.iloc[i] < adj_lower else dnos.iloc[i-1])

    bull_break = (upos == 1) & (upos.shift(1) == 0)
    bear_break = (dnos == 1) & (dnos.shift(1) == 0)
    return bull_break, bear_break


def atr_rsi_sl(open_, high, low, close, atr_len=6, rsi_len=13):
    a = atr(high, low, close, atr_len)
    r = rsi(close, rsi_len)
    mul = pd.Series(1.5, index=close.index)
    mul[(r >= 30) & (r < 50)]  = 1.4 - 0.0466 * (50 - r[(r >= 30) & (r < 50)])
    mul[(r >= 50) & (r <= 70)] = 1.0 + 0.0466 * (r[(r >= 50) & (r <= 70)] - 50)
    return open_ - a * mul, open_ + a * mul, a, r


def sling_shot(close, fast=38, slow=62):
    """CM Sling Shot System — EMA cloud (38/62) with pullback/conservative entry signals."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    cloud_bull = ema_fast > ema_slow
    cloud_bear = ema_fast < ema_slow
    # Aggressive entry: price pulled back into/below fast EMA while cloud is directional
    pb_long  = cloud_bull & (close < ema_fast)
    pb_short = cloud_bear & (close > ema_fast)
    # Conservative entry: price crosses back through fast EMA after pullback
    en_long  = cloud_bull & (close > ema_fast) & (close.shift(1) <= ema_fast.shift(1))
    en_short = cloud_bear & (close < ema_fast) & (close.shift(1) >= ema_fast.shift(1))
    return ema_fast, ema_slow, cloud_bull, cloud_bear, pb_long, pb_short, en_long, en_short


def williams_r(high, low, close, length=21, ema_len=13):
    """Williams %R oscillator with EMA smoothing. Range: -100 (oversold) to 0 (overbought)."""
    upper     = high.rolling(length).max()
    lower     = low.rolling(length).min()
    willr     = 100 * (close - upper) / (upper - lower).replace(0, np.nan)
    willr_ema = willr.ewm(span=ema_len, adjust=False).mean()
    return willr, willr_ema


def ema_line(close, length=200):
    """Simple EMA. Used as the EMA 200 trend filter."""
    return close.ewm(span=length, adjust=False).mean()
