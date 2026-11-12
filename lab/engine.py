"""
Portfolio simulator for the research lab.

One simulator for every hypothesis, so that two strategies are never compared
across two different sets of fill assumptions. It answers one question: given
a list of signals and an exit rule, what would a book with a concurrency cap
have realised, in R, after costs, under a stated fill model?

RULES IT ENFORCES (each one is a bug this project has already paid for)

  No look-ahead. A signal is read off a CLOSED bar and filled at the NEXT
      bar's open. (`entry="close"` exists only to reproduce the legacy
      sweep_trail_mode.py model in the parity check -- never for research.)

  The resting stop is tested BEFORE anything favourable on the same bar. A bar
      that touches both the stop and the target is a loss.

  A stop that the market gaps through fills at the open, not at the stop.
      2026-11-10 is the worked example: two "locked" 4R winners came home at
      +0.13R and -0.35R.

  A freshly raised stop gets no immunity. `fill` selects how a stop raised
      intrabar is treated on that same bar:
        "pess"  taken out if the bar's adverse extreme reaches it. Harshest;
                assumes the adverse print came after the favourable one.
        "mid"   taken out only if the bar CLOSES through it -- the one case
                where the order of prints is not in doubt.
        "opt"   not tested until the next bar. This is the old "close" model's
                free bar of immunity and sits ABOVE what live can achieve.
      Reality is inside [pess, mid]. Report pess; never quote opt.

  Costs are always on: taker fee both sides, slippage both sides, funding for
      the bars held. `cost_mult=2` is the standing stress test.

ACCOUNTING
    Everything is in R, where 1R is the distance from entry to the INITIAL
    stop. Sizing is fixed-fractional on a constant base (no compounding), so
    account % = R x risk-per-trade, and a result does not depend on the order
    in which trades happened to close.

    The concurrency cap is honoured exactly even though trades are walked one
    at a time: a trade's exit depends only on its own coin's path, so its exit
    time is known the moment it is opened.
"""
import numpy as np
import pandas as pd

# Hyperliquid taker fee, base tier -- same constant as backtest2.TAKER_FEE.
FEE = 0.00045

# Slippage per side, as a fraction of price. NOT calibrated from the live
# journal, deliberately: those fills happened on the testnet book, whose depth
# bears no relation to mainnet's (it produced 8-10% stop slippage). These are
# conservative placeholders for small size on liquid majors -- a market entry
# crossing the spread, and a stop-market firing into a move -- and they are to
# be replaced by measured values once paper_exec walks the real mainnet book.
SLIP_IN = 0.0003
SLIP_OUT = 0.0006


class Market:
    """Per-coin numpy arrays on one timeframe, plus optional aux series."""

    def __init__(self, frames, funding=None):
        self.coins = list(frames)
        self.a = {}
        self.tf_ns = None
        for c, df in frames.items():
            t = df.index.values.astype("datetime64[ns]").astype(np.int64)
            a = {"t": t, "index": df.index,
                 "o": df["open"].values.astype(float),
                 "h": df["high"].values.astype(float),
                 "l": df["low"].values.astype(float),
                 "c": df["close"].values.astype(float)}
            if len(t) > 1:
                step = int(np.median(np.diff(t)))
                self.tf_ns = step if self.tf_ns is None else min(self.tf_ns, step)
            # Cumulative funding paid by a LONG up to and including each bar's
            # open. cf[x] - cf[e] is what a long entered at bar e's open and
            # exited by bar x's open has paid.
            f = funding.get(c) if funding else None
            if f is not None and len(f):
                ft = f.index.values.astype("datetime64[ns]").astype(np.int64)
                cs = np.concatenate([[0.0], np.cumsum(f.values.astype(float))])
                a["cf"] = cs[np.searchsorted(ft, t, side="right")]
            else:
                a["cf"] = np.zeros(len(t))
            self.a[c] = a

    def add(self, name, by_coin):
        """Attach an aux array (e.g. 'atr', 'exit_long') aligned to each coin."""
        for c, arr in by_coin.items():
            if c in self.a:
                v = np.asarray(arr)
                if len(v) != len(self.a[c]["t"]):
                    raise ValueError("%s/%s: length %d != %d bars" % (
                        c, name, len(v), len(self.a[c]["t"])))
                self.a[c][name] = v
        return self

    def pos(self, coin, ts):
        """Bar position whose OPEN time is exactly ts, or -1."""
        t = self.a[coin]["t"]
        k = np.int64(pd.Timestamp(ts).value)
        i = int(np.searchsorted(t, k))
        return i if i < len(t) and t[i] == k else -1


def _walk(a, e, d, entry, stop, R, ex, fill, gap_fill, tf_ns):
    """Walk one trade from entry bar `e`. Returns the exit as a dict."""
    o, h, l, c, t = a["o"], a["h"], a["l"], a["c"], a["t"]
    n = len(t)
    tp = entry + d * ex["tp_r"] * R if ex.get("tp_r") else None
    rat = ex.get("ratchet")
    ch = ex.get("chandelier")
    atr = a.get(ch.get("atr", "atr")) if ch else None
    be_at = ex.get("be_at_r")
    max_bars = ex.get("max_bars")
    xs = a.get("exit_long" if d == 1 else "exit_short") if ex.get("exit_signal") else None

    moved = False
    armed = False
    be_done = False
    best = entry
    mfe = mae = 0.0

    def out(j, px, why, free_t):
        return {"x": j, "exit": px, "reason": why, "free_t": free_t,
                "mfe_r": mfe, "mae_r": mae}

    for j in range(e, n):
        oj, hj, lj, cj = o[j], h[j], l[j], c[j]
        fav, adv = (hj, lj) if d == 1 else (lj, hj)
        bar_end = t[j] + tf_ns

        # 1. Gap through the resting stop: filled at the open, wherever it is.
        if gap_fill and j > e and d * (oj - stop) <= 0:
            mae = min(mae, d * (oj - entry) / R)
            return out(j, oj, "gap", t[j])

        # 2. Resting stop, tested before any favourable event on this bar.
        if d * (adv - stop) <= 0:
            mae = min(mae, d * (stop - entry) / R)
            return out(j, stop, "trail" if moved else "sl", bar_end)

        mae = min(mae, d * (adv - entry) / R)

        # 3. Fixed target.
        if tp is not None and d * (fav - tp) >= 0:
            mfe = max(mfe, d * (tp - entry) / R)
            return out(j, tp, "tp", bar_end)

        mfe = max(mfe, d * (fav - entry) / R)

        # 4. Intrabar stop raises (breakeven, ratchet). These are driven by a
        #    TOUCH, so the raised stop can be hit on the same bar -- see `fill`.
        new = None
        if be_at is not None and not be_done and d * (fav - entry) / R >= be_at:
            be_done = True
            new = entry
        if rat:
            ref = cj if rat.get("drive") == "close" else fav
            run = d * (ref - entry) / R
            if run >= rat["start_r"]:
                if fill == "pess" and not armed and rat.get("drive") != "close":
                    locked = rat["start_r"]
                else:
                    locked = rat["start_r"] + int(
                        (run - rat["start_r"]) / rat["step_r"] + 1e-9) * rat["step_r"]
                armed = True
                cand = entry + d * locked * R
                if new is None or d * (cand - new) > 0:
                    new = cand
        if new is not None and d * (new - stop) > 0:
            stop, moved = new, True
            if rat and rat.get("drive") == "close":
                pass                      # legacy model: immune until next bar
            elif fill == "pess" and d * (adv - stop) <= 0:
                return out(j, stop, "trail", bar_end)
            elif fill == "mid" and d * (cj - stop) <= 0:
                return out(j, stop, "trail", bar_end)

        # 5. Time stop, at this bar's close.
        if max_bars and j - e + 1 >= max_bars:
            return out(j, cj, "time", bar_end)

        # 6. Exit signal read at this close, filled at the next open.
        if xs is not None and xs[j]:
            if j + 1 < n:
                return out(j + 1, o[j + 1], "signal", t[j + 1])
            return out(j, cj, "signal", bar_end)

        # 7. Chandelier: computed from data up to this close, so it is a
        #    resting order for the NEXT bar and has no intrabar ambiguity.
        if ch:
            best = max(best, hj) if d == 1 else min(best, lj)
            if atr is not None and atr[j] == atr[j]:
                cand = best - d * ch["mult"] * atr[j]
                if d * (cand - stop) > 0:
                    stop, moved = cand, True

    return out(n - 1, c[n - 1], "eod", t[n - 1] + tf_ns)


def simulate(mkt, signals, exit, max_open=None, max_per_dir=None,
             fee=FEE, slip_in=SLIP_IN, slip_out=SLIP_OUT, cost_mult=1.0,
             fill="pess", entry="next_open", gap_fill=True, use_funding=True,
             min_r_frac=0.5):
    """Run a capped book over `signals`. Returns a DataFrame of closed trades.

    signals: DataFrame with
        ts     open time of the CLOSED bar the signal was read from
        coin, dir (+1 / -1)
        stop_dist  price distance entry->stop, applied to the actual fill
          or stop  absolute stop price (then R = |fill - stop|)
        rank   optional; higher wins when the book cannot take every signal

    exit: dict, any of
        tp_r        fixed target in R
        ratchet     {"start_r", "step_r"[, "drive": "close"]}  touch-driven
        chandelier  {"mult"[, "atr": aux name]}   trail off the best extreme
        be_at_r     move stop to entry once this many R have printed
        max_bars    time stop
        exit_signal True -> use aux 'exit_long' / 'exit_short'
    """
    if fill not in ("pess", "mid", "opt"):
        raise ValueError("fill must be pess | mid | opt")
    if not len(signals):
        return pd.DataFrame()
    sig = signals.copy()
    if "rank" not in sig.columns:
        sig["rank"] = 0.0
    sig = sig.sort_values(["ts", "rank", "coin"], ascending=[True, False, True])
    use_dist = "stop_dist" in sig.columns
    tf_ns = mkt.tf_ns

    book = []          # (free_t, coin, dir) for trades still holding a slot
    rows = []
    skipped = {"no_bar": 0, "coin_busy": 0, "book_full": 0, "dir_full": 0,
               "bad_stop": 0, "no_next_bar": 0}

    for s in sig.itertuples(index=False):
        a = mkt.a.get(s.coin)
        if a is None:
            skipped["no_bar"] += 1
            continue
        i = mkt.pos(s.coin, s.ts)
        if i < 0:
            skipped["no_bar"] += 1
            continue
        e = i if entry == "close" else i + 1
        if e >= len(a["t"]):
            skipped["no_next_bar"] += 1
            continue
        entry_t = a["t"][i] + tf_ns          # the instant the fill happens

        book = [b for b in book if b[0] > entry_t]
        if any(b[1] == s.coin for b in book):
            skipped["coin_busy"] += 1
            continue
        if max_open is not None and len(book) >= max_open:
            skipped["book_full"] += 1
            continue
        d = int(s.dir)
        if max_per_dir is not None and sum(1 for b in book if b[2] == d) >= max_per_dir:
            skipped["dir_full"] += 1
            continue

        px = a["c"][i] if entry == "close" else a["o"][e]
        if use_dist:
            R = float(s.stop_dist)
            stop = px - d * R
        else:
            stop = float(s.stop)
            R = d * (px - stop)
            # The stop was placed off the signal close; if the open has run
            # most of the way to it, the trade that was validated no longer
            # exists (size is 1/R, so it explodes exactly here). FIL
            # 2026-08-12 is the live case.
            ref = abs(a["c"][i] - stop)
            if R <= 0 or (ref > 0 and R < min_r_frac * ref):
                skipped["bad_stop"] += 1
                continue
        if not (R > 0) or not np.isfinite(R):
            skipped["bad_stop"] += 1
            continue

        # entry="close" fills at bar i's close, so the walk starts on bar i+1.
        start = e + 1 if entry == "close" else e
        if start >= len(a["t"]):
            skipped["no_next_bar"] += 1
            continue
        w = _walk(a, start, d, px, stop, R, exit, fill, gap_fill, tf_ns)
        book.append((w["free_t"], s.coin, d))

        x = w["x"]
        gross = d * (w["exit"] - px) / R
        cost = (2 * fee + slip_in + slip_out) * px / R * cost_mult
        fund = (-d * (a["cf"][x] - a["cf"][e]) * px / R) if use_funding else 0.0
        row = {"coin": s.coin, "dir": d, "signal_ts": pd.Timestamp(s.ts),
               "entry_ts": pd.Timestamp(entry_t), "exit_ts": pd.Timestamp(w["free_t"]),
               "entry": px, "stop": stop, "exit": w["exit"],
               "r_dist_pct": 100 * R / px, "bars": x - e + 1,
               "reason": w["reason"], "r_gross": gross, "cost_r": cost,
               "fund_r": fund, "r": gross - cost + fund,
               "mfe_r": w["mfe_r"], "mae_r": w["mae_r"]}
        for k in ("tag", "rank"):
            if hasattr(s, k):
                row[k] = getattr(s, k)
        rows.append(row)

    out = pd.DataFrame(rows)
    out.attrs["skipped"] = skipped
    out.attrs["fill"] = fill
    out.attrs["cost_mult"] = cost_mult
    return out


def weights_backtest(closes, weights, fee=FEE, slip=SLIP_IN, funding=None,
                     cost_mult=1.0):
    """Returns for a rebalanced weight book (cross-sectional strategies).

    closes   time x coin close prices
    weights  time x coin target weights DECIDED at that bar's close and held
             over the following bar. Gross exposure is whatever they sum to.
    funding  optional time x coin funding paid by a long over the following bar

    Returns a Series of net period returns as a fraction of capital. Costs are
    charged on turnover, i.e. on the change in weights, both ways.
    """
    w = weights.reindex(closes.index).fillna(0.0)
    nxt = closes.pct_change(fill_method=None).shift(-1)
    gross = (w * nxt).sum(axis=1, min_count=1)
    turnover = w.diff().abs().sum(axis=1)
    turnover.iloc[0] = w.iloc[0].abs().sum()
    cost = turnover * (fee + slip) * cost_mult
    fund = 0.0
    if funding is not None:
        fund = (w * funding.reindex(closes.index).shift(-1).fillna(0.0)).sum(axis=1)
    net = gross - cost - fund
    return net.dropna()
