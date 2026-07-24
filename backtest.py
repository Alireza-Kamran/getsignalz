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


def backtest_coin(coin, tf="1h", bars=1500):
    """Walk every historical bar of `coin`/`tf` through the live score_setup(),
    simulate SL/TP fills. Returns a list of simulated trade dicts."""
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
            hit_sl = (row["real_low"] <= in_trade["sl"]) if direction == 1 else (row["real_high"] >= in_trade["sl"])
            hit_tp = (row["real_high"] >= in_trade["tp"]) if direction == 1 else (row["real_low"] <= in_trade["tp"])
            if hit_sl:
                trades.append({**in_trade, "exit": in_trade["sl"], "result": "sl", "close_time": row.name})
                in_trade = None
            elif hit_tp:
                trades.append({**in_trade, "exit": in_trade["tp"], "result": "tp", "close_time": row.name})
                in_trade = None
            continue

        macro_dir = int(macro.loc[row.name]) if row.name in macro.index else 0

        for direction in (1, -1):
            score, reasons, sl, tp, leverage, sl_pct, tp_pct = score_setup(sub, direction, macro_dir)
            if score == 0:
                continue
            score = min(score + TF_BONUS.get(tf, 0), 8)
            if score >= MIN_SCORE:
                in_trade = {
                    "coin": coin, "tf": tf, "direction": direction, "score": score,
                    "reasons": reasons, "entry": row["real_close"], "sl": sl, "tp": tp,
                    "leverage": leverage, "open_time": row.name,
                }
                break

    return trades


def run_full_backtest(coins=None, timeframes=("1h",), bars=1500):
    """Backtest the full watchlist (or a subset). Returns a flat list of trades."""
    coins = coins or WATCHLIST
    all_trades = []
    for coin in coins:
        for tf in timeframes:
            try:
                all_trades.extend(backtest_coin(coin, tf, bars))
            except Exception as e:
                print(f"[backtest] {coin}/{tf} error: {e}")
    return all_trades


def _r_pct(t):
    """Leveraged % outcome for one simulated trade, same basis as live signal SL/TP prices."""
    raw = (t["tp"] - t["entry"]) / t["entry"] if t["result"] == "tp" else (t["sl"] - t["entry"]) / t["entry"]
    return raw * 100 * t["direction"] * t["leverage"]


def summarize(trades):
    """Score-band + per-coin EV breakdown — same framework the nightly deep
    self-learn session already uses when reading live journal.json by hand."""
    if not trades:
        return "No trades simulated."

    df = pd.DataFrame(trades)
    df["r_pct"] = df.apply(_r_pct, axis=1)

    lines = [f"Total simulated trades: {len(df)}", ""]

    lines.append(f"── BY SCORE (MIN_SCORE={MIN_SCORE}, TP_RATIO={TP_RATIO}) ──")
    for score, g in sorted(df.groupby("score"), key=lambda x: x[0]):
        wr = (g["result"] == "tp").mean() * 100
        lines.append(f"  Score {score}: n={len(g):<4} WR={wr:5.1f}%  avg={g['r_pct'].mean():+6.1f}%")

    lines.append(f"\n── BY COIN (score >= {MIN_SCORE} only) ──")
    qual = df[df["score"] >= MIN_SCORE]
    if qual.empty:
        lines.append("  (no qualifying trades at current MIN_SCORE)")
    else:
        for coin, g in sorted(qual.groupby("coin"), key=lambda x: -x[1]["r_pct"].mean()):
            wr = (g["result"] == "tp").mean() * 100
            lines.append(f"  {coin:<6} n={len(g):<4} WR={wr:5.1f}%  avg={g['r_pct'].mean():+6.1f}%")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    coins = sys.argv[1:] or WATCHLIST
    print(f"Backtesting {coins} on 1h against the live trader.py strategy...")
    trades = run_full_backtest(coins=coins, timeframes=("1h",))
    print()
    print(summarize(trades))
