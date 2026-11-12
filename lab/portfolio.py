"""
Phase 3: combine the surviving sleeves into one book.

The three sleeves and their specifications are the PRIMARIES written in
HYPOTHESES.md before anything was run. They are frozen here; the best-looking
neighbour of each was deliberately not substituted.

    TREND_SSL  C1  4h Heikin Ashi SSL-200 flip, 3 ATR stop, exit on the
                   opposite flip                     (the owner's indicator)
    TREND_DON  A   1d 40-day Donchian breakout, 3 ATR stop, 3 ATR chandelier
    XSMOM      B   weekly 28-day cross-sectional momentum, top/bottom fifth

Each sleeve is turned into a daily series of account-% P&L, scaled so that the
three contribute EQUAL volatility over the train period (so no sleeve is
chosen for its return), and the total is scaled to TARGET_VOL. Those scale
factors are fitted on train ONLY and then applied unchanged to later periods.

Trend trades are MARKED TO MARKET every day (`mtm_daily`), not booked on the
day they close. The first version booked at the close and reported a 3.4%
drawdown for a sleeve whose trades run for months: an open +20R winner that
gives back 10R before its exit simply did not appear. It also made the two
trend sleeves look uncorrelated (0.01), because the same move was booked in
different months. Booking at the close flatters exactly the sleeves with the
longest holds.

Usage:  python -m lab.portfolio train
        python -m lab.portfolio validate
        python -m lab.portfolio holdout "<reason>"     # logged; run ONCE
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from lab import data, engine, validate as v
from lab.strats import kamran, trend, xsmom

TARGET_VOL = 15.0          # % per year for the combined book. Owner's dial.
SCALES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_scales.json")
SLEEVES = ("TREND_SSL", "TREND_DON", "XSMOM")


def _span(period):
    """(data end, first day, last day) for a period."""
    a, b = v.PERIODS[period]
    end = None if period == "holdout" else b
    return end, pd.Timestamp(a), pd.Timestamp(b)


def sleeve_trades(period, reason=None):
    """Trade lists for the two trend sleeves, entries inside `period`.

    Signals are simulated from 2021 up to the period's end so the book's
    occupancy carries across the boundary the way it would have live, then the
    trades are split by entry date.
    """
    end, a, b = _span(period)
    out = {}

    fr = data.load_all(tf="4h", end=end)
    mkt = engine.Market(fr, {c: data.load_funding(c) for c in fr})
    sig, xl, xs = kamran.ssl_flip_signals(fr, 200)
    mkt.add("exit_long", xl).add("exit_short", xs)
    sig = sig[(sig["ts"] >= pd.Timestamp("2021-01-01")) & (sig["ts"] < b)]
    out["TREND_SSL"] = engine.simulate(mkt, sig, {"exit_signal": True},
                                       max_open=8, max_per_dir=6, fill="pess")

    fr = data.load_all(tf="1d", end=end)
    mkt = engine.Market(fr, {c: data.load_funding(c) for c in fr})
    mkt.add("atr", trend.prepare(fr))
    sig = trend.signals(fr, lookback=40, stop_atr=3.0)
    sig = sig[(sig["ts"] >= pd.Timestamp("2021-01-01")) & (sig["ts"] < b)]
    out["TREND_DON"] = engine.simulate(mkt, sig, {"chandelier": {"mult": 3.0}},
                                       max_open=8, max_per_dir=6, fill="pess")
    for k in out:
        out[k] = v.split(out[k], period, reason=reason)
        out[k] = out[k][out[k]["reason"] != "eod"]
    return out


def xs_returns(period):
    end, a, b = _span(period)
    fr = data.load_all(tf="1d", end=end)
    closes = pd.DataFrame({c: d["close"] for c, d in fr.items()})
    fund = pd.DataFrame({c: data.load_funding(c).resample("1D").sum()
                         for c in fr}).reindex(closes.index).fillna(0.0)
    r = engine.weights_backtest(closes, xsmom.weights(closes), funding=fund)
    return r[(r.index >= a) & (r.index < b)]


def mtm_daily(trades, idx, day_close):
    """Daily mark-to-market P&L in R for a list of trades, on calendar `idx`.

    Each trade is marked at every daily close it is open across, so open
    profit that is later given back shows up as a gain and then a loss in the
    months where they actually happened. Costs are charged on the entry day,
    funding on the exit day; each trade's increments sum to its net `r`.
    """
    out = pd.Series(0.0, index=idx)
    one = pd.Timedelta(days=1)
    for t in trades.itertuples(index=False):
        R = abs(t.entry - t.stop)
        closes = day_close[t.coin]
        d0 = t.entry_ts.normalize()
        dx = (t.exit_ts - pd.Timedelta(1, "ns")).normalize()
        days = pd.date_range(d0, dx, freq="D")
        # the close of day D is known at D+1 00:00; the last day is marked at
        # the actual exit price instead
        marks = closes.reindex(days).values.astype(float)
        marks[-1] = t.exit
        prev = t.entry
        for day, mk in zip(days, marks):
            if mk != mk:                 # no daily bar (gap): carry the mark
                continue
            if day in out.index:
                out[day] += t.dir * (mk - prev) / R
            prev = mk
        if d0 in out.index:
            out[d0] -= t.cost_r
        if dx in out.index:
            out[dx] += t.fund_r
    return out


def daily(period, reason=None):
    """Daily P&L per sleeve in RAW units (R for trend sleeves, fraction of
    gross capital for XSMOM), on a complete calendar for the period."""
    end, a, b = _span(period)
    tr = sleeve_trades(period, reason)
    xs = xs_returns(period)
    last = min(b, pd.Timestamp.utcnow().tz_localize(None).normalize())
    idx = pd.date_range(a, last - pd.Timedelta(days=1), freq="D")
    d = pd.DataFrame(0.0, index=idx, columns=list(SLEEVES))
    day_close = {c: f["close"] for c, f in data.load_all(tf="1d", end=end).items()}
    for k in ("TREND_SSL", "TREND_DON"):
        if len(tr[k]):
            d[k] = mtm_daily(tr[k], idx, day_close)
    d["XSMOM"] = xs.reindex(idx).fillna(0.0)
    return d, tr


def fit_scales(d):
    """Equal-volatility sleeve weights, then scale the sum to TARGET_VOL."""
    vol = d.std(ddof=1) * math.sqrt(365)
    k = {c: 1.0 / vol[c] for c in d.columns}
    comb = sum(d[c] * k[c] for c in d.columns)
    g = TARGET_VOL / (comb.std(ddof=1) * math.sqrt(365))
    return {c: float(k[c] * g) for c in d.columns}


def stats(pnl):
    """pnl: daily account-% series (additive)."""
    m = pnl.resample("MS").sum()
    eq = pnl.cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()
    sd = pnl.std(ddof=1)
    return {"ann": pnl.mean() * 365, "vol": sd * math.sqrt(365),
            "sharpe": pnl.mean() / sd * math.sqrt(365) if sd > 0 else 0.0,
            "t": pnl.mean() / sd * math.sqrt(len(pnl)) if sd > 0 else 0.0,
            "dd": dd, "pos": 100 * (m > 0).mean(), "worst": m.min(),
            "months": len(m), "monthly": m}


def report(period, reason=None):
    d, tr = daily(period, reason)
    if period == "train":
        k = fit_scales(d)
        with open(SCALES, "w") as f:
            json.dump({"target_vol": TARGET_VOL, "fitted_on": "train", "scales": k}, f, indent=1)
    else:
        if not os.path.exists(SCALES):
            raise SystemExit("run `train` first: the scales are fitted there and frozen")
        k = json.load(open(SCALES))["scales"]
    acct = pd.DataFrame({c: d[c] * k[c] for c in d.columns})
    acct["BOOK"] = acct.sum(axis=1)

    print("=== %s  (%s .. %s)" % (period.upper(), d.index[0].date(), d.index[-1].date()))
    print("frozen scales: risk per trade  SSL %.2f%%  DON %.2f%%  | XSMOM gross exposure %.2fx" % (
        k["TREND_SSL"], k["TREND_DON"], k["XSMOM"] / 100))
    print("%-10s %7s %6s %7s %6s %7s %5s %8s" % (
        "sleeve", "ann%", "vol%", "sharpe", "t", "maxDD%", "mo+%", "worstMo%"))
    for c in acct.columns:
        s = stats(acct[c])
        print("%-10s %+7.1f %6.1f %+7.2f %+6.2f %7.1f %5.0f %+8.1f" % (
            c, s["ann"], s["vol"], s["sharpe"], s["t"], s["dd"], s["pos"], s["worst"]))
    m = acct.resample("MS").sum()
    print("monthly correlation between sleeves:")
    print(m[list(SLEEVES)].corr().round(2).to_string())
    for kname in ("TREND_SSL", "TREND_DON"):
        print(v.line(kname + " trades", v.summarize(tr[kname])))
    print("BOOK by month (account %):")
    b = m["BOOK"]
    for y, g in b.groupby(b.index.year):
        print("  %d  %s  | year %+6.1f" % (y, " ".join("%+5.1f" % x for x in g.values), g.sum()))
    # open initial risk at once, in account %
    worst = 0.0
    for kname in ("TREND_SSL", "TREND_DON"):
        t = tr[kname]
        if len(t):
            ev = pd.concat([pd.Series(1, index=t["entry_ts"]), pd.Series(-1, index=t["exit_ts"])]).sort_index(kind="stable")
            worst += ev.cumsum().max() * k[kname]
    print("open_risk: at most %.1f%% of the account was at initial risk in the trend sleeves at once" % worst)
    return acct


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "train"
    report(p, reason=sys.argv[2] if len(sys.argv) > 2 else None)
