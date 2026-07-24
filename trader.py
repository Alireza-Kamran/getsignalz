"""
Strategy brain — Liquidity Pool target-seeking with Order Block / FVG entry
and path-clean confirmation.

Rebuilt 2026-07-24 to match Kamran's actual manual process (see project
memory for the full description he gave): wait for a liquidity pool (LP)
with high probability of being reached, in the direction of the broader
trend; confirm the path to it is clean of obstacles (no order block/FVG in
the way that could react first and stop-hunt); enter precisely off a
nearby order block or FVG (the entry zone is a DIFFERENT, closer zone than
the LP target); size risk so max stop is ~20% leveraged loss; require at
least 1:2 R:R; trail through progressive targets, extending toward the far
edge of an FVG as the final target when one exists beyond the LP.

Replaces the 2026-06-25 "CM Sling Shot" EMA-cloud system, which turned out
not to match how Kamran actually trades (he described a completely
different, liquidity/OB/FVG-based process on 2026-07-24). That system's
indicator functions (sling_shot, williams_r, ema_line) remain in
indicators.py — harmless, just no longer wired into scoring here.
"""
import numpy as np
import pandas as pd
from loguru import logger
from indicators import (fetch_candles, rsi, adx, order_blocks, fvg,
                        atr_rsi_sl, liquidity_pools, ssl_channel)

# ── Coins to monitor ──────────────────────────────────────────────────────────
WATCHLIST = [
    "ETH", "WLD", "ARB", "SUI", "SOL", "INJ", "AAVE", "AVAX", "ATOM", "OP", "APT", "DOGE"
]

# ── Strategy params ───────────────────────────────────────────────────────────
MIN_SCORE     = 6         # minimum confluence (max 8) -- initial value, tuned nightly like every prior regime
TP_RATIO      = 2.0       # 1:2 RR minimum -- Kamran's stated floor
RISK_PCT      = 0.01
MAX_TRADES    = 1
MAX_LEV_LOSS  = 20.0      # Kamran's stated max stop. Tighter than executor.py's independent
                          # 25% outer safety clamp (untouched, unrelated defense-in-depth --
                          # protects against ANY strategy bug, not specific to this one).
MAX_LEVERAGE  = 25.0

TF_BONUS = {"4h": 2, "1d": 2, "1h": 1, "15m": 0}   # higher-TF zones score a bonus (stronger, per Kamran).
                          # Shared constant -- backtest.py imports this too so the qualification bar
                          # (score_setup() raw score + bonus, compared to MIN_SCORE) can't drift between
                          # live and backtest the way it did once already during this rebuild.

PATH_CLEAR_PAD  = 0.05    # % padding so a zone touching the entry/target edge isn't falsely "in the way"
RELIABILITY_MIN_SAMPLE = 3
RELIABILITY_GOOD_RATE  = 0.6
REACTION_ZONE_MULT     = 1.0   # a "clean reaction" moves at least 1x the zone's own height away before mitigation

# ── Session: London + NY close (11:00 - 24:00 UTC) ────────────────────────────
SESSION_START = 11
SESSION_END   = 24


def in_session(hour_utc: int) -> bool:
    return SESSION_START <= hour_utc < SESSION_END


def build_df(coin, tf="1h", bars=600):
    df = fetch_candles(coin, tf, lookback_bars=bars)
    if df is None or len(df) < 220:
        return None

    df["rsi"] = rsi(df["close"])
    adx_v, plus_di, minus_di = adx(df["high"], df["low"], df["close"])
    df["adx"] = adx_v

    # Drop warmup NaNs BEFORE computing zones -- liquidity_pools()/order_blocks()
    # return bar positions (0..len-1) into whatever series they're given, so the
    # dataframe's row count must never change again after this point or those
    # positions stop lining up with df.iloc[] rows.
    df = df.dropna(subset=["rsi", "adx"])
    if len(df) < 220:
        return None

    bull_ob, bear_ob, ob_bull_top, ob_bull_bot, ob_bear_top, ob_bear_bot, struct = order_blocks(
        df["high"], df["low"], df["close"], df["volume"]
    )
    df["bull_ob"] = bull_ob
    df["bear_ob"] = bear_ob
    df["ob_bull_top"] = ob_bull_top
    df["ob_bull_bot"] = ob_bull_bot
    df["ob_bear_top"] = ob_bear_top
    df["ob_bear_bot"] = ob_bear_bot
    df["struct"] = struct

    fvg_bull, fvg_bear = fvg(df["high"], df["low"], df["close"])
    df["fvg_bull"] = fvg_bull
    df["fvg_bear"] = fvg_bear

    sl_long, sl_short, _, _ = atr_rsi_sl(df["open"], df["high"], df["low"], df["close"])
    df["sl_long"] = sl_long
    df["sl_short"] = sl_short

    # Cosmetic-only fields kept purely so live.py's existing candle-scan log line
    # (SSL {s['ssl']:+d}) keeps working unchanged -- SSL no longer gates or scores.
    ssl_hlv, ssl_mh, ssl_ml = ssl_channel(df["high"], df["low"], df["close"], 200)
    df["ssl"] = ssl_hlv
    df["ssl_str"] = df["ssl"].rolling(10).apply(lambda x: (x == x.iloc[-1]).sum())

    bear_zones, bull_zones = liquidity_pools(df["open"], df["high"], df["low"], df["close"], df["volume"])
    df.attrs["bear_zones"] = bear_zones
    df.attrs["bull_zones"] = bull_zones

    if len(df) >= 3 and df["real_close"].iloc[-3:].nunique() == 1:
        return None
    return df


def _active_zones(zones, upto_bar):
    """Zones confirmed by `upto_bar` and not yet mitigated -- the no-lookahead
    'as of this bar' view. Confirmation bar ('right'), not creation bar
    ('left'), is what gates visibility: a zone only becomes knowable once its
    contact-count/wait-bar confirmation actually fired."""
    return [z for z in zones
            if z["right"] <= upto_bar and (z["mitigated_bar"] is None or z["mitigated_bar"] > upto_bar)]


def _classify_zone_reactions(df, zones, is_bear):
    """Tag each mitigated zone with `_reaction`: True if price moved a clean
    distance away before eventually mitigating, False if it plowed through
    with little pushback. Bear zones reject downward (reaction = price
    dropping below the zone after touching it); bull zones reject upward."""
    high = df["high"].values
    low  = df["low"].values
    for z in zones:
        if z["mitigated_bar"] is None:
            z["_reaction"] = None
            continue
        if "_reaction" in z and z["_reaction"] is not None:
            continue  # already classified (zones list is shared/mutated across calls)
        height = max(z["top"] - z["bottom"], 1e-9)
        window_hi = high[z["right"]:z["mitigated_bar"] + 1]
        window_lo = low[z["right"]:z["mitigated_bar"] + 1]
        if len(window_hi) == 0:
            z["_reaction"] = False
            continue
        if is_bear:
            excursion = z["bottom"] - window_lo.min()
        else:
            excursion = window_hi.max() - z["top"]
        z["_reaction"] = bool(excursion >= height * REACTION_ZONE_MULT)


def _zone_reliability(zones, upto_bar):
    """Of all zones of this type already mitigated by `upto_bar`, what fraction
    produced a clean reaction before eventually being mitigated -- vs. price
    plowing straight through. Mirrors Kamran's 'check if it hunted well
    historically, some pools aren't credible' read."""
    resolved = [z for z in zones if z["right"] <= upto_bar and z.get("_reaction") is not None]
    if len(resolved) < RELIABILITY_MIN_SAMPLE:
        return None, len(resolved)
    hit_rate = sum(1 for z in resolved if z["_reaction"]) / len(resolved)
    return hit_rate, len(resolved)


def get_macro_trend(coin, current_tf="1h"):
    """Higher-TF market structure direction (order_blocks' struct: 1=bullish,
    0=bearish break of structure). Replaces the old Sling-Shot-cloud read
    with a structure-based one, consistent with an SMC-style trend concept
    rather than a moving-average one. Returns 1, -1, or 0 on data failure."""
    macro_tf = "4h" if current_tf in ("1h", "15m") else "1d"
    try:
        df4 = fetch_candles(coin, macro_tf, lookback_bars=300)
        if df4 is None or len(df4) < 50:
            return 0
        _, _, _, _, _, _, struct = order_blocks(df4["high"], df4["low"], df4["close"], df4["volume"])
        last = struct.iloc[-1]
        if pd.isna(last):
            return 0
        return 1 if int(last) == 1 else -1
    except Exception:
        return 0


def _active_ob_zones(df, top_col, bot_col, flag_col, upto_bar, is_bull):
    """order_blocks() only flags the bar an OB formed on -- it doesn't track
    mitigation the way liquidity_pools() zones do. Backtesting surfaced this:
    order blocks from months ago that price had long since closed through
    were still being treated as live entry triggers. This rebuilds a
    mitigated-or-not view on top of the raw per-bar flags: a bull OB is
    invalidated once close drops below its bottom at any point after it
    formed; a bear OB once close rises above its top."""
    flags = df[flag_col].values
    tops  = df[top_col].values
    bots  = df[bot_col].values
    closes = df["close"].values
    zones = []
    for b in np.where(flags[:upto_bar + 1])[0]:
        top, bot = tops[b], bots[b]
        if pd.isna(top) or pd.isna(bot):
            continue
        future = closes[b + 1:upto_bar + 1]
        if is_bull:
            mitigated = len(future) > 0 and future.min() < bot
        else:
            mitigated = len(future) > 0 and future.max() > top
        if not mitigated:
            zones.append({"top": top, "bottom": bot, "right": b, "mitigated_bar": None})
    return zones


def _unmitigated_fvg_targets(df, upto_bar, direction):
    """Far edge of each not-yet-filled FVG in the OPPOSING direction to the
    trade -- a bearish gap (left behind by a past decline) sitting above
    price is what a long's rally would revisit and close; a bullish gap
    below is what a short's decline would revisit. fvg() only flags the
    formation bar with no fill-tracking (same gap as order_blocks()), so
    this checks whether any later close has already closed the gap."""
    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    targets = []
    if direction == 1:
        flag = df["fvg_bear"].values   # bear = high < low.shift(2): gap [high[b], low[b-2]]
        for b in np.where(flag[:upto_bar + 1])[0]:
            if b < 2:
                continue
            gap_bottom, gap_top = high[b], low[b - 2]
            future = close[b + 1:upto_bar + 1]
            if len(future) > 0 and future.max() > gap_top:
                continue  # already filled
            targets.append(gap_top)
    else:
        flag = df["fvg_bull"].values   # bull = low > high.shift(2): gap [high[b-2], low[b]]
        for b in np.where(flag[:upto_bar + 1])[0]:
            if b < 2:
                continue
            gap_top, gap_bottom = low[b], high[b - 2]
            future = close[b + 1:upto_bar + 1]
            if len(future) > 0 and future.min() < gap_bottom:
                continue
            targets.append(gap_bottom)
    return targets


def _confirmed_entry_zone(last, zones_list, direction):
    """Zone the current (closed) candle just rejected FROM -- wicked into the
    zone but closed back outside it. This is the conservative-entry pattern:
    wait for the reaction to have already happened, don't anticipate one.

    Bare proximity (price merely sitting near a zone) was tried first and
    backtested at 0% WR with absurd R:R skew (SL invalidated almost
    immediately, entries taken with no confirmation the zone was actually
    holding) -- the same "aggressive entry" failure mode the prior CM Sling
    Shot system had already proven costs ~-10.4%/trade. This replaces it.
    """
    lo, hi, close = float(last["real_low"]), float(last["real_high"]), float(last["real_close"])
    best = None
    for z in zones_list:
        if direction == 1:
            wicked_in  = lo <= z["top"]
            rejected   = close > z["top"]
        else:
            wicked_in  = hi >= z["bottom"]
            rejected   = close < z["bottom"]
        if wicked_in and rejected:
            if best is None or abs(close - (z["top"] if direction == 1 else z["bottom"])) < \
               abs(close - (best["top"] if direction == 1 else best["bottom"])):
                best = z
    return best


def _path_clear(price, target_top, target_bottom, direction, obstacle_zones):
    """True if no other active OB/FVG zone sits strictly between price and
    the target zone's near edge (the 'nothing in the way to get hunted by'
    check)."""
    near_edge = target_bottom if direction == 1 else target_top
    lo, hi = (price, near_edge) if direction == 1 else (near_edge, price)
    if hi <= lo:
        return True
    pad = (hi - lo) * PATH_CLEAR_PAD / 100
    lo, hi = lo + pad, hi - pad
    for z in obstacle_zones:
        if z["top"] > lo and z["bottom"] < hi:
            return False
    return True


def score_setup(df, direction, macro_trend=0):
    """
    Liquidity-pool target-seeking scoring. Max 8 pts. Fire at >= MIN_SCORE.
    Scoring: base(2) + zone reliability(0-2) + OB/FVG confluence(0-2) + timeframe strength(0-2, added by find_best_setup)
    Returns (score, reasons, sl, tp, leverage, sl_pct, tp_pct)
    """
    FAIL = (0, [], None, None, None, None, None)
    last  = df.iloc[-1]
    price = float(last["real_close"])
    i     = len(df) - 1
    reasons = []

    bear_zones = df.attrs.get("bear_zones", [])
    bull_zones = df.attrs.get("bull_zones", [])
    _classify_zone_reactions(df, bear_zones, is_bear=True)
    _classify_zone_reactions(df, bull_zones, is_bear=False)

    active_bear = _active_zones(bear_zones, i)
    active_bull = _active_zones(bull_zones, i)

    # ── HARD GATE: trend alignment (must not be against the higher-TF structure) ──
    if macro_trend != 0 and macro_trend != direction:
        return FAIL

    # Order block zones (both types, mitigation-filtered) -- computed once,
    # reused for entry detection and as path/confluence obstacles below.
    ob_bull = _active_ob_zones(df, "ob_bull_top", "ob_bull_bot", "bull_ob", i, is_bull=True)
    ob_bear = _active_ob_zones(df, "ob_bear_top", "ob_bear_bot", "bear_ob", i, is_bull=False)

    # ── HARD GATE: entry zone -- price must currently be at/near a same-direction
    # support (long) or resistance (short) zone -- OB or LP zone both count ──
    entry_candidates = (active_bull + ob_bull) if direction == 1 else (active_bear + ob_bear)
    entry_zone = _confirmed_entry_zone(last, entry_candidates, direction)
    if entry_zone is None:
        return FAIL

    # ── HARD GATE: target liquidity pool -- opposite zone type, farther out,
    # in the trade direction (longs target resting sell-liquidity above =
    # bear zones; shorts target resting buy-liquidity below = bull zones) ──
    target_pool = active_bear if direction == 1 else active_bull
    candidates  = [z for z in target_pool if (z["bottom"] > price if direction == 1 else z["top"] < price)]
    if not candidates:
        return FAIL
    target = min(candidates, key=lambda z: abs((z["bottom"] if direction == 1 else z["top"]) - price))

    # ── HARD GATE: path must be clean of other zones between entry and target.
    # All zone types count as potential obstacles/confluence -- LP zones, and
    # order blocks (both directions, unmitigated) -- matching Kamran's
    # description ("order block ... or FVG ... anything against it"). ──
    obstacles = [z for z in (active_bear + active_bull + ob_bull + ob_bear)
                 if z is not target and z is not entry_zone]
    if not _path_clear(price, target["top"], target["bottom"], direction, obstacles):
        return FAIL

    # ── SL: just beyond the entry zone (tight, precise) ──
    if direction == 1:
        sl = entry_zone["bottom"] * 0.995
        if sl >= price:
            sl = price * 0.98
    else:
        sl = entry_zone["top"] * 1.005
        if sl <= price:
            sl = price * 1.02

    sl_pct = abs(price - sl) / price * 100
    if sl_pct < 0.4:
        return FAIL

    # ── TP: at minimum the target zone's near edge; extend to a further
    # unfilled FVG's far edge if one exists beyond it. The relevant gap is the
    # OPPOSING type: a long's final target is an unfilled BEARISH gap (left
    # behind by a past decline) sitting above price that the market tends to
    # revisit and close -- "the end of the gap" Kamran described -- not a
    # bullish gap, which would be behind/below a long, not ahead of it. ──
    tp = target["bottom"] if direction == 1 else target["top"]
    for far_edge in _unmitigated_fvg_targets(df, i, direction)[-5:]:
        if direction == 1 and far_edge > tp:
            tp = far_edge
        elif direction == -1 and far_edge < tp:
            tp = far_edge

    tp_pct = abs(tp - price) / price * 100
    if tp_pct < sl_pct * TP_RATIO:
        # Target pool alone doesn't clear the minimum R:R -- reject rather than
        # silently taking a worse trade than Kamran's stated floor.
        return FAIL

    leverage = min(round(MAX_LEV_LOSS / sl_pct, 1), MAX_LEVERAGE)
    if sl_pct * leverage > MAX_LEV_LOSS:
        return FAIL

    # ── SCORING (max 6 here; find_best_setup adds up to +2 for timeframe strength) ──
    score = 2
    reasons.append(f"{'Bullish' if direction==1 else 'Bearish'} entry zone + LP target")

    hit_rate, sample = _zone_reliability(target_pool, i)
    if hit_rate is None:
        score += 1
        reasons.append("Target zone: no track record yet")
    elif hit_rate >= RELIABILITY_GOOD_RATE:
        score += 2
        reasons.append(f"Target zone reliable ({hit_rate*100:.0f}% clean reactions, n={sample})")
    else:
        reasons.append(f"Target zone weak track record ({hit_rate*100:.0f}%, n={sample})")

    zone_h = target["top"] - target["bottom"]
    confluence = [z for z in obstacles
                  if z["top"] >= target["bottom"] - zone_h * 0.5
                  and z["bottom"] <= target["top"] + zone_h * 0.5]
    if confluence:
        score += 2
        reasons.append(f"OB/FVG confluence near target ({len(confluence)})")

    return max(score, 0), reasons, sl, tp, leverage, round(sl_pct, 3), round(tp_pct, 3)


def find_best_setup(open_positions, hour_utc=None):
    """
    Scan all coins on 4h / 1h / 15m. Return highest-scoring valid setup, or None.
    Higher timeframes score a bonus (stronger zones, per Kamran's stated
    preference) -- naturally lets a 4h-sourced setup outrank an equivalent
    1h/15m one when both qualify.
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
                df = build_df(coin, tf, 600)
                if df is None:
                    continue

                macro = get_macro_trend(coin, tf)

                for direction in [1, -1]:
                    result = score_setup(df, direction, macro)
                    if result[0] == 0:
                        continue
                    score, reasons, sl, tp, leverage, sl_pct, tp_pct = result
                    bonus = TF_BONUS.get(tf, 0)
                    score = min(score + bonus, 8)
                    if bonus:
                        reasons = reasons + [f"{tf} timeframe (+{bonus})"]
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
        df   = build_df(coin, tf, 600)
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
            "ssl":   int(last["ssl"]) if not pd.isna(last["ssl"]) else 0,
            "ssl_str": int(last["ssl_str"]) if not pd.isna(last["ssl_str"]) else 0,
            "macro": macro,
            "long_score":  l_sc,
            "short_score": s_sc,
            "best_score":  max(l_sc, s_sc),
            "best_dir":    1 if l_sc >= s_sc and l_sc > 0 else (-1 if s_sc > 0 else 0),
        }
    except Exception:
        return None
