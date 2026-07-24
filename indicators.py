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


def liquidity_pools(open_, high, low, close, volume, contact_min=2, gap_bars=5, wait_bars=10):
    """
    Liquidity Pool zones — bar-by-bar port of the "Liquidity Pools" block in
    ind.txt (Kamran's own Pine Script indicator). A zone is a price level
    that has been repeatedly wicked into and rejected from (without closing
    through) — validated by requiring `contact_min` separate contacts,
    spaced at least `gap_bars` apart, before it confirms `wait_bars` after
    the last contact. This is the user's actual manual target-selection
    method: these zones are the "liquidity pools" he waits for price to
    reach.

    Runs on whatever OHLC series is passed in (this project already applies
    Heikin Ashi upstream via fetch_candles(), same as order_blocks()/
    atr_rsi_sl() — kept consistent with that established convention rather
    than special-casing raw price just for this indicator).

    Returns (bear_zones, bull_zones) — each a list of zone dicts:
      {"left": int, "right": int, "top": float, "bottom": float,
       "vol": float, "mitigated_bar": int or None}
    "left"/"right"/"mitigated_bar" are POSITIONAL bar indices into the input
    series (0..len-1) — map to real time via series.index[pos]. A zone with
    mitigated_bar=None is still active as of the last bar. bear_zones sit
    above price (resistance, formed from rejected highs); bull_zones sit
    below (support, formed from rejected lows).

    Note: Pine's box-merge logic pushes overlapping zones as duplicate array
    entries referencing the same mutable box (an artifact of how it's
    written, not a deliberate design choice) — this port merges overlapping
    zones into a single widened entry instead, which is behaviorally
    equivalent for "what zones are active / how strong are they" but avoids
    carrying that duplication forward.
    """
    n = len(close)
    if n < 3:
        return [], []

    o_a  = open_.values
    c_a  = close.values
    h_a  = high.values
    l_a  = low.values
    v_a  = volume.values
    c_top_a = np.maximum(o_a, c_a)
    c_bot_a = np.minimum(o_a, c_a)

    # Running structures: [h, t, b, l, bi] (highest-high / lowest-low anchors)
    hst = [h_a[0], c_top_a[0], c_bot_a[0], l_a[0], 0]
    lst = [h_a[0], c_top_a[0], c_bot_a[0], l_a[0], 0]
    h_count = l_count = 0
    last_h_wick = last_l_wick = 0
    hi_vol = lo_vol = 0.0

    # Per-bar history for Pine's [1]/[2]-offset comparisons
    hst_t_hist  = [hst[1]]
    lst_b_hist  = [lst[2]]
    h_wick_hist = [False]
    l_wick_hist = [False]
    bs_hw_hist  = [0]
    bs_lw_hist  = [0]

    h_zn = None   # in-progress running zone (dict) — mirrors Pine's h_zn/l_zn
    l_zn = None
    bear_zones, bull_zones = [], []

    for i in range(1, n):
        cur = (h_a[i], c_top_a[i], c_bot_a[i], l_a[i], i)

        # ── Adjusting High/Low check boundaries ──
        if (h_a[i] > hst[0]) and (c_top_a[i] > hst[0] or c_top_a[i] < hst[1]):
            if h_count > 1:
                lst = list(cur); lo_vol = 0.0; l_count = 0
            hst = list(cur); hi_vol = 0.0; h_count = 1; last_h_wick = i

        if (l_a[i] < lst[3]) and (c_bot_a[i] < lst[3] or c_bot_a[i] > lst[2]):
            if l_count > 1:
                hst = list(cur); hi_vol = 0.0; h_count = 0
            lst = list(cur); lo_vol = 0.0; l_count = 1; last_l_wick = i

        # ── Contact counting: previous bar's wick flag + stable structure + gap spacing ──
        if h_wick_hist[i - 1] and (hst_t_hist[i - 1] == hst_t_hist[i - 2]) and (bs_hw_hist[i - 1] > gap_bars):
            h_count += 1
            last_h_wick = i - 1
        if l_wick_hist[i - 1] and (lst_b_hist[i - 1] == lst_b_hist[i - 2]) and (bs_lw_hist[i - 1] > gap_bars):
            l_count += 1
            last_l_wick = i - 1

        bs_hw = abs(last_h_wick - i)
        bs_lw = abs(last_l_wick - i)
        prev_bs_hw, prev_bs_lw = bs_hw_hist[i - 1], bs_lw_hist[i - 1]

        # ── Extend structure extremes ──
        if h_a[i] > hst[0]: hst[0] = h_a[i]
        if l_a[i] < lst[3]: lst[3] = l_a[i]

        # ── Volume tracking ──
        rng = h_a[i] - l_a[i]
        if rng > 0:
            hi_vol += max(h_a[i] - hst[1], 0) / rng * v_a[i]
            lo_vol += max(lst[2] - l_a[i], 0) / rng * v_a[i]

        # ── Current-bar wick flags (read next iteration for contact counting) ──
        h_wick = (h_a[i] > hst[1]) and (c_top_a[i] <= hst[1])
        l_wick = (l_a[i] < lst[2]) and (c_bot_a[i] >= lst[2])

        # ── Zone creation/merging ──
        crossover_hw = (prev_bs_hw <= wait_bars) and (bs_hw > wait_bars)
        crossover_lw = (prev_bs_lw <= wait_bars) and (bs_lw > wait_bars)

        if h_count >= contact_min and crossover_hw and c_a[i] < hst[1]:
            new_top, new_bot = hst[0], hst[1]
            if h_zn is not None and (hst[4] == h_zn["left"] or (new_top >= h_zn["bottom"] and new_bot <= h_zn["top"])):
                h_zn["top"]    = max(new_top, h_zn["top"])
                h_zn["bottom"] = min(new_bot, h_zn["bottom"])
                h_zn["vol"]   += hi_vol
                h_zn["right"]  = i
            else:
                h_zn = {"left": hst[4], "right": i, "top": new_top, "bottom": new_bot,
                        "vol": hi_vol, "state": 0, "mitigated_bar": None}
                bear_zones.append(h_zn)

        if l_count >= contact_min and crossover_lw and c_a[i] > lst[2]:
            new_top, new_bot = lst[2], lst[3]
            if l_zn is not None and (lst[4] == l_zn["left"] or (new_top >= l_zn["bottom"] and new_bot <= l_zn["top"])):
                l_zn["top"]    = max(new_top, l_zn["top"])
                l_zn["bottom"] = min(new_bot, l_zn["bottom"])
                l_zn["vol"]   += lo_vol
                l_zn["right"]  = i
            else:
                l_zn = {"left": lst[4], "right": i, "top": new_top, "bottom": new_bot,
                        "vol": lo_vol, "state": 0, "mitigated_bar": None}
                bull_zones.append(l_zn)

        # ── Mitigation: 2 consecutive closes through the zone removes it ──
        for z in bull_zones:
            if z["mitigated_bar"] is not None:
                continue
            if c_a[i] < z["bottom"]:
                if z["state"] < 0:
                    z["mitigated_bar"] = i
                z["state"] -= 1
            else:
                z["state"] = 0
        for z in bear_zones:
            if z["mitigated_bar"] is not None:
                continue
            if c_a[i] > z["top"]:
                if z["state"] < 0:
                    z["mitigated_bar"] = i
                z["state"] -= 1
            else:
                z["state"] = 0

        hst_t_hist.append(hst[1]); lst_b_hist.append(lst[2])
        h_wick_hist.append(h_wick); l_wick_hist.append(l_wick)
        bs_hw_hist.append(bs_hw); bs_lw_hist.append(bs_lw)

    return bear_zones, bull_zones
