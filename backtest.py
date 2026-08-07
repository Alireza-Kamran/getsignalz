"""
Backtest engine — walks historical candles through the LIVE trader.py strategy.

No scoring/indicator logic is reimplemented here: this calls trader.build_df()
and trader.score_setup() directly, so a backtest run always tests exactly what
the live bot would have signaled, and automatically stays in sync whenever
trader.py's strategy changes (no separate copy to keep updated by hand).

This replaces the old backtest.py, which tested a completely different, no
longer live scoring system (OB/RSI/SSL/UT-Bot/FVG combos, pre-2026-06-25
CM Sling Shot rebuild) — its numbers had no relationship to what the bot
actually trades today.

Known simplifications (documented, not hidden):
- Same-bar SL+TP ambiguity: if a single candle's range covers both, SL is
  assumed to fill first (conservative/worst-case, not optimistic).
- One open trade at a time PER COIN is modeled, not a true cross-coin
  MAX_TRADES gate — a real portfolio walk would need to interleave every
  coin on a shared timeline. This means backtested trade frequency is an
  upper bound vs. live, where a position in one coin can block a signal
  in another. Score-band/coin EV comparisons are still valid; raw trade
  count should be read as "how often does this setup qualify," not "how
  many the live bot would actually take."
- No slippage/fee modeling — same idealized SL/TP-price basis the nightly
  memory session already uses when hand-computing EV from journal.json.
- Bounded by whatever historical depth Hyperliquid testnet's candle API
  actually returns (typically a few months on 1h) — a large, fast sample
  for hypothesis-testing, not a multi-year statistical guarantee.
"""
import pandas as pd
from trader import build_df, score_setup, WATCHLIST, MIN_SCORE, TP_RATIO, TF_BONUS
from indicators import order_blocks, fetch_candles


def _macro_series(coin, base_tf, base_index):
    """Higher-TF (4h/1d) market structure direction, as-of each base-tf timestamp.

    Mirrors trader.get_macro_trend() (order_blocks' struct: 1=bullish,
    0=bearish) but vectorized across the whole backtest window instead of
    one live API call per bar — same source data, no lookahead (each
    base-tf bar only ever sees higher-TF bars already closed by then).
    """
    macro_tf = "4h" if base_tf in ("1h", "15m") else "1d"
    df4 = fetch_candles(coin, macro_tf, lookback_bars=1500)
    if df4 is None or len(df4) < 100:
        return pd.Series(0, index=base_index)
    _, _, _, _, _, _, struct = order_blocks(df4["high"], df4["low"], df4["close"], df4["volume"])
    macro = pd.Series(0, index=df4.index)
    macro[struct == 1] = 1
    macro[struct == 0] = -1
    return macro.reindex(base_index, method="ffill").fillna(0).astype(int)


# Hyperliquid testnet's 1h candle API returns ~5000 bars max regardless of how
# far back you ask (verified 2026-07-26: requesting 8760 returned 5002, back to
# 2025-12-29) -- ~7 months, not the 62 days the old bars=1500 default covered.
# fetch_candles() silently truncates to whatever's available, so asking for
# more than exists is harmless; this constant is the real ceiling, not a guess.
MAX_AVAILABLE_BARS = 5000


def backtest_coin(coin, tf="1h", bars=MAX_AVAILABLE_BARS, attrition=None, skip_gates=None,
                  tp_r_multiple=None, trail_r_step=None):
    """Walk every historical bar of `coin`/`tf` through the live score_setup(),
    simulate SL/TP fills. Returns a list of simulated trade dicts.

    attrition/skip_gates: passed straight through to score_setup() for
    instrumentation (see its docstring) -- both no-ops by default.

    trail_r_step: when set (e.g. 1.0), models the progressive risk-free ladder
    Kamran described instead of a fixed TP exit -- every full R of favourable
    excursion ratchets the stop up one step, so reaching +1R moves the stop to
    breakeven, +2R locks +1R, +3R locks +2R, and so on with no upper bound.
    The position then exits ONLY when that trailing stop is hit, never at a
    fixed TP. Default None keeps the original fixed SL/TP behaviour, so every
    earlier result stays reproducible."""
    df = build_df(coin, tf, bars=bars)
    if df is None or len(df) < 50:
        return []

    macro = _macro_series(coin, tf, df.index)

    trades = []
    in_trade = None

    for i in range(1, len(df)):
        sub = df.iloc[:i + 1]
        row = df.iloc[i]

        if in_trade:
            direction = in_trade["direction"]
            # SL is always evaluated FIRST, against the stop as it stood at the
            # start of this bar -- trailing it up using the same bar's favourable
            # extreme and only then testing the stop would be lookahead, and would
            # silently turn losing bars into winners.
            hit_sl = (row["real_low"] <= in_trade["sl"]) if direction == 1 else (row["real_high"] >= in_trade["sl"])
            if hit_sl:
                result = "sl" if in_trade["sl"] == in_trade["sl_original"] else "trail"
                trades.append({**in_trade, "exit": in_trade["sl"], "result": result,
                               "close_time": row.name})
                in_trade = None
                continue

            if trail_r_step:
                R = in_trade["R"]
                extreme = row["real_high"] if direction == 1 else row["real_low"]
                mfe_r = (extreme - in_trade["entry"]) * direction / R
                # floor() of the excursion is the rung reached; rung n locks in
                # (n-1)R, so the first rung (+1R) is exactly breakeven.
                rung = int(mfe_r // trail_r_step)
                if rung >= 1:
                    locked = (rung - 1) * trail_r_step
                    new_sl = in_trade["entry"] + direction * locked * R
                    if (new_sl > in_trade["sl"]) if direction == 1 else (new_sl < in_trade["sl"]):
                        in_trade["sl"] = new_sl
                        in_trade["max_rung"] = rung
                continue

            hit_tp = (row["real_high"] >= in_trade["tp"]) if direction == 1 else (row["real_low"] <= in_trade["tp"])
            if hit_tp:
                trades.append({**in_trade, "exit": in_trade["tp"], "result": "tp", "close_time": row.name})
                in_trade = None
            continue

        macro_dir = int(macro.loc[row.name]) if row.name in macro.index else 0

        for direction in (1, -1):
            score, reasons, sl, tp, leverage, sl_pct, tp_pct = score_setup(
                sub, direction, macro_dir, attrition=attrition, skip_gates=skip_gates,
                tp_r_multiple=tp_r_multiple)
            if score == 0:
                continue
            score = min(score + TF_BONUS.get(tf, 0), 8)
            if score >= MIN_SCORE:
                entry = row["real_close"]
                in_trade = {
                    "coin": coin, "tf": tf, "direction": direction, "score": score,
                    "reasons": reasons, "entry": entry, "sl": sl, "tp": tp,
                    "sl_original": sl, "R": abs(entry - sl), "max_rung": 0,
                    "leverage": leverage, "open_time": row.name,
                }
                break

    # A trailing position still open when history runs out has no exit price;
    # counting it as anything would be inventing a result, so it is reported
    # separately rather than folded into win rate or expectancy.
    if in_trade is not None and trail_r_step:
        trades.append({**in_trade, "exit": None, "result": "still_open",
                       "close_time": None})

    return trades


def run_full_backtest(coins=None, timeframes=("1h",), bars=MAX_AVAILABLE_BARS,
                       attrition=None, skip_gates=None, tp_r_multiple=None,
                       trail_r_step=None):
    """Backtest the full watchlist (or a subset). Returns a flat list of trades."""
    coins = coins or WATCHLIST
    all_trades = []
    for coin in coins:
        for tf in timeframes:
            try:
                all_trades.extend(backtest_coin(coin, tf, bars, attrition=attrition,
                                                 skip_gates=skip_gates,
                                                 tp_r_multiple=tp_r_multiple,
                                                 trail_r_step=trail_r_step))
            except Exception as e:
                print(f"[backtest] {coin}/{tf} error: {e}")
    return all_trades


# ── Gates in the order score_setup() evaluates them (defines the funnel) ──────
GATE_ORDER = ["macro_trend", "entry_zone", "target_pool", "path_clear",
              "sl_floor", "rr_floor", "leverage_cap"]

# Pure-threshold gates safe to fully disable for ablation. entry_zone/target_pool
# are excluded: sl/tp are computed FROM their output, so "skipping" them isn't a
# threshold relaxation, it's removing the trade's entry/target definition entirely.
ABLATABLE_GATES = ["macro_trend", "path_clear", "sl_floor", "rr_floor", "leverage_cap"]


def run_attrition_analysis(coins=None, tf="1h", bars=MAX_AVAILABLE_BARS):
    """Run the full watchlist once, recording which gate stops each candle
    that doesn't produce a trade. Returns (funnel_rows, total_evaluated, trades).

    total_evaluated comes from score_setup()'s own call counter (attrition
    dict's "_evaluated" key), not a candle-count estimate -- backtest_coin
    skips evaluation entirely while a trade is open, so counting candles
    directly would overstate the denominator and understate every pass rate."""
    coins = coins or WATCHLIST
    attrition = {}
    trades = run_full_backtest(coins, (tf,), bars, attrition=attrition)
    total = attrition.pop("_evaluated", 0)

    funnel = []
    reached = total
    for gate in GATE_ORDER:
        failed = attrition.get(gate, 0)
        funnel.append({"gate": gate, "reached": reached, "failed": failed,
                        "pass_rate_here": (reached - failed) / reached * 100 if reached else 0,
                        "pct_of_total": reached / total * 100 if total else 0})
        reached -= failed
    funnel.append({"gate": "PASSED_ALL_GATES", "reached": reached, "failed": 0,
                    "pass_rate_here": 100.0, "pct_of_total": reached / total * 100 if total else 0})
    return funnel, total, trades


def format_funnel(funnel, total):
    lines = [f"Total candle×direction checks: {total}", ""]
    lines.append(f"{'Gate':<16} {'Reached':>8} {'Failed':>8} {'% of total reaching this stage':>32}")
    for row in funnel:
        lines.append(f"{row['gate']:<16} {row['reached']:>8} {row['failed']:>8} {row['pct_of_total']:>31.2f}%")
    return "\n".join(lines)


def run_ablation_study(coins=None, tf="1h", bars=MAX_AVAILABLE_BARS):
    """Baseline vs. each ablatable gate disabled one at a time. Returns a
    dict of {label: {'trades': n, 'wr': %, 'avg_r_pct': %}}."""
    coins = coins or WATCHLIST
    results = {}

    baseline_trades = run_full_backtest(coins, (tf,), bars)
    results["baseline"] = _ablation_stats(baseline_trades)

    for gate in ABLATABLE_GATES:
        trades = run_full_backtest(coins, (tf,), bars, skip_gates={gate})
        results[f"skip_{gate}"] = _ablation_stats(trades)

    return results


def run_exit_study(coins=None, tf="4h", bars=MAX_AVAILABLE_BARS,
                    multiples=(1.0, 1.5, 2.0, 3.0)):
    """Same entries and gates, different exits: LP/FVG target (baseline) vs.
    fixed R multiples. Answers 'what win rate do closer targets actually buy,
    and does the payoff loss outweigh it' with data instead of theory."""
    coins = coins or WATCHLIST
    results = {"baseline_LP_target": _ablation_stats(run_full_backtest(coins, (tf,), bars))}
    for m in multiples:
        trades = run_full_backtest(coins, (tf,), bars, tp_r_multiple=m)
        results[f"fixed_{m}R"] = _ablation_stats(trades)
    return results


def format_exit_study(results):
    """Expectancy is the decision metric here, not win rate -- a high-WR/low-RR
    variant can post a great WR and still be the worse system."""
    lines = [f"{'Exit rule':<22} {'Trades':>7} {'WR%':>7} {'AvgR%':>9} {'TotalR%':>10}"]
    for label, r in results.items():
        total = r["avg_r_pct"] * r["trades"]
        lines.append(f"{label:<22} {r['trades']:>7} {r['wr']:>7.1f} "
                      f"{r['avg_r_pct']:>9.2f} {total:>10.1f}")
    return "\n".join(lines)


def _ablation_stats(trades):
    """Win rate is measured by realised P&L sign, not by which order type fired.
    A trailing exit above entry is a win even though it closed on a stop, and
    the old result=="tp" test would have scored every one of them as a loss --
    the exact mislabelling that corrupted the nightly tuner twice (see the
    disabled auto-adjust blocks in review.py)."""
    empty = {"trades": 0, "wr": 0.0, "avg_r_pct": 0.0, "wins": 0,
             "breakeven": 0, "losses": 0, "still_open": 0}
    if not trades:
        return empty
    df = pd.DataFrame(trades)
    still_open = int((df["result"] == "still_open").sum()) if "result" in df else 0
    df = df[df["result"] != "still_open"]
    df = df[df["score"] >= MIN_SCORE]
    if df.empty:
        return {**empty, "still_open": still_open}
    df = df.copy()
    df["r_pct"] = df.apply(_r_pct, axis=1)
    wins = int((df["r_pct"] > 0).sum())
    breakeven = int((df["r_pct"] == 0).sum())
    losses = int((df["r_pct"] < 0).sum())
    return {"trades": len(df), "wr": round(wins / len(df) * 100, 1),
            "avg_r_pct": round(df["r_pct"].mean(), 2), "wins": wins,
            "breakeven": breakeven, "losses": losses, "still_open": still_open}


def run_trail_study(coins=None, tf="4h", bars=MAX_AVAILABLE_BARS, steps=(1.0, 0.5)):
    """Fixed TP exit (baseline) vs. the progressive risk-free ladder at various
    step sizes. Same entries and gates throughout -- only the exit differs."""
    coins = coins or WATCHLIST
    results = {"baseline_fixed_TP": _ablation_stats(run_full_backtest(coins, (tf,), bars))}
    for s in steps:
        trades = run_full_backtest(coins, (tf,), bars, trail_r_step=s)
        results[f"trail_{s}R_ladder"] = _ablation_stats(trades)
    return results


def format_trail_study(results):
    lines = [f"{'Exit rule':<22} {'N':>5} {'Win':>5} {'BE':>4} {'Loss':>5} "
             f"{'WR%':>7} {'AvgR%':>8} {'TotalR%':>9} {'Open':>5}"]
    for label, r in results.items():
        total = r["avg_r_pct"] * r["trades"]
        lines.append(f"{label:<22} {r['trades']:>5} {r['wins']:>5} {r['breakeven']:>4} "
                      f"{r['losses']:>5} {r['wr']:>7.1f} {r['avg_r_pct']:>8.2f} "
                      f"{total:>9.1f} {r['still_open']:>5}")
    return "\n".join(lines)


def format_ablation(results):
    lines = [f"{'Variant':<20} {'Trades':>8} {'WR%':>8} {'Avg R%':>10}"]
    base = results["baseline"]
    for label, r in results.items():
        lines.append(f"{label:<20} {r['trades']:>8} {r['wr']:>8.1f} {r['avg_r_pct']:>10.2f}")
        if label != "baseline":
            lines.append(f"{'  vs baseline':<20} {r['trades']-base['trades']:>+8}")
    return "\n".join(lines)


# Hyperliquid taker fee, one side. Round trip costs twice this on notional --
# and because position notional is leverage x margin, the cost measured against
# margin (which is what r_pct is denominated in) scales with leverage too.
# Ignoring this was harmless while the strategy took <1 trade/month; at the
# ~20/month a gate-relaxed variant produces it is the difference between a
# profitable system and a losing one, so it is modelled explicitly.
TAKER_FEE = 0.00035


def _fee_pct(t, fee=TAKER_FEE):
    """Round-trip fee for one trade, as a % of margin (same basis as _r_pct)."""
    return fee * 2 * t["leverage"] * 100


def _r_pct(t, fee=0.0):
    """Leveraged % outcome for one simulated trade, same basis as live signal SL/TP prices.

    Reads the recorded exit price rather than re-deriving it from tp/sl, so a
    trailing-stop exit (which lands somewhere between the two) is measured
    correctly. Identical to the old tp/sl form for fixed-exit trades, where the
    exit price IS whichever of the two was hit.

    fee: pass TAKER_FEE to subtract round-trip cost. Defaults to 0 so every
    earlier gross-return figure stays reproducible."""
    if t.get("exit") is None:
        return 0.0
    raw = (t["exit"] - t["entry"]) / t["entry"]
    gross = raw * 100 * t["direction"] * t["leverage"]
    return gross - (_fee_pct(t, fee) if fee else 0.0)


def summarize(trades):
    """Score-band + per-coin EV breakdown — same framework the nightly deep
    self-learn session already uses when reading live journal.json by hand."""
    if not trades:
        return "No trades simulated."

    df = pd.DataFrame(trades)
    df["r_pct"] = df.apply(_r_pct, axis=1)

    lines = [f"Total simulated trades: {len(df)}", ""]

    # WR by realised P&L sign, not which order fired -- result=="tp" mislabels
    # every winning trail exit as a loss (same bug already fixed in this file's
    # own _ablation_stats(), missed here since this is a separate entry point).
    lines.append(f"── BY SCORE (MIN_SCORE={MIN_SCORE}, TP_RATIO={TP_RATIO}) ──")
    for score, g in sorted(df.groupby("score"), key=lambda x: x[0]):
        wr = (g["r_pct"] > 0).mean() * 100
        lines.append(f"  Score {score}: n={len(g):<4} WR={wr:5.1f}%  avg={g['r_pct'].mean():+6.1f}%")

    lines.append(f"\n── BY COIN (score >= {MIN_SCORE} only) ──")
    qual = df[df["score"] >= MIN_SCORE]
    if qual.empty:
        lines.append("  (no qualifying trades at current MIN_SCORE)")
    else:
        for coin, g in sorted(qual.groupby("coin"), key=lambda x: -x[1]["r_pct"].mean()):
            wr = (g["r_pct"] > 0).mean() * 100
            lines.append(f"  {coin:<6} n={len(g):<4} WR={wr:5.1f}%  avg={g['r_pct'].mean():+6.1f}%")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    coins = sys.argv[1:] or WATCHLIST
    print(f"Backtesting {coins} on 1h against the live trader.py strategy...")
    trades = run_full_backtest(coins=coins, timeframes=("1h",))
    print()
    print(summarize(trades))
