"""
Trade journal — persistent log of every signal, trade, and outcome.
This is what makes the bot an agent, not just a script.
Reviewed nightly and weekly to improve strategy.
"""
import json
import os
from datetime import datetime, timezone

JOURNAL_FILE = "/root/trade/journal.json"


def _load():
    if not os.path.exists(JOURNAL_FILE):
        return {"trades": [], "signals": [], "reviews": []}
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def _save(data):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def log_signal(coin, direction, score, reasons, price, sl, tp, tf,
               fired=True, adx=None, rsi=None, ssl=None, session_hour=None):
    data = _load()
    data["signals"].append({
        "time":         datetime.now(timezone.utc).isoformat(),
        "coin":         coin,
        "direction":    direction,
        "score":        score,
        "reasons":      reasons,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "tf":           tf,
        "fired":        fired,
        "adx":          adx,
        "rsi":          rsi,
        "ssl":          ssl,
        "session_hour": session_hour,
    })
    _save(data)


def log_trade_open(coin, direction, entry, sl, tp, size, leverage):
    data = _load()
    data["trades"].append({
        "id":        len(data["trades"]) + 1,
        "open_time": datetime.now(timezone.utc).isoformat(),
        "close_time": None,
        "coin":      coin,
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "tp":        tp,
        "size":      size,
        "leverage":  leverage,
        "exit":      None,
        "result":    None,   # "tp" or "sl"
        "lev_pct":   None,
        "duration_h": None,
    })
    _save(data)
    return len(data["trades"]) - 1  # index


def log_trade_close(coin, exit_price, result, lev_pct, duration_h):
    data = _load()
    # Find last open trade for this coin
    for t in reversed(data["trades"]):
        if t["coin"] == coin and t["close_time"] is None:
            t["close_time"] = datetime.now(timezone.utc).isoformat()
            t["exit"]       = exit_price
            t["result"]     = result
            t["lev_pct"]    = lev_pct
            t["duration_h"] = duration_h
            break
    _save(data)


def get_today_summary():
    data  = _load()
    today = datetime.now(timezone.utc).date().isoformat()

    today_trades = [t for t in data["trades"]
                    if t["close_time"] and t["close_time"][:10] == today]
    today_signals = [s for s in data["signals"]
                     if s["time"][:10] == today]

    wins    = [t for t in today_trades if t["result"] == "tp" and (t["lev_pct"] or 0) > 0]
    losses  = [t for t in today_trades if t["result"] == "sl" and (t["lev_pct"] or 0) < 0]
    decided = len(wins) + len(losses)
    total_lev = sum(t["lev_pct"] or 0 for t in today_trades)

    return {
        "date":          today,
        "signals_fired": len([s for s in today_signals if s["fired"]]),
        "trades_closed": len(today_trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      len(wins) / decided * 100 if decided > 0 else 0,
        "total_lev_pct": total_lev,
        "best_win":      max((t["lev_pct"] or 0 for t in wins), default=0),
        "worst_loss":    min((t["lev_pct"] or 0 for t in losses), default=0),
        "trades":        today_trades,
    }


def get_week_summary():
    data   = _load()
    trades = [t for t in data["trades"] if t["close_time"]]

    if not trades:
        return None

    wins    = [t for t in trades if t["result"] == "tp" and (t["lev_pct"] or 0) > 0]
    losses  = [t for t in trades if t["result"] == "sl" and (t["lev_pct"] or 0) < 0]
    decided = len(wins) + len(losses)
    total_r = sum(t["lev_pct"] or 0 for t in trades)

    # Which confluence reasons appeared most in wins vs losses
    win_reasons   = {}
    loss_reasons  = {}
    for t in wins:
        sig = next((s for s in data["signals"]
                    if s["coin"] == t["coin"] and s["time"][:10] == t["open_time"][:10]), None)
        if sig:
            for r in sig.get("reasons", []):
                win_reasons[r] = win_reasons.get(r, 0) + 1
    for t in losses:
        sig = next((s for s in data["signals"]
                    if s["coin"] == t["coin"] and s["time"][:10] == t["open_time"][:10]), None)
        if sig:
            for r in sig.get("reasons", []):
                loss_reasons[r] = loss_reasons.get(r, 0) + 1

    return {
        "total_trades": len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     len(wins) / decided * 100 if decided > 0 else 0,
        "total_lev_pct": total_r,
        "avg_win":      sum(t["lev_pct"] or 0 for t in wins) / len(wins) if wins else 0,
        "avg_loss":     sum(t["lev_pct"] or 0 for t in losses) / len(losses) if losses else 0,
        "top_win_signals": sorted(win_reasons.items(), key=lambda x: -x[1])[:3],
        "top_loss_signals": sorted(loss_reasons.items(), key=lambda x: -x[1])[:3],
    }


def compact(keep_days=30):
    """Remove signals older than keep_days to save memory."""
    data    = _load()
    cutoff  = datetime.now(timezone.utc).date()
    from datetime import timedelta
    cutoff  = (cutoff - timedelta(days=keep_days)).isoformat()

    before  = len(data["signals"])
    data["signals"] = [s for s in data["signals"] if s["time"][:10] >= cutoff]
    after   = len(data["signals"])

    _save(data)
    return before - after  # removed count
