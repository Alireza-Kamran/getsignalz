"""
Strategy brain — full indicator suite, multi-coin, multi-timeframe, regime + session filters.
Only fires on genuinely rare high-confluence setups.
"""
import numpy as np
import pandas as pd
from loguru import logger
from indicators import (fetch_candles, rsi, atr, adx, ut_bot, ssl_channel,
                        order_blocks, fvg, trendline_breaks, atr_rsi_sl)

# ── Coins to monitor ──────────────────────────────────────────────────────────
WATCHLIST = [
    "ETH",
    "WLD",
    "ARB",
    "SUI",
    "SOL",
    "INJ",
    "AAVE",
    "AVAX",
    "ATOM",
    "OP",
    "RUNE",
    "BNB",
    "APT",
    "DOGE"
]

# ── Strategy params ───────────────────────────────────────────────────────────
MIN_SCORE   = 8       # minimum confluence to open a trade
MIN_ADX     = 30
TP_RATIO    = 1.5     # risk:reward
RISK_PCT    = 0.02    # 3% account risk per trade
MAX_TRADES  = 1

# ── Session: London + NY only (07:00 - 22:00 UTC) ────────────────────────────
SESSION_START = 11
SESSION_END   = 23


def in_session(hour_utc: int) -> bool:
    return SESSION_START <= hour_utc < SESSION_END


def build_df(coin, tf="1h", bars=350):
    df = fetch_candles(coin, tf, lookback_bars=bars)
    if df is None or len(df) < 220:
        return None

    df["rsi"]  = rsi(df["close"])
    df["atr"]  = atr(df["high"], df["low"], df["close"])
    adx_v, plus_di, minus_di = adx(df["high"], df["low"], df["close"])
    df["adx"]      = adx_v
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di

    ut_buy_s, ut_sell_s, ut_trail_s = ut_bot(df["close"], df["high"], df["low"])
    df["ut_buy"]   = ut_buy_s
    df["ut_sell"]  = ut_sell_s
    df["ut_trail"] = ut_trail_s

    ssl_hlv, ssl_mh, ssl_ml = ssl_channel(df["high"], df["low"], df["close"], 200)
    df["ssl"]     = ssl_hlv
    df["ssl_mh"]  = ssl_mh
    df["ssl_ml"]  = ssl_ml
    df["ssl_str"] = df["ssl"].rolling(10).apply(lambda x: (x == x.iloc[-1]).sum())

    bull_ob, bear_ob, bt, bb, brt, brb, struct = order_blocks(
        df["high"], df["low"], df["close"], df["volume"]
    )
    df["bull_ob"] = bull_ob
    df["bear_ob"] = bear_ob
    df["struct"]  = struct

    fvg_bull, fvg_bear = fvg(df["high"], df["low"], df["close"])
    df["fvg_bull"] = fvg_bull.rolling(5).sum() > 0
    df["fvg_bear"] = fvg_bear.rolling(5).sum() > 0

    tl_bull, tl_bear = trendline_breaks(df["high"], df["low"], df["close"])
    df["tl_bull"] = tl_bull.rolling(3).sum() > 0
    df["tl_bear"] = tl_bear.rolling(3).sum() > 0

    df["vol_spike"] = df["volume"] > df["volume"].rolling(20).mean() * 1.5

    sl_long, sl_short, _, _ = atr_rsi_sl(
        df["open"], df["high"], df["low"], df["close"]
    )
    df["sl_long"]  = sl_long
    df["sl_short"] = sl_short

    return df.dropna(subset=["rsi", "ssl", "atr", "adx"])


def get_macro_trend(coin, current_tf="1h"):
    """Get 4h trend direction as macro filter. Returns 1, -1, or 0."""
    macro_tf = "4h" if current_tf in ["1h", "15m"] else "1d"
    try:
        df4 = fetch_candles(coin, macro_tf, lookback_bars=220)
        if df4 is None or len(df4) < 210:
            return 0
        ssl_hlv, _, _ = ssl_channel(df4["high"], df4["low"], df4["close"], 200)
        return int(ssl_hlv.iloc[-1])
    except Exception:
        return 0


def is_trending(df) -> bool:
    """Market regime filter — only trade trending conditions."""
    last = df.iloc[-1]
    adx_ok  = last["adx"] > MIN_ADX
    ssl_ok  = abs(last.get("ssl_str", 0)) >= 5  # SSL consistent for 5+ bars
    return adx_ok and ssl_ok


def score_setup(df, direction, macro_trend=0):
    """
    Score a potential trade 0-11. Only trade >= MIN_SCORE.
    Returns (score, reasons, sl, tp).
    """
    last  = df.iloc[-1]
    price = last["real_close"]
    ssl   = last["ssl"]
    r     = last["rsi"]
    score = 0
    reasons = []

    # ── HARD GATES (instant reject) ─────────────────────────────────────────
    if direction == 1 and ssl != 1:   return 0, [], None, None
    if direction == -1 and ssl != -1: return 0, [], None, None
    if direction == 1 and r >= 68:    return 0, [], None, None  # overbought = bad long
    if direction == -1 and r <= 25:                          return 0, [], None, None  # ultra-oversold = exhausted move, poor short entry
    if direction == -1 and r <= 32 and last["adx"] <= 40:   return 0, [], None, None
    if not is_trending(df):           return 0, [], None, None  # ranging market = skip

    # ── SSL TREND (2 pts) ─────────────────────────────────────────────────────
    score += 2
    reasons.append(f"SSL {'bullish' if direction==1 else 'bearish'}")

    # Strong trend bonus (1 pt)
    if last["ssl_str"] >= 7:
        score += 1
        reasons.append(f"Strong trend {int(last['ssl_str'])}/10 bars")

    # ── MACRO 4H TREND (2 pts) ────────────────────────────────────────────────
    if macro_trend == direction:
        score += 2
        reasons.append(f"4H trend aligned")
    elif macro_trend == 0:
        score += 1
        reasons.append("4H trend neutral")
    else:
        score -= 1  # against macro trend = penalty

    # ── MARKET STRUCTURE (1 pt) ───────────────────────────────────────────────
    if direction == 1 and last["struct"] == 1:
        score += 1
        reasons.append("Bull market structure")
    elif direction == -1 and last["struct"] == 0:
        score += 1
        reasons.append("Bear market structure")

    # ── ORDER BLOCK (2 pts) ───────────────────────────────────────────────────
    if direction == 1 and last["bull_ob"]:
        score += 2
        reasons.append("Fresh bullish OB")
    elif direction == -1 and last["bear_ob"]:
        score += 2
        reasons.append("Fresh bearish OB")

    # ── FAIR VALUE GAP (+1 long / -2 short) ──────────────────────────────────────
    if direction == 1 and last["fvg_bull"]:
        score += 1
        reasons.append("Bullish FVG")
    elif direction == -1 and last["fvg_bear"]:
        score -= 2
        reasons.append("Bearish FVG")

    # UT Bot removed: 0W/3L on 1H — lagging signal, no predictive value

    # Trendline break removed: 0W/2L on 1H — too slow for 1H scalping

    # ── RSI CONTEXT (0-2 pts) ─────────────────────────────────────────────────
    if direction == 1:
        if r <= 35:
            score += 2; reasons.append(f"RSI oversold {r:.0f}")
        elif r <= 50:
            score += 1; reasons.append(f"RSI {r:.0f} healthy")
    else:
        if r >= 65:
            score += 2; reasons.append(f"RSI overbought {r:.0f}")
        elif r >= 50:
            score += 1; reasons.append(f"RSI {r:.0f} healthy")

    # ── VOLUME SPIKE (1 pt) ───────────────────────────────────────────────────
    if last["vol_spike"]:
        score += 1; reasons.append("Volume spike")

    # ── SL — place at nearest key technical level ─────────────────────────────
    # Priority: OB edge > FVG edge > ATR×RSI level > 2% fallback
    sl = None

    if direction == -1:
        # Short: SL above entry at nearest resistance
        if last["bear_ob"] and not pd.isna(last.get("sl_short", float("nan"))):
            candidate = last["sl_short"]
            if candidate > price:
                sl = candidate
        if sl is None:
            # Use UT Bot trail as resistance
            trail = last.get("ut_trail", float("nan"))
            if not pd.isna(trail) and trail > price:
                sl = trail * 1.002  # tiny buffer above trail
        if sl is None:
            sl = price * 1.015  # 1.5% fallback
    else:
        # Long: SL below entry at nearest support
        if last["bull_ob"] and not pd.isna(last.get("sl_long", float("nan"))):
            candidate = last["sl_long"]
            if candidate < price:
                sl = candidate
        if sl is None:
            trail = last.get("ut_trail", float("nan"))
            if not pd.isna(trail) and trail < price:
                sl = trail * 0.998
        if sl is None:
            sl = price * 0.985  # 1.5% fallback

    sl_pct = abs(price - sl) / price * 100
    if sl_pct < 0.4:
        return 0, [], None, None, None, None, None

    MAX_LEV_LOSS = 25.0   # max 25% leveraged loss at SL
    MAX_LEVERAGE = 25.0

    # Compute leverage first so gate can check actual trade leverage
    raw_tp_pct = min(max(sl_pct * 2.0, 1.5), 6.0)
    tp_pct     = raw_tp_pct
    leverage   = min(round(50.0 / tp_pct, 1), MAX_LEVERAGE)

    # Gate: reject if SL risk at this trade's leverage exceeds limit
    if sl_pct * leverage > MAX_LEV_LOSS:
        return 0, [], None, None, None, None, None

    tp = price + direction * price * (tp_pct / 100)

    return max(score, 0), reasons, sl, tp, leverage, round(sl_pct, 3), round(tp_pct, 3)


def find_best_setup(open_positions, hour_utc=None):
    """
    Scan all coins, return the highest-scoring valid setup, or None.
    Respects session filter and regime filter.
    """
    if hour_utc is not None and not in_session(hour_utc):
        return None

    best = None
    best_score = MIN_SCORE - 1

    for coin in WATCHLIST:
        if coin in open_positions:
            continue
        try:
            df = build_df(coin, "1h", 350)
            if df is None:
                continue

            macro = get_macro_trend(coin, "1h")

            for direction in [1, -1]:
                result = score_setup(df, direction, macro)
                if result[0] == 0:
                    continue
                score, reasons, sl, tp, leverage, sl_pct, tp_pct = result
                if score > best_score:
                    best_score = score
                    best = {
                        "coin":      coin,
                        "direction": direction,
                        "score":     score,
                        "reasons":   reasons,
                        "price":     df.iloc[-1]["real_close"],
                        "sl":        sl,
                        "tp":        tp,
                        "leverage":  leverage,
                        "sl_pct":    sl_pct,
                        "tp_pct":    tp_pct,
                        "macro":     macro,
                        "adx":       round(float(df.iloc[-1]["adx"]), 1),
                        "rsi":       round(float(df.iloc[-1]["rsi"]), 1),
                    }
        except Exception as e:
            logger.warning(f"Scan error {coin}: {e}")

    return best


def quick_state(coin, tf="1h"):
    """Return brief state dict for a coin — used in Telegram candle updates."""
    try:
        df   = build_df(coin, tf, 350)
        if df is None: return None
        last = df.iloc[-1]
        macro = get_macro_trend(coin, tf)
        l_res = score_setup(df, 1,  macro)
        s_res = score_setup(df, -1, macro)
        l_sc  = l_res[0]
        s_sc  = s_res[0]
        return {
            "coin":  coin, "price": last["real_close"],
            "rsi":   last["rsi"], "adx": last["adx"],
            "ssl":   int(last["ssl"]),
            "ssl_str": int(last["ssl_str"]),
            "macro": macro,
            "long_score":  l_sc,
            "short_score": s_sc,
            "best_score":  max(l_sc, s_sc),
            "best_dir":    1 if l_sc >= s_sc and l_sc > 0 else (-1 if s_sc > 0 else 0),
        }
    except Exception:
        return None
