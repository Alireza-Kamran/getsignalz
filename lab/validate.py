"""
Validation protocol: the part of the lab that is allowed to say "no".

THE SPLIT IS FIXED AND WRITTEN DOWN BEFORE ANY RESULT EXISTS

    train     2021-01-01 .. 2024-12-31   fit, explore, compare variants
    validate  2025-01-01 .. 2025-12-31   judge a FROZEN candidate; a failure
                                         here rejects it, it does not send it
                                         back for another round of tuning
    holdout   2026-01-01 .. today        opened ONCE, at the very end, for the
                                         final portfolio only

    2020 is warm-up for indicators and is in no period.

    The holdout is only a test for as long as nothing has been chosen by
    looking at it. Every read of it is appended to lab/holdout_log.txt with
    the caller's stated reason, so "we only looked once" is a fact that can
    be checked rather than a promise.

WHAT A RESULT HAS TO SURVIVE
    summarize()  n, win rate, mean R, t-stat, profit factor, max drawdown,
                 share of positive months, and the result with the best 5% of
                 trades removed -- this project's edge has twice turned out to
                 be five trades.
    The same numbers under cost_mult=2 (engine), on each year separately, on
    each side separately, and on neighbouring parameter values.

    deflated_t() discounts a t-stat for how many variants were tried to find
    it. Trying 50 things and reporting the best is not a t of 3.
"""
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

PERIODS = {
    "train": ("2021-01-01", "2025-01-01"),
    "validate": ("2025-01-01", "2026-01-01"),
    "holdout": ("2026-01-01", "2100-01-01"),
    "dev": ("2021-01-01", "2026-01-01"),        # train + validate
}

HOLDOUT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "holdout_log.txt")


def split(trades, period, reason=None):
    """Trades whose ENTRY falls in the named period."""
    if period == "all":
        raise ValueError("there is no 'all': it would include the holdout. "
                         "Use 'dev' for train+validate.")
    if period == "holdout":
        if not reason:
            raise ValueError("reading the holdout needs a stated reason")
        with open(HOLDOUT_LOG, "a") as f:
            f.write("%s  n=%d  %s\n" % (
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                len(trades), reason))
    if not len(trades):
        return trades
    a, b = PERIODS[period]
    m = (trades["entry_ts"] >= pd.Timestamp(a)) & (trades["entry_ts"] < pd.Timestamp(b))
    return trades[m]


def clip_signals(signals, period):
    """Signals inside a period. Use this BEFORE simulating research runs, so
    holdout trades are never even computed, let alone glanced at."""
    if period == "holdout":
        raise ValueError("simulate the holdout only through split(..., reason=)")
    a, b = PERIODS[period]
    return signals[(signals["ts"] >= pd.Timestamp(a)) & (signals["ts"] < pd.Timestamp(b))]


def _t(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(len(x))) if sd > 0 else 0.0


def max_dd(r_by_time):
    """Deepest peak-to-trough of the cumulative R curve (a negative number)."""
    eq = np.cumsum(np.asarray(r_by_time, dtype=float))
    if not len(eq):
        return 0.0
    peak = np.maximum.accumulate(np.maximum(eq, 0.0))
    return float((eq - peak).min())


def monthly(trades):
    """Sum of R per calendar month of EXIT, including months with no trade
    (a flat month is a real month and counts against 'share positive')."""
    if not len(trades):
        return pd.Series(dtype=float)
    s = trades.set_index("exit_ts")["r"].resample("MS").sum()
    return s


def summarize(trades):
    if not len(trades):
        return {"n": 0}
    t = trades.sort_values("exit_ts")
    r = t["r"].values
    wins, losses = r[r > 0], r[r <= 0]
    m = monthly(t)
    k = max(1, int(round(0.05 * len(r))))
    ex_top = np.sort(r)[:-k]
    span_y = max((t["exit_ts"].max() - t["entry_ts"].min()).days / 365.25, 1e-9)
    return {
        "n": len(r),
        "wr": 100 * len(wins) / len(r),
        "mean_r": float(r.mean()),
        "sum_r": float(r.sum()),
        "t": _t(r),
        "pf": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "max_dd": max_dd(r),
        "r_per_year": float(r.sum() / span_y),
        "months": len(m),
        "pos_months": 100 * float((m > 0).mean()) if len(m) else 0.0,
        "worst_month": float(m.min()) if len(m) else 0.0,
        "ex_top5_sum": float(ex_top.sum()),
        "ex_top5_mean": float(ex_top.mean()) if len(ex_top) else 0.0,
        "month_sharpe": float(m.mean() / m.std(ddof=1) * math.sqrt(12))
                        if len(m) > 2 and m.std(ddof=1) > 0 else 0.0,
        "cost_r": float(t["cost_r"].mean()) if "cost_r" in t else 0.0,
        "bars": float(t["bars"].median()) if "bars" in t else 0.0,
    }


HEAD = ("%-26s %5s %5s %7s %7s %6s %5s %7s %6s %5s %7s %7s" % (
    "label", "n", "WR%", "meanR", "sumR", "t", "PF", "maxDD", "R/yr",
    "mo+%", "worstMo", "exTop5"))


def line(label, s):
    if not s.get("n"):
        return "%-26s %5d   (no trades)" % (label[:26], 0)
    return "%-26s %5d %5.1f %+7.3f %+7.1f %6.2f %5.2f %7.1f %+6.1f %5.0f %+7.1f %+7.1f" % (
        label[:26], s["n"], s["wr"], s["mean_r"], s["sum_r"], s["t"],
        min(s["pf"], 99), s["max_dd"], s["r_per_year"], s["pos_months"],
        s["worst_month"], s["ex_top5_sum"])


def show(label, trades):
    print(line(label, summarize(trades)))


def by_year(trades, label=""):
    if not len(trades):
        return
    for y, g in trades.groupby(trades["entry_ts"].dt.year):
        show("%s %d" % (label, y), g)


def by_side(trades, label=""):
    if not len(trades):
        return
    show(label + " LONG", trades[trades["dir"] == 1])
    show(label + " SHORT", trades[trades["dir"] == -1])


def by_coin(trades):
    if not len(trades):
        return
    g = trades.groupby("coin")["r"].agg(["count", "sum", "mean"])
    return g.sort_values("sum", ascending=False)


def deflated_t(t_obs, n_trials):
    """The t-stat left after accounting for having tried n_trials variants.

    Under the null, the best of N independent t-stats is expected to be about
    sqrt(2 ln N) (the Gumbel leading term). Subtracting it is a deliberately
    simple stand-in for the deflated Sharpe ratio: it answers "how far above
    what luck alone would have produced is this?" and it is conservative when
    the variants are correlated, which neighbouring parameters always are.
    """
    if n_trials <= 1:
        return t_obs
    return t_obs - math.sqrt(2 * math.log(n_trials))


def bootstrap_dd(trades, n=2000, q=0.95, seed=7):
    """Drawdown that the same trades could have produced in another order.

    The realised drawdown is one path. Resampling the trade sequence in
    monthly blocks (so losses that cluster stay clustered) gives the depth
    that a given share of plausible paths stays above -- size risk off that,
    not off the single path that happened.
    """
    if not len(trades):
        return 0.0
    t = trades.sort_values("exit_ts")
    blocks = [g["r"].values for _, g in t.groupby(t["exit_ts"].dt.to_period("M"))]
    rng = np.random.default_rng(seed)
    dds = []
    for _ in range(n):
        pick = rng.integers(0, len(blocks), len(blocks))
        dds.append(max_dd(np.concatenate([blocks[i] for i in pick])))
    return float(np.quantile(dds, 1 - q))


def returns_summary(ret, periods_per_year):
    """Same headline numbers for a return SERIES (weights_backtest output)."""
    r = ret.dropna()
    if len(r) < 3:
        return {"n": len(r)}
    m = (1 + r).resample("MS").prod() - 1
    eq = (1 + r).cumprod()
    return {
        "n": len(r),
        "ann_ret": 100 * float(r.mean() * periods_per_year),
        "ann_vol": 100 * float(r.std(ddof=1) * math.sqrt(periods_per_year)),
        "sharpe": float(r.mean() / r.std(ddof=1) * math.sqrt(periods_per_year))
                  if r.std(ddof=1) > 0 else 0.0,
        "t": _t(r.values),
        "max_dd": 100 * float((eq / eq.cummax() - 1).min()),
        "pos_months": 100 * float((m > 0).mean()),
        "worst_month": 100 * float(m.min()),
    }
