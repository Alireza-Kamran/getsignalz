"""
Telegram channel integration.
ALL percentages shown are LEVERAGED — never raw price movement.
"""
import requests, os
from datetime import datetime

from config import TELEGRAM_TOKEN as TOKEN, TELEGRAM_CHANNEL as CHANNEL, TELEGRAM_OWNER_ID as OWNER_ID
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

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
        return d["result"]["message_id"] if d.get("ok") else None
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


def dm_owner(text: str):
    try:
        requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": OWNER_ID, "text": text, "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        print(f"[TG] DM failed: {e}")


# ── Signal post — single message, three states ────────────────────────────────

def send_signal(coin, direction, score, price, sl, tp, reasons,
                account_val, risk_usd, tf="1h", leverage=10):
    """Post the signal as 'waiting for entry'. Returns (sig_num, msg_id)."""
    side     = "LONG 🟢" if direction == 1 else "SHORT 🔴"

    sl_pct   = abs(price - sl) / price * 100
    tp_pct   = abs(tp - price) / price * 100
    lev_gain = round(tp_pct * leverage, 1)
    lev_loss = round(sl_pct * leverage, 1)
    rr       = round(lev_gain / lev_loss, 1) if lev_loss > 0 else 2.0

    reasons_txt = "\n".join(f"  ✅ {r}" for r in reasons)
    num = _next_signal_num()

    msg = (
        f"<b>{coin} {side}  #Signal{num}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>⏳ WAITING FOR ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Score: {score}/14  ·  {tf}  ·  <b>{leverage}x</b>\n"
        f"💰 Entry:  <code>${price:.5g}</code>\n"
        f"🛑 SL:     <code>${sl:.5g}</code>  →  <b>-{lev_loss:.1f}%</b>\n"
        f"🎯 TP:     <code>${tp:.5g}</code>  →  <b>+{lev_gain:.1f}%</b>\n"
        f"⚖️ R:R:    1 : {rr}\n\n"
        f"📋 <b>Confluence:</b>\n{reasons_txt}"
    )
    msg_id = send(msg)
    return num, msg_id


# ── Private DMs to owner ──────────────────────────────────────────────────────

def dm_trade_close(coin, direction, entry, exit_px, lev_pct, hit,
                   balance_before, balance_after, stats, max_adverse_pct=None, size=0):
    side    = "LONG 🟢" if direction == 1 else "SHORT 🔴"
    won     = hit == "tp"
    emoji   = "✅" if won else "❌"
    pct_s   = f"+{lev_pct:.1f}%" if lev_pct >= 0 else f"{lev_pct:.1f}%"
    # Dollar PnL from the trade itself (not balance diff, which can be skewed by other positions)
    raw_usd = (exit_px - entry) * direction * abs(size)
    usd_s   = f"  ({'+' if raw_usd >= 0 else '-'}${abs(raw_usd):.2f})"
    adv_s   = f"  📉 Max adverse: <b>{max_adverse_pct:.1f}%</b>\n" if max_adverse_pct is not None else ""
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
