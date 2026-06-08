import numpy as np
import pandas as pd
import time
from indicators import fetch_candles, rsi, atr, ut_bot, ssl_channel, order_blocks, fvg, atr_rsi_sl


def prepare(df, ssl_len=200):
    df = df.copy()
    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df["high"], df["low"], df["close"])

    ut_buy_s, ut_sell_s, ut_trail_s = ut_bot(df["close"], df["high"], df["low"])
    df["ut_buy"] = ut_buy_s
    df["ut_sell"] = ut_sell_s
    df["ut_trail"] = ut_trail_s

    ssl_hlv, ssl_mh, ssl_ml = ssl_channel(df["high"], df["low"], df["close"], ssl_len)
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

    return df.dropna(subset=["rsi", "atr"])


def simulate(df, long_cond, short_cond, tp_ratio=2.0):
    """Simulate trades bar by bar. Returns list of trade results."""
    trades = []
    in_trade = False
    direction = 0
    entry = sl = tp = 0.0

    for i in range(len(df)):
        row = df.iloc[i]

        if in_trade:
            if direction == 1:
                if row["low"] <= sl:
                    pnl = -1.0
                    trades.append(pnl)
                    in_trade = False
                elif row["high"] >= tp:
                    pnl = tp_ratio
                    trades.append(pnl)
                    in_trade = False
            else:
                if row["high"] >= sl:
                    pnl = -1.0
                    trades.append(pnl)
                    in_trade = False
                elif row["low"] <= tp:
                    pnl = tp_ratio
                    trades.append(pnl)
                    in_trade = False
            continue

        go_long = long_cond.iloc[i]
        go_short = short_cond.iloc[i]

        if not go_long and not go_short:
            continue

        direction = 1 if go_long else -1
        entry = row["close"]
        a = row["atr"]

        if direction == 1:
            raw_sl = row["sl_long"]
            sl = raw_sl if (not pd.isna(raw_sl) and raw_sl < entry) else entry - 1.5 * a
        else:
            raw_sl = row["sl_short"]
            sl = raw_sl if (not pd.isna(raw_sl) and raw_sl > entry) else entry + 1.5 * a

        risk = abs(entry - sl)
        if risk < entry * 0.001:
            continue

        tp = entry + direction * risk * tp_ratio
        in_trade = True

    return trades


def stats(trades):
    if len(trades) < 4:
        return None
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    win_rate = len(wins) / len(trades) * 100
    total_r = sum(trades)
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_win / gross_loss
    return {
        "n": len(trades),
        "wr": win_rate,
        "total_r": total_r,
        "pf": pf,
        "avg_r": total_r / len(trades),
    }


STRATEGIES = {
    "ob_rsi_30_70": lambda df: (
        df["bull_ob"] & (df["rsi"] <= 30),
        df["bear_ob"] & (df["rsi"] >= 70),
    ),
    "ob_ssl": lambda df: (
        df["bull_ob"] & (df["ssl"] == 1),
        df["bear_ob"] & (df["ssl"] == -1),
    ),
    "ob_ssl_struct": lambda df: (
        df["bull_ob"] & (df["ssl"] == 1) & (df["struct"] == 1),
        df["bear_ob"] & (df["ssl"] == -1) & (df["struct"] == 0),
    ),
    "ob_ssl_rsi50": lambda df: (
        df["bull_ob"] & (df["ssl"] == 1) & (df["rsi"] < 55),
        df["bear_ob"] & (df["ssl"] == -1) & (df["rsi"] > 45),
    ),
    "ut_ssl": lambda df: (
        df["ut_buy"] & (df["ssl"] == 1),
        df["ut_sell"] & (df["ssl"] == -1),
    ),
    "ut_ssl_struct": lambda df: (
        df["ut_buy"] & (df["ssl"] == 1) & (df["struct"] == 1),
        df["ut_sell"] & (df["ssl"] == -1) & (df["struct"] == 0),
    ),
    "ut_ob_ssl": lambda df: (
        df["ut_buy"] & df["bull_ob"] & (df["ssl"] == 1),
        df["ut_sell"] & df["bear_ob"] & (df["ssl"] == -1),
    ),
    "fvg_ssl_struct": lambda df: (
        df["fvg_bull"] & (df["ssl"] == 1) & (df["struct"] == 1),
        df["fvg_bear"] & (df["ssl"] == -1) & (df["struct"] == 0),
    ),
    "fvg_ut_ssl": lambda df: (
        df["fvg_bull"] & df["ut_buy"] & (df["ssl"] == 1),
        df["fvg_bear"] & df["ut_sell"] & (df["ssl"] == -1),
    ),
    "ut_only": lambda df: (
        df["ut_buy"],
        df["ut_sell"],
    ),
    "ssl_rsi_zone": lambda df: (
        (df["ssl"] == 1) & (df["rsi"] >= 40) & (df["rsi"] <= 60),
        (df["ssl"] == -1) & (df["rsi"] >= 40) & (df["rsi"] <= 60),
    ),
    "ob_fvg_ssl": lambda df: (
        (df["bull_ob"] | df["fvg_bull"]) & (df["ssl"] == 1) & (df["struct"] == 1),
        (df["bear_ob"] | df["fvg_bear"]) & (df["ssl"] == -1) & (df["struct"] == 0),
    ),
    "ut_rsi_trend": lambda df: (
        df["ut_buy"] & (df["rsi"] > 45) & (df["rsi"] < 65) & (df["ssl"] == 1),
        df["ut_sell"] & (df["rsi"] > 35) & (df["rsi"] < 55) & (df["ssl"] == -1),
    ),
    "triple_confirm": lambda df: (
        df["bull_ob"] & df["ut_buy"] & (df["ssl"] == 1),
        df["bear_ob"] & df["ut_sell"] & (df["ssl"] == -1),
    ),
    "fvg_only_trend": lambda df: (
        df["fvg_bull"] & (df["ssl"] == 1),
        df["fvg_bear"] & (df["ssl"] == -1),
    ),
}

TARGET_COINS = [
    "SOL", "AVAX", "LINK", "DOGE", "INJ", "SUI", "APT",
    "ARB", "OP", "MATIC", "AAVE", "UNI", "DOT", "ATOM",
    "FTM", "RUNE", "SEI", "WLD", "LDO", "SNX",
]

TP_RATIOS = [1.5, 2.0]
TIMEFRAMES = ["1h", "4h"]

results = []

for coin in TARGET_COINS:
    for tf in TIMEFRAMES:
        try:
            bars = 500 if tf == "1h" else 350
            df_raw = fetch_candles(coin, tf, lookback_bars=bars)
            if len(df_raw) < 150:
                continue
            df = prepare(df_raw)
            days = max((df.index[-1] - df.index[0]).days, 1)

            for strat_name, signal_fn in STRATEGIES.items():
                try:
                    long_c, short_c = signal_fn(df)
                    for tp in TP_RATIOS:
                        trades = simulate(df, long_c, short_c, tp)
                        s = stats(trades)
                        if s:
                            results.append({
                                "coin": coin,
                                "tf": tf,
                                "strategy": strat_name,
                                "tp": tp,
                                "n": s["n"],
                                "tpd": round(s["n"] / days, 2),
                                "wr": round(s["wr"], 1),
                                "pf": round(s["pf"], 2),
                                "total_r": round(s["total_r"], 2),
                                "avg_r": round(s["avg_r"], 3),
                            })
                except Exception:
                    pass
            time.sleep(0.1)
        except Exception as e:
            print(f"  Skip {coin} {tf}: {e}")
            continue

df_res = pd.DataFrame(results)

if df_res.empty:
    print("No results.")
else:
    # Sort by win rate, then profit factor
    df_res = df_res.sort_values(["wr", "pf"], ascending=False)

    print("\n" + "="*85)
    print("  FULL ALTCOIN INVESTIGATION — TOP 30 BY WIN RATE")
    print("="*85)
    print(f"  {'Coin':<7} {'TF':<5} {'Strategy':<20} {'TP':<5} {'N':<5} {'TPD':<6} {'WR%':<8} {'PF':<7} {'TotalR'}")
    print("-"*85)
    for _, r in df_res.head(30).iterrows():
        print(f"  {r['coin']:<7} {r['tf']:<5} {r['strategy']:<20} {r['tp']:<5} "
              f"{r['n']:<5} {r['tpd']:<6} {r['wr']:.1f}%{'':<3} {r['pf']:<7} {r['total_r']}")
    print("="*85)

    # Best with >=8 trades and 1-4 tpd
    quality = df_res[(df_res["n"] >= 8) & (df_res["tpd"] <= 4.0) & (df_res["tpd"] >= 0.3)]
    if not quality.empty:
        print("\n  TOP 15 (min 8 trades, 0.3-4 per day):")
        print(f"  {'Coin':<7} {'TF':<5} {'Strategy':<20} {'TP':<5} {'N':<5} {'TPD':<6} {'WR%':<8} {'PF':<7} {'TotalR'}")
        print("-"*85)
        for _, r in quality.head(15).iterrows():
            print(f"  {r['coin']:<7} {r['tf']:<5} {r['strategy']:<20} {r['tp']:<5} "
                  f"{r['n']:<5} {r['tpd']:<6} {r['wr']:.1f}%{'':<3} {r['pf']:<7} {r['total_r']}")

    print("\n  BEST SINGLE RECOMMENDATION:")
    best = quality.sort_values(["wr", "pf"], ascending=False).iloc[0] if not quality.empty else df_res.iloc[0]
    print(f"  >>> {best['coin']} on {best['tf']} | {best['strategy']} | TP {best['tp']}x")
    print(f"  >>> Win Rate: {best['wr']}% | Profit Factor: {best['pf']} | {best['tpd']} trades/day")
