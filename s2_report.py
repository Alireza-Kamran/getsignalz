"""
Per-trade backtest export for strategy 2, delivered to the owner DM as a file.

Every trade carries the fields needed to audit it individually rather than
trusting an aggregate: realised R:R, which order closed it, how far it ran
against the position before resolving (MAE), and how long it stayed open.
Aggregates hide exactly the things that matter -- a 66% win rate says nothing
about whether the winners survived a 0.9R drawdown first.

Usage:  python3 s2_report.py            # backtest + CSV + send to owner DM
        python3 s2_report.py --no-send  # write the CSV only
"""
import sys
import csv
import time
from datetime import datetime

import pandas as pd

import backtest2 as bt2
import strategy2 as s2
import tg

OUT = "/root/trade/s2_backtest_trades.csv"


def build_rows(coins=None, tf="1h", bars=5000, retries=3):
    """Backtest every coin, retrying transient fetch failures.

    A silently-skipped coin does not error -- it just contributes zero trades,
    and the report still looks complete. That actually happened: one run
    reported 128 trades and 66.4% WR, the next 138 and 65.9%, purely because a
    single coin's fetch had failed. Failures are retried and then raised, so a
    partial dataset can never be published as if it were the whole thing.
    """
    coins = coins or s2.WATCHLIST
    trades, failed = [], []
    for c in coins:
        for attempt in range(retries):
            try:
                trades += bt2.backtest_coin(c, tf, bars=bars)
                break
            except Exception as e:
                if attempt == retries - 1:
                    failed.append(f"{c}: {str(e)[:60]}")
                else:
                    time.sleep(2)
    if failed:
        raise RuntimeError(
            "refusing to build a partial report — these coins failed after "
            f"{retries} attempts: " + "; ".join(failed))

    rows = []
    for t in trades:
        entry, sl, tp, d = t["entry"], t["sl"], t["tp"], t["direction"]
        risk = abs(entry - sl)                    # 1R in price terms
        gross = bt2.r_pct(t)
        net = bt2.r_pct(t, bt2.TAKER_FEE)
        # Realised R:R -- how many R the trade actually returned, signed.
        rr = ((t["exit"] - entry) * d) / risk if risk else 0.0
        mae_r = (t.get("mae_abs", 0.0) / risk) if risk else 0.0
        dur_h = (pd.Timestamp(t["close_time"]) - pd.Timestamp(t["open_time"])).total_seconds() / 3600
        rows.append({
            "coin": t["coin"],
            "direction": "LONG" if d == 1 else "SHORT",
            "open_time": str(t["open_time"]),
            "close_time": str(t["close_time"]),
            "duration_h": round(dur_h, 2),
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp": round(tp, 6),
            "exit": round(t["exit"], 6),
            "sl_hit": "YES" if t["result"] == "sl" else "",
            "tp_hit": "YES" if t["result"] == "tp" else "",
            "realised_RR": round(rr, 3),
            "max_drawdown_R": round(mae_r, 3),
            "max_drawdown_pct": round(t.get("mae_abs", 0.0) / entry * 100, 4),
            "leverage": t["leverage"],
            "pct_with_leverage": round(gross, 3),
            "pct_no_leverage": round(gross / t["leverage"], 4),
            "pct_after_fees": round(net, 3),
            "account_impact_pct": round(net / 20, 4),
            "rsi_at_entry": t.get("rsi"),
            "adx_at_entry": t.get("adx"),
            "stretch_atr": t.get("stretch"),
        })
    rows.sort(key=lambda r: r["open_time"])
    return rows


def write_csv(rows, path=OUT):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def summarise(rows):
    df = pd.DataFrame(rows)
    n = len(df)
    tp = int((df["tp_hit"] == "YES").sum())
    sl = int((df["sl_hit"] == "YES").sum())
    wins = df[df["pct_after_fees"] > 0]
    losses = df[df["pct_after_fees"] <= 0]
    eq = df["account_impact_pct"].cumsum()
    max_dd = (eq - eq.cummax()).min()
    months = max((pd.to_datetime(df["open_time"]).max()
                  - pd.to_datetime(df["open_time"]).min()).days / 30.44, 0.1)
    return (
        f"📁 <b>گزارش کامل بک‌تست استراتژی ۲</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"معاملات: <b>{n}</b>  ·  TP خورده: <b>{tp}</b>  ·  SL خورده: <b>{sl}</b>\n"
        f"وین ریت: <b>{len(wins) / n * 100:.1f}%</b>  ·  "
        f"در ماه: <b>{n / months:.1f}</b>\n\n"
        f"<b>با اهرم:</b> جمع {df['pct_with_leverage'].sum():+.1f}%  ·  "
        f"میانگین {df['pct_with_leverage'].mean():+.2f}%\n"
        f"<b>بدون اهرم:</b> جمع {df['pct_no_leverage'].sum():+.2f}%  ·  "
        f"میانگین {df['pct_no_leverage'].mean():+.3f}%\n"
        f"<b>بعد کارمزد:</b> جمع {df['pct_after_fees'].sum():+.1f}% اهرمی  =  "
        f"<b>{df['account_impact_pct'].sum():+.2f}% حساب</b>\n\n"
        f"<b>R:R واقعی:</b> میانگین برد {wins['realised_RR'].mean():+.2f}R  ·  "
        f"میانگین باخت {losses['realised_RR'].mean():+.2f}R\n"
        f"<b>افت درون معامله (MAE):</b> میانگین {df['max_drawdown_R'].mean():.2f}R  ·  "
        f"بدترین {df['max_drawdown_R'].max():.2f}R\n"
        f"  در بردها: {wins['max_drawdown_R'].mean():.2f}R  ·  "
        f"در باخت‌ها: {losses['max_drawdown_R'].mean():.2f}R\n\n"
        f"<b>مدت باز بودن:</b> میانه {df['duration_h'].median():.1f} ساعت  ·  "
        f"میانگین {df['duration_h'].mean():.1f}  ·  حداکثر {df['duration_h'].max():.1f}\n"
        f"<b>افت حساب (پشت سر هم):</b> {max_dd:.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>بک‌تست ۲۰ کوین · ۱ ساعته · کارمزد لحاظ شده · صفر معامله زنده</i>"
    )


TXT = "/root/trade/s2_backtest_monthly.txt"


def _dur(h):
    """1.0 -> '1h', 0.5 -> '0.5h', 41.0 -> '41h'."""
    return f"{h:.1f}".rstrip("0").rstrip(".") + "h"


def _block(rows, title, out):
    """One month (or the all-time roll-up) with per-column totals underneath."""
    out.append(f"──── {title} ────")
    out.append(f"{'Symbol':<10}{'Dir':<6}{'Res':<5}{'Dur':>7}{'Lev':>7}"
               f"{'With Lev':>12}{'No Lev':>10}{'RR':>7}")
    for r in rows:
        out.append(
            f"{r['coin']:<10}{r['direction']:<6}"
            f"{('TP' if r['tp_hit'] else 'SL'):<5}"
            f"{_dur(r['duration_h']):>7}"
            f"{r['leverage']:>6.0f}x"
            f"{r['pct_with_leverage']:>+11.2f}%"
            f"{r['pct_no_leverage']:>+9.3f}%"
            f"{r['realised_RR']:>+7.1f}"
        )
    n    = len(rows)
    tp   = sum(1 for r in rows if r["tp_hit"])
    sl   = n - tp
    lev  = sum(r["pct_with_leverage"] for r in rows)
    nolv = sum(r["pct_no_leverage"] for r in rows)
    rr   = sum(r["realised_RR"] for r in rows)
    dur  = sum(r["duration_h"] for r in rows)
    out.append("-" * 74)
    out.append(
        f"{'TOTAL':<10}{'':<6}{f'{tp}/{sl}':<5}"
        f"{_dur(dur):>7}{'':>7}"
        f"{lev:>+11.2f}%{nolv:>+9.3f}%{rr:>+7.1f}"
    )
    out.append(
        f"  trades {n}  ·  TP {tp} / SL {sl}  ·  WR {tp / n * 100:.1f}%"
        f"  ·  avg RR {rr / n:+.3f}  ·  avg dur {_dur(dur / n)}"
    )
    out.append("")


def build_monthly_txt(rows, path=TXT):
    df = pd.DataFrame(rows)
    df["ym"] = pd.to_datetime(df["open_time"]).dt.to_period("M").astype(str)

    out = []
    out.append("=" * 74)
    out.append("  STRATEGY 2 (mean reversion) — BACKTEST TRADE REPORT")
    out.append(f"  {len(s2.WATCHLIST)} coins · {s2.TF} · RSI {s2.RSI_OVERSOLD}/"
               f"{s2.RSI_OVERBOUGHT} · ADX<{s2.MAX_ADX} · TP {s2.TP_R}R")
    out.append(f"  generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC · "
               f"percentages are gross (fees shown separately at end)")
    out.append("=" * 74)
    out.append("")

    for ym in sorted(df["ym"].unique()):
        _block([r for r, m in zip(rows, df["ym"]) if m == ym], ym, out)

    out.append("=" * 74)
    _block(rows, "ALL TIME", out)

    # Fee line kept apart from the per-trade columns above, which are gross.
    net = sum(r["pct_after_fees"] for r in rows)
    gross = sum(r["pct_with_leverage"] for r in rows)
    out.append("=" * 74)
    out.append(f"  GROSS (with leverage) : {gross:+.2f}%")
    out.append(f"  FEES                  : {gross - net:+.2f}%")
    out.append(f"  NET   (with leverage) : {net:+.2f}%")
    out.append("=" * 74)
    out.append("  Backtest only — zero live trades. Slippage not modelled.")

    text = "\n".join(out)
    with open(path, "w") as f:
        f.write(text)
    return path, text


if __name__ == "__main__":
    print("running strategy-2 backtest...")
    rows = build_rows()
    if not rows:
        print("no trades")
        sys.exit(1)
    csv_path = write_csv(rows)
    txt_path, txt = build_monthly_txt(rows)
    print(f"{len(rows)} trades -> {csv_path} , {txt_path}")

    df = pd.DataFrame(rows)
    tp = int((df["tp_hit"] == "YES").sum())
    cap = (
        f"📁 <b>گزارش ماه‌به‌ماه بک‌تست استراتژی ۲</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"معاملات: <b>{len(df)}</b>  ·  TP <b>{tp}</b> / SL <b>{len(df) - tp}</b>\n"
        f"وین ریت: <b>{tp / len(df) * 100:.1f}%</b>  ·  "
        f"میانگین RR: <b>{df['realised_RR'].mean():+.3f}</b>\n\n"
        f"<b>با اهرم:</b> {df['pct_with_leverage'].sum():+.1f}%  "
        f"(خالص بعد کارمزد <b>{df['pct_after_fees'].sum():+.1f}%</b>)\n"
        f"<b>بدون اهرم:</b> {df['pct_no_leverage'].sum():+.2f}%\n"
        f"<b>مجموع RR:</b> {df['realised_RR'].sum():+.0f}R\n\n"
        f"<i>فایل شامل هر ماه جدا با جمع هر ستون + جمع کل</i>"
    )
    print(txt[:1500])
    if "--no-send" not in sys.argv:
        ok1 = tg.dm_owner_file(txt_path, caption=cap)
        ok2 = tg.dm_owner_file(csv_path, caption="📊 همان داده به صورت CSV")
        print("sent to owner DM" if (ok1 and ok2) else "SEND FAILED")
