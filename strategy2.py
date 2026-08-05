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
TP_R           = 5.0     # RAISED 3.0 -> 5.0 on 2026-08-05, as a direct
                         # consequence of TRAIL_START_R 1.00 -> 2.50. TP_R was
                         # picked when the ratchet armed at 0.75R, so the
                         # backstop sat far beyond it. Arming at 2.5R left it
                         # only 0.5R away, which is exactly the truncation
                         # problem the block below was written to avoid --
                         # re-measured at the new arming level, worst case
                         # (every TP touch fills before the ratchet cancels it):
                         #
                         #   TP_R   n    net      t     maxDD
                         #   3.0   128  +31.32  1.53   -10.29
                         #   4.0   126  +38.80  1.82   -10.29
                         #   5.0   125  +41.87  1.93   -10.29
                         #   none  125  +41.87  1.93   -10.29
                         #
                         # 3.0 was costing ~10.5pp for no risk benefit: max
                         # drawdown is IDENTICAL at every value, because the
                         # backstop only ever truncates winners.
                         #
                         # 5.0 is not a boundary pick. The curve PLATEAUS there
                         # -- it is identical to "no TP", meaning no trade in
                         # the sample reaches beyond it -- so it captures the
                         # full benefit while still leaving a resting order to
                         # protect a position if the bot dies mid-trade. That
                         # is the same reasoning the block below used to reject
                         # "none", now satisfied at an interior value instead of
                         # a compromise one.
                         #
                         # ---- everything below was measured at TRAIL_START_R
                         # ---- 0.75 and is kept for the reasoning, not the
                         # ---- numbers. Do not quote its table.
                         #
                         # Re-derived on real prices 2026-08-03 (sweep_tp.py),
                         # replacing the VOID Heikin-Ashi comparison that chose
                         # 1.0 (1.0R "beating" 1.5R on WR 63.6% vs 48.7%, net
                         # +64% vs +43%). Do not quote those figures.
                         #
                         # This constant no longer picks an exit. Since
                         # 2026-08-02 the ratchet arms at TRAIL_START_R=0.75,
                         # BELOW the take-profit, and cancels it -- so the only
                         # thing TP_R still does is decide where a resting
                         # backstop order sits for the case where the bot dies
                         # mid-trade. At 1.0 that backstop sat 0.25R above the
                         # arming threshold, which made it a truncation device:
                         # a move that travels 0.75R -> 1.0R inside a single
                         # ~20s poll window fills the TP before the ratchet can
                         # cancel it, capping the trade at exactly 1R. That
                         # lands on precisely the fast, strongly trending
                         # trades the ratchet exists to harvest.
                         #
                         # Worst case (every touch of the TP level fills before
                         # the ratchet arms), 20 coins, 5000 bars, capped book,
                         # fees in:
                         #
                         #   TP_R   n    WR      net      maxDD    EV       t
                         #   1.0   121  57.9%  +15.59%   -9.26%  +0.129%  1.32
                         #   1.5   120  56.7%  +25.66%  -10.76%  +0.214%  1.94
                         #   2.0   120  56.7%  +32.20%  -10.26%  +0.268%  2.31
                         #   3.0   120  56.7%  +39.03%   -9.26%  +0.325%  2.61
                         #   5.0   120  56.7%  +45.68%   -9.02%  +0.381%  2.79
                         #   none  118  56.8%  +48.83%   -9.02%  +0.414%  2.82
                         #
                         # 46 of 121 trades (38%) touched the 1.0R level while
                         # unarmed, giving up 36.75R in aggregate in the worst
                         # case. Raising the backstop cannot turn a winner into
                         # a loser: any trade that reaches 1.0R has already
                         # armed the ratchet at 0.75R, so its resting stop is
                         # locked at >= +0.75R. Win rate barely moves (57.9 ->
                         # 56.7) and max drawdown does not worsen.
                         #
                         # The relationship is monotonic toward "no TP", so the
                         # optimum sits at the boundary. 3.0 is deliberately an
                         # interior value, on the same reasoning that kept
                         # TRAIL_STEP_R at 0.25: a backstop retains some value
                         # against a dead bot, and picking the boundary of a
                         # monotone curve is not a measured edge. Neighbours are
                         # smooth (2.0 -> +32.2%, 3.5 -> +42.6%, 4.0 -> +45.7%).
                         #
                         # Survives the standing rejection tests against 1.0:
                         # concurrency cap 1 (+27.5 vs +12.3), cap 2 (+39.0 vs
                         # +15.6), cap 4 (+45.1 vs +25.4); alphabetical instead
                         # of |stretch| ranking (+34.0 vs +12.8); and the
                         # out-of-sample split, where 3.0 is positive in BOTH
                         # halves (in-sample +9.94% t=0.94, OOS +29.10% t=2.76)
                         # while 1.0's in-sample half is NEGATIVE (-2.87%).
                         # Independent of last night's change: the same effect
                         # holds at the old TRAIL_START_R=1.0 (+2.41% -> +37.46%).

MAX_LEV_LOSS   = 20.0    # same risk envelope as strategy 1
MAX_LEVERAGE   = 25.0
MIN_SL_PCT     = 0.4     # reject stops so tight they are inside the spread


def build_df(coin, tf="1h", bars=1500):
    df = fetch_candles(coin, tf, lookback_bars=bars)
    if df is None or len(df) < EMA_LEN + 50:
        return None
    df = df.copy()

    # Drop non-trading bars BEFORE the indicators are computed, not after.
    #
    # A bar with no volume is not a quiet market, it is a gap in the feed, and
    # it poisons this strategy's trigger specifically: ATR collapses toward zero
    # across a run of them, and stretch = (close - ema) / atr then clears
    # MIN_STRETCH_ATR on noise. When the feed resumes with a gap, that reads as
    # the "reversion" the strategy exists to trade.
    #
    # This mattered enormously because every backtest ran on testnet candles
    # until 2026-08-05 (see indicators.BASE_URL): 17.1% of testnet bars have
    # zero volume against 0.0% on mainnet, and 12 of the 20 watchlist coins were
    # above 10% frozen. Filtering after computing indicators would not help --
    # the contamination is inside the indicator values.
    if "volume" in df.columns:
        df = df[df["volume"] > 0]
        if len(df) < EMA_LEN + 50:
            return None

    df["rsi"] = rsi(df["close"], RSI_LEN)
    adx_v, _, _ = adx(df["high"], df["low"], df["close"], ADX_LEN)
    df["adx"] = adx_v
    df["atr"] = atr(df["high"], df["low"], df["close"], ATR_LEN)
    df["ema"] = ema_line(df["close"], EMA_LEN)
    df["stretch"] = (df["close"] - df["ema"]) / df["atr"]
    df = df.dropna(subset=["rsi", "adx", "atr", "ema", "stretch"])

    # Live freshness guard, ported from trader.py:117. A feed that has printed
    # the same close three bars running is stale; acting on it would size a
    # trade off an ATR that no longer describes the market.
    price_col = "real_close" if "real_close" in df.columns else "close"
    if len(df) >= 3 and df[price_col].iloc[-3:].nunique() == 1:
        return None
    return df


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

# S2 sizes off its OWN risk constant, not trader.RISK_PCT.
#
# live.py used to size S2 entries from trader.RISK_PCT, which review.py:112
# machine-rewrites every night from strategy_config.json. review.py:213-215
# raises it toward 4% whenever `drawdown < 5 and wr > 65 and decided >= 10` --
# and it computes that win rate from the shared journal, which has contained S2
# trades since S2 was promoted out of shadow mode. So S2's position size could
# quadruple off a rule written for the retired S1 engine, with no code change
# and nothing in the diff to notice.
#
# Defined here because _patch_trader's regex set only touches trader.py, which
# puts this out of the nightly tuner's automatic reach. That is deliberate:
# risk per trade is the one parameter that scales drawdown one-for-one, and it
# should move only when a human decides it should.
S2_RISK_PCT = 0.01

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
# REVERTED TO 1.00 on 2026-08-04. The table above is not wrong, it is measured
# against a model live cannot reproduce, and every row of it inherits that flaw.
#
# portfolio2.simulate drives the ratchet off the bar CLOSE and, having raised the
# stop at the end of a bar, does not test that stop until the NEXT bar. Its
# docstring called close-driving "the conservative proxy for a level price
# actually held long enough for a 20s poll to act on". That is conservative about
# WHEN the stop arms and silently optimistic about whether it SURVIVES -- it
# grants each freshly-raised stop a full bar of immunity that no real stop has.
#
# live._check_trail_s2 polls the mid every ~20s and places the stop at exactly
# TRAIL_START_R the instant price prints it -- i.e. AT the market, with no
# cushion at all. The first live trade to arm settled it: BTC 2026-08-03 armed at
# +0.75R at 13:49:56 and was filled 22 seconds later at +0.706R.
#
# sweep_trail_mode.py re-measures both achievable bounds (it asserts it
# reproduces portfolio2 exactly at mode="close", so the rows are comparable).
# 20 coins, 5000 bars, real prices, fees in, cap 2:
#
#   model        n    WR      net      EV/trade   t     ex-top5  >=1.5R  >=3R
#   close       121  57.0%  +42.49%   +0.351%   2.62   +18.56%    19      8
#   touch_opt   122  58.2%  +34.65%   +0.284%   2.45   +15.02%    15      7
#   touch_pess  122  59.0%   +8.74%   +0.072%   0.83    -2.81%     7      2
#
# The close row sits ABOVE the optimistic bound, i.e. outside the range live can
# occupy at all. Note the signature: win rate goes UP as EV collapses. Placing
# the stop on top of the price rescues near-misses and truncates runners, and
# this strategy's entire EV is in the runners.
#
# Under both achievable models the 0.75-vs-1.00 ordering INVERTS, monotonically
# (net acct %):
#
#   TRAIL_START_R   close    touch_opt   touch_pess
#   0.50           +30.07     +33.51      +12.26
#   0.75           +42.49     +34.65       +8.74     <- local MINIMUM under pess
#   1.00           +40.92     +40.45      +14.54
#   1.25           +40.20     +49.26      +23.98
#   1.50           +40.70     +52.61      +25.17
#
# Mechanically obvious once seen: if arming places the stop at market, arming
# early truncates early. 1.00 and not 1.25/1.50 because the curve is monotone to
# the edge of the swept range, and picking the boundary of a monotone
# relationship is a mechanical fact, not a measured edge -- the same reasoning
# that kept TRAIL_STEP_R at 0.25 and TP_R off "none". 1.00 is also not a newly
# fitted value: it is what ran until 2026-08-02, so this reverts a change made on
# bad evidence rather than fitting a fresh one.
#
# Rejection tests vs 0.75, run under BOTH achievable models: wins at cap 2
# (+39.71/+14.54 vs +33.91/+8.74), cap 4 (+40.55/+12.74 vs +35.00/+7.93) and
# under alphabetical selection (+38.67/+13.50 vs +33.12/+7.70); positive in BOTH
# out-of-sample halves under both models, where 0.75 is NEGATIVE in-sample under
# touch_pess (-2.47%); and it is the only setting whose ex-top5 is non-negative
# under the pessimistic bound (+0.54% vs -2.81%), which speaks directly to this
# strategy's known tail-dependence. Recorded honestly: at concurrency cap 1 the
# two are a TIE (+22.80/+5.16 vs +22.78/+5.71) -- 0.75 is marginally ahead on the
# pessimistic bound there. Cap 1 is not the deployed setting (MAX_TRADES=2).
#
# The cost is real and is the exact thing 2026-08-02 bought: the 15.3% of trades
# that peak between 0.5R and 1.0R go back to being full stops, and win rate drops
# ~5pp (touch_pess 59.0% -> 53.7%). EV rises anyway (+0.072% -> +0.120%). Lower
# win rate, higher expected value, taken deliberately.
#
# TRAIL_STEP_R deliberately NOT changed. Smaller is monotonically better (0.1
# -> +34.4%, 0.25 -> +32.4%, 0.5 -> +28.3%, 1.0 -> +25.4%) with no plateau, so
# the optimum sits at the boundary -- that is a smooth mechanical relationship,
# not a measured edge, and chasing it means more stop-modification calls on the
# exact path whose failure mode had to be fixed on 2026-07-30. Re-checked at
# TRAIL_START_R=1.00 under both new models: 0.25 still beats 0.50.
#
# A per-rung "gap" that forbids the stop from resting within X R of price was
# built and MEASURED as the direct fix for at-market placement, then rejected:
# it is non-monotonic noise (touch_pess at tsr=0.75 goes +8.74 -> +4.56 -> +3.11
# -> +1.03 -> +13.09 across gap 0 -> 0.5). See sweep_trail_mode.py.
# ── 2026-08-05: 1.00 -> 2.50 ────────────────────────────────────────────────
# EVERY figure in the block above is VOID. All of it was measured on testnet
# candles, and indicators.py hardcoded that feed until today: 18.5% of testnet
# bars repeat the previous close and 17.1% have zero volume, against 0.4% and
# 0.0% on mainnet. Do not quote any of it.
#
# Re-derived on mainnet candles, real fee 0.00045, slippage priced, capped book,
# 20 coins, ~7 months, pessimistic fill bound:
#
#   arm at   n     WR%     net       t     maxDD    ex-top5
#   0.50    161   69.6   -4.44%   -0.52   -11.11    -6.80
#   0.75    154   61.7   +1.15%    0.11   -11.39    -2.43
#   1.00    153   54.2   +1.75%    0.14   -17.59    -3.07   <- was deployed
#   2.00    140   41.8  +26.46%    1.54   -11.23   +15.53
#   2.50    132   38.3  +35.76%    1.86   -10.29   +22.35   <- now
#   3.00    127   30.5  +18.60%    0.91   -16.09    +2.70
#   4.00    118   18.6  -16.62%   -0.80   -29.93   -36.36
#   never    45    4.4  -11.02%   -0.44   -37.61   -42.25
#
# Two things make this an interior optimum rather than the boundary artefact
# this project has twice rejected: the curve turns (2.5 beats both 2.0 and 3.0),
# and "never arm" is firmly negative, so the ratchet itself earns its keep -- it
# was simply arming far too early and truncating the winners the strategy exists
# to harvest. Win rate FALLS 54.2% -> 38.3% while net rises 20x; fewer winners,
# much larger ones.
#
# Neighbourhood is smooth, so the region matters and the exact value does not:
# 2.00 +26.46 / 2.25 +26.82 / 2.50 +35.76 / 2.75 +31.02 / 3.00 +18.60.
#
# Passes: n>=100, ex-top5 +22.35%, both OOS halves positive (+0.85 / +33.79),
# 7/8 months positive (vs 5/8 at 1.00), concurrency caps 2/3/4, alphabetical
# instead of |stretch| ranking (+31.14%), and every TRAIL_STEP_R value.
#
# FAILS TWO STANDING TESTS, recorded rather than glossed:
#   - pessimistic t = 1.86, below this project's t >= 2.0 bar. The bar was NOT
#     lowered to accommodate it.
#   - concurrency cap 1 is -1.58%, so the result depends on running 2 positions.
#     (The old 1.00 setting also fails cap 1, at -5.66%.)
#
# Deployed anyway, and the reasoning matters: 1.00 is not a validated incumbent
# being displaced by an unvalidated challenger. Both are unvalidated. 1.00 was
# chosen on data now known to be fabricated AND sits near the worst point of the
# measured range, so keeping it is not the conservative option -- it is just the
# status quo. This replaces an unsupported value with a better-supported one. It
# is NOT a demonstrated edge, and it must be re-judged on live results.
TRAIL_START_R = 2.50

# Unchanged. Measured invariant at the new arming level: 0.25 / 0.5 / 1.0 all
# return exactly +35.76%, because most trades now exit at the arming rung and
# the second rung rarely fires at all.
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
