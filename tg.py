"""
Telegram channel integration.
ALL percentages shown are LEVERAGED — never raw price movement.
"""
import requests, os, time
from datetime import datetime

from config import TELEGRAM_TOKEN as TOKEN, TELEGRAM_CHANNEL as CHANNEL, TELEGRAM_OWNER_ID as OWNER_ID
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


# ── Channel-outage escalation ────────────────────────────────────────────────
# The channel IS the product. When Telegram rejects a post at the CHAT level --
# the username stops resolving, or the bot loses its membership -- signals,
# result cards and the dashboard all vanish at once while the bot keeps trading
# perfectly happily, and the only trace is one identical line per refresh in
# bot.log. That is exactly what happened on 2026-08-03 22:01 UTC: '@GetSignalz'
# began returning "chat not found" and produced 194 identical lines in four
# hours, discovered only by a manual audit the next night. Same failure class as
# the silent self-learn cron of 2026-07-23 -- a thing that breaks quietly and is
# found late. The owner DM is a different chat and keeps working, so use it.
_CHANNEL_DOWN_MARKERS = (
    "chat not found", "bot was kicked", "bot is not a member",
    "not enough rights", "have no rights", "chat was upgraded",
    "user is deactivated", "chat_id is empty",
)
_CHANNEL_ALERT_COOLDOWN_S = 3600
_channel_alert = {"desc": None, "at": 0.0}


def note_channel_failure(desc, where=""):
    """Escalate a chat-level Telegram rejection to the owner, at most hourly.

    Returns True if this looked like a channel outage rather than a problem with
    one particular message, so callers can suppress their own per-attempt log
    line and stop the spam that would otherwise bury real errors.
    """
    d = (desc or "").lower()
    if not any(m in d for m in _CHANNEL_DOWN_MARKERS):
        return False
    now = time.time()
    if _channel_alert["desc"] == d and now - _channel_alert["at"] < _CHANNEL_ALERT_COOLDOWN_S:
        return True
    # Past the cooldown gate above, so this is either the first failure, a
    # DIFFERENT cause than last time, or the same outage still unfixed an hour
    # later. All three are worth a DM: the 2026-08-03 outage ran four hours
    # undetected, and an hourly nudge is cheap next to a dark channel.
    _channel_alert.update(desc=d, at=now)
    print(f"[TG] CHANNEL UNREACHABLE ({where}): {desc}")
    dm_owner(
        "🚨 کانال در دسترس نیست\n\n"
        "تلگرام ارسال به این کانال را رد می کند:\n"
        f"<code>{CHANNEL}</code>\n\n"
        "پیام خطا:\n"
        f"<code>{str(desc)[:120]}</code>\n\n"
        "سیگنال و کارت نتیجه و داشبورد ارسال نمی شود.\n"
        "ربات به معامله ادامه می دهد.\n\n"
        "لطفا نام کانال و عضویت ربات را بررسی کنید."
    )
    return True


_counter_file = "/root/trade/.signal_count"

def _next_signal_num():
    n = 1
    if os.path.exists(_counter_file):
        with open(_counter_file) as f:
            n = int(f.read().strip() or 1)
    with open(_counter_file, "w") as f:
        f.write(str(n + 1))
    return n


def send(text: str, parse_mode="HTML", disable_preview=True):
    try:
        r = requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": CHANNEL, "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }, timeout=10)
        d = r.json()
        if d.get("ok"):
            return d["result"]["message_id"]
        # Was silent before 2026-08-04: a rejected signal post returned None and
        # left no trace anywhere, so the channel could be down for hours with the
        # bot still trading and nothing to show for it.
        if not note_channel_failure(d.get("description"), "send"):
            print(f"[TG] send rejected: {d.get('description')}")
        return None
    except Exception as e:
        print(f"[TG] send failed: {e}")
        return None


def edit(msg_id, text: str):
    if not msg_id:
        return
    try:
        requests.post(f"{BASE_URL}/editMessageText", json={
            "chat_id": CHANNEL, "message_id": msg_id,
            "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        print(f"[TG] edit failed: {e}")


DM_LIMIT = 3900   # Telegram hard-rejects above 4096; leave room for the counter.


def _split_for_telegram(text, limit=DM_LIMIT):
    """Split on line boundaries so HTML tags, which never span a line here,
    stay balanced within each chunk."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        piece = line if not cur else cur + "\n" + line
        if len(piece) <= limit:
            cur = piece
            continue
        if cur:
            chunks.append(cur)
        # A single line longer than the limit is the only case we must cut
        # blind; hard-wrap it rather than lose it.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        cur = line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def dm_owner(text: str):
    """Send to the owner DM, splitting anything over Telegram's length limit.

    Silently dropped long messages before 2026-08-04: the nightly self-learn
    report is the whole visible output of a session, and at 6328 chars it was
    rejected with "message is too long" while this function swallowed the
    response and returned as if it had worked. Same silent-failure class as the
    channel outage fixed the same night -- a rejection nobody ever sees.
    """
    parts = _split_for_telegram(text)
    n = len(parts)
    ok = True
    for i, part in enumerate(parts, 1):
        body = part if n == 1 else f"{part}\n\n<i>({i}/{n})</i>"
        try:
            r = requests.post(f"{BASE_URL}/sendMessage", json={
                "chat_id": OWNER_ID, "text": body, "parse_mode": "HTML",
            }, timeout=10)
            d = r.json()
            if not d.get("ok"):
                ok = False
                print(f"[TG] DM rejected ({i}/{n}): {d.get('description')}")
        except Exception as e:
            ok = False
            print(f"[TG] DM failed ({i}/{n}): {e}")
    return ok


def dm_owner_file(path, caption=""):
    """Send a file to the owner DM. Used for per-trade backtest exports, which
    are far too long to survive Telegram's message length limit."""
    try:
        with open(path, "rb") as fh:
            r = requests.post(
                f"{BASE_URL}/sendDocument",
                data={"chat_id": OWNER_ID, "caption": caption[:1024],
                      "parse_mode": "HTML"},
                files={"document": fh},
                timeout=60,
            )
        ok = r.json().get("ok", False)
        if not ok:
            print(f"[TG] file send rejected: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[TG] file send failed: {e}")
        return False


# ── Signal post — single message, three states ────────────────────────────────

def send_signal(coin, direction, score, price, sl, tp, reasons,
                account_val, risk_usd, tf="1h", leverage=10, strategy="S1",
                trail_start_r=None):
    """Post the signal as 'waiting for entry'. Returns (sig_num, msg_id)."""
    side     = "LONG 🟢" if direction == 1 else "SHORT 🔴"

    sl_pct   = abs(price - sl) / price * 100
    tp_pct   = abs(tp - price) / price * 100
    lev_gain = round(tp_pct * leverage, 1)
    lev_loss = round(sl_pct * leverage, 1)
    rr       = round(lev_gain / lev_loss, 1) if lev_loss > 0 else 2.0

    reasons_txt = "\n".join(f"  ✅ {r}" for r in reasons)
    num = _next_signal_num()
    # Strategy 2 has no confluence score -- its entries are threshold-based, so
    # printing "0/8" would read as a terrible signal rather than a different kind.
    strat_name = "Mean-Reversion" if strategy == "S2" else "Liquidity-Pool"
    score_txt  = f"Score: {score}/8  ·  " if strategy == "S1" else ""

    # Strategy 2 does not exit at its take-profit and has not since 2026-08-02:
    # the stop ratchet arms BELOW the TP and cancels it, so the resting TP is a
    # backstop against the bot dying mid-trade, not a target. Advertising it as
    # "TP -> +X%, R:R 1:3" would print a goal every trade is designed to miss.
    # The number that actually describes the trade is the ratchet floor.
    if strategy == "S2" and trail_start_r:
        lock_pct = round(trail_start_r * lev_loss, 1)
        exit_block = (
            f"🎯 Exit:   trailing stop, arms at <b>+{trail_start_r:g}R</b>\n"
            f"🔒 Floor:  <b>+{lock_pct:.1f}%</b> once armed, then trails up\n"
            f"🛡️ Backstop TP: <code>${tp:.5g}</code>  <i>(cancelled on arming)</i>\n\n"
        )
    else:
        exit_block = (
            f"🎯 TP:     <code>${tp:.5g}</code>  →  <b>+{lev_gain:.1f}%</b>\n"
            f"⚖️ R:R:    1 : {rr}\n\n"
        )

    msg = (
        f"<b>{coin} {side}  #Signal{num}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>⏳ WAITING FOR ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 <i>{strat_name}</i>\n"
        f"📊 {score_txt}{tf}  ·  <b>{leverage}x</b>\n"
        f"💰 Entry:  <code>${price:.5g}</code>\n"
        f"🛑 SL:     <code>${sl:.5g}</code>  →  <b>-{lev_loss:.1f}%</b>\n"
        f"{exit_block}"
        f"📋 <b>Confluence:</b>\n{reasons_txt}"
    )
    msg_id = send(msg)
    return num, msg_id


# ── Private DMs to owner ──────────────────────────────────────────────────────

def dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                   balance_before, balance_after, stats, max_adverse_pct=None, size=0,
                   max_drawdown_pct=None, peak_roe_pct=None):
    side    = "LONG 🟢" if direction == 1 else "SHORT 🔴"
    won     = hit == "tp"
    emoji   = "✅" if won else "❌"
    pct_s   = f"+{lev_pct:.1f}%" if lev_pct >= 0 else f"{lev_pct:.1f}%"
    # Dollar PnL from the trade itself (not balance diff, which can be skewed by other positions)
    raw_usd = (exit_px - entry) * direction * abs(size)
    usd_s   = f"  ({'+' if raw_usd >= 0 else '-'}${abs(raw_usd):.2f})"
    # Peak + drawdown describe the ride; max-adverse alone said nothing about a
    # trade that ran far into profit and gave most of it back.
    parts = []
    if peak_roe_pct:
        parts.append(f"  📈 Peak:  <b>+{peak_roe_pct:.1f}%</b>")
    if max_adverse_pct is not None and max_adverse_pct < 0:
        parts.append(f"  📉 Max drawdown:  <b>{max_adverse_pct:.1f}%</b>")
    adv_s = ("\n".join(parts) + "\n") if parts else ""
    dm_owner(
        f"{emoji} <b>CLOSED — {coin} {side}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{'💹' if won else '💀'} Result:  <b>{pct_s}</b>{usd_s}\n"
        f"{'TP HIT 🎯' if won else 'SL HIT 🛑'}\n"
        f"{adv_s}"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Balance:  <b>${balance_after:.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Running stats:</b>\n"
        f"  {stats.get('wins',0)}W / {stats.get('losses',0)}L  ·  "
        f"WR: {stats.get('win_rate',0):.0f}%\n"
        f"  Total: <b>{'+' if stats.get('total_pct',0)>=0 else ''}"
        f"{stats.get('total_pct',0):.1f}%</b>"
    )


def send_error(msg_text):
    send(f"⚠️ <b>Bot Error</b>\n<code>{msg_text}</code>")
