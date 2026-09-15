"""
Live engine — scans 15 coins every 1h candle, fires on best setup.
Includes: session filter, regime filter, trail stop, nightly/weekly review, trade journal.
"""
import time, sys, json, os
from datetime import datetime, timezone, timedelta
from loguru import logger

from trader import (find_best_setup, quick_state, RISK_PCT, MAX_TRADES,
                    WATCHLIST, MIN_SCORE, TP_RATIO, TRAIL_R_STEP,
                    in_session, SESSION_START, SESSION_END)
from executor import (get_account_value, get_positions, get_mids, open_trade,
                      get_price, update_sl, get_close_fill, get_stop_price,
                      close_trade)
from journal import log_signal, log_trade_open, log_trade_close
from review  import (should_quiet, should_nightly_review, should_weekly_review,
                     nightly_review, weekly_review, version_push)
import tracker
import tg
import strategy2
from io_safe import atomic_write_json

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | {message}", colorize=True)
# Importing this module attaches the bot.log sink, so any test that imports it
# writes into the PRODUCTION log -- and a test's fixture prices then read back as
# real incidents. test_naked_stop asserts on a missing stop, so it was emitting
# "[BTC] NO STOP RESTING on a live position" at ERROR into the same file the
# nightly review reads for incidents. Four test files import live today; keying
# off the entrypoint name covers every future one without each having to opt in.
_IS_TEST_RUN = os.path.basename(sys.argv[0] or "").startswith("test_")
if not _IS_TEST_RUN:
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
_quiet_logged  = None   # date the quiet-hours notice was last logged

# ── Hang watchdog ─────────────────────────────────────────────────────────────
# On 2026-07-28 22:05 UTC the bot did nothing at all for 4 hours while systemd
# still reported it "active (running)": the main thread was parked in a
# Hyperliquid socket read that carried no timeout, so it never woke up and never
# raised. Scanning stopped, and had a position been open the S2 stop ratchet --
# its only exit path other than the resting stop -- would have been frozen too.
# executor.HTTP_TIMEOUT fixes that specific cause; this catches the whole class.
# If the loop has not ticked within its allowance the process exits hard and
# systemd (Restart=always, RestartSec=30) brings it back, restoring open trades
# from state.json exactly as it does after any other restart.
_last_tick     = time.time()
_tick_deadline = 900     # seconds of silence tolerated for an ordinary cycle

# The watchdog above only covers a loop that stalls while the PROCESS survives.
# On 2026-08-21 22:02 UTC the HOST went down and stayed down for 19h48m (uptime
# and ExecMainStartTimestamp both put the return at 2026-08-22 17:50; NRestarts
# was 0, so systemd never even saw a failure). An in-process watchdog cannot
# report that -- it died with the process. An ETH SHORT opened 76 seconds before
# the outage sat through all of it: the resting exchange stop still protected it,
# but the ratchet, which is where this strategy's entire edge lives, was frozen.
# The only thing that survives a host death is a timestamp on disk, so the beat
# is persisted here and compared against the clock at the next startup.
#
# Deliberately its own file rather than a key in state.json: tracker.save_state
# is a non-atomic read-modify-write (it silently erased OP on 2026-08-13), and
# writing a heartbeat through it every cycle would widen exactly that race.
HEARTBEAT_FILE = "/root/trade/.heartbeat"
_last_beat_write = 0.0

# ── Review latches, persisted ─────────────────────────────────────────────────
# _nightly_done / _weekly_done are module globals, so they reset to None on every
# restart -- and review._self_improve() ends in os.execv() (review.py:410), an
# IN-PLACE restart triggered from inside nightly_review() itself, during the very
# window that decides whether to run it.
#
# That was harmless while should_nightly_review() matched minute==0 exactly: a
# restart a few minutes later landed outside the window. Widening the window to
# the first 10 minutes on 2026-09-02 (needed because the reorder puts three more
# Hyperliquid round-trips ahead of the check) turns it into a re-entry bug --
# review runs, execs itself at 23:03, comes back with an empty latch, sees
# minute 3, and reviews again. So the latch has to outlive the process.
#
# Its own file for the same reason as HEARTBEAT_FILE above: tracker.save_state is
# a non-atomic read-modify-write that has already silently erased one position.
REVIEW_LATCH_FILE = "/root/trade/.review_latch"


def _load_review_latches():
    """(nightly_date, weekly_isoweek) from disk; (None, None) if absent/corrupt."""
    try:
        with open(REVIEW_LATCH_FILE) as fh:
            raw = json.load(fh)
        night = raw.get("nightly")
        night = datetime.strptime(night, "%Y-%m-%d").date() if night else None
        week = raw.get("weekly")
        return night, (int(week) if week is not None else None)
    except Exception:
        return None, None


def _save_review_latches(nightly, weekly):
    """Persist the latches. Never raises -- a failure here must not stop the loop
    (it degrades to the old in-memory behaviour, it does not break trading)."""
    try:
        with open(REVIEW_LATCH_FILE, "w") as fh:
            json.dump({"nightly": nightly.isoformat() if nightly else None,
                       "weekly": weekly}, fh)
    except Exception as e:
        logger.warning(f"could not persist review latch: {e}")
# 30 min: far above an ordinary systemd bounce (RestartSec=30) and the nightly
# restart, far below the outage class this exists to catch. A nightly review
# that blocks the loop longer than this and is then restarted will also trip it
# -- correctly, because a blocked loop is not ratcheting either.
DOWNTIME_ALERT_SEC = 1800

# The two mechanisms above cover a stalled loop and a dead host. Neither covers
# the third way this bot goes blind, observed 2026-08-27 00:01-02:00+ UTC: the
# loop cycles normally, _beat() fires every pass, the heartbeat file stays
# fresh and systemd reports active -- but every Hyperliquid call times out, so
# get_positions() raises before the candle check is ever reached and NO SCAN
# COMPLETES. Every liveness signal the bot has says healthy while it is in fact
# not looking at the market at all. (Root cause that night was external: the
# whole 99.86.171.0/24 CloudFront edge serving both api.hyperliquid.xyz and
# api.hyperliquid-testnet.xyz was unreachable from this host for hours, while
# the rest of the internet resolved and connected fine.)
#
# tg.send_error() does fire on the Cycle error, but it collapses to
# "upstream gateway error x25 in the last 30 min" -- which reads as transient
# noise, not as "you have not seen a price in two hours". Hence a separate,
# explicitly-worded alert keyed on the thing that actually matters: time since
# a scan last COMPLETED, not time since the loop last ran.
#
# Deliberately does NOT restart the process. A restart cannot fix an unreachable
# API, and restarting into one is actively worse: the trade-restore block in
# run() needs get_positions() to succeed, and when it does not, _open_trades
# stays permanently empty and any open position is orphaned from the bot's own
# trailing/exit management for the life of that process. Alert, keep cycling,
# recover when the API does.
#
# 2h15m: scans are hourly, so this needs two consecutive misses to trip. It
# must also clear the longest legitimate scan-free stretch, which is the
# nightly review (ai_brain runs to BRAIN_TIMEOUT=3600s, ~1h10m with its cycle);
# quiet hours are excluded separately below rather than budgeted for here.
SCAN_STALE_ALERT_SEC = 8100
_last_scan_ok  = time.time()
_scan_alerted  = False


def _mark_scan_ok():
    """Record that a candle scan ran to completion. Clears any blind alert."""
    global _last_scan_ok, _scan_alerted
    _last_scan_ok = time.time()
    if _scan_alerted:
        _scan_alerted = False
        try:
            tg.dm_owner("✅ <b>Scanning recovered</b> — a candle scan completed. "
                        "The bot is reading the market again.")
        except Exception:
            pass
        logger.info("Scanning recovered — candle scan completed after blind period")


def _unscheduled_stale_seconds(t0, t1):
    """Scan-free seconds in [t0, t1] during which scanning was actually due.

    Quiet hours are a *scheduled* scan-free window, so counting them as
    staleness makes the detector report the schedule as an outage: with the
    last scan at 01:00 and the next due at 04:00, plain wall-clock staleness
    reads 3.0h against a 2.25h limit and fires every single night. Observed
    2026-08-28/29/30 -- a 🚨 DM followed by a ✅ recovery DM 56s later.

    The quiet window is derived from `should_quiet()` rather than re-stating
    2-4 here: this detector exists because a real outage was missed, and a
    threshold that drifts away from the gate it is supposed to model is how
    that happens again.
    """
    if t1 <= t0:
        return 0.0
    quiet = 0.0
    cur = datetime.fromtimestamp(t0, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    while cur.timestamp() < t1:
        nxt = cur + timedelta(hours=1)
        if should_quiet(cur.hour):
            quiet += max(0.0, min(t1, nxt.timestamp()) - max(t0, cur.timestamp()))
        cur = nxt
    return (t1 - t0) - quiet


def _check_scan_stale():
    """Alert once per episode if no scan has completed in SCAN_STALE_ALERT_SEC.

    Skipped during quiet hours, when not scanning is the intended behaviour,
    and quiet time is discounted from the staleness clock itself so the
    scheduled pause cannot age into a false alarm at 04:00.
    """
    global _scan_alerted
    if _scan_alerted:
        return
    # One clock read for both the gate and the arithmetic: reading the hour
    # from datetime.now() and the elapsed time from time.time() is two sources
    # that can disagree across a boundary, and it made this untestable.
    now = time.time()
    if should_quiet(datetime.fromtimestamp(now, tz=timezone.utc).hour):
        return
    stale = _unscheduled_stale_seconds(_last_scan_ok, now)
    if stale <= SCAN_STALE_ALERT_SEC:
        return

    _scan_alerted = True
    hrs = stale / 3600
    lines = [f"🚨 <b>Bot is blind</b> — no candle scan has completed in "
             f"{hrs:.1f}h (limit {SCAN_STALE_ALERT_SEC/3600:.1f}h).",
             "",
             "The process is alive and the loop is cycling, so the watchdog and "
             "the heartbeat both read healthy. Scanning is what stopped — "
             "usually every exchange API call failing.",
             ""]
    if _open_trades:
        lines.append(f"⚠️ {len(_open_trades)} position(s) OPEN and unmanaged: "
                     f"{', '.join(_open_trades)}")
        lines.append("The resting exchange stop still protects them, but the "
                     "ratchet cannot arm or advance while this lasts.")
    else:
        lines.append("Book is flat — scanning downtime only, no position at risk.")
    lines.append("")
    lines.append("Not restarting: a restart cannot reach a dead API and risks "
                 "orphaning open positions from the bot's exit management.")
    try:
        logger.error(f"Bot blind: no scan completed in {hrs:.1f}h")
    except Exception:
        pass
    try:
        tg.dm_owner("\n".join(lines))
    except Exception:
        pass


def _beat(allowance=900):
    """Mark the main loop alive. Widen `allowance` around known-slow work."""
    global _last_tick, _tick_deadline, _last_beat_write
    _last_tick     = time.time()
    _tick_deadline = allowance
    # Throttled: the loop beats every ~POLL seconds, the file needs far less.
    if _last_tick - _last_beat_write >= 60:
        _last_beat_write = _last_tick
        try:
            with open(HEARTBEAT_FILE, "w") as fh:
                fh.write(str(int(_last_tick)))
        except Exception:
            pass      # never let bookkeeping break the trading loop


def _downtime_since_last_beat():
    """Seconds since the main loop last beat, or None if there is no record.

    Measures 'how long was nothing managing the book', which is the question
    that matters -- it does not distinguish a dead host from a dead process,
    and should not, because the open positions cannot tell the difference.
    """
    try:
        with open(HEARTBEAT_FILE) as fh:
            return max(0.0, time.time() - float(fh.read().strip()))
    except Exception:
        return None


def _watchdog():
    import os
    while True:
        time.sleep(60)
        # Blind-bot check first: it is the failure mode the stall check below
        # cannot see, because a loop erroring every pass still ticks normally.
        # Never allowed to break the stall check that follows it.
        try:
            _check_scan_stale()
        except Exception:
            pass
        stale = time.time() - _last_tick
        if stale <= _tick_deadline:
            continue
        mins  = int(stale // 60)
        limit = int(_tick_deadline // 60)
        msg = (f"🚨 Watchdog: main loop stalled {mins} min "
               f"(limit {limit} min) — restarting the bot.")
        try:
            logger.error(msg)
        except Exception:
            pass
        try:
            tg.dm_owner(msg)
        except Exception:
            pass
        try:
            _release_lock()
        except Exception:
            pass
        os._exit(1)   # hard exit: a thread stuck in a syscall cannot be unwound




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


# A signalled setup whose order never filled must not keep re-signalling. The
# cooldown that already exists only fires on a stop-out, so a coin that cannot
# be entered at all stayed eligible and re-signalled every candle: NEAR did it
# twice on 2026-09-04 and would have continued for as long as its RSI stayed
# extended. Long enough to break the loop, short enough that a transient book
# does not cost a day.
ENTRY_FAIL_COOLDOWN_S = 6 * 3600


def _mark_entry_failed(coin, why=""):
    """Cool a coin down after an entry that did not fill, and persist it so a
    restart does not immediately retry."""
    expiry = int(time.time()) + ENTRY_FAIL_COOLDOWN_S
    _cooldown_until[coin] = expiry
    try:
        st = tracker.load_state()
        st.setdefault("cooldowns", {})[coin] = expiry
        tracker.save_state(st)
    except Exception:
        pass
    logger.warning(f"{coin}: entry did not fill{(' — ' + why) if why else ''} "
                   f"— cooled down for {ENTRY_FAIL_COOLDOWN_S // 3600}h, "
                   f"no channel post")


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

        # Exchange first, record second -- the reverse of what this used to do.
        # update_sl cancels the old stop before placing the new one and raises
        # if the placement is rejected, so writing state up front left the bot
        # believing in a stop that no order backed. Worse, trail_stage >= 1 also
        # drops the position out of the at_risk count that enforces MAX_TRADES,
        # so a naked position would stop consuming the risk budget too.
        # _check_trail_s2 already has this ordering; S1 is dead today
        # (S1_ENABLED = False) but must not be a trap when it comes back.
        try:
            update_sl(coin, direction, t["size"], new_sl, entry=entry)
        except Exception as e:
            logger.error(f"[{coin}] trail stop move failed — position "
                         f"UNPROTECTED until retry: {e}")
            if not t.get("naked_alerted"):
                t["naked_alerted"] = True
                tg.dm_owner(f"🚨 <b>{coin}</b>\nجابجایی استاپ ناموفق بود\n"
                            f"پوزیشن تا تلاش بعدی بدون استاپ است")
            continue
        t.pop("naked_alerted", None)
        _open_trades[coin]["sl"] = new_sl
        _open_trades[coin]["trail_stage"] = rung
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
            # update_sl cancels the resting TP and stop BEFORE placing the new
            # stop, so a failure here leaves the position naked. t["sl"] is
            # deliberately left untouched: `improves` stays true, so the next
            # poll (~20s) retries and re-protects it. Alert once per position
            # rather than every poll, because a persistent failure is the one
            # case where the bot is running an unhedged position silently.
            logger.error(f"[S2] {coin} stop ratchet failed — position "
                         f"UNPROTECTED until retry: {e}")
            if not t.get("naked_alerted"):
                t["naked_alerted"] = True
                tg.dm_owner(
                    f"🚨 <b>[S2] {coin}</b>\n"
                    f"جابجایی استاپ ناموفق بود\n"
                    f"پوزیشن تا تلاش بعدی بدون استاپ است\n"
                    f"<code>{tg.esc(str(e)[:120])}</code>"
                )
            continue

        t.pop("naked_alerted", None)

        t["sl"] = new_sl
        t["locked_r"] = locked
        tracker.update_trail(coin, new_sl, 0, locked_r=locked)
        logger.info(f"[S2] {coin} stop -> +{locked:g}R  ${new_sl:.5g}")
        tg.dm_owner(
            f"🔒 <b>[S2] {coin}</b>\n"
            f"استاپ قفل شد روی <b>+{locked:g}R</b>\n"
            f"قیمت استاپ: <code>${new_sl:.5g}</code>\n"
            f"<i>از اینجا به بعد ضرر ممکن نیست</i>"
        )


# ── Naked-position detection ─────────────────────────────────────────────────
# One check per coin per 5 minutes. frontend_open_orders is rate-limited and the
# main loop runs every POLL=20s, so checking every tick would spend the whole
# budget on a question whose answer changes only when an order moves.
STOP_VERIFY_INTERVAL_S = 300
_stop_verified_at = {}


def _verify_stops(positions):
    """Confirm every open position actually has a stop resting on the exchange.

    Nothing did this. get_stop_price() existed for exactly this purpose and was
    called from one place -- _reconcile_stop, on restart only -- where it could
    detect a stop at the WRONG price but never a MISSING one, because a failed
    read and an absent order were both None.

    The gap this closes: open_trade's bracket stop could be rejected while the
    entry had already filled, and the position was then carried with the bot
    believing in a stop no order backed. open_trade now aborts on that, but the
    stop can also vanish afterwards -- a cancel that raced the ratchet, a manual
    intervention, a venue-side expiry -- and this is the only thing that looks.

    Never raises: this runs inside the position-management block, and a failure
    here must not stop _check_closed or the ratchet from running.
    """
    now = time.time()
    for coin, t in list(_open_trades.items()):
        if coin not in positions:
            continue
        if now - _stop_verified_at.get(coin, 0) < STOP_VERIFY_INTERVAL_S:
            continue
        try:
            real_sl = get_stop_price(coin)
        except Exception as e:
            # Could not look. Explicitly NOT treated as "no stop".
            logger.debug(f"[{coin}] stop verify skipped: {e}")
            continue
        _stop_verified_at[coin] = now

        if real_sl is not None and abs(real_sl - t["sl"]) <= 0.005 * real_sl:
            if not t.get("stop_verified_once"):
                t["stop_verified_once"] = True
                logger.info(f"[{coin}] stop verified resting @ ${real_sl:.5g}")
            t.pop("naked_alerted", None)
            continue

        if real_sl is None:
            logger.error(f"[{coin}] NO STOP RESTING on a live position — "
                         f"replacing at ${t['sl']:.5g}")
            try:
                update_sl(coin, t["dir"], t["size"], t["sl"], entry=t.get("entry"))
            except Exception as e:
                logger.error(f"[{coin}] stop replacement failed: {e}")
                if not t.get("naked_alerted"):
                    t["naked_alerted"] = True
                    tg.dm_owner(
                        f"🚨 <b>{coin} بدون استاپ است</b>\n"
                        f"پوزیشن باز است ولی هیچ استاپی روی صرافی نیست\n"
                        f"تلاش برای گذاشتن دوباره ناموفق بود\n"
                        f"<code>{tg.esc(str(e)[:150])}</code>")
                continue
            t.pop("naked_alerted", None)
            tg.dm_owner(f"⚠️ <b>{coin}</b>\nاستاپ روی صرافی نبود و دوباره گذاشته شد\n"
                        f"<code>${t['sl']:.5g}</code>")
            continue

        # Same 0.5% band as _reconcile_stop: absorbs the tick rounding update_sl
        # applies when it places a trigger (2684.3609 rests as 2684.4).
        if abs(real_sl - t["sl"]) > 0.005 * real_sl:
            logger.error(f"[{coin}] tracked stop ${t['sl']:.5g} disagrees with "
                         f"the resting stop ${real_sl:.5g} — adopting the exchange")
            old_sl = t["sl"]
            t["sl"] = real_sl
            R = t.get("R") or 0
            if R > 0:
                t["locked_r"] = max(0.0, round((real_sl - t["entry"]) * t["dir"] / R, 2))
            tg.dm_owner(f"⚠️ <b>{coin}</b>\nاختلاف استاپ\n"
                        f"ربات: <code>${old_sl:.5g}</code>\n"
                        f"صرافی: <code>${real_sl:.5g}</code>\n"
                        f"قیمت صرافی مبنا شد")
        t.pop("naked_alerted", None)


def _reconcile_stop(coin, restored, tracked):
    """Correct a restored stop that disagrees with the one resting on HL.

    state.json is only the bot's RECORD of where its stop is; the exchange holds
    the order that will actually fire. Believing a stale record is not harmless.
    _check_trail_s2's monotonic `improves` guard compares every candidate stop
    against this number, so a single bad value freezes the ratchet for the whole
    life of the position -- no error, no alert, just a trade that never locks in
    a cent. That is exactly what happened on 2026-08-23: test_ratchet.py wrote
    its fixture stop (sl=103.0, locked_r=3.0) over a live ETH short whose real
    stop was 2684.4, and nothing in the system noticed for a day and a half.

    The exchange price wins by definition. `locked_r` is then re-derived from it
    rather than trusted, because whatever corrupted one field had every chance
    to corrupt the other, and it is a pure function of entry/stop/R anyway.

    get_stop_price now RAISES when it cannot read and returns None only when the
    read succeeded and nothing is resting; both land in the except below and are
    skipped here, because _verify_stops owns the missing-stop case and runs every
    cycle anyway. This function's job is narrower: correct a restored stop that
    is present but at the wrong price. The 0.5% band absorbs the tick rounding
    update_sl applies when it places a trigger (2684.3609 goes on the book as
    2684.4). Never raises: a restore that dies here would orphan the position
    from the bot's own management, a strictly worse failure than the one guarded
    against.
    """
    try:
        real_sl = get_stop_price(coin)
        stale_sl = restored["sl"]
        if not real_sl or abs(real_sl - stale_sl) <= 0.005 * real_sl:
            return
        logger.error(f"[{coin}] tracked SL ${stale_sl:.5g} disagrees with the "
                     f"resting exchange stop ${real_sl:.5g} — adopting the "
                     f"exchange price")
        restored["sl"] = tracked["sl"] = real_sl
        R = restored.get("R") or 0
        locked = ((real_sl - restored["entry"]) * restored["dir"] / R) if R > 0 else 0.0
        locked = max(0.0, round(locked, 2))
        restored["locked_r"] = tracked["locked_r"] = locked
        tg.dm_owner(
            f"⚠️ اختلاف استاپ\n"
            f"<b>{coin}</b>\n"
            f"استاپ ثبت شده در ربات:\n"
            f"<code>${stale_sl:.5g}</code>\n"
            f"استاپ واقعی روی صرافی:\n"
            f"<code>${real_sl:.5g}</code>\n"
            f"قیمت صرافی مبنا قرار گرفت\n"
            f"سود قفل شده: <b>+{locked:g}R</b>")
    except Exception as e:
        logger.warning(f"[{coin}] stop reconcile skipped: {e}")


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
        "exit": exit_px, "sl": t["sl"], "sl_orig": t.get("sl_orig", t["sl"]), "tp": t["tp"],
        "leverage": lev,
        "lev_pct": round(lev_pct, 2),   # with leverage  (what a signal shows)
        "raw_pct": round(raw_pct, 3),   # without leverage (pure price move)
        "result": hit, "duration_h": round(dur, 2),
        "opened_at": t["opened_at"].isoformat(),
        "closed_at": datetime.utcnow().isoformat(),
    })
    atomic_write_json(path, data, default=None)

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


# A stop that fills 97% of a position leaves the rest resting, and the residue
# is not zero -- so executor.get_positions() (`if sz != 0`) still reports the
# coin as open and _check_closed() below, which closes a trade only when the
# coin is ABSENT from that dict, never fires. Fraction of the size we opened,
# not a dollar amount: the threshold has to mean the same thing on BTC and on
# DOGE, and only the ratio does.
DUST_FRACTION = 0.10
# Between dust and whole is the dangerous middle: a real position remains, and
# because the stop order was consumed by the partial fill, nothing is resting
# under it. Too consequential to resolve unattended, so it escalates instead.
PARTIAL_ALERT_FRACTION = 0.90


def _num(v, default=0.0):
    """float(v) that survives a null. See tracker.py:298 -- `.get(k, d)` returns
    None when the key EXISTS holding null, so the default never fires and the
    arithmetic downstream raises. Reached here through records written by hand.
    """
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _reconcile_dust(positions):
    """Treat a position the exchange has all-but-closed as closed.

    Hyperliquid's "Stop Market" is really a stop-limit with a ~0.3% band, filled
    IOC. When the resting book inside that band is thinner than the order, it
    fills what it can and the remainder is cancelled with the position still
    open. BTC 2026-09-03 19:21:24 is the first instance in 19 trades: the stop
    swept five levels (83472 -> 83514) for 0.00473 of 0.00486 and left 0.00013,
    worth $10.74. Every earlier close in the record filled to exactly zero,
    which is why `sz != 0` had never been wrong before.

    The cost of missing it is not the $10. The trade holds a MAX_TRADES slot
    forever (the bot ran at an effective MAX_TRADES=1 for 6.7 hours), its -1.12R
    never reaches the journal so every published statistic is overstated, and
    the residue has no stop under it -- while the report still describes a $400
    position protected at -1.00R.

    Mutates `positions` so the callers below see the coin as gone.
    """
    for coin in list(_open_trades.keys()):
        try:
            if coin not in positions:
                continue          # already absent; _check_closed handles it
            t         = _open_trades[coin]
            opened_sz = abs(_num(t.get("size_orig")) or _num(t.get("size")))
            live_sz   = abs(_num(positions[coin].get("size")))
            if opened_sz <= 0 or live_sz <= 0:
                continue
            frac = live_sz / opened_sz
            if frac > PARTIAL_ALERT_FRACTION:
                continue          # whole position still there, nothing to do

            if frac > DUST_FRACTION:
                # Deliberately NOT auto-resolved. Re-arming a stop, or flattening
                # a position this size, is a trading decision on a leg the bot
                # cannot price without knowing why the fill stopped short.
                logger.error(
                    f"{coin}: PARTIAL CLOSE — {frac*100:.1f}% of the position "
                    f"remains ({live_sz} of {opened_sz}) and its stop was "
                    f"consumed by the fill. NOT auto-resolved.")
                tg.dm_owner(
                    f"‼️ <b>{tg.esc(coin)} partially closed</b>\n"
                    f"{frac*100:.1f}% still open ({live_sz} of {opened_sz}).\n"
                    f"The stop order was consumed by the partial fill, so the "
                    f"remainder is <b>unprotected</b>. Needs a manual decision.")
                continue

            # Dust. The stop fired; this is what it could not fill.
            logger.warning(
                f"{coin}: stop filled short — {live_sz} of {opened_sz} "
                f"({frac*100:.1f}%) left resting. Flattening the residue and "
                f"recording the trade as closed.")
            try:
                close_trade(coin)
            except Exception as e:
                # Non-fatal by design. The accounting below is correct either
                # way; an unflattened residue costs funding and would blend its
                # entry price into the next trade on this coin (0.03% on the
                # BTC case), which is worth a DM but not worth dropping the
                # close record over.
                logger.error(f"{coin}: could not flatten residue ({e})")
                tg.dm_owner(
                    f"⚠️ <b>{tg.esc(coin)}</b> stopped out leaving a residue of "
                    f"{live_sz} that could not be flattened: {tg.esc(str(e))}\n"
                    f"The trade is recorded as closed. Clear the residue "
                    f"manually or it will blend into the next {tg.esc(coin)} entry.")
            positions.pop(coin, None)
        except Exception as e:
            # One malformed record must not stop the others being reconciled,
            # and must never take the trading loop down. See analyze._naive_utc.
            logger.error(f"{coin}: dust reconcile failed ({e})")


def _exit_price(coin, t):
    """Price a closed trade actually exited at, falling back to the current mid.

    See executor.get_close_fill for why the mid is not good enough: it is up to
    POLL seconds stale at the moment the bot notices the position is gone, and
    that staleness systematically overstates losses.
    """
    try:
        opened, since = t.get("opened_at"), 0
        if opened is not None:
            # Accepts both shapes deliberately: _open_trades holds a datetime,
            # but state.json round-trips opened_at as an ISO string, and the
            # ghost-close path on startup reads straight from state. Calling
            # .tzinfo on the string raised AttributeError, which this function's
            # own except-clause swallowed into a silent mid-price fallback --
            # the one caller that most needs a real fill quietly never got one.
            if isinstance(opened, str):
                opened = datetime.fromisoformat(opened)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            since = int(opened.timestamp() * 1000)
        px = get_close_fill(coin, since)
        if px:
            return px
        logger.warning(f"{coin}: no closing fill found — using mid price")
    except Exception as e:
        logger.warning(f"{coin}: close-fill lookup failed ({e}) — using mid price")
    return get_price(coin)


def _check_closed(positions, account_val):
    """Detect closed trades and post results."""
    for coin in list(_open_trades.keys()):
        if coin not in positions:
            t            = _open_trades.pop(coin)
            exit_px      = _exit_price(coin, t)
            direction    = t["dir"]
            entry        = t["entry"]
            balance_before = t.get("balance_before", account_val)
            # get_account_value raises rather than returning a silent 0.0 (see
            # its docstring). The trade is already popped from _open_trades by
            # this point, so letting that propagate would drop the close record
            # entirely -- the journal, the stats and the channel message would
            # all be lost over a transient balance read. Balance is display-only
            # here; the P&L below is derived from fill prices.
            try:
                balance_after = get_account_value()
            except Exception as e:
                logger.warning(f"{coin}: balance read failed on close ({e}) "
                               f"— reporting with the pre-trade balance")
                balance_after = balance_before
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
            if hit == "sl" and lev_pct < 0:
                _expiry = int(time.time()) + 10800
                _cooldown_until[coin] = _expiry
                try:
                    _s = tracker.load_state()
                    _s.setdefault("cooldowns", {})[coin] = _expiry
                    tracker.save_state(_s)
                except Exception:
                    pass
            _pre_close = tracker.load_state().get("tracked", {}).get(coin, {})
            stats = tracker.close_position(coin, exit_px, hit, lev_pct, balance_before, balance_after)

            # Private DM to owner
            tg.dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                              balance_before, balance_after,
                              stats or tracker.load_state().get("stats", {}),
                              max_adverse_pct=_pre_close.get("max_adverse_pct", 0.0),
                              size=t.get("size", 0),
                              max_drawdown_pct=_pre_close.get("max_drawdown_pct", 0.0),
                              peak_roe_pct=_pre_close.get("peak_roe_pct", 0.0))


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
            if old_pid == os.getpid():
                # review._self_improve() restarts the bot with os.execv, which
                # keeps the PID and skips atexit, so the file still holds OUR
                # pid and os.kill(pid, 0) is trivially true. Until 2026-09-15
                # this branch exited against itself on every execv restart
                # ("Another instance already running (PID <own pid>)", 09-13
                # 23:29:35) and only systemd's Restart= brought the bot back,
                # 32 s later, with exit status 1.
                logger.info(f"Lockfile holds our own PID {old_pid} — "
                            f"in-place restart, keeping it")
            else:
                os.kill(old_pid, 0)      # check if process is alive
                logger.error(f"Another instance already running (PID {old_pid}). Exiting.")
                raise SystemExit(1)
        except ProcessLookupError:
            pass                         # stale lockfile — process is dead
        except ValueError:
            pass                         # corrupt lockfile — overwrite it
    open(LOCKFILE, "w").write(str(os.getpid()))

def _release_lock():
    """Remove the lockfile, but only if this process still owns it."""
    import os
    try:
        if int(open(LOCKFILE).read().strip()) != os.getpid():
            return          # a newer instance owns it now -- leave it
        os.unlink(LOCKFILE)
    except (FileNotFoundError, ValueError):
        pass


def run():
    import os, signal as _signal
    global _nightly_done, _weekly_done, _version_done, _quiet_logged

    _acquire_lock()
    # Clean up lockfile on exit
    import atexit
    atexit.register(_release_lock)
    _signal.signal(_signal.SIGTERM, lambda *_: sys.exit(0))

    # Restore the review latches before the loop can act on them, so a restart
    # inside the review window (review._self_improve() execv's itself) cannot
    # re-run a review that already ran today. See REVIEW_LATCH_FILE.
    _nightly_done, _weekly_done = _load_review_latches()
    if _nightly_done or _weekly_done is not None:
        logger.info(f"Review latches restored — nightly={_nightly_done} "
                    f"weekly(isoweek)={_weekly_done}")

    # Read BEFORE the first _beat() overwrites the file. This is the only
    # evidence a host-level outage leaves behind.
    _downtime = _downtime_since_last_beat()
    if _downtime is not None and _downtime >= DOWNTIME_ALERT_SEC:
        logger.warning(f"Bot was not managing the book for "
                       f"{_downtime/3600:.1f}h before this start")

    import threading
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    logger.info("Watchdog armed — self-restart if the main loop stalls")

    logger.info("="*55)
    logger.info("  GETSIGNAL AI — ONLINE")
    # Banner reports the LIVE engine first. It used to print only strategy 1
    # numbers, which stopped being true when S1 was disabled -- an operator
    # reading the log saw a session window the live engine no longer obeys.
    if S1_ENABLED:
        logger.info(f"  S1 structural: {len(WATCHLIST)} coins | 15m/1h/4h | "
                    f"min score {MIN_SCORE}/8 | session "
                    f"{SESSION_START:02d}:00-{SESSION_END:02d}:00 UTC")
    else:
        logger.info("  S1 structural: DISABLED")
    logger.info(f"  S2 mean-reversion: {len(strategy2.WATCHLIST)} coins | "
                f"{strategy2.TF} | RSI {strategy2.RSI_OVERSOLD}/"
                f"{strategy2.RSI_OVERBOUGHT} | ADX<{strategy2.MAX_ADX} | all hours")
    logger.info("  Quiet: 02:00-04:00 UTC (scanning only)")
    logger.info("="*55)

    tracker.start()

    # Restore open trades from persistent tracker state on restart.
    #
    # get_positions()/get_account_value() already retry internally (_hl_call,
    # ~15s of backoff) but that's not enough against a real HL outage: on
    # 2026-08-07 the testnet API 502'd continuously for ~6 minutes (08:12-
    # 08:18 UTC). A restart landing inside a window like that used to fail
    # this whole block once and give up -- if `saved` names a real open
    # position, _open_trades then stays permanently empty for the rest of
    # this process's life (nothing else ever re-populates it), silently
    # orphaning that position from the bot's own trailing/exit management
    # until it closes on its own. Only the resting exchange SL protects it
    # meanwhile. Retrying here across a few cycles' worth of time covers the
    # outages actually observed; a loud DM (instead of a log line only) on
    # final failure covers the rest, matching every other silent-failure fix
    # this project has made (self-learn cron, channel outage, dm_owner length).
    saved = tracker.load_state().get("tracked", {})
    live = current_bal = None
    for attempt in range(3):
        try:
            live        = get_positions()
            current_bal = get_account_value()
            break
        except Exception as e:
            if attempt < 2:
                logger.warning(f"Trade-restore fetch failed ({e}) — retry in 10s")
                time.sleep(10)
    if live is None:
        logger.warning("Could not restore trades: HL API unavailable after retries")
        if saved:
            tg.dm_owner(
                f"⚠️ Could not restore {len(saved)} tracked position(s) "
                f"({', '.join(saved)}) on startup — HL API unavailable after "
                f"retries. The exchange-side stop still protects them, but the "
                f"bot's own trailing/exit logic will not manage them until a "
                f"restart succeeds. Check manually.")
    else:
        try:
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
                        "strategy":     t.get("strategy", "S1"),
                        "sl_orig":      t.get("sl_orig", t["sl"]),
                        "locked_r":     t.get("locked_r", 0.0),
                        # Read BEFORE t["size"] is overwritten with the HL actual
                        # below. Falling back to t["size"] is only correct on the
                        # FIRST restart after a partial close -- after that the
                        # residue has already been written back as the size -- so
                        # the fallback is a migration for records opened before
                        # size_orig existed, not a substitute for it.
                        "size_orig":    _num(t.get("size_orig")) or _num(t.get("size")) or abs(hl["size"]),
                        "R":            abs(hl["entry"] - (t.get("sl_orig") or t["sl"])),
                    }
                    _reconcile_stop(coin, _open_trades[coin], t)
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
                        # sl and locked_r too, so a stop reconciled against the
                        # exchange above is repaired on disk rather than re-read
                        # stale on the next restart -- and so the dashboard's
                        # lock badge stops advertising a profit that is not
                        # actually protected by any resting order.
                        t["sl"]       = _open_trades[coin]["sl"]
                        # Backfill for records opened before size_orig existed.
                        # Written here so the migration survives; t["size"] has
                        # already been overwritten with the HL actual by now, so
                        # this is the last point the original is still known.
                        t["size_orig"] = _open_trades[coin]["size_orig"]
                        t["locked_r"] = _open_trades[coin]["locked_r"]
                tracker.save_state(synced)

            # Detect positions that closed while bot was down
            for coin, t in saved.items():
                if coin not in live and coin not in _open_trades:
                    try:
                        # Fills, not the mid. This position closed while the bot
                        # was DOWN, so the current mid can be hours of drift away
                        # from the price that actually traded -- the worst case
                        # of the staleness get_close_fill exists to remove, and
                        # it is written straight into the permanent journal and
                        # lifetime stats. Falls back to the mid only if the
                        # exchange reports no attributable closing fill.
                        exit_px   = _exit_price(coin, t)
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
    # A restart after a 30-second systemd bounce and a restart after a 19-hour
    # host outage used to send the owner the byte-identical "Bot started" line,
    # so the 2026-08-21 blackout was invisible until it was mined out of the
    # candle timestamps four nights later. Say how long the book was unmanaged,
    # and name the positions that sat through it.
    if _downtime is None:
        tg.dm_owner(f"⚡️ Bot started — {len(WATCHLIST)} pairs | "
                    f"restored {len(_open_trades)} open trades")
    elif _downtime < DOWNTIME_ALERT_SEC:
        tg.dm_owner(f"⚡️ Bot started — {len(WATCHLIST)} pairs | "
                    f"restored {len(_open_trades)} open trades | "
                    f"gap {_downtime/60:.0f}m")
    else:
        _gap = (f"{_downtime/3600:.1f}h" if _downtime >= 3600
                else f"{_downtime/60:.0f}m")
        _lines = [f"🚨 Bot resumed after {_gap} with nothing managing the book.",
                  "",
                  "The in-process watchdog cannot catch this: it stops when the "
                  "process or host does. Detected from the on-disk heartbeat.",
                  ""]
        if _open_trades:
            _lines.append(f"⚠️ {len(_open_trades)} position(s) were open "
                          f"throughout and got NO ratchet management for {_gap}:")
            for _c, _t in _open_trades.items():
                _side = "SHORT" if _t["dir"] == -1 else "LONG"
                _lines.append(f"  • {_c} {_side} @ ${_t['entry']:.4f} "
                              f"(stop ${_t['sl']:.4f})")
            _lines.append("")
            _lines.append("The resting exchange stop still protected them, but "
                          "any favourable excursion during the gap could not be "
                          "locked in. Check whether the trail should have armed.")
        else:
            _lines.append("No positions were open — scanning downtime only.")
        tg.dm_owner("\n".join(_lines))

    last_candle = 0

    while True:
        try:
            _beat()
            now_utc = datetime.now(timezone.utc)
            h, m, wd = now_utc.hour, now_utc.minute, now_utc.weekday()

            # ── Fetch positions + prices (shared singleton connection) ─────────
            positions   = get_positions()
            account_val = get_account_value()
            mids        = get_mids() if _open_trades else {}

            # ── Trail stop + closed trade detection ───────────────────────────
            # Deliberately ahead of the quiet-hours gate below: that rule pauses
            # *scanning*, not position management. Strategy 2 exits exclusively
            # through this ratchet, so freezing it 02:00-04:00 meant an open
            # trade could not lock in profit for two hours a night. Lost upside
            # rather than lost capital -- the resting exchange stop always sits
            # underneath -- but there is no reason to give it away.
            #
            # Deliberately ahead of the maintenance block below for the SAME
            # reason, found 2026-09-02. nightly_review() runs ai_brain and
            # _self_improve() INLINE in this loop, and it used to sit above this
            # point: measured over 41 nights the 23:00 candle started a median
            # 21s late but a p90 of 19.4 min and a worst case of 27.7 min, with
            # 14 nights over 5 min -- every other hour of the day sits at ~10s.
            # For all of that time _check_trail_s2 simply did not execute. Eleven
            # review-nights crossed an open position (70 min of frozen ratchet in
            # total, 63 of them the ETH SHORT of 08-21..08-25), and the ceiling is
            # far higher than the observed worst case: _beat(4800) below tolerates
            # 80 minutes and BRAIN_TIMEOUT is 3600s. The measured cost so far is
            # ~0, but this is the third time this exact hazard has been found --
            # the socket hang (07-28) and the quiet-hours gate (07-28) were the
            # other two -- and the rule it keeps teaching is that NOTHING blocking
            # may sit above position management.
            # Ahead of _check_closed because it is what makes _check_closed fire:
            # it removes all-but-closed positions from `positions`, and absence
            # from that dict is the only signal _check_closed reads. Ahead of
            # _check_trail_s2 for the same reason the ordering above matters --
            # ratcheting a residue would move a stop for a trade that is over.
            _reconcile_dust(positions)
            _check_closed(positions, account_val)
            if _open_trades:
                if S1_ENABLED:
                    _check_trail(positions, account_val, mids=mids)
                _check_trail_s2(positions, mids=mids)
                # After the ratchet, not before: a stop moved this cycle should
                # be verified as the value the ratchet just placed.
                _verify_stops(positions)

            # ── Blocking maintenance (runs AFTER the ratchet, see above) ───────
            # NOTE: the candle scan still sits BELOW this block, so the review
            # continues to delay *entries* by up to ~20 min on the 23:00 candle
            # (live-exercised once: SOL 2026-08-16 23:09:47, +2.48R). Moving the
            # scan above the review as well would also invalidate the budget
            # SCAN_STALE_ALERT_SEC=8100 is built on -- it is sized to clear
            # exactly this review -- and that detector was only stabilised on
            # 08-31 after a 3-of-4 false-alarm run. Left for a session that can
            # re-derive the threshold with it. Reported, not done.

            # ── Version push fallback (catches missed pushes after restarts) ──
            import os as _os
            if (h >= 4 and _version_done != now_utc.date()
                    and _os.path.exists("/root/trade/.night_report.json")):
                _version_done = now_utc.date()
                _beat(1800)      # git push can stall on the network
                version_push()

            # ── Nightly review ───────────────────────────────────────────────
            if should_nightly_review(h, m) and _nightly_done != now_utc.date():
                _nightly_done = now_utc.date()
                # Persist BEFORE the call: nightly_review() can os.execv() from
                # inside _self_improve(), and an in-memory-only latch dies there.
                _save_review_latches(_nightly_done, _weekly_done)
                _beat(4800)      # ai_brain runs to BRAIN_TIMEOUT=3600s
                logger.info("Nightly review starting (blocks the loop)")
                nightly_review()
                logger.info("Nightly review complete")
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
                _save_review_latches(_nightly_done, _weekly_done)
                _beat(1800)
                logger.info("Weekly review starting (blocks the loop)")
                weekly_review()
                logger.info("Weekly review complete")

            # ── Quiet hours ──────────────────────────────────────────────────
            if should_quiet(h):
                if _quiet_logged != now_utc.date():
                    _quiet_logged = now_utc.date()
                    logger.info("Quiet hours (2-4 AM) — scanning paused, "
                                "open positions still managed")
                time.sleep(POLL)
                continue

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

            # Liveness is marked HERE, on the first real market read of the
            # candle, and gated on `states` so it means "prices were actually
            # returned" rather than "the loop got this far". Marking it further
            # down instead let the off-session `continue` below (00:00-11:00
            # UTC) starve the clock for 11h a night, which alerted at 01:17 and
            # "recovered" at 11:01 on 08-28 and 08-29 — and delayed the REAL
            # 08-27 recovery notice by ~9h.
            if states:
                _mark_scan_ok()

            # ── Strategy 2: mean reversion (live, official) ───────────────────
            # Runs on its own risk budget and its own watchlist/timeframe, so it
            # neither blocks nor is blocked by the structural system below.
            # Promoted out of shadow mode 2026-07-26: signals now post to the
            # channel, register a live tracker message, and count in the main
            # journal and stats exactly like strategy 1. journal_s2.json is
            # still written alongside so the two strategies stay separable when
            # reviewing which one is carrying the record.
            try:
                s2_at_risk = sum(1 for c, t in _open_trades.items()
                                 if t.get("strategy") == "S2")
                if s2_at_risk < strategy2.MAX_TRADES:
                    s2_open = {**positions, **{c: {} for c in _open_trades},
                               **{c: {} for c, exp in _cooldown_until.items() if int(time.time()) < exp}}
                    s2_best = strategy2.find_setup(s2_open)
                    # Belt-and-suspenders: the cooldown was already threaded into
                    # s2_open so find_setup should have excluded it — but NEAR
                    # bypassed it on back-to-back candles (2026-09-04), burning
                    # two signal numbers. Check _cooldown_until directly here,
                    # before any log_signal or open_trade call, so a leaky
                    # open_positions exclusion in find_setup cannot reach the
                    # channel or the exchange.
                    if s2_best and _cooldown_until.get(s2_best["coin"], 0) > int(time.time()):
                        logger.info(f"[S2] {s2_best['coin']} on entry-fail cooldown — skipping")
                        s2_best = None
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
                        # strategy2's own constant, NOT trader.RISK_PCT — the
                        # latter is machine-rewritten nightly by review.py from
                        # S1 statistics. See strategy2.S2_RISK_PCT.
                        risk2 = account_val * strategy2.S2_RISK_PCT
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
                        # Journal every setup that fired, filled or not -- that
                        # record is what signal-quality analysis reads.
                        log_signal(c2, dir2, 0, reasons2, s2_best["entry"],
                                   s2_best["sl"], s2_best["tp"], strategy2.TF,
                                   adx=s2_best["adx"], rsi=s2_best["rsi"],
                                   ssl=None, session_hour=h,
                                   stretch=s2_best.get("stretch"))
                        if not res2:
                            # The CHANNEL is a different matter. This used to
                            # post unconditionally, before res2 was even
                            # checked, so subscribers saw two NEAR signals on
                            # 2026-09-04 for a trade that never opened -- and it
                            # burned a signal number each time.
                            _mark_entry_failed(c2)
                        if res2:
                            sig2, sig2_mid = tg.send_signal(
                                coin=c2, direction=dir2, score=0,
                                price=s2_best["entry"],
                                sl=s2_best["sl"], tp=s2_best["tp"],
                                reasons=reasons2, account_val=account_val,
                                risk_usd=risk2, tf=strategy2.TF,
                                leverage=s2_best["leverage"], strategy="S2",
                                trail_start_r=strategy2.TRAIL_START_R,
                            )
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
                                # See tracker.register_position: `size` tracks the
                                # live HL position, `size_orig` is what we opened.
                                "size_orig": size2,
                                "R": abs(entry2 - res2["sl"]),
                            }
                            log_trade_open(c2, dir2, entry2, res2["sl"],
                                           res2["tp"], size2, lev2, strategy="S2")
                            tracker.register_position(
                                coin=c2, direction=dir2, entry=entry2,
                                sl=res2["sl"], tp=res2["tp"], size=size2,
                                leverage=lev2, signal_num=sig2,
                                signal_msg_id=sig2_mid,
                                balance_before=account_val, strategy="S2",
                                sl_orig=res2["sl"],
                            )
                else:
                    # The capacity gate was a SILENT early-return for its whole
                    # life: when the book is full this branch skipped the entire
                    # S2 block, including the "scanned N coins -- no setup"
                    # heartbeat two levels down that exists precisely so a
                    # quiet scanner is distinguishable from a dead one. So the
                    # log recorded the same thing for "we looked and found
                    # nothing" and "we never looked" -- and the second case is
                    # the only one that costs a trade.
                    #
                    # Reconstructing the cost after the fact needs the position
                    # intervals joined against the scan stream (analyze._capacity
                    # does this, 2026-09-10: 33 of 918 scan-hours full, and the
                    # gate-qualifying rate in them was 4.3x baseline). That join
                    # only works because a SEPARATE loop happens to print RSI/ADX
                    # for every coin every hour; it is not a record this branch
                    # ever kept. Naming the holders and their age here makes the
                    # opportunity cost first-class going forward, and puts the
                    # age of a stalled position -- the ETH SHORT has held a slot
                    # for 156h with its stop still at -1.00R -- in the same line
                    # as the thing it is blocking.
                    held = []
                    for c, t in _open_trades.items():
                        if t.get("strategy") != "S2":
                            continue
                        opened = t.get("opened_at")
                        age = ""
                        if isinstance(opened, datetime):
                            age = f" {(datetime.utcnow() - opened).total_seconds() / 3600:.0f}h"
                        held.append(f"{c}{age}")
                    logger.info(
                        f"[S2] book full ({s2_at_risk}/{strategy2.MAX_TRADES}) "
                        f"— not scanning; holders: {', '.join(held) or 'unknown'}"
                    )
            except Exception as s2_err:
                logger.error(f"[S2] error: {s2_err}")

            # ── Observability for the coins the scan above cannot see ──────────
            # The scan at the top of this candle iterates `WATCHLIST`, imported
            # from trader.py -- the RETIRED S1 list of 12 -- while the engine
            # trades strategy2.WATCHLIST (20). Eight live coins (ADA, BNB, BTC,
            # FIL, LDO, NEAR, TIA, XLM) have therefore never printed a single
            # RSI/ADX line, and every report section mined from this log was
            # blind to them:
            #   - FEED HEALTH could not see NEAR, whose candles went stale on
            #     2026-09-04 (two signals an hour apart with byte-identical
            #     rsi/adx/stretch). That was caught by an exchange rejection at
            #     21:01, not by the table that exists to catch exactly it.
            #   - REALISED R BY FEED QUALITY silently dropped 6 of 19 closed
            #     trades -- 32% of the book, 5 of them losses, meanR -0.717,
            #     including all four BTC trades, the worst record we have --
            #     because `stale.get(coin)` returns None for an unlogged coin
            #     and the trade just falls out of the sample. The exclusion
            #     criterion was membership of a dead engine's watchlist, which
            #     has nothing to do with feed quality.
            #
            # Deliberately placed BELOW the S2 entry decision: this costs one
            # candle fetch per coin and must never sit between the signal and
            # the order, where every added second is signal-to-fill drift (see
            # the FIL sizing incident). The loop is idle for the rest of the
            # hour, so here it costs nothing.
            #
            # Indicators are computed inline rather than via strategy2.build_df
            # ON PURPOSE: build_df returns None when its own freshness guard
            # trips, so routing through it would emit no row for precisely the
            # coins whose feeds are worst -- the monitor would go blind exactly
            # where it needs to see. Staleness is the report's judgement to
            # make, not the fetch's to hide.
            #
            # Format matches analyze._SCAN_RE exactly, and reports real_close
            # rather than the polled mid, so a frozen CANDLE reads as frozen
            # (NEAR's mid moved 2.5% while its candle sat still).
            try:
                from indicators import fetch_candles as _fc, rsi as _rsi, adx as _adx
                _covered = set(WATCHLIST)
                for _c in strategy2.WATCHLIST:
                    if _c in _covered:
                        continue
                    try:
                        _df = _fc(_c, strategy2.TF, lookback_bars=600)
                        if _df is None or len(_df) < strategy2.ADX_LEN + 50:
                            logger.info(f"{_c:<6} no usable candles  [S2-only]")
                            continue
                        _pc = "real_close" if "real_close" in _df.columns else "close"
                        _r = _rsi(_df["real_close"], strategy2.RSI_LEN).iloc[-1]
                        _a = _adx(_df["high"], _df["low"], _df["close"],
                                  strategy2.ADX_LEN)[0].iloc[-1]
                        if _r != _r or _a != _a:      # NaN: not enough history
                            logger.info(f"{_c:<6} indicators unavailable  [S2-only]")
                            continue
                        logger.info(f"{_c:<6} ${float(_df[_pc].iloc[-1]):.4f}  "
                                    f"RSI {float(_r):.0f}  ADX {float(_a):.0f}"
                                    f"  [S2-only]")
                    except Exception as _ce:
                        logger.warning(f"{_c}: observability scan failed ({_ce})")
            except Exception as _oe:
                logger.error(f"[S2] observability scan error: {_oe}")

            # Strategy 2 runs BEFORE the session gate below, deliberately.
            # That gate is a strategy-1 inheritance: S1 was a structural system
            # where session liquidity plausibly mattered. S2 was validated on
            # every 1h bar, 24/7, and measuring it under the live 11-24 window
            # (20 coins, 205 days, fees, live ratchet semantics) gave 0.39
            # signals/day vs 0.53 with the gate lifted -- for no win-rate gain
            # (69.6% vs 67.0%, noise at this n). 0.39/day misses the >=1 signal
            # per 2 days this engine exists to deliver.

            # ── Skip trading if off-session (strategy 1 only) ───────────────────────────────────
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

                    # Order first, announce second. Announcing first posts a
                    # signal for a trade that may never open -- the same defect
                    # the S2 path had, which put two phantom NEAR signals in the
                    # channel on 2026-09-04. S1 is disabled today; this must not
                    # be waiting for it when it comes back.
                    result = open_trade(
                        coin=best["coin"], direction=best["direction"],
                        risk_usd=risk_usd, sl_price=best["sl"], tp_price=best["tp"],
                        leverage=leverage, tp_ratio=TP_RATIO,
                    )
                    if not result:
                        _mark_entry_failed(best["coin"])
                    sig_num = sig_msg_id = None
                    if result:
                        sig_num, sig_msg_id = tg.send_signal(
                            coin=best["coin"], direction=best["direction"],
                            score=best["score"], price=best["price"],
                            sl=best["sl"], tp=best["tp"],
                            reasons=best["reasons"],
                            account_val=account_val, risk_usd=risk_usd,
                            tf=best.get("tf", TF), leverage=leverage,
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
                            "size_orig": actual_size,
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

            # Redundant with the mark after the scan loop above (this line is
            # unreachable off-session, which is exactly why it could not be the
            # only one). Kept because reaching here is a strictly stronger
            # statement: the full candle, both engines, ran without raising.
            _mark_scan_ok()

        except KeyboardInterrupt:
            logger.info("Bot stopped")
            tg.send("⛔️ <b>ربات متوقف شد</b>\n⛔️ <b>Bot stopped</b>")
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            tg.send_error(str(e))

        time.sleep(POLL)


if __name__ == "__main__":
    run()
