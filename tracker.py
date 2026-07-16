"""
Persistent live tracker.
- Posts a live message per open position, edits it every 60s
- Maintains a pinned dashboard with links + track record
- Saves all state to state.json so it survives restarts
"""
import threading, time, json, os, math
import requests
from datetime import datetime, timezone


def _px(x):
    """Round to HL order precision — same rounding used when placing orders."""
    if x == 0: return 0.0
    d = math.floor(math.log10(abs(x)))
    return round(x, min(-d + 4, 4))

from config import TELEGRAM_TOKEN as TOKEN, TELEGRAM_CHANNEL as CHANNEL, HYPERLIQUID_ACCOUNT as ACCOUNT
CHANNEL_USERNAME = CHANNEL.lstrip("@")
BASE     = f"https://api.telegram.org/bot{TOKEN}"
STATE_F  = "/root/trade/state.json"
TESTNET  = "https://api.hyperliquid-testnet.xyz"

_lock    = threading.Lock()
_running = False


# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    with _lock:
        if os.path.exists(STATE_F):
            with open(STATE_F) as f:
                return json.load(f)
    return {
        "dashboard_msg_id": None, "signal_count": 1,
        "tracked": {}, "closed_trades": [],
        "stats": {
            "total": 0, "wins": 0, "losses": 0, "total_pct": 0.0,
            "win_rate": 0.0, "peak_balance": 0.0,
            "max_drawdown_pct": 0.0, "current_drawdown_pct": 0.0
        }
    }


def save_state(s):
    with _lock:
        with open(STATE_F, "w") as f:
            json.dump(s, f, indent=2, default=str)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _send(text):
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id": CHANNEL, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True
    }, timeout=10)
    d = r.json()
    return d["result"]["message_id"] if d.get("ok") else None


def _edit(msg_id, text):
    if not msg_id:
        return
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}/editMessageText", json={
                "chat_id": CHANNEL, "message_id": msg_id,
                "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=10)
            d = r.json()
            if d.get("ok"):
                return
            if "not modified" in d.get("description", ""):
                return
            if d.get("error_code") == 429:
                retry_after = d.get("parameters", {}).get("retry_after", 5)
                time.sleep(min(retry_after, 10))
                continue
            print(f"[tracker] edit {msg_id} failed: {d.get('description')}")
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"[tracker] edit {msg_id} error: {e}")


def _pin(msg_id):
    requests.post(f"{BASE}/pinChatMessage", json={
        "chat_id": CHANNEL, "message_id": msg_id,
        "disable_notification": True
    }, timeout=10)


def _msg_link(msg_id):
    return f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"


# ── Live position message ─────────────────────────────────────────────────────

def _live_text(t, current_price, closed=False, close_result=None, final_pct=None,
               hl_roe=None, hl_pnl_usd=None, hl_leverage=None, hl_entry=None):
    """
    hl_roe, hl_pnl_usd, hl_leverage, hl_entry: live data from HL API.
    When provided they override local calculations so the display matches HL exactly.
    """
    coin      = t["coin"]
    direction = t["dir"]
    entry     = hl_entry if hl_entry is not None else t["entry"]
    sl        = _px(t["sl"])
    tp        = _px(t["tp"])
    leverage  = hl_leverage if hl_leverage is not None else t["leverage"]
    sig_num   = t["signal_num"]
    opened_at = datetime.fromisoformat(t["opened_at"])
    max_adv   = t.get("max_adverse_pct", 0.0)

    side      = "LONG 🟢" if direction == 1 else "SHORT 🔴"
    now       = datetime.now(timezone.utc)
    dur_secs  = int((now.replace(tzinfo=None) - opened_at).total_seconds())
    dur_h     = dur_secs // 3600
    dur_m     = (dur_secs % 3600) // 60
    dur_str   = f"{dur_h}h {dur_m}m" if dur_h > 0 else f"{dur_m}m"

    # Use HL's ROE (return on equity = PnL / margin) when available — matches HL UI exactly
    if hl_roe is not None:
        lev_pnl = hl_roe * 100
    else:
        raw_move = (current_price - entry) / entry * 100
        lev_pnl  = raw_move * leverage * direction

    # Static SL/TP risk-reward % (what happens if each is hit — fixed, based on entry)
    sl_lev_pct  = abs(sl - entry) / entry * 100 * leverage   # max loss if SL hits
    tp_lev_pct  = abs(tp - entry) / entry * 100 * leverage   # max gain if TP hits
    # Dynamic distance for the color indicator only
    dist_to_sl  = abs(current_price - sl) / entry * 100 * leverage

    pnl_emoji = "💹" if lev_pnl >= 0 else "💀"
    bar_n     = min(int(abs(lev_pnl) / 3), 10)
    bar       = "█" * bar_n + "░" * (10 - bar_n)
    sl_status = "🟢" if dist_to_sl > 15 else "🟡" if dist_to_sl > 7 else "🔴"
    tp_close  = " 🎯" if abs(current_price - tp) / entry * 100 * leverage < 5 else ""
    if hl_pnl_usd is not None:
        sign = "+" if hl_pnl_usd >= 0 else "-"
        pnl_usd_s = f"  ({sign}${abs(hl_pnl_usd):.2f})"
    else:
        pnl_usd_s = ""

    if closed:
        is_profit = (final_pct or 0) >= 0
        if close_result == "tp" and is_profit:
            status_line = "✅ TP HIT"
            pnl_emoji   = "💹"
        elif close_result == "sl" and not is_profit:
            status_line = "❌ SL HIT"
            pnl_emoji   = "💀"
        else:
            status_line = "🔘 CLOSED"
            pnl_emoji   = "💹" if is_profit else "💀"
        pct_s = f"+{final_pct:.1f}%" if final_pct >= 0 else f"{final_pct:.1f}%"
        # Dollar PnL from the position itself (entry→exit)
        raw_dollar = (current_price - entry) * direction * abs(t.get("size", 0))
        dollar_s   = f"  ({'+' if raw_dollar >= 0 else '-'}${abs(raw_dollar):.2f})"
        adv_line   = f"\n📉 Max adverse: <b>{max_adv:.1f}%</b>" if max_adv < 0 else ""
        return (
            f"<b>{coin} {side}  #Signal{sig_num}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>{status_line}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Entry <code>${entry:.5g}</code> → Exit <code>${current_price:.5g}</code>\n"
            f"Duration: {dur_str}\n\n"
            f"{pnl_emoji} <b>{pct_s}</b>{dollar_s}{adv_line}"
        )

    adv_line    = f"\n📉 Max adverse: <b>{max_adv:.1f}%</b>" if max_adv < 0 else ""
    activity    = t.get("activity", [])
    act_lines   = "\n".join(f"  · {a}" for a in activity[-4:])
    act_section = f"\n━━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Activity:</b>\n{act_lines}" if act_lines else ""
    return (
        f"<b>{coin} {side}  #Signal{sig_num}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>📡 IN POSITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry  <code>${entry:.5g}</code>  ·  {leverage}x\n"
        f"Now    <code>${current_price:.5g}</code>  ({dur_str})\n\n"
        f"{pnl_emoji} <b>{'+' if lev_pnl>=0 else ''}{lev_pnl:.1f}%</b>{pnl_usd_s}  "
        f"<code>[{bar}]</code>\n\n"
        f"{sl_status} SL <code>${sl:.5g}</code>  <b>-{sl_lev_pct:.1f}%</b>\n"
        f"🎯 TP <code>${tp:.5g}</code>  <b>+{tp_lev_pct:.1f}%</b>{tp_close}{adv_line}"
        f"{act_section}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Updates every 60s</i>"
    )


# ── Dashboard pinned message ───────────────────────────────────────────────────

def _dashboard_text(state, tracked_with_prices, current_balance=None):
    now   = datetime.utcnow().strftime("%d %b · %H:%M UTC")
    stats = state.get("stats", {})
    total = stats.get("total", 0)
    wins  = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total_pct = stats.get("total_pct", 0.0)
    wr    = (wins/total*100) if total > 0 else 0

    # Open positions section
    if tracked_with_prices:
        pos_lines = []
        for coin, (t, price) in tracked_with_prices.items():
            direction = t["dir"]
            entry     = t["entry"]
            leverage  = t["leverage"]
            raw_move  = (price - entry) / entry * 100
            lev_pnl   = raw_move * leverage * direction
            side      = "LONG" if direction == 1 else "SHORT"
            msg_id    = t.get("msg_id")
            link      = f'<a href="{_msg_link(msg_id)}">→ Live</a>' if msg_id else ""
            pnl_s     = f"+{lev_pnl:.1f}%" if lev_pnl >= 0 else f"{lev_pnl:.1f}%"
            pos_lines.append(
                f"  {'🟢' if direction==1 else '🔴'} <b>{coin}</b> {side}  "
                f"{pnl_s}  {link}"
            )
        open_section = "📂 <b>OPEN POSITIONS</b>\n" + "\n".join(pos_lines)
    else:
        open_section = "📂 <b>OPEN POSITIONS</b>\n  — None currently —"

    # Avg adverse from closed trades
    closed = state.get("closed_trades", [])
    adverse_vals = [t.get("max_adverse_pct", 0) for t in closed if t.get("max_adverse_pct", 0) < 0]
    avg_dd = round(sum(adverse_vals) / len(adverse_vals), 1) if adverse_vals else 0.0

    # Balance section
    start_bal = stats.get("start_balance", 999.0)
    if current_balance is not None:
        bal_change = (current_balance - start_bal) / start_bal * 100
        bal_sign   = "+" if bal_change >= 0 else ""
        bal_section = (
            f"💰 <b>BALANCE</b>\n"
            f"  Start: <code>${start_bal:,.2f}</code>  →  Now: <code>${current_balance:,.2f}</code>"
            f"  (<b>{bal_sign}{bal_change:.1f}%</b>)"
        )
    else:
        bal_section = f"💰 Start balance: <code>${start_bal:,.2f}</code>"

    if total > 0:
        pct_s = f"+{total_pct:.1f}%" if total_pct >= 0 else f"{total_pct:.1f}%"
        stats_section = (
            f"📊 <b>TRACK RECORD</b>\n"
            f"  {wins}W / {losses}L  ·  WR: <b>{wr:.0f}%</b>  ·  Total: <b>{pct_s}</b>\n"
            f"  Avg adverse: <b>{avg_dd:.1f}%</b>"
        )
    else:
        stats_section = (
            f"📊 <b>TRACK RECORD</b>\n"
            f"  {state.get('signal_count',1)-1} signals fired — building record\n"
            f"  Avg adverse: <b>{avg_dd:.1f}%</b>"
        )

    return (
        f"📌 <b>GETSIGNALZ AI — DASHBOARD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{open_section}\n\n"
        f"{bal_section}\n\n"
        f"{stats_section}\n\n"
        f"🔬 Testnet mode  ·  1h candles  ·  13 pairs\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Updated {now}</i>"
    )


def update_dashboard(state):
    """Rebuild and edit the pinned dashboard message."""
    try:
        from executor import _clients, _hl_call, get_account_value
        info, _ = _clients()
        mids    = _hl_call(info.all_mids)

        tracked = state.get("tracked", {})
        twp = {}
        for coin, t in tracked.items():
            price = float(mids.get(coin, t["entry"]))
            twp[coin] = (t, price)

        current_balance = get_account_value()
        text    = _dashboard_text(state, twp, current_balance=current_balance)
        dash_id = state.get("dashboard_msg_id")

        if dash_id:
            _edit(dash_id, text)
        else:
            msg_id = _send(text)
            if msg_id:
                state["dashboard_msg_id"] = msg_id
                _pin(msg_id)
                save_state(state)
    except Exception as e:
        print(f"[tracker] dashboard error: {e}")


# ── Main update loop ──────────────────────────────────────────────────────────

def _current_equity(info=None):
    """Spot USDC balance — delegates to executor's get_account_value (singleton, retries)."""
    try:
        from executor import get_account_value
        return get_account_value()
    except Exception:
        return None


def _update_drawdown(state, equity):
    """Update peak equity and calculate real-time drawdown."""
    stats = state.setdefault("stats", {
        "total": 0, "wins": 0, "losses": 0, "total_pct": 0.0,
        "peak_balance": equity, "max_drawdown_pct": 0.0, "win_rate": 0.0,
        "current_drawdown_pct": 0.0
    })

    peak = stats.get("peak_balance", equity)
    if equity is None:
        return stats

    # Update peak
    if equity > peak:
        stats["peak_balance"] = equity
        peak = equity

    # Current drawdown
    if peak > 0:
        current_dd = max((peak - equity) / peak * 100, 0)
        stats["current_drawdown_pct"] = round(current_dd, 2)
        if current_dd > stats.get("max_drawdown_pct", 0):
            stats["max_drawdown_pct"] = round(current_dd, 2)

    return stats


def _trail_tp(coin, t, price, exchange, info):
    """
    Trailing TP engine — runs every 60s per open position.

    Logic:
    - Once price passes the original TP, switch to trailing mode
    - Trail at 8% behind the position's peak price (ATR-like buffer)
    - Every tick: if new peak → update trail SL upward
    - If trail SL is hit → position closes naturally via the order

    This replaces the fixed TP with a dynamic one that lets winners run.
    """
    direction = t["dir"]
    entry     = t["entry"]
    orig_tp   = t["tp"]
    leverage  = t["leverage"]

    # Have we passed original TP?
    past_tp = (direction == 1 and price >= orig_tp) or \
              (direction == -1 and price <= orig_tp)

    if not past_tp:
        return  # still before TP, fixed orders handle it

    # Trail distance: 8% of price (generous enough to not get shaken out)
    trail_pct = 0.08

    # Track peak price for this position
    peak_key = f"trail_peak_{coin}"
    old_peak = t.get(peak_key)

    if direction == 1:
        new_peak = max(old_peak or price, price)
        trail_sl = new_peak * (1 - trail_pct)
    else:
        new_peak = min(old_peak or price, price)
        trail_sl = new_peak * (1 + trail_pct)

    # Only update if peak moved meaningfully (avoid spamming orders)
    if old_peak and abs(new_peak - old_peak) / old_peak < 0.005:
        return

    # Save new peak
    state = load_state()
    if coin in state["tracked"]:
        state["tracked"][coin][peak_key] = new_peak
        state["tracked"][coin]["sl"]     = trail_sl  # keep tracker in sync
        save_state(state)

    # Update the order on exchange
    import math
    from executor import _hl_call

    def px(x):
        if x == 0: return 0.0
        d = math.floor(math.log10(abs(x)))
        return round(x, min(-d + 4, 4))

    is_buy_to_close = direction == -1  # short → buy to close

    try:
        # Cancel existing SL/TP orders for this coin (trailing replaces both)
        orders = _hl_call(info.open_orders, ACCOUNT)
        for o in orders:
            if o["coin"] == coin and o.get("reduceOnly"):
                exchange.cancel(coin, o["oid"])

        # Place new trailing SL (acts as both SL and trailing TP)
        sl_trigger = px(trail_sl)
        sl_limit   = px(trail_sl * (1.02 if is_buy_to_close else 0.98))
        r = exchange.order(coin, is_buy=is_buy_to_close, sz=t["size"],
            limit_px=sl_limit,
            order_type={"trigger": {"triggerPx": sl_trigger, "isMarket": False, "tpsl": "sl"}},
            reduce_only=True)

        lev_gain = round(abs(trail_sl - entry) / entry * 100 * leverage, 1)
        print(f"[trail_tp] {coin} new trail @ ${trail_sl:.5f} | locks +{lev_gain}% | peak=${new_peak:.5f}")
        _append_activity(coin, f"📈 Trail SL → ${trail_sl:.4f}  (peak ${new_peak:.4f}  locks +{lev_gain}%)")

        import sys; sys.path.insert(0, "/root/trade")
        import tg
        tg.dm_owner(
            f"📈 <b>Trail TP updated — {coin}</b>\n"
            f"Peak: <code>${new_peak:.5f}</code>\n"
            f"Trail SL: <code>${trail_sl:.5f}</code>  "
            f"(locks <b>+{lev_gain}%</b>)"
        )
    except Exception as e:
        print(f"[trail_tp] {coin} error: {e}")


def _loop():
    while _running:
        try:
            # Reuse the shared singleton clients from executor (same process, same pool)
            import sys; sys.path.insert(0, "/root/trade")
            from executor import _clients, _hl_call
            info, exchange = _clients()
            mids = _hl_call(info.all_mids)

            state   = load_state()
            tracked = state.get("tracked", {})

            # Real-time equity + drawdown
            equity = _current_equity(info)
            if equity is not None:
                _update_drawdown(state, equity)
                save_state(state)

            # Get full HL position data for accurate live display
            hl_positions = {}
            try:
                hl_state = _hl_call(info.user_state, ACCOUNT)
                for p in hl_state["assetPositions"]:
                    pos = p["position"]
                    sz  = float(pos["szi"])
                    if sz != 0:
                        lev_info = pos.get("leverage", {})
                        hl_positions[pos["coin"]] = {
                            "entry":    float(pos["entryPx"]),
                            "roe":      float(pos.get("returnOnEquity", 0)),
                            "pnl_usd":  float(pos.get("unrealizedPnl", 0)),
                            "leverage": int(lev_info.get("value", 1)) if isinstance(lev_info, dict) else 1,
                        }
            except Exception as e:
                print(f"[tracker] hl_positions error: {e}")

            # Per-position: trailing TP + max adverse tracking + live message update
            for coin, t in list(tracked.items()):
                price  = float(mids.get(coin, t["entry"]))
                hl_pos = hl_positions.get(coin, {})

                # Trailing TP engine
                _trail_tp(coin, t, price, exchange, info)

                # Track per-trade max adverse using HL's ROE (matches display exactly)
                hl_roe = hl_pos.get("roe")
                if hl_roe is not None:
                    roe_pct = hl_roe * 100
                else:
                    direction = t["dir"]
                    entry     = t["entry"]
                    leverage  = t["leverage"]
                    roe_pct   = (price - entry) / entry * 100 * leverage * direction

                if roe_pct < t.get("max_adverse_pct", 0.0):
                    state2 = load_state()
                    if coin in state2["tracked"]:
                        state2["tracked"][coin]["max_adverse_pct"] = round(roe_pct, 2)
                        save_state(state2)

                # Edit live message with HL-accurate data
                msg_id = t.get("msg_id")
                if msg_id:
                    t_fresh = load_state()["tracked"].get(coin)
                    if t_fresh is None or coin not in hl_positions:
                        # Position closed — skip; close_position() already edited this message
                        continue
                    text = _live_text(
                        t_fresh, price,
                        hl_roe=hl_pos.get("roe"),
                        hl_pnl_usd=hl_pos.get("pnl_usd"),
                        hl_leverage=hl_pos.get("leverage"),
                        hl_entry=hl_pos.get("entry"),
                    )
                    _edit(msg_id, text)

            update_dashboard(load_state())

        except Exception as e:
            print(f"[tracker] loop error: {e}")

        time.sleep(60)


# ── Public API ────────────────────────────────────────────────────────────────

def start():
    global _running
    if _running:
        return
    _running = True
    th = threading.Thread(target=_loop, daemon=True)
    th.start()
    print("[tracker] started")


def stop():
    global _running
    _running = False


def _append_activity(coin, msg):
    """Append an event to the position's activity log (keep last 8)."""
    state = load_state()
    if coin in state.get("tracked", {}):
        acts = state["tracked"][coin].setdefault("activity", [])
        acts.append(msg)
        state["tracked"][coin]["activity"] = acts[-8:]
        save_state(state)


def register_position(coin, direction, entry, sl, tp, size, leverage, signal_num,
                      signal_msg_id=None, balance_before=None):
    """Called when a new trade opens. Edits the waiting signal message to live state."""
    t = {
        "coin": coin, "dir": direction, "entry": entry,
        "sl": sl, "tp": tp, "size": size, "leverage": leverage,
        "signal_num": signal_num,
        "opened_at": datetime.utcnow().isoformat(),
        "msg_id": signal_msg_id,
        "max_adverse_pct": 0.0,
        "activity": [f"📥 Opened @ ${entry:.5g}"],
        "balance_before": balance_before,
    }
    text = _live_text(t, entry)
    if signal_msg_id:
        _edit(signal_msg_id, text)
    else:
        new_id = _send(text)
        t["msg_id"] = new_id

    state = load_state()
    state["tracked"][coin] = t
    save_state(state)

    update_dashboard(state)
    return t["msg_id"]


def update_trail(coin, new_sl, trail_stage):
    """Persist updated SL and trail_stage to state after a trail fires."""
    state = load_state()
    if coin in state.get("tracked", {}):
        state["tracked"][coin]["sl"]          = new_sl
        state["tracked"][coin]["trail_stage"] = trail_stage
        label = "breakeven" if trail_stage == 1 else "+0.5R"
        acts  = state["tracked"][coin].setdefault("activity", [])
        acts.append(f"🔒 Trail {label} → SL ${new_sl:.5g}")
        state["tracked"][coin]["activity"] = acts[-8:]
        save_state(state)


def close_position(coin, exit_price, result, lev_pct, balance_before=None, balance_after=None):
    """Called when trade closes. Final edit of live message, updates stats."""
    state   = load_state()
    tracked = state.get("tracked", {})
    t       = tracked.pop(coin, None)

    max_adverse = t.get("max_adverse_pct", 0.0) if t else 0.0

    if t:
        msg_id = t.get("msg_id")
        text   = _live_text(t, exit_price, closed=True,
                            close_result=result, final_pct=lev_pct)
        _edit(msg_id, text)

    # Win/loss logic (classify by realized PnL, NOT the tp/sl order label):
    # - Win:     trade closed in profit — includes trailing-stop exits, which fire the
    #            resting SL order and therefore carry an "sl" label despite being winners
    # - Loss:    trade closed at a net loss
    # - Neutral: exact breakeven — excluded from win rate
    # Old logic required result=="tp", so profitable trail exits (the bot's main alpha)
    # were dropped from the count, deflating the public win rate well below reality.
    is_win  = lev_pct > 0
    is_loss = lev_pct < 0

    stats = state.setdefault("stats", {
        "total":0,"wins":0,"losses":0,"total_pct":0.0,
        "peak_balance":balance_before or 999.0,
        "win_rate":0.0
    })
    if is_win or is_loss:
        stats["total"]   += 1
    stats["wins"]      += 1 if is_win else 0
    stats["losses"]    += 1 if is_loss else 0
    stats["total_pct"]  = round(stats["total_pct"] + lev_pct, 2)
    decided = stats["wins"] + stats["losses"]
    stats["win_rate"]   = round(stats["wins"] / decided * 100, 1) if decided > 0 else 0.0

    # Archive closed trade with max_adverse
    if t:
        state.setdefault("closed_trades", []).append({
            **t, "exit": exit_price, "result": result,
            "lev_pct": lev_pct, "max_adverse_pct": max_adverse
        })

    save_state(state)
    update_dashboard(state)
    return stats
