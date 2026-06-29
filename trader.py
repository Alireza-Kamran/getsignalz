"""
Strategy brain — CM Sling Shot System (EMA 38/62 cloud) with multi-TF scanning.
Fires on conservative/aggressive pullback entries with cloud + EMA200 + SSL confirmation.
"""
import numpy as np
import pandas as pd
from loguru import logger
from indicators import (fetch_candles, rsi, atr, adx, ssl_channel,
                        order_blocks, atr_rsi_sl, sling_shot, williams_r, ema_line)

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
MIN_SCORE   = 6       # minimum confluence (max 8)
MIN_ADX     = 30
TP_RATIO    = 2.0         # 1:2 RR minimum — aligns with score_setup qualification logic
RISK_PCT    = 0.01
MAX_TRADES  = 1

# ── Session: London + NY close (11:00 - 24:00 UTC) ────────────────────────────
SESSION_START = 11
SESSION_END   = 24


def in_session(hour_utc: int) -> bool:
    return SESSION_START <= hour_utc < SESSION_END


def build_df(coin, tf="1h", bars=350):
    df = fetch_candles(coin, tf, lookback_bars=bars)
    if df is None or len(df) < 220:
        return None

    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df["high"], df["low"], df["close"])
    adx_v, plus_di, minus_di = adx(df["high"], df["low"], df["close"])
    df["adx"]      = adx_v
    df["plus_di"]  = plus_di
    df["minus_di"] = minus_di

    ssl_hlv, ssl_mh, ssl_ml = ssl_channel(df["high"], df["low"], df["close"], 200)
    df["ssl"]     = ssl_hlv
    df["ssl_mh"]  = ssl_mh
    df["ssl_ml"]  = ssl_ml
    df["ssl_str"] = df["ssl"].rolling(10).apply(lambda x: (x == x.iloc[-1]).sum())

    # CM Sling Shot: EMA 38/62 cloud + entry signals
    ss_fast, ss_slow, cloud_bull, cloud_bear, pb_long, pb_short, en_long, en_short = sling_shot(df["close"])
    df["ss_fast"]    = ss_fast
    df["ss_slow"]    = ss_slow
    df["cloud_bull"] = cloud_bull
    df["cloud_bear"] = cloud_bear
    df["pb_long"]    = pb_long    # aggressive: price pulled back to fast EMA
    df["pb_short"]   = pb_short
    df["en_long"]    = en_long    # conservative: price crossed back through fast EMA
    df["en_short"]   = en_short

    # Williams %R (21) + EMA 13 smoothing — entry filter, future exit signal
    willr, willr_ema = williams_r(df["high"], df["low"], df["close"])
    df["willr"]     = willr
    df["willr_ema"] = willr_ema

    # EMA 200 direction gate (long only above, short only below)
    df["ema200"] = ema_line(df["close"], 200)

    # Order blocks — kept for SL placement
    bull_ob, bear_ob, bt, bb, brt, brb, struct = order_blocks(
        df["high"], df["low"], df["close"], df["volume"]
    )
    df["bull_ob"] = bull_ob
    df["bear_ob"] = bear_ob

    sl_long, sl_short, _, _ = atr_rsi_sl(
        df["open"], df["high"], df["low"], df["close"]
    )
    df["sl_long"]  = sl_long
    df["sl_short"] = sl_short

    return df.dropna(subset=["rsi", "ssl", "atr", "adx", "ss_fast", "willr", "ema200"])


def get_macro_trend(coin, current_tf="1h"):
    """Higher-TF trend via CM Sling Shot cloud direction. Returns 1, -1, or 0."""
    macro_tf = "4h" if current_tf in ["1h", "15m"] else "1d"
    try:
        df4 = fetch_candles(coin, macro_tf, lookback_bars=220)
        if df4 is None or len(df4) < 100:
            return 0
        ss_fast, ss_slow, *_ = sling_shot(df4["close"])
        if ss_fast.iloc[-1] > ss_slow.iloc[-1]:
            return 1
        elif ss_fast.iloc[-1] < ss_slow.iloc[-1]:
            return -1
        return 0
    except Exception:
        return 0


def score_setup(df, direction, macro_trend=0):
    """
    CM Sling Shot scoring. Max 8 pts. Fire at >= MIN_SCORE (6).
    Scoring: cloud(2) + entry type(1-2) + 4H alignment(0-2) + ADX(1) + cloud width(1)
    Returns (score, reasons, sl, tp, leverage, sl_pct, tp_pct)
    """
    last  = df.iloc[-1]
    price = last["real_close"]
    score = 0
    reasons = []

    # ── HARD GATES ────────────────────────────────────────────────────────────
    if direction == 1 and not last.get("cloud_bull", False):
        return 0, [], None, None, None, None, None
    if direction == -1 and not last.get("cloud_bear", False):
        return 0, [], None, None, None, None, None
    # SSL (orange channel) must agree
    if direction == 1 and last["ssl"] != 1:
        return 0, [], None, None, None, None, None
    if direction == -1 and last["ssl"] != -1:
        return 0, [], None, None, None, None, None
    # EMA 200 direction gate
    ema200 = float(last.get("ema200", price))
    if direction == 1 and price < ema200:
        return 0, [], None, None, None, None, None
    if direction == -1 and price > ema200:
        return 0, [], None, None, None, None, None
    # Entry signal required
    has_conservative = (direction == 1 and bool(last.get("en_long", False))) or                        (direction == -1 and bool(last.get("en_short", False)))
    has_aggressive   = (direction == 1 and bool(last.get("pb_long", False))) or                        (direction == -1 and bool(last.get("pb_short", False)))
    if not has_conservative and not has_aggressive:
        return 0, [], None, None, None, None, None
    # Williams %R: reject if already exhausted (late entry)
    willr     = float(last.get("willr", -50))
    willr_ema = float(last.get("willr_ema", -50))
    if direction == 1 and willr > -20 and willr_ema > -20:
        return 0, [], None, None, None, None, None
    if direction == -1 and willr < -80 and willr_ema < -80:
        return 0, [], None, None, None, None, None
    # RSI extremes: entering a short when RSI < 25 means price has already fallen hard;
    # mean-reversion risk dominates and these entries have shown immediate SL hits.
    rsi_val = float(last["rsi"])
    if direction == -1 and rsi_val < 25:
        return 0, [], None, None, None, None, None
    if direction == 1 and rsi_val > 75:
        return 0, [], None, None, None, None, None
    # Minimum trend filter
    if float(last["adx"]) < 25:
        return 0, [], None, None, None, None, None

    # ── SCORING ───────────────────────────────────────────────────────────────
    # Cloud direction (2 pts — always once past gates)
    score += 2
    reasons.append(f"EMA cloud {'bullish' if direction == 1 else 'bearish'}")

    # Entry type: conservative (2) beats aggressive (1)
    if has_conservative:
        score += 2
        reasons.append("Conservative entry (EMA cross)")
    else:
        score += 1
        reasons.append("Aggressive entry (EMA pullback)")

    # 4H macro via higher-TF cloud
    if macro_trend == direction:
        score += 2
        reasons.append("4H cloud aligned")
    elif macro_trend == 0:
        score += 1
        reasons.append("4H cloud neutral")
    else:
        score -= 1
        reasons.append("4H cloud against")

    # Strong trend ADX > 30 (1 pt)
    if float(last["adx"]) > MIN_ADX:
        score += 1
        reasons.append(f"ADX {last['adx']:.0f}")

    # Wide cloud = strong EMA separation (1 pt)
    ss_fast  = float(last.get("ss_fast", price))
    ss_slow  = float(last.get("ss_slow", price))
    cloud_pct = abs(ss_fast - ss_slow) / price * 100
    if cloud_pct > 0.5:
        score += 1
        reasons.append(f"Cloud {cloud_pct:.1f}%")

    # ── SL PLACEMENT ─────────────────────────────────────────────────────────
    # Priority: OB edge > fast EMA cloud edge > 1.5% fallback
    sl = None

    if direction == -1:
        if last.get("bear_ob", False) and not pd.isna(last.get("sl_short", float("nan"))):
            candidate = last["sl_short"]
            if candidate > price:
                sl = candidate
        if sl is None and ss_fast > price:
            sl = ss_fast * 1.005  # en_short: SL just above fast EMA
        if sl is None and ss_slow > price:
            sl = ss_slow * 1.005  # pb_short: SL just above slow EMA (cloud top)
        if sl is None:
            sl = price * 1.02
    else:
        if last.get("bull_ob", False) and not pd.isna(last.get("sl_long", float("nan"))):
            candidate = last["sl_long"]
            if candidate < price:
                sl = candidate
        if sl is None and ss_fast < price:
            sl = ss_fast * 0.995  # en_long: SL just below fast EMA
        if sl is None and ss_slow < price:
            sl = ss_slow * 0.995  # pb_long: SL just below slow EMA (cloud bottom)
        if sl is None:
            sl = price * 0.98

    sl_pct = abs(price - sl) / price * 100
    if sl_pct < 0.4:
        return 0, [], None, None, None, None, None

    MAX_LEV_LOSS = 25.0
    MAX_LEVERAGE = 25.0

    raw_tp_pct = min(max(sl_pct * 2.0, 1.5), 6.0)
    tp_pct     = raw_tp_pct
    leverage   = min(round(50.0 / tp_pct, 1), MAX_LEVERAGE)

    if sl_pct * leverage > MAX_LEV_LOSS:
        return 0, [], None, None, None, None, None

    tp = price + direction * price * (tp_pct / 100)

    return max(score, 0), reasons, sl, tp, leverage, round(sl_pct, 3), round(tp_pct, 3)


def find_best_setup(open_positions, hour_utc=None):
    """
    Scan all coins on 4h / 1h / 15m. Return highest-scoring valid setup, or None.
    """
    if hour_utc is not None and not in_session(hour_utc):
        return None

    best       = None
    best_score = MIN_SCORE - 1

    for coin in WATCHLIST:
        if coin in open_positions:
            continue
        for tf in ["4h", "1h", "15m"]:
            try:
                df = build_df(coin, tf, 350)
                if df is None:
                    continue

                macro = get_macro_trend(coin, tf)

                for direction in [1, -1]:
                    result = score_setup(df, direction, macro)
                    if result[0] == 0:
                        continue
                    score, reasons, sl, tp, leverage, sl_pct, tp_pct = result
                    if score > best_score:
                        best_score = score
                        best = {
                            "coin":      coin,
                            "tf":        tf,
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
                logger.warning(f"Scan error {coin}/{tf}: {e}")

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
