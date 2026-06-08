"""
Main loop — runs every hour, scans all coins, executes only the best setups.
All signals, results, and scans are posted to Telegram @GetSignalz.
"""
import time
import sys
from datetime import datetime
from loguru import logger
import pandas as pd

from trader import find_best_setup, scan_report, RISK_PCT, MAX_TRADES, WATCHLIST, build_df, score_setup
from executor import get_account_value, get_positions, open_trade, close_trade, get_price
import tg

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | {message}")
logger.add("trades.log", rotation="1 week", format="{time} | {level} | {message}")

# Track open trades locally for result reporting
_open_trades = {}


def run_cycle():
    logger.info("━━━ CYCLE START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    account_val = get_account_value()
    positions   = get_positions()
    logger.info(f"Account: ${account_val:.2f} | Open: {len(positions)}")

    # ── Report any closed positions ──
    for coin in list(_open_trades.keys()):
        if coin not in positions:
            t = _open_trades.pop(coin)
            exit_px = get_price(coin)
            direction = t["dir"]
            entry     = t["entry"]
            sl        = t["sl"]
            tp        = t["tp"]
            pnl_pct   = (exit_px - entry) / entry * 100 * direction
            pnl_usd   = t["size"] * abs(exit_px - entry)
            hit       = "tp" if (direction == 1 and exit_px >= tp) or (direction == -1 and exit_px <= tp) else "sl"
            duration  = (datetime.utcnow() - t["opened_at"]).seconds / 3600

            logger.info(f"Trade closed: {coin} | PnL ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
            tg.send_trade_result(coin, direction, entry, exit_px, sl, tp, pnl_usd, pnl_pct, hit, duration)

    # ── Scan all coins and build report ──
    scan_results = []
    for coin in WATCHLIST:
        try:
            df   = build_df(coin, "1h", 300)
            if df is None:
                continue
            last = df.iloc[-1]
            price = last["close"]
            rsi_v = last["rsi"]
            ssl_v = int(last["ssl"])
            ssl_label = {1: "BULL", -1: "BEAR", 0: "NEUT"}.get(ssl_v, "?")

            l_score, _, _, _ = score_setup(df, 1)
            s_score, _, _, _ = score_setup(df, -1)
            best_score = max(l_score, s_score)
            best_dir   = 1 if l_score >= s_score and l_score > 0 else (-1 if s_score > 0 else 0)

            scan_results.append({
                "coin": coin, "price": price, "rsi": rsi_v,
                "ssl_label": ssl_label, "best_score": best_score, "best_dir": best_dir,
            })
        except Exception as e:
            logger.warning(f"Scan error {coin}: {e}")

    # ── Post scan summary to Telegram every cycle ──
    try:
        tg.send_scan_summary(scan_results, account_val, positions)
    except Exception as e:
        logger.warning(f"TG scan post failed: {e}")

    if len(positions) >= MAX_TRADES:
        logger.info(f"Max positions reached — not opening new trades")
        return

    # ── Find best setup and trade ──
    best = find_best_setup(positions)

    if best is None:
        logger.info("No high-quality setup this cycle")
        return

    from trader import print_setup
    print_setup(best)

    risk_usd = account_val * RISK_PCT
    logger.info(f"Risking ${risk_usd:.2f}")

    # Post signal to Telegram before executing
    tg.send_signal(
        coin      = best["coin"],
        direction = best["direction"],
        score     = best["score"],
        price     = best["price"],
        sl        = best["sl"],
        tp        = best["tp"],
        reasons   = best["reasons"],
        account_val = account_val,
        risk_usd  = risk_usd,
        tf        = "1h",
    )

    result = open_trade(
        coin      = best["coin"],
        direction = best["direction"],
        risk_usd  = risk_usd,
        sl_price  = best["sl"],
        tp_price  = best["tp"],
    )

    if result:
        logger.info(f"Trade opened: {result}")
        _open_trades[best["coin"]] = {
            "dir":       best["direction"],
            "entry":     result["entry"],
            "sl":        best["sl"],
            "tp":        best["tp"],
            "size":      result["size"],
            "opened_at": datetime.utcnow(),
        }
    else:
        logger.error("Trade execution failed")
        tg.send_error(f"Trade execution failed for {best['coin']}")


def run_loop(interval_minutes=60):
    logger.info("="*55)
    logger.info("  TRADING BOT STARTED — @GetSignalz")
    logger.info(f"  Interval: {interval_minutes}min | Risk: {RISK_PCT*100:.0f}% | MaxPos: {MAX_TRADES}")
    logger.info("="*55)

    tg.send(f"⚡️ <b>Bot loop started</b> — scanning every {interval_minutes}min\nWatchlist: {' · '.join(WATCHLIST)}")

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            logger.info("Stopped")
            tg.send("⛔️ <b>Bot stopped manually</b>")
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            tg.send_error(str(e))

        next_run = pd.Timestamp.now() + pd.Timedelta(minutes=interval_minutes)
        logger.info(f"Next scan at {next_run.strftime('%H:%M')} — sleeping {interval_minutes}m")
        time.sleep(interval_minutes * 60)


def quick_scan():
    """Scan + post to Telegram without trading."""
    account_val = get_account_value()
    positions   = get_positions()
    scan_results = []

    for coin in WATCHLIST:
        try:
            df    = build_df(coin, "1h", 300)
            if df is None: continue
            last  = df.iloc[-1]
            ssl_v = int(last["ssl"])
            ssl_label = {1: "BULL", -1: "BEAR", 0: "NEUT"}.get(ssl_v, "?")
            l_score, _, _, _ = score_setup(df, 1)
            s_score, _, _, _ = score_setup(df, -1)
            best_score = max(l_score, s_score)
            best_dir   = 1 if l_score >= s_score and l_score > 0 else (-1 if s_score > 0 else 0)
            scan_results.append({
                "coin": coin, "price": last["close"], "rsi": last["rsi"],
                "ssl_label": ssl_label, "best_score": best_score, "best_dir": best_dir,
            })
            signal = " ◄ SIGNAL" if best_score >= 6 else ""
            logger.info(f"{coin:<6} ${last['close']:.4f}  RSI {last['rsi']:.1f}  {ssl_label}{signal}")
        except Exception as e:
            logger.warning(f"{coin}: {e}")

    tg.send_scan_summary(scan_results, account_val, positions)
    logger.info("Scan posted to Telegram")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        quick_scan()
    else:
        run_loop(interval_minutes=60)
