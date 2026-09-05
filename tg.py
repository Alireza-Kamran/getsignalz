"""
Telegram channel integration.
ALL percentages shown are LEVERAGED — never raw price movement.
"""
import requests, os, re, time, html
from datetime import datetime

from config import TELEGRAM_TOKEN as TOKEN, TELEGRAM_CHANNEL as CHANNEL, TELEGRAM_OWNER_ID as OWNER_ID
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


def esc(text) -> str:
    """Escape free-form text (tracebacks, exception strings, exchange error
    bodies) before it goes inside a <code>/<b> block in a parse_mode=HTML
    message. traceback.format_exc() almost always contains '<module>' --
    without this, an error report can itself be rejected by Telegram's HTML
    parser ('can't parse entities'), exactly the failure mode error-reporting
    exists to avoid. Never call on text that already contains real tags."""
    return html.escape(str(text))


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
        f"<code>{esc(str(desc)[:120])}</code>\n\n"
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
        r = requests.post(f"{BASE_URL}/editMessageText", json={
            "chat_id": CHANNEL, "message_id": msg_id,
            "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        d = r.json()
        if not d.get("ok"):
            # Same silent-rejection class fixed in send()/dm_owner() on 2026-08-04
            # -- this call ignored the response body entirely, so a rejected edit
            # (e.g. "message not modified", or a channel outage) left no trace.
            if not note_channel_failure(d.get("description"), "edit"):
                print(f"[TG] edit rejected: {d.get('description')}")
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

def send_version_update(version, changed_files=None, highlights=None):
    """Announce a shipped version in the channel, short and bilingual.

    version_push has always pushed to GitHub and then told only the owner, so
    subscribers never saw that the bot was being improved. This is the public
    half; the owner DM keeps the param/code-change counts and full detail.
    """
    import brand
    return send(brand.version_update(version, changed_files, highlights))


def send_signal(coin, direction, score, price, sl, tp, reasons,
                account_val, risk_usd, tf="1h", leverage=10, strategy="S1",
                trail_start_r=None):
    """Post the signal as 'waiting for entry'. Returns (sig_num, msg_id)."""
    side     = "LONG" if direction == 1 else "SHORT"

    sl_pct   = abs(price - sl) / price * 100
    tp_pct   = abs(tp - price) / price * 100
    lev_gain = round(tp_pct * leverage, 1)
    lev_loss = round(sl_pct * leverage, 1)
    rr       = round(lev_gain / lev_loss, 1) if lev_loss > 0 else 2.0

    reasons_txt = "\n".join(f"  ✅ {r}" for r in reasons)
    num = _next_signal_num()
    # Strategy 2 has no confluence score -- its entries are threshold-based, so
    # printing "0/8" would read as a terrible signal rather than a different kind.
    import brand as _brand
    strat_name = _brand.strategy_name(strategy)
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

    import brand
    rows = [("Entry", brand.fmt_px(price)),
            ("Stop",  f"{brand.fmt_px(sl)}   -{lev_loss:.1f}%")]
    if strategy == "S2" and trail_start_r:
        # Strategy 2 does not exit at its take-profit and has not since
        # 2026-08-02: the stop ratchet arms below the TP and cancels it, so the
        # resting TP is a backstop against the bot dying mid-trade, not a target.
        # Advertising it would print a goal every trade is designed to miss.
        rows.append(("Arms at", f"+{trail_start_r:g}R"))
        rows.append(("Backstop", f"{brand.fmt_px(tp)}   (cancelled on arming)"))
        foot_fa = "خروج با استاپ متحرک، نه با تارگت ثابت"
        foot_en = "Exit is a trailing stop, not a fixed target"
    else:
        rows.append(("Target", f"{brand.fmt_px(tp)}   +{lev_gain:.1f}%"))
        rows.append(("R:R", f"1 : {rr}"))
        foot_fa = "تارگت و حد ضرر از قبل مشخص است"
        foot_en = "Target and stop are set in advance"

    msg = "\n".join([
        brand.mark_line(f"#Signal{num}"), "",
        "⏳ <b>در انتظار ورود</b>",
        "⏳ <b>Waiting for Entry</b>",
        brand.rule(),
        f"<b>{coin}  {side}  {leverage}×</b>  ·  <i>{strat_name}</i>",
        f"<i>{score_txt}{tf}</i>" if score_txt else f"<i>{tf}</i>",
        "",
        brand.numeric_block(rows),
        f"📋 <b>Confluence</b>\n{reasons_txt}",
        brand.rule(),
        f"🧠 {foot_fa}",
        f"🧠 {foot_en}",
    ])
    msg_id = send(msg)
    return num, msg_id


# ── Private DMs to owner ──────────────────────────────────────────────────────

def dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                   balance_before, balance_after, stats, max_adverse_pct=None, size=0,
                   max_drawdown_pct=None, peak_roe_pct=None):
    side    = "LONG 🟢" if direction == 1 else "SHORT 🔴"
    # Classify on realised P&L, never on which ORDER closed the trade. Same bug
    # class already fixed in analyze.full_report and result_card/_live_text
    # (both 2026-08-04): S2's ratchet cancels the take-profit at TRAIL_START_R,
    # so every S2 exit -- winners included -- fires the stop and arrives here
    # as hit="sl". `won = hit == "tp"` could therefore never be true for S2,
    # and this owner DM captioned AVAX +15.7%, ETH +17.4% and BTC +13.3% all
    # "SL HIT 🛑 / 💀" -- this call site was missed both previous times.
    won     = lev_pct > 0
    emoji   = "✅" if won else "❌"
    if hit == "tp":
        status_txt = "TP HIT 🎯"
    elif lev_pct > 0:
        status_txt = "TRAIL EXIT 📈"
    elif lev_pct == 0:
        status_txt = "BREAKEVEN ⚪️"
    else:
        status_txt = "STOP HIT 🛑"
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
        f"{status_txt}\n"
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


# ── Operational error reporting ──────────────────────────────────────────────
# These go to the OWNER, never to the channel, and are deduplicated.
#
# send_error used to call send(), i.e. post to the public channel, and live.py
# calls it from the main loop's catch-all. A transient upstream 502 -- which the
# bot already retries and recovers from without missing a trade -- therefore
# published a raw nginx HTML error page to subscribers. 58 of them went out
# between 2026-08-05 and 08-07. The channel is the product; it should carry
# signals and results, not the exception stream of the process producing them.
#
# Transient upstream failures are also not worth waking the owner for one at a
# time. They are counted and reported once per cooldown with an occurrence
# count, so a passing blip stays quiet while a sustained outage still escalates.
_TRANSIENT_MARKERS = (
    "502", "503", "504", "bad gateway", "service unavailable", "gateway time",
    "timed out", "timeout", "connection reset", "connection aborted",
    "temporarily unavailable", "max retries exceeded", "remote end closed",
)
_ERROR_COOLDOWN_S = 1800
_error_state = {}          # signature -> {"at": epoch, "n": count since last DM}


def _signature(text):
    """Collapse an error to a comparable shape: digits and quoted bodies vary
    between occurrences of what is really the same fault."""
    t = re.sub(r"\d+", "#", str(text))
    t = re.sub(r"\s+", " ", t)
    return t[:160]


def send_error(msg_text, force=False):
    """Report an operational error to the owner, deduplicated.

    force=True bypasses the cooldown for genuinely one-off, high-signal events.
    """
    text = str(msg_text)
    sig  = _signature(text)
    now  = time.time()
    st   = _error_state.setdefault(sig, {"at": 0.0, "n": 0})
    st["n"] += 1

    transient = any(m in text.lower() for m in _TRANSIENT_MARKERS)
    if not force and now - st["at"] < _ERROR_COOLDOWN_S:
        return                      # already reported recently; just keep counting

    repeats = st["n"]
    st.update(at=now, n=0)

    # An upstream gateway error says nothing useful in its HTML body.
    body = "upstream gateway error" if transient else text[:600]
    extra = f"\n<i>x{repeats} in the last {_ERROR_COOLDOWN_S // 60} min</i>" if repeats > 1 else ""
    dm_owner(f"⚠️ <b>Bot error</b>\n<code>{esc(body)}</code>{extra}")
