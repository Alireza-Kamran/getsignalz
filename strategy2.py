"""
Strategy 2 — mean-reversion at extremes (indicator-based, high frequency).

Companion to trader.py's structural liquidity-pool system, NOT a replacement.
That one waits for a rare, clean market structure and consequently fires ~1-2
times a month; every attempt to speed it up (lower timeframes, 150 coins,
dropping each gate in turn, six different exit rules) was measured on 2026-07-26
and every one traded its edge away for frequency. This module attacks the
frequency problem from the opposite side: an entry condition that is common by
construction, with the discipline applied to filtering and exits instead.

HYPOTHESIS (written before testing, deliberately):
    Crypto overshoots on liquidation cascades. When price is stretched far from
    its mean AND momentum is exhausted (RSI at an extreme), the snap back is
    more likely than continuation -- but ONLY when no strong trend is driving
    it, since mean reversion is exactly what fails in a trend.

    Therefore: fade RSI extremes when ADX is low and price is far from EMA,
    target a partial reversion, stop beyond the extreme.

Deliberate design constraints, to avoid the overfitting that has already broken
this project's nightly tuner three times (see the disabled blocks in review.py):
  - One hypothesis, stated up front. No combinatorial search over indicators.
  - Parameters are round numbers, not tuned to the third decimal.
  - Must survive an out-of-sample split (fit on the older half, verify on the
    newer half) -- a rule that only works in-sample is rejected outright.
  - Fees modelled from the first run, since at 20+ trades/month they are the
    difference between profit and loss (measured: a 20-trade/month variant of
    strategy 1 paid 10% of account in fees over 20 months).
  - Must be robust to neighbouring parameter values. A rule that works at
    RSI<25 but breaks at RSI<27 is noise, not edge.
"""
import numpy as np
import pandas as pd

from indicators import fetch_candles, rsi, adx, atr, ema_line

# ── Entry ─────────────────────────────────────────────────────────────────────
RSI_LEN        = 14
RSI_OVERSOLD   = 25      # long when RSI dips below this
RSI_OVERBOUGHT = 75      # short when RSI pops above this

# ── Regime filter ─────────────────────────────────────────────────────────────
# Mean reversion is precisely the thing that fails in a strong trend, so a high
# ADX reading disqualifies the setup rather than merely down-weighting it.
ADX_LEN        = 14
MAX_ADX        = 25      # WARNING: the figures that used to justify this value
                         # (18.9/mo vs 47.2, 8/8 positive months, maxDD -16.4%
                         # -> -4.2%) were Heikin-Ashi-path and are VOID.
                         # Re-derived on real prices 2026-07-31: the gate buys
                         # NO expected value. Removing it entirely measures
                         # better on every axis (n=697 EV +0.478% t=6.31 maxDD
                         # -10.59%, vs n=116 EV +0.315% t=2.13 maxDD -12.49%
                         # here), and it discards 88.5% of RSI-extreme bars.
                         # Kept at 25 anyway, deliberately: the no-gate edge
                         # decays hard across the sample (Jan +72% -> Jul
                         # +6.8%), and going to 3.4 trades/day on an engine
                         # with a handful of live trades is a risk-profile
                         # decision for Kamran, not a nightly auto-tune.
                         # Staged move if he wants frequency is 25 -> 30.

# Price must be genuinely stretched, not just drifting: at least this many ATRs
# away from the EMA. Without it, RSI extremes in a quiet range produce constant
# low-quality signals.
EMA_LEN        = 100
MIN_STRETCH_ATR = 1.5

# ── Exit ──────────────────────────────────────────────────────────────────────
ATR_LEN        = 14
SL_ATR_MULT    = 1.5     # stop beyond the extreme
TP_R           = 1.0     # VOID NUMBERS WARNING: the comparison that chose this
                         # (1.0R beating 1.5R on WR 63.6% vs 48.7%, net +64% vs
                         # +43%, drawdown -16.4% vs -24.6%) was measured on the
                         # Heikin-Ashi path and has NOT been re-derived on real
                         # prices. Treat the value as unvalidated, not as
                         # settled. Note this constant now only sets where the
                         # resting take-profit is placed: since 2026-08-02 the
                         # ratchet arms below it (TRAIL_START_R 0.75), so the TP
                         # is cancelled before it can be reached on any trade
                         # that gets that far. Re-deriving it is a live open
                         # question -- see the memory file.

MAX_LEV_LOSS   = 20.0    # same risk envelope as strategy 1
MAX_LEVERAGE   = 25.0
MIN_SL_PCT     = 0.4     # reject stops so tight they are inside the spread


def build_df(coin, tf="1h", bars=1500):
    df = fetch_candles(coin, tf, lookback_bars=bars)
    if df is None or len(df) < EMA_LEN + 50:
        return None
    df = df.copy()
    df["rsi"] = rsi(df["close"], RSI_LEN)
    adx_v, _, _ = adx(df["high"], df["low"], df["close"], ADX_LEN)
    df["adx"] = adx_v
    df["atr"] = atr(df["high"], df["low"], df["close"], ATR_LEN)
    df["ema"] = ema_line(df["close"], EMA_LEN)
    df["stretch"] = (df["close"] - df["ema"]) / df["atr"]
    return df.dropna(subset=["rsi", "adx", "atr", "ema", "stretch"])


def signal(df, i=-1):
    """Evaluate the bar at position `i`. Returns a dict or None.

    Reads only the given bar's already-closed values -- no forward reference,
    so it is identical whether called live on the latest closed candle or
    walked over history by the backtester.
    """
    row = df.iloc[i]
    price = float(row["real_close"]) if "real_close" in df.columns else float(row["close"])
    r, a, stretch, atr_v = row["rsi"], row["adx"], row["stretch"], row["atr"]

    if a >= MAX_ADX:
        return None                      # trending: mean reversion is the wrong tool

    if r <= RSI_OVERSOLD and stretch <= -MIN_STRETCH_ATR:
        direction = 1
    elif r >= RSI_OVERBOUGHT and stretch >= MIN_STRETCH_ATR:
        direction = -1
    else:
        return None

    sl = price - direction * SL_ATR_MULT * atr_v
    sl_pct = abs(price - sl) / price * 100
    if sl_pct < MIN_SL_PCT:
        return None

    tp = price + direction * TP_R * abs(price - sl)

    leverage = min(round(MAX_LEV_LOSS / sl_pct, 1), MAX_LEVERAGE)
    if sl_pct * leverage > MAX_LEV_LOSS:
        return None

    return {
        "direction": direction, "entry": price, "sl": sl, "tp": tp,
        "leverage": leverage, "sl_pct": round(sl_pct, 3),
        "rsi": round(float(r), 1), "adx": round(float(a), 1),
        "stretch": round(float(stretch), 2),
    }


# The 20 coins the strategy was validated on. Deliberately the liquid majors
# rather than a backtest-ranked selection -- picking coins by their own backtest
# result is how you manufacture an edge that does not survive contact with live
# markets.
WATCHLIST = ["BTC", "ETH", "SOL", "AVAX", "SUI", "DOGE", "BNB", "AAVE", "ARB",
             "ADA", "WLD", "TIA", "INJ", "NEAR", "APT", "OP", "ATOM", "XLM",
             "FIL", "LDO"]

TF = "1h"

# Backtested concurrency never exceeded 2 (1 position 50% of the time, 2 only
# 6.6%, none 43%), so a budget of 2 reproduces the tested behaviour rather than
# throttling it. Risk ceiling stays modest: 2 x RISK_PCT of the account.
MAX_TRADES = 2

# ── Exit management ───────────────────────────────────────────────────────────
# On reaching TRAIL_START_R the fixed take-profit is cancelled and replaced by a
# stop locked at that same level, which then ratchets up every TRAIL_STEP_R.
# Because the stop can only ever sit at or above TRAIL_START_R once armed, the
# trade's floor is unchanged -- this only adds upside, never removes profit.
#
# The numbers that used to justify 1.0 here (+628.5% / +1358.6% / maxDD -4.23%
# / 8-of-8 positive months) were measured on the Heikin Ashi price path and are
# VOID -- see the 2026-07-30 bug. Do not quote them. Re-derived 2026-08-02 in
# portfolio2.py (capped book, real prices, fees in), 20 coins, 5000 bars:
#
#   TRAIL_START_R   n    WR      net      maxDD    EV/trade   t
#   0.50           120  60.8%  +21.54%   -9.23%   +0.179%   1.65
#   0.65           120  57.5%  +34.49%   -6.10%   +0.287%   2.30
#   0.75 (chosen)  118  55.9%  +33.73%   -9.02%   +0.286%   2.28
#   0.85           117  53.0%  +34.86%   -8.93%   +0.298%   2.24
#   1.00 (was)     117  49.6%  +32.41%  -12.49%   +0.277%   2.04
#   1.25           116  44.0%  +30.75%  -16.45%   +0.265%   1.81
#
# Arming below the 1R take-profit rescues the trades that run most of the way to
# target and then reverse into a full stop: 18 of 118 (15.3%) peak between 0.5R
# and 1.0R, and under a 1.0 trigger every one of them is a maximum loss. Buying
# those back costs a little off the winners (a trade peaking at 1.0R now locks
# 0.75R, not 1.00R), which is why net return barely moves. The gain is in the
# risk profile, not the return: win rate +6.3pp and max drawdown ~28% smaller.
#
# 0.75 over the slightly better-scoring 0.70 because the whole 0.65-0.85 band is
# flat (net +33.7 to +36.7, t 2.24-2.43) and picking the peak inside a flat band
# is fitting noise; 0.75 is a round number with strong neighbours on both sides.
# Survives the standing rejection tests: better than 1.0 at concurrency cap 1, 2
# and 4, and under alphabetical instead of |stretch| selection. Caveat kept in
# plain sight -- it is 4-of-7 positive months against 1.0's 5-of-7 (February
# moves +0.9% -> -0.1%, i.e. zero either way) and still tail-dependent.
#
# TRAIL_STEP_R deliberately NOT changed. Smaller is monotonically better (0.1
# -> +34.4%, 0.25 -> +32.4%, 0.5 -> +28.3%, 1.0 -> +25.4%) with no plateau, so
# the optimum sits at the boundary -- that is a smooth mechanical relationship,
# not a measured edge, and chasing it means more stop-modification calls on the
# exact path whose failure mode had to be fixed on 2026-07-30.
TRAIL_START_R = 0.75
TRAIL_STEP_R  = 0.25


def find_setup(open_positions):
    """Scan the watchlist and return the most stretched qualifying setup.

    Ranked by |stretch| so that when several coins qualify on the same candle,
    the most extreme dislocation wins -- the one the mean-reversion premise
    applies to most strongly.
    """
    best = None
    for coin in WATCHLIST:
        if coin in open_positions:
            continue
        try:
            df = build_df(coin, TF, bars=600)
            if df is None or len(df) < 50:
                continue
            sig = signal(df, -1)
            if not sig:
                continue
            if best is None or abs(sig["stretch"]) > abs(best["stretch"]):
                best = {**sig, "coin": coin, "tf": TF}
        except Exception:
            continue
    return best
