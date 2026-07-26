"""
Live engine — scans 15 coins every 1h candle, fires on best setup.
Includes: session filter, regime filter, trail stop, nightly/weekly review, trade journal.
"""
import time, sys, json
from datetime import datetime, timezone
from loguru import logger

from trader import (find_best_setup, quick_state, RISK_PCT, MAX_TRADES,
                    WATCHLIST, MIN_SCORE, TP_RATIO, TRAIL_R_STEP,
                    in_session, SESSION_START, SESSION_END)
from executor import get_account_value, get_positions, get_mids, open_trade, get_price, update_sl
from journal import log_signal, log_trade_open, log_trade_close
from review  import (should_quiet, should_nightly_review, should_weekly_review,
                     nightly_review, weekly_review, version_push)
import tracker
import tg
import strategy2

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | {message}", colorize=True)
logger.add("bot.log", rotation="1 week", retention="4 weeks",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")

POLL   = 20       # seconds between polls
TF     = "1h"     # primary timeframe

# Strategy 1 (structural liquidity-pool) is retired as of 2026-07-26. Measured
# over ~20 months it produced roughly 2 trades a month, and its profit was
# carried by a handful of outliers -- removing the best five coins turned the
# whole record slightly negative. Strategy 2 replaced it on every axis that
# matters here: ~19 trades/month, 66% win rate, -4.2% max drawdown, and every
# month in the sample positive.
#
# Left switchable rather than deleted: the scan code, its backtest harness and
# its measured history stay intact, so re-enabling is one flag if the
# mean-reversion engine ever needs a companion again.
S1_ENABLED = False

# ── Internal state ─────────────────────────────────────────────────────────────
_open_trades    = {}
_cooldown_until = {}   # coin -> epoch-seconds when 1h SL cooldown expires
_nightly_done  = None
_weekly_done   = None
_version_done  = None




def _candle_ts():
    return (int(time.time()) // 3600) * 3600


# Progressive risk-free ladder: every TRAIL_R_STEP of favourable excursion
# ratchets the stop up one rung, and rung n locks in (n-1) steps of profit --
# so the first rung is exactly breakeven and every rung after banks more.
# Unbounded, so a runner keeps locking gains instead of stalling.
#
# Replaced the old fixed 2-stage version (breakeven at 1R, +0.5R at 1.5R, then
# nothing until TP ~5R away) on 2026-07-26. Measured on the full watchlist, 4h,
# ~20 months of history, same entries and gates, only the exit differing:
#
#   exit rule        n   win  BE  loss   WR     acct    maxDD   ex-best-trade
#   fixed TP        17    4    0   13   23.5%  +6.49%  -6.97%   -0.87% (neg)
#   1.0R ladder     21    9    4    8   42.9%  +7.93%  -1.00%   <- in use
#   0.5R ladder     22   11    6    5   50.0%  +8.95%  -1.00%   +5.48%
#
# The old rule's entire profit rode on one trade (remove it and 20 months went
# negative); the ladder's median trade is itself positive (+0.25%). Losses fell
# 13 -> 5 because trades that ran into profit and reversed now exit at or above
# breakeven instead of giving the full R back.
#
# TRAIL_R_STEP is defined in trader.py (tracker.py needs it too).


def _check_trail(positions, account_val, mids=None):
    """Ratchet SL up one rung per TRAIL_R_STEP of favourable excursion."""
    for coin, t in list(_open_trades.items()):
        if coin not in positions:
            continue
        # Strategy 2 exits at a fixed 1R take-profit and was validated that way.
        # Ratcheting its stop would be a different system than the one measured,
        # and its TP sits at the very rung the first ratchet fires on, so the two
        # would race on the same bar.
        if t.get("strategy") == "S2":
            continue
        price = float(mids[coin]) if mids and coin in mids else get_price(coin)
        entry, sl, tp, direction = t["entry"], t["sl"], t["tp"], t["dir"]

        # Don't interfere once tracker trail_tp has taken over (price past TP)
        past_tp = (direction == 1 and price >= tp) or (direction == -1 and price <= tp)
        if past_tp:
            continue

        # Use original R derived from TP (TP = entry ± TP_RATIO*R) so risk stays
        # correct even after SL is moved to breakeven (where entry-sl = 0)
        risk  = abs(tp - entry) / TP_RATIO
        favor = (price - entry) * direction  # positive only when trade is in profit
        if risk <= 0:
            continue

        # trail_stage doubles as "highest rung reached", so a stop never walks
        # back down if price retraces after a rung fires.
        rung  = int(favor / (risk * TRAIL_R_STEP))
        stage = t.get("trail_stage", 0)
        if rung < 1 or rung <= stage:
            continue

        locked = (rung - 1) * TRAIL_R_STEP
        new_sl = entry + direction * risk * locked
        _open_trades[coin]["sl"] = new_sl
        _open_trades[coin]["trail_stage"] = rung
        update_sl(coin, direction, t["size"], new_sl, entry=entry)
        tracker.update_trail(coin, new_sl, rung)

        label = "breakeven" if locked == 0 else f"+{locked:g}R"
        logger.info(f"[{coin}] Trail → {label} @ ${new_sl:.4f} (rung {rung})")
        tg.dm_owner(
            f"{'🔒' if locked == 0 else '📈'} {coin} SL → {label} "
            f"<code>${new_sl:.4f}</code>"
        )


def _check_trail_s2(positions, mids=None):
    """Ratchet the stop on strategy-2 positions once they reach TRAIL_START_R.

    The first ratchet also cancels the resting take-profit -- update_sl() is
    called WITHOUT `entry`, which drops every reduce-only order including the
    TP, then places the new stop. Leaving the TP in place would simply close the
    trade at 1R and the ratchet would never do anything.

    The stop only ever moves further into profit (guarded below), so a position
    that reaches 1R cannot come back and lose.
    """
    for coin, t in list(_open_trades.items()):
        if t.get("strategy") != "S2" or coin not in positions:
            continue
        R = t.get("R") or 0
        if R <= 0:
            continue

        price = float(mids[coin]) if mids and coin in mids else get_price(coin)
        d     = t["dir"]
        mfe_r = (price - t["entry"]) * d / R
        if mfe_r < strategy2.TRAIL_START_R:
            continue

        rung   = int((mfe_r - strategy2.TRAIL_START_R) / strategy2.TRAIL_STEP_R)
        locked = strategy2.TRAIL_START_R + rung * strategy2.TRAIL_STEP_R
        new_sl = t["entry"] + d * locked * R

        improves = (new_sl > t["sl"]) if d == 1 else (new_sl < t["sl"])
        if not improves:
            continue

        try:
            update_sl(coin, d, t["size"], new_sl)   # no entry -> also cancels TP
        except Exception as e:
            logger.error(f"[S2] {coin} stop ratchet failed: {e}")
            continue

        t["sl"] = new_sl
        t["locked_r"] = locked
        logger.info(f"[S2] {coin} stop -> +{locked:g}R  ${new_sl:.5g}")
        tg.dm_owner(
            f"🔒 <b>[S2] {coin}</b>\n"
            f"استاپ قفل شد روی <b>+{locked:g}R</b>\n"
            f"قیمت استاپ: <code>${new_sl:.5g}</code>\n"
            f"<i>از اینجا به بعد ضرر ممکن نیست</i>"
        )


def _log_s2_close(coin, t, exit_px, lev_pct, hit, dur):
    """Mirror a strategy-2 close into its own journal.

    Written IN ADDITION to the main journal, not instead of it: S2 counts in the
    public record now, but keeping a separate per-strategy file is what makes it
    possible to answer "which strategy is actually carrying the results" later
    without re-deriving it from a mixed journal.
    """
    path = "/root/trade/journal_s2.json"
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {"trades": []}
    lev = t["leverage"] or 1
    raw_pct = lev_pct / lev            # price move alone, before leverage
    data["trades"].append({
        "coin": coin, "direction": t["dir"], "entry": t["entry"],
        "exit": exit_px, "sl": t["sl"], "tp": t["tp"],
        "leverage": lev,
        "lev_pct": round(lev_pct, 2),   # with leverage  (what a signal shows)
        "raw_pct": round(raw_pct, 3),   # without leverage (pure price move)
        "result": hit, "duration_h": round(dur, 2),
        "opened_at": t["opened_at"].isoformat(),
        "closed_at": datetime.utcnow().isoformat(),
    })
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    tg.dm_owner(_s2_report(header=(
        f"🧪 <b>[S2] {coin} closed {hit.upper()}</b>\n"
        f"با اهرم: <b>{'+' if lev_pct >= 0 else ''}{lev_pct:.2f}%</b>  "
        f"({lev:g}x)\n"
        f"بدون اهرم: <b>{'+' if raw_pct >= 0 else ''}{raw_pct:.3f}%</b>\n"
        f"مدت: {dur:.1f} ساعت\n"
    )))


def _s2_report(header=""):
    """Cumulative shadow-mode record for strategy 2, leveraged and unleveraged.

    Both are reported because they answer different questions and are routinely
    confused: the leveraged figure is what a signal message shows, while the
    unleveraged one is the actual price move. Neither is the account return --
    that is leveraged/20, since only RISK_PCT (1%) of the account backs a trade.
    """
    try:
        with open("/root/trade/journal_s2.json") as f:
            trades = json.load(f).get("trades", [])
    except Exception:
        trades = []

    if not trades:
        return (header + "\n📊 <b>آمار استراتژی ۲</b>\n"
                "هنوز هیچ معامله‌ای بسته نشده.\n"
                "<i>بک‌تست: وین ریت ۶۶.۴٪ · ۱۸.۹ ترید در ماه · "
                "افت حداکثر ۴.۲۳٪</i>")

    n      = len(trades)
    wins   = [t for t in trades if (t.get("lev_pct") or 0) > 0]
    losses = [t for t in trades if (t.get("lev_pct") or 0) < 0]
    lev_t  = sum(t.get("lev_pct") or 0 for t in trades)
    raw_t  = sum(t.get("raw_pct") or 0 for t in trades)
    wr     = len(wins) / n * 100

    def avg(rows, key):
        return sum(r.get(key) or 0 for r in rows) / len(rows) if rows else 0.0

    return (
        header +
        f"\n📊 <b>آمار استراتژی ۲ (سایه)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"معاملات: <b>{n}</b>  ·  برد {len(wins)} / باخت {len(losses)}  ·  "
        f"وین ریت <b>{wr:.0f}%</b>\n\n"
        f"<b>با اهرم:</b>\n"
        f"  جمع: <b>{'+' if lev_t >= 0 else ''}{lev_t:.2f}%</b>\n"
        f"  میانگین برد: +{avg(wins, 'lev_pct'):.2f}%  ·  "
        f"میانگین باخت: {avg(losses, 'lev_pct'):.2f}%\n\n"
        f"<b>بدون اهرم:</b>\n"
        f"  جمع: <b>{'+' if raw_t >= 0 else ''}{raw_t:.3f}%</b>\n"
        f"  میانگین برد: +{avg(wins, 'raw_pct'):.3f}%  ·  "
        f"میانگین باخت: {avg(losses, 'raw_pct'):.3f}%\n\n"
        f"<b>اثر روی حساب:</b> {'+' if lev_t >= 0 else ''}{lev_t / 20:.2f}%  "
        f"<i>(هر معامله ۱٪ حساب ریسک می‌کند)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>بک‌تست: وین ریت ۶۶.۴٪ · ۱۸.۹ در ماه · افت ۴.۲۳٪</i>"
    )


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

            if t.get("strategy") == "S2":
                _log_s2_close(coin, t, exit_px, lev_pct, hit, dur)

            tag = "[S2] " if t.get("strategy") == "S2" else ""
            logger.info(f"{tag}CLOSED {coin} | "
                        f"{'+' if lev_pct>0 else ''}{lev_pct:.1f}% | {hit.upper()}")
            log_trade_close(coin, exit_px, hit, lev_pct, dur)
            if hit == "sl":
                _expiry = int(time.time()) + 10800
                _cooldown_until[coin] = _expiry
                try:
                    _s = tracker.load_state()
                    _s.setdefault("cooldowns", {})[coin] = _expiry
                    tracker.save_state(_s)
                except Exception:
                    pass
            max_adverse = t.get("max_adverse_pct", 0.0)
            _st = tracker.load_state().get("closed_trades", [])
            _last = _st[-1] if _st else {}
            stats = tracker.close_position(coin, exit_px, hit, lev_pct, balance_before, balance_after)

            # Private DM to owner
            tg.dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                              balance_before, balance_after,
                              stats or tracker.load_state().get("stats", {}),
                              max_adverse_pct=max_adverse, size=t.get("size", 0),
                              max_drawdown_pct=_last.get("max_drawdown_pct"),
                              peak_roe_pct=_last.get("peak_roe_pct"))


def _post_scan(states, account_val, positions, hour_utc):
    """Post compact scan summary to Telegram every candle."""
    from trader import in_session
    session = "🟢 Active" if in_session(hour_utc) else "🔴 Off-hours"
    lines   = []
    for s in states:
        if s is None: continue
        ssl_e  = "🟢" if s["ssl"]==1 else ("🔴" if s["ssl"]==-1 else "⚪️")
        macro_e = "↑" if s["macro"]==1 else ("↓" if s["macro"]==-1 else "→")
        sig    = f"  🔔<b>{s['best_score']}/8</b>" if s["best_score"] >= MIN_SCORE else ""
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
    logger.info(f"  Coins: {len(WATCHLIST)} | TFs: 15m/1h/4h | Min score: {MIN_SCORE}/8")
    logger.info(f"  Session: {SESSION_START:02d}:00-{SESSION_END:02d}:00 UTC | Quiet: 02:00-04:00 UTC")
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

    try:
        _cd = tracker.load_state().get("cooldowns", {})
        _now_ts = int(time.time())
        for _c, _exp in _cd.items():
            if _now_ts < _exp:
                _cooldown_until[_c] = _exp
        if _cooldown_until:
            logger.info(f"Restored cooldowns: {list(_cooldown_until.keys())}")
    except Exception as _e:
        logger.warning(f"Could not restore cooldowns: {_e}")
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
                # Shadow-mode strategy 2 reports separately: its numbers are
                # deliberately kept out of the main review, which drives the
                # public win rate and Trust Score.
                try:
                    tg.dm_owner(_s2_report())
                except Exception as e:
                    logger.error(f"[S2] nightly report failed: {e}")

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
                if S1_ENABLED:
                    _check_trail(positions, account_val, mids=mids)
                _check_trail_s2(positions, mids=mids)

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
                    if s["best_score"] >= MIN_SCORE:
                        score_tag = f"  ◄ SIGNAL {s['best_score']}/8"
                    elif s["best_score"] > 0:
                        score_tag = f"  [{s['best_score']}/8]"
                    else:
                        score_tag = ""
                    logger.info(f"{coin:<6} ${s['price']:.4f}  RSI {s['rsi']:.0f}  "
                                f"ADX {s['adx']:.0f}  SSL {s['ssl']:+d}{score_tag}")
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
            # MAX_TRADES is a risk budget: only positions still risking capital
            # count against it. Free-rolling trades (trail_stage >= 1 -> SL locked
            # at/beyond entry) and cooldown markers must not block new entries.
            at_risk = sum(
                1 for c in set(positions) | set(_open_trades)
                if _open_trades.get(c, {}).get("trail_stage", 0) < 1
            )
            if S1_ENABLED and at_risk < MAX_TRADES:
                best = find_best_setup(already_open, hour_utc=h)

                if best:
                    logger.info(
                        f"SIGNAL {best['coin']} {'LONG' if best['direction']==1 else 'SHORT'} "
                        f"score={best['score']}/8 {best.get('tf', '1h')}"
                    )
                    risk_usd = account_val * RISK_PCT

                    log_signal(best["coin"], best["direction"], best["score"],
                               best["reasons"], best["price"], best["sl"], best["tp"], best.get("tf", TF),
                               adx=best.get("adx"), rsi=best.get("rsi"),
                               ssl=best.get("macro"), session_hour=h)

                    leverage = best.get("leverage", 10)

                    sig_num, sig_msg_id = tg.send_signal(
                        coin=best["coin"], direction=best["direction"],
                        score=best["score"], price=best["price"],
                        sl=best["sl"], tp=best["tp"],
                        reasons=best["reasons"],
                        account_val=account_val, risk_usd=risk_usd,
                        tf=best.get("tf", TF), leverage=leverage,
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
            elif not S1_ENABLED:
                logger.info("Strategy 1 disabled — mean-reversion only")

            # ── Strategy 2: mean reversion (live, official) ───────────────────
            # Runs on its own risk budget and its own watchlist/timeframe, so it
            # neither blocks nor is blocked by the structural system above.
            # Promoted out of shadow mode 2026-07-26: signals now post to the
            # channel, register a live tracker message, and count in the main
            # journal and stats exactly like strategy 1. journal_s2.json is
            # still written alongside so the two strategies stay separable when
            # reviewing which one is carrying the record.
            try:
                s2_at_risk = sum(1 for c, t in _open_trades.items()
                                 if t.get("strategy") == "S2")
                if s2_at_risk < strategy2.MAX_TRADES:
                    s2_open = {**positions, **{c: {} for c in _open_trades}}
                    s2_best = strategy2.find_setup(s2_open)
                    # Logged even when empty: a scanner that only speaks when it
                    # fires is indistinguishable from a broken one, and this runs
                    # unattended for weeks while shadow data accumulates.
                    if not s2_best:
                        logger.info(f"[S2] scanned {len(strategy2.WATCHLIST)} coins "
                                    f"— no mean-reversion setup")
                    if s2_best:
                        c2  = s2_best["coin"]
                        dir2 = s2_best["direction"]
                        logger.info(
                            f"[S2] SIGNAL {c2} {'LONG' if dir2 == 1 else 'SHORT'} "
                            f"rsi={s2_best['rsi']} adx={s2_best['adx']} "
                            f"stretch={s2_best['stretch']}"
                        )
                        risk2 = account_val * RISK_PCT
                        res2  = open_trade(
                            coin=c2, direction=dir2, risk_usd=risk2,
                            sl_price=s2_best["sl"], tp_price=s2_best["tp"],
                            leverage=s2_best["leverage"], tp_ratio=strategy2.TP_R,
                        )
                        reasons2 = [
                            f"RSI {s2_best['rsi']} "
                            f"({'oversold' if dir2 == 1 else 'overbought'})",
                            f"{abs(s2_best['stretch']):.1f} ATR from mean",
                            f"ADX {s2_best['adx']} (ranging, not trending)",
                        ]
                        log_signal(c2, dir2, 0, reasons2, s2_best["entry"],
                                   s2_best["sl"], s2_best["tp"], strategy2.TF,
                                   adx=s2_best["adx"], rsi=s2_best["rsi"],
                                   ssl=None, session_hour=h)
                        sig2, sig2_mid = tg.send_signal(
                            coin=c2, direction=dir2, score=0, price=s2_best["entry"],
                            sl=s2_best["sl"], tp=s2_best["tp"], reasons=reasons2,
                            account_val=account_val, risk_usd=risk2,
                            tf=strategy2.TF, leverage=s2_best["leverage"],
                            strategy="S2",
                        )
                        if res2:
                            hl2   = get_positions().get(c2, {})
                            entry2 = hl2.get("entry", res2["entry"])
                            size2  = abs(hl2.get("size", res2["size"]))
                            lev2   = hl2.get("leverage", s2_best["leverage"])
                            _open_trades[c2] = {
                                "dir": dir2, "entry": entry2,
                                "sl": res2["sl"], "tp": res2["tp"],
                                "size": size2, "leverage": lev2,
                                "opened_at": datetime.utcnow(), "trail_stage": 0,
                                "signal_num": sig2, "balance_before": account_val,
                                "max_adverse_pct": 0.0, "strategy": "S2",
                                # R is measured off the ORIGINAL stop, so the
                                # ladder keeps its reference once the stop moves.
                                "sl_orig": res2["sl"], "locked_r": 0.0,
                                "R": abs(entry2 - res2["sl"]),
                            }
                            log_trade_open(c2, dir2, entry2, res2["sl"],
                                           res2["tp"], size2, lev2)
                            tracker.register_position(
                                coin=c2, direction=dir2, entry=entry2,
                                sl=res2["sl"], tp=res2["tp"], size=size2,
                                leverage=lev2, signal_num=sig2,
                                signal_msg_id=sig2_mid,
                                balance_before=account_val, strategy="S2",
                            )
            except Exception as s2_err:
                logger.error(f"[S2] error: {s2_err}")

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
