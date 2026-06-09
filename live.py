"""
Live engine — scans 15 coins every 1h candle, fires on best setup.
Includes: session filter, regime filter, trail stop, nightly/weekly review, trade journal.
"""
import time, sys
from datetime import datetime, timezone
from loguru import logger

from trader import (find_best_setup, quick_state, RISK_PCT, MAX_TRADES,
                    WATCHLIST, MIN_SCORE, TP_RATIO, in_session)
from executor import get_account_value, get_positions, get_mids, open_trade, get_price, update_sl
from journal import log_signal, log_trade_open, log_trade_close
from review  import (should_quiet, should_nightly_review, should_weekly_review,
                     nightly_review, weekly_review, version_push)
import tracker
import tg

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | {message}", colorize=True)
logger.add("bot.log", rotation="1 week", retention="4 weeks",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")

POLL   = 20       # seconds between polls
TF     = "1h"     # primary timeframe

# ── Internal state ─────────────────────────────────────────────────────────────
_open_trades    = {}
_cooldown_until = {}   # coin -> epoch-seconds when 1h SL cooldown expires
_nightly_done  = None
_weekly_done   = None
_version_done  = None




def _candle_ts():
    return (int(time.time()) // 3600) * 3600


def _check_trail(positions, account_val, mids=None):
    """Move SL to breakeven once trade reaches 1:1 R. Locks profit."""
    for coin, t in list(_open_trades.items()):
        if coin not in positions:
            continue
        price = float(mids[coin]) if mids and coin in mids else get_price(coin)
        entry, sl, tp, direction = t["entry"], t["sl"], t["tp"], t["dir"]

        # Don't interfere once tracker trail_tp has taken over (price past TP)
        past_tp = (direction == 1 and price >= tp) or (direction == -1 and price <= tp)
        if past_tp:
            continue

        # Use original R derived from TP (TP = entry ± 2R) so risk stays
        # correct even after SL is moved to breakeven (where entry-sl = 0)
        risk  = abs(tp - entry) / 2.0
        moved = abs(price - entry)

        # At 1:1 — move SL to breakeven
        if moved >= risk and t.get("trail_stage", 0) == 0:
            new_sl = entry
            _open_trades[coin]["sl"] = new_sl
            _open_trades[coin]["trail_stage"] = 1
            update_sl(coin, direction, t["size"], new_sl, entry=entry)
            tracker.update_trail(coin, new_sl, 1)
            logger.info(f"[{coin}] Trail → breakeven @ ${new_sl:.4f}")
            tg.dm_owner(f"🔒 {coin} SL moved to breakeven ${new_sl:.4f}")

        # At 1.5:1 — lock in 0.5R profit
        elif moved >= risk * 1.5 and t.get("trail_stage", 0) == 1:
            new_sl = entry + direction * risk * 0.5
            _open_trades[coin]["sl"] = new_sl
            _open_trades[coin]["trail_stage"] = 2
            update_sl(coin, direction, t["size"], new_sl, entry=entry)
            tracker.update_trail(coin, new_sl, 2)
            logger.info(f"[{coin}] Trail → 0.5R @ ${new_sl:.4f}")
            tg.dm_owner(f"📈 {coin} SL trailed to ${new_sl:.4f}")


def _check_closed(positions, account_val):
    """Detect closed trades and post results."""
    for coin in list(_open_trades.keys()):
        if coin not in positions:
            t            = _open_trades.pop(coin)
            exit_px      = get_price(coin)
            direction    = t["dir"]
            entry        = t["entry"]
            balance_before = t.get("balance_before", account_val)
            balance_after  = get_account_value()
            price_move   = abs(exit_px - entry) / entry * 100
            lev_pct      = price_move * t["leverage"] * (
                1 if (direction==1 and exit_px>entry) or (direction==-1 and exit_px<entry) else -1)
            hit = "tp" if (
                (direction==1 and exit_px>=t["tp"]) or
                (direction==-1 and exit_px<=t["tp"])
            ) else "sl"
            dur = (datetime.utcnow() - t["opened_at"]).total_seconds() / 3600

            logger.info(f"CLOSED {coin} | {'+' if lev_pct>0 else ''}{lev_pct:.1f}% | {hit.upper()}")
            log_trade_close(coin, exit_px, hit, lev_pct, dur)
            if hit == "sl":
                _cooldown_until[coin] = int(time.time()) + 3600
            max_adverse = t.get("max_adverse_pct", 0.0)
            stats = tracker.close_position(coin, exit_px, hit, lev_pct, balance_before, balance_after)

            # Private DM to owner
            tg.dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                              balance_before, balance_after,
                              stats or tracker.load_state().get("stats", {}),
                              max_adverse_pct=max_adverse, size=t.get("size", 0))


def _post_scan(states, account_val, positions, hour_utc):
    """Post compact scan summary to Telegram every candle."""
    from trader import in_session
    session = "🟢 Active" if in_session(hour_utc) else "🔴 Off-hours"
    lines   = []
    for s in states:
        if s is None: continue
        ssl_e  = "🟢" if s["ssl"]==1 else ("🔴" if s["ssl"]==-1 else "⚪️")
        macro_e = "↑" if s["macro"]==1 else ("↓" if s["macro"]==-1 else "→")
        sig    = f"  🔔<b>{s['best_score']}/13</b>" if s["best_score"] >= MIN_SCORE else ""
        lines.append(
            f"{ssl_e} <b>{s['coin']}</b> ${s['price']:.4f}  "
            f"RSI {s['rsi']:.0f}  ADX {s['adx']:.0f}  {macro_e}{sig}"
        )

    pos_txt = ""
    if positions:
        pos_txt = "\n\n📂 <b>Open positions:</b>\n"
        for coin, pos in positions.items():
            curr    = get_price(coin)
            side    = "LONG 🟢" if pos["direction"]==1 else "SHORT 🔴"
            raw_pct = (curr-pos["entry"])/pos["entry"]*100*pos["direction"]
            lev     = _open_trades.get(coin, {}).get("leverage", 1)
            lev_pct = raw_pct * lev
            pos_txt += f"  · {coin} {side}  {'+' if lev_pct>=0 else ''}{lev_pct:.1f}%\n"

    now = datetime.utcnow().strftime("%d %b · %H:%M UTC")
    tg.send(
        f"📡 <b>Scan</b>  {now}  |  {session}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) +
        (f"\n━━━━━━━━━━━━━━━━━━━━━━━{pos_txt}" if pos_txt else "")
    )


LOCKFILE = "/tmp/getsignalz.pid"

def _acquire_lock():
    """Exit if another instance is already running."""
    import os
    if os.path.exists(LOCKFILE):
        try:
            old_pid = int(open(LOCKFILE).read().strip())
            os.kill(old_pid, 0)          # check if process is alive
            logger.error(f"Another instance already running (PID {old_pid}). Exiting.")
            raise SystemExit(1)
        except ProcessLookupError:
            pass                         # stale lockfile — process is dead
    open(LOCKFILE, "w").write(str(os.getpid()))

def _release_lock():
    import os
    try:
        os.unlink(LOCKFILE)
    except FileNotFoundError:
        pass


def run():
    import os, signal as _signal
    global _nightly_done, _weekly_done, _version_done

    _acquire_lock()
    # Clean up lockfile on exit
    import atexit
    atexit.register(_release_lock)
    _signal.signal(_signal.SIGTERM, lambda *_: sys.exit(0))

    logger.info("="*55)
    logger.info("  GETSIGNALZ AI — ONLINE")
    logger.info(f"  Coins: {len(WATCHLIST)} | TF: {TF} | Min score: {MIN_SCORE}/13")
    logger.info(f"  Session: 07:00-22:00 UTC | Quiet: 02:00-04:00 UTC")
    logger.info("="*55)

    tracker.start()

    # Restore open trades from persistent tracker state on restart
    try:
        saved        = tracker.load_state().get("tracked", {})
        live         = get_positions()
        current_bal  = get_account_value()   # real balance NOW (best proxy for balance_before)
        for coin, t in saved.items():
            if coin in live:
                hl = live[coin]  # authoritative HL position data
                _open_trades[coin] = {
                    "dir":          hl["direction"],
                    "entry":        hl["entry"],        # HL blended entry (correct)
                    "sl":           t["sl"],            # our tracked SL order price
                    "tp":           t["tp"],            # our tracked TP order price
                    "size":         abs(hl["size"]),    # HL actual size
                    "leverage":     hl["leverage"],     # HL actual leverage
                    "opened_at":    datetime.fromisoformat(t["opened_at"]),
                    "trail_stage":  t.get("trail_stage", 0),
                    "signal_num":   t["signal_num"],
                    "balance_before": t.get("balance_before", current_bal),
                    "max_adverse_pct": t.get("max_adverse_pct", 0.0),
                }
                # Keep state.json in sync with HL actuals
                t["entry"]    = hl["entry"]
                t["size"]     = abs(hl["size"])
                t["leverage"] = hl["leverage"]
        if _open_trades:
            logger.info(f"Restored {len(_open_trades)} open trades from state: {list(_open_trades.keys())}")
            # Persist HL-synced values back to state.json
            synced = tracker.load_state()
            for coin, t in synced.get("tracked", {}).items():
                if coin in _open_trades:
                    t["entry"]    = _open_trades[coin]["entry"]
                    t["size"]     = _open_trades[coin]["size"]
                    t["leverage"] = _open_trades[coin]["leverage"]
            tracker.save_state(synced)

        # Detect positions that closed while bot was down
        for coin, t in saved.items():
            if coin not in live and coin not in _open_trades:
                try:
                    exit_px   = get_price(coin)
                    direction = t["dir"]
                    entry_px  = t["entry"]
                    tp_px     = t["tp"]
                    lev       = t.get("leverage", 1.0)
                    sign      = 1 if (direction == 1 and exit_px > entry_px) or (direction == -1 and exit_px < entry_px) else -1
                    lev_pct   = abs(exit_px - entry_px) / entry_px * 100 * lev * sign
                    hit       = "tp" if (direction == -1 and exit_px <= tp_px) or (direction == 1 and exit_px >= tp_px) else "sl"
                    logger.warning(f"Ghost close: {coin} closed while offline → {hit.upper()} ~${exit_px:.4f} ({lev_pct:+.1f}%)")
                    log_trade_close(coin, exit_px, hit, lev_pct, 0)
                    tracker.close_position(coin, exit_px, hit, lev_pct)
                    tg.dm_owner(f"⚠️ Ghost close: {coin} was closed while bot was offline. Approx exit ${exit_px:.4f}, classified as {hit.upper()} ({lev_pct:+.1f}%). Verify manually.")
                except Exception as ge:
                    logger.warning(f"Ghost close detection failed for {coin}: {ge}")
    except Exception as e:
        logger.warning(f"Could not restore trades: {e}")

    tg.dm_owner(f"⚡️ Bot started — {len(WATCHLIST)} pairs | restored {len(_open_trades)} open trades")

    last_candle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            h, m, wd = now_utc.hour, now_utc.minute, now_utc.weekday()

            # ── Quiet hours ──────────────────────────────────────────────────
            if should_quiet(h):
                logger.info("Quiet hours (2-4 AM) — resting")
                time.sleep(600)
                continue

            # ── Version push fallback (catches missed pushes after restarts) ──
            import os as _os
            if (h >= 4 and _version_done != now_utc.date()
                    and _os.path.exists("/root/trade/.night_report.json")):
                _version_done = now_utc.date()
                version_push()

            # ── Nightly review ───────────────────────────────────────────────
            if should_nightly_review(h, m) and _nightly_done != now_utc.date():
                _nightly_done = now_utc.date()
                nightly_review()

            # ── Weekly review ────────────────────────────────────────────────
            if should_weekly_review(wd, h, m) and _weekly_done != now_utc.isocalendar()[1]:
                _weekly_done = now_utc.isocalendar()[1]
                weekly_review()

            # ── Fetch positions + prices (shared singleton connection) ─────────
            positions   = get_positions()
            account_val = get_account_value()
            mids        = get_mids() if _open_trades else {}

            # ── Trail stop + closed trade detection ───────────────────────────
            _check_closed(positions, account_val)
            if _open_trades:
                _check_trail(positions, account_val, mids=mids)

            # ── New 1h candle? ────────────────────────────────────────────────
            now_ts = _candle_ts()
            if now_ts == last_candle:
                time.sleep(POLL)
                continue

            last_candle = now_ts
            candle_time = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%H:%M UTC")
            logger.info(f"━━━ Candle {candle_time} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # ── Scan all coins ────────────────────────────────────────────────
            states = []
            for coin in WATCHLIST:
                s = quick_state(coin, TF)
                if s:
                    states.append(s)
                    sig = f"  ◄ SIGNAL {s['best_score']}/13" if s["best_score"] >= MIN_SCORE else ""
                    logger.info(f"{coin:<6} ${s['price']:.4f}  RSI {s['rsi']:.0f}  "
                                f"ADX {s['adx']:.0f}  SSL {s['ssl']:+d}{sig}")
            # Scan summary stays in logs only — no channel post

            # ── Skip trading if off-session ───────────────────────────────────
            if not in_session(h):
                logger.info("Off-session — not trading")
                time.sleep(POLL)
                continue

            # ── Find and execute best setup ───────────────────────────────────
            # Use union of HL positions + locally tracked to prevent double-entry
            # when a just-opened trade isn't reflected in HL positions yet
            _now = int(time.time())
            already_open = {**positions, **{c: {} for c in _open_trades},
                            **{c: {} for c, exp in _cooldown_until.items() if _now < exp}}
            if len(already_open) < MAX_TRADES:
                best = find_best_setup(already_open, hour_utc=h)

                if best:
                    logger.info(
                        f"SIGNAL {best['coin']} {'LONG' if best['direction']==1 else 'SHORT'} "
                        f"score={best['score']}/13"
                    )
                    risk_usd = account_val * RISK_PCT

                    log_signal(best["coin"], best["direction"], best["score"],
                               best["reasons"], best["price"], best["sl"], best["tp"], TF,
                               adx=best.get("adx"), rsi=best.get("rsi"),
                               ssl=best.get("macro"), session_hour=h)

                    leverage = best.get("leverage", 10)

                    sig_num, sig_msg_id = tg.send_signal(
                        coin=best["coin"], direction=best["direction"],
                        score=best["score"], price=best["price"],
                        sl=best["sl"], tp=best["tp"],
                        reasons=best["reasons"],
                        account_val=account_val, risk_usd=risk_usd,
                        tf=TF, leverage=leverage,
                    )
                    result = open_trade(
                        coin=best["coin"], direction=best["direction"],
                        risk_usd=risk_usd, sl_price=best["sl"], tp_price=best["tp"],
                        leverage=leverage, tp_ratio=TP_RATIO,
                    )

                    if result:
                        coin = best["coin"]
                        # Use HL's actual leverage (account setting may differ from trader calc)
                        hl_pos = get_positions().get(coin, {})
                        actual_leverage = hl_pos.get("leverage", leverage)
                        actual_entry    = hl_pos.get("entry", result["entry"])
                        actual_size     = abs(hl_pos.get("size", result["size"]))

                        _open_trades[coin] = {
                            "dir": best["direction"], "entry": actual_entry,
                            "sl": result["sl"], "tp": result["tp"],
                            "size": actual_size, "leverage": actual_leverage,
                            "opened_at": datetime.utcnow(), "trail_stage": 0,
                            "signal_num": sig_num, "balance_before": account_val,
                            "max_adverse_pct": 0.0,
                        }
                        log_trade_open(coin, best["direction"], actual_entry,
                                       result["sl"], result["tp"], actual_size, actual_leverage)

                        tracker.register_position(
                            coin=coin, direction=best["direction"],
                            entry=actual_entry, sl=result["sl"], tp=result["tp"],
                            size=actual_size, leverage=actual_leverage,
                            signal_num=sig_num, signal_msg_id=sig_msg_id,
                            balance_before=account_val,
                        )
                else:
                    logger.info("No qualifying setup this candle")

        except KeyboardInterrupt:
            logger.info("Bot stopped")
            tg.send("⛔️ <b>Bot stopped</b>")
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            tg.send_error(str(e))

        time.sleep(POLL)


if __name__ == "__main__":
    run()
