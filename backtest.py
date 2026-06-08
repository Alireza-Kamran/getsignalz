import numpy as np
import pandas as pd
import time
from indicators import fetch_candles, rsi, atr, ut_bot, ssl_channel, order_blocks, fvg, atr_rsi_sl


def run_backtest(df, signals, tp_ratio=2.0, use_atr_sl=True):
    """
    Simulate trades on historical data.
    signals: boolean Series, True = enter trade
    direction: 1 = long, -1 = short
    Returns stats dict.
    """
    trades = []
    in_trade = False

    for i in range(len(df)):
        if in_trade:
            row = df.iloc[i]
            if direction == 1:
                if row["low"] <= sl:
                    trades.append({"pnl_r": -1.0, "win": False})
                    in_trade = False
                elif row["high"] >= tp:
                    trades.append({"pnl_r": tp_ratio, "win": True})
                    in_trade = False
            else:
                if row["high"] >= sl:
                    trades.append({"pnl_r": -1.0, "win": False})
                    in_trade = False
                elif row["low"] <= tp:
                    trades.append({"pnl_r": tp_ratio, "win": True})
                    in_trade = False
            continue

        sig = signals.iloc[i]
        if sig == 0 or sig is None:
            continue

        direction = int(sig)
        entry = df.iloc[i]["close"]
        a = df.iloc[i]["atr"]

        # SL based on ATR x RSI from strategy
        if direction == 1:
            sl = df.iloc[i]["sl_long"]
            if pd.isna(sl) or sl >= entry:
                sl = entry - 1.5 * a
        else:
            sl = df.iloc[i]["sl_short"]
            if pd.isna(sl) or sl <= entry:
                sl = entry + 1.5 * a

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        tp = entry + direction * risk * tp_ratio
        in_trade = True

    if not trades:
        return None

    wins = sum(1 for t in trades if t["win"])
    total = len(trades)
    total_r = sum(t["pnl_r"] for t in trades)
    avg_win_r = np.mean([t["pnl_r"] for t in trades if t["win"]]) if wins > 0 else 0
    avg_loss_r = np.mean([t["pnl_r"] for t in trades if not t["win"]]) if (total - wins) > 0 else 0

    return {
        "trades": total,
        "wins": wins,
        "win_rate": wins / total * 100,
        "total_r": total_r,
        "profit_factor": (wins * avg_win_r) / max(abs((total - wins) * avg_loss_r), 0.001),
        "avg_r": total_r / total,
    }


def build_signals(df, combo):
    """Build signal series based on combination name."""
    sig = pd.Series(0, index=df.index)

    if combo == "ob_rsi":
        # Your core strategy: OB + RSI
        long = df["bull_ob"] & (df["rsi"] <= 30)
        short = df["bear_ob"] & (df["rsi"] >= 70)
        sig[long] = 1
        sig[short] = -1

    elif combo == "ob_rsi_ssl":
        # OB + RSI + SSL trend filter
        long = df["bull_ob"] & (df["rsi"] <= 30) & (df["ssl"] == 1)
        short = df["bear_ob"] & (df["rsi"] >= 70) & (df["ssl"] == -1)
        sig[long] = 1
        sig[short] = -1

    elif combo == "ob_rsi_ssl_ut":
        # OB + RSI + SSL + UT Bot confirmation
        long = df["bull_ob"] & (df["rsi"] <= 30) & (df["ssl"] == 1) & (df["ut_buy"])
        short = df["bear_ob"] & (df["rsi"] >= 70) & (df["ssl"] == -1) & (df["ut_sell"])
        sig[long] = 1
        sig[short] = -1

    elif combo == "ut_ssl":
        # UT Bot signal in SSL trend direction
        long = df["ut_buy"] & (df["ssl"] == 1)
        short = df["ut_sell"] & (df["ssl"] == -1)
        sig[long] = 1
        sig[short] = -1

    elif combo == "ut_only":
        # Pure UT Bot signals
        sig[df["ut_buy"]] = 1
        sig[df["ut_sell"]] = -1

    elif combo == "ssl_flip":
        # Trade SSL trend flips
        long = (df["ssl"] == 1) & (df["ssl"].shift(1) == -1)
        short = (df["ssl"] == -1) & (df["ssl"].shift(1) == 1)
        sig[long] = 1
        sig[short] = -1

    elif combo == "fvg_rsi":
        # FVG + RSI
        long = df["fvg_bull"] & (df["rsi"] <= 45)
        short = df["fvg_bear"] & (df["rsi"] >= 55)
        sig[long] = 1
        sig[short] = -1

    elif combo == "ob_ssl_struct":
        # OB + SSL + Market Structure alignment
        long = df["bull_ob"] & (df["ssl"] == 1) & (df["struct"] == 1)
        short = df["bear_ob"] & (df["ssl"] == -1) & (df["struct"] == 0)
        sig[long] = 1
        sig[short] = -1

    elif combo == "rsi_extreme_ssl":
        # RSI extreme (25/75) + SSL trend
        long = (df["rsi"] <= 25) & (df["ssl"] == 1)
        short = (df["rsi"] >= 75) & (df["ssl"] == -1)
        sig[long] = 1
        sig[short] = -1

    elif combo == "full_confluence":
        # All signals aligned (strictest)
        long = (df["bull_ob"] | df["ut_buy"]) & (df["rsi"] <= 40) & (df["ssl"] == 1) & (df["struct"] == 1)
        short = (df["bear_ob"] | df["ut_sell"]) & (df["rsi"] >= 60) & (df["ssl"] == -1) & (df["struct"] == 0)
        sig[long] = 1
        sig[short] = -1

    return sig


def investigate(symbol="ETH", timeframes=["1h", "4h"], tp_ratios=[1.5, 2.0, 2.5]):
    combos = [
        "ob_rsi",
        "ob_rsi_ssl",
        "ob_rsi_ssl_ut",
        "ut_ssl",
        "ut_only",
        "ssl_flip",
        "fvg_rsi",
        "ob_ssl_struct",
        "rsi_extreme_ssl",
        "full_confluence",
    ]

    all_results = []

    for tf in timeframes:
        print(f"\nFetching {symbol} {tf} data...")
        bars = 500 if tf == "1h" else 300 if tf == "4h" else 600
        df = fetch_candles(symbol, tf, lookback_bars=bars)
        print(f"  Got {len(df)} bars ({df.index[0].date()} to {df.index[-1].date()})")

        # Calculate all indicators
        df["rsi"] = rsi(df["close"])
        df["atr"] = atr(df["high"], df["low"], df["close"])
        ut_buy_s, ut_sell_s, _ = ut_bot(df["close"], df["high"], df["low"])
        df["ut_buy"] = ut_buy_s
        df["ut_sell"] = ut_sell_s
        ssl_hlv, _, _ = ssl_channel(df["high"], df["low"], df["close"])
        df["ssl"] = ssl_hlv
        bull_ob_s, bear_ob_s, _, _, _, _, struct_s = order_blocks(
            df["high"], df["low"], df["close"], df["volume"]
        )
        df["bull_ob"] = bull_ob_s
        df["bear_ob"] = bear_ob_s
        df["struct"] = struct_s
        fvg_bull_s, fvg_bear_s = fvg(df["high"], df["low"], df["close"])
        df["fvg_bull"] = fvg_bull_s
        df["fvg_bear"] = fvg_bear_s
        sl_long_s, sl_short_s, _, _ = atr_rsi_sl(
            df["open"], df["high"], df["low"], df["close"]
        )
        df["sl_long"] = sl_long_s
        df["sl_short"] = sl_short_s

        df = df.dropna(subset=["rsi", "ssl", "atr"])

        for tp in tp_ratios:
            for combo in combos:
                signals = build_signals(df, combo)
                stats = run_backtest(df, signals, tp_ratio=tp)
                if stats and stats["trades"] >= 5:
                    # Trades per day
                    days = max((df.index[-1] - df.index[0]).days, 1)
                    trades_per_day = stats["trades"] / days
                    all_results.append({
                        "timeframe": tf,
                        "strategy": combo,
                        "tp_ratio": tp,
                        **stats,
                        "trades_per_day": round(trades_per_day, 2),
                    })

    return pd.DataFrame(all_results)


if __name__ == "__main__":
    results = investigate("ETH", timeframes=["1h", "4h"], tp_ratios=[1.5, 2.0, 2.5])

    if results.empty:
        print("No results.")
    else:
        results = results.sort_values("win_rate", ascending=False)

        print("\n" + "="*75)
        print("  STRATEGY INVESTIGATION RESULTS — ETH PERPS")
        print("="*75)
        print(f"  {'Strategy':<22} {'TF':<5} {'TP':<5} {'Trades':<8} {'TPD':<6} {'WR%':<8} {'PF':<7} {'Total R'}")
        print("-"*75)

        for _, row in results.head(20).iterrows():
            print(f"  {row['strategy']:<22} {row['timeframe']:<5} {row['tp_ratio']:<5} "
                  f"{row['trades']:<8} {row['trades_per_day']:<6} "
                  f"{row['win_rate']:.1f}%{'':<3} {row['profit_factor']:.2f}{'':>4} {row['total_r']:.2f}R")

        print("="*75)
        print("\nTPD = trades per day | PF = profit factor | Total R = total risk units gained")

        # Best overall
        best = results.sort_values(["win_rate", "profit_factor"], ascending=False).iloc[0]
        print(f"\n>>> BEST WIN RATE: {best['strategy']} on {best['timeframe']} "
              f"| TP {best['tp_ratio']}x | WR {best['win_rate']:.1f}% | PF {best['profit_factor']:.2f}")

        # Best profit factor
        best_pf = results.sort_values("profit_factor", ascending=False).iloc[0]
        print(f">>> BEST PROFIT FACTOR: {best_pf['strategy']} on {best_pf['timeframe']} "
              f"| TP {best_pf['tp_ratio']}x | WR {best_pf['win_rate']:.1f}% | PF {best_pf['profit_factor']:.2f}")

        # Best for daily trades target (1-3/day)
        daily = results[(results["trades_per_day"] >= 0.5) & (results["trades_per_day"] <= 4)]
        if not daily.empty:
            best_daily = daily.sort_values(["win_rate", "profit_factor"], ascending=False).iloc[0]
            print(f">>> BEST FOR 1-4 TRADES/DAY: {best_daily['strategy']} on {best_daily['timeframe']} "
                  f"| TP {best_daily['tp_ratio']}x | WR {best_daily['win_rate']:.1f}% "
                  f"| {best_daily['trades_per_day']} trades/day")
