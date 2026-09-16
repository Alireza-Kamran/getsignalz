"""
Nightly & weekly self-review.
Daily review → single editable message in channel (or new if none exists).
Weekly review → same slot, replaces daily.
Everything else stays in logs or DM.
"""
import json, os
from datetime import datetime, timezone
from journal import get_today_summary, get_week_summary, compact
from analyze import health_score
import tg
from config import TELEGRAM_TOKEN as TOKEN, TELEGRAM_CHANNEL as CHANNEL
import brand
from io_safe import atomic_write_json, atomic_write_text

REVIEW_STATE = "/root/trade/.review_msg_id"

# Called every ~20s while the Claude brain subprocess runs inside
# _self_improve(). live.py installs its position-management pass here at
# startup (review.KEEPALIVE = _manage_book) so the ratchet keeps running for
# the 5-36 minutes the review used to block it. None when review.py is driven
# from anywhere other than the live loop, in which case the wait simply blocks
# as before.
KEEPALIVE = None


def _get_review_msg_id():
    if os.path.exists(REVIEW_STATE):
        with open(REVIEW_STATE) as f:
            return int(f.read().strip())
    return None


def _save_review_msg_id(mid):
    with open(REVIEW_STATE, "w") as f:
        f.write(str(mid))


def _post_or_edit(text):
    """Edit the review message slot if it exists, otherwise post new and save ID."""
    import requests
    BASE  = f"https://api.telegram.org/bot{TOKEN}"
    mid   = _get_review_msg_id()
    if mid:
        r = requests.post(f"{BASE}/editMessageText", json={
            "chat_id": CHANNEL, "message_id": mid,
            "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        if r.json().get("ok"):
            return mid
    # Post new
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id": CHANNEL, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True
    }, timeout=10)
    if r.json().get("ok"):
        mid = r.json()["result"]["message_id"]
        _save_review_msg_id(mid)
        return mid
    return None


def nightly_review():
    s   = get_today_summary()
    now = datetime.now(timezone.utc).strftime("%d %b %Y")

    if s["trades_closed"] == 0 and s["signals_fired"] == 0:
        msg = (
            brand.header("🌙", f"گزارش روزانه — {now}", f"Daily Report — {now}")
            + "\n\nامروز معامله ای نبود\nهیچ موقعیت معتبری پیدا نشد"
            + f"\n{brand.rule()}\n"
            + "No trades today\nNo qualifying setup was found"
        )
    else:
        wr_e   = "🟢" if s["win_rate"] >= 50 else "🔴"
        tot_e  = "💹" if s["total_lev_pct"] > 0 else "💀"
        def _emoji(t):
            # Classify on realised P&L, not which order fired -- a S2 trail-stop
            # winner carries result=="sl" and used to fall through to a neutral
            # 🔘 here instead of ✅ (same bug class as the ADX/coin/hour counters
            # below, fixed the same night).
            p = t.get("lev_pct") or 0
            if p > 0: return "✅"
            if p < 0: return "❌"
            return "🔘"

        detail = "".join(
            f"  {_emoji(t)} {t['coin']} "
            f"{'+' if (t.get('lev_pct') or 0)>=0 else ''}{(t.get('lev_pct') or 0):.1f}%\n"
            for t in s["trades"]
        )
        # Channel gets the headline only. The per-trade breakdown is owner
        # detail: a subscriber cannot act on it and it made the one persistent
        # review message the longest thing in the channel.
        msg = (
            brand.header("🌙", f"گزارش روزانه — {now}", f"Daily Report — {now}")
            + "\n" + brand.numeric_block([
                ("Signals",  s["signals_fired"]),
                ("Closed",   f"{s['trades_closed']}  ({s['wins']}W / {s['losses']}L)"),
                ("Win rate", f"{s['win_rate']:.0f}%"),
                ("Today",    f"{'+' if s['total_lev_pct']>=0 else ''}{s['total_lev_pct']:.1f}%"),
            ])
        )
        if detail:
            tg.dm_owner(f"🌙 <b>Daily detail — {now}</b>\n"
                        f"{wr_e} WR {s['win_rate']:.0f}%  {tot_e} "
                        f"{'+' if s['total_lev_pct']>=0 else ''}{s['total_lev_pct']:.1f}%\n\n"
                        f"{detail}")

    _post_or_edit(msg)
    _self_improve()


def _match_signal(trade, signals):
    """Find the signal that originated this trade (coin + open date match)."""
    open_date = (trade.get("open_time") or "")[:10]
    return next((s for s in signals
                 if s["coin"] == trade["coin"] and s.get("time", "")[:10] == open_date), None)


def _patch_trader(config):
    """Patch ALL tunable constants in trader.py from strategy_config.json."""
    import re
    path = "/root/trade/trader.py"
    with open(path) as f:
        code = f.read()

    code = re.sub(r'MIN_SCORE\s*=\s*[\d.]+',   f'MIN_SCORE   = {config["min_score"]}',           code)
    code = re.sub(r'MIN_ADX\s*=\s*[\d.]+',     f'MIN_ADX     = {config["min_adx"]}',             code)
    code = re.sub(r'TP_RATIO\s*=\s*[\d.]+',    f'TP_RATIO    = {config["tp_ratio"]}',            code)
    code = re.sub(r'RISK_PCT\s*=\s*[\d.]+',    f'RISK_PCT    = {config["risk_pct"]}',            code)
    code = re.sub(r'MAX_TRADES\s*=\s*\d+',     f'MAX_TRADES  = {config["max_trades"]}',          code)
    code = re.sub(r'SESSION_START\s*=\s*\d+',  f'SESSION_START = {config["session_start_utc"]}', code)
    code = re.sub(r'SESSION_END\s*=\s*\d+',    f'SESSION_END   = {config["session_end_utc"]}',   code)

    # Rebuild WATCHLIST block
    wl_items = '",\n    "'.join(config["watchlist"])
    wl_str   = f'[\n    "{wl_items}"\n]'
    code = re.sub(r'WATCHLIST = \[.*?\]', f'WATCHLIST = {wl_str}', code, flags=re.DOTALL)

    # Same hazard as analyze._apply_config: this rewrites trader.py itself.
    atomic_write_text(path, code)


def _self_improve():
    """
    Comprehensive nightly self-improve.
    Analyses per-coin performance, session hours, confluence signal quality,
    TP ratio, drawdown risk, concurrent trade correlation, and ADX buckets.
    Adjusts up to 8 strategy parameters, patches trader.py, auto-restarts if safe.
    """
    try:
        import re, sys, os
        from collections import defaultdict
        from datetime import datetime, timedelta
        sys.path.insert(0, "/root/trade")
        from analyze import load_journal, load_state, load_config, save_config

        journal  = load_journal()
        state    = load_state()
        config   = load_config()
        signals  = journal.get("signals", [])

        all_trades = [t for t in journal.get("trades", [])
                      if t.get("result") and t.get("lev_pct") is not None]

        # Enrich with max_adverse from state.json (more precise than journal)
        for sc in state.get("closed_trades", []):
            for t in all_trades:
                if (t["coin"] == sc.get("coin") and
                        (t.get("open_time") or "")[:10] == (sc.get("opened_at") or sc.get("open_time") or "")[:10]):
                    if sc.get("max_adverse_pct") is not None:
                        t["max_adverse_pct"] = sc["max_adverse_pct"]
                    break

        MIN_TRADES = 10
        if len(all_trades) < MIN_TRADES:
            tg.dm_owner(
                f"🧠 Self-improve: {len(all_trades)} closed trades — "
                f"observing, not adjusting (need {MIN_TRADES}+)"
            )
            return

        wins    = [t for t in all_trades if (t.get("lev_pct") or 0) > 0]
        losses  = [t for t in all_trades if (t.get("lev_pct") or 0) < 0]
        decided = len(wins) + len(losses)
        wr      = len(wins) / decided * 100 if decided > 0 else 0
        total_pnl = sum(t.get("lev_pct", 0) or 0 for t in all_trades)

        changes  = []
        insights = []

        # MIN_SCORE auto-adjust DISABLED (uses blended WR=wrong metric; set by nightly self-learn only)

        # ── 2. MIN_ADX ──────────────────────────────────────────────────────────
        cur_adx = config.get("min_adx", 30)
        low_adx  = [t for t in all_trades
                    if (sig := _match_signal(t, signals)) and (sig.get("adx") or 0) < cur_adx]
        # Classify on realised P&L, not which order fired -- result=="tp" mislabels
        # every S2 trail-stop winner as a loss (same bug class fixed in
        # analyze.full_report / result_card / tg.dm_trade_close, all 2026-08-04).
        low_wins = [t for t in low_adx if t["lev_pct"] > 0]
        low_wr   = len(low_wins) / len(low_adx) * 100 if low_adx else 100

        # MIN_ADX auto-adjust DISABLED 2026-07-10 — FOURTH blended-metric regression
        # (after MIN_SCORE, TP_RATIO, SESSION). This block ratcheted MIN_ADX 30->35 (Jul 8)
        # ->40 (Jul 9) using low_wr counted with result=='tp', which mislabels every trail-SL
        # winner as a loss, over BLENDED all_trades (pre/post 2026-06-25 rebuild). Post-rebuild
        # CM data shows high-ADX trades did WORSE, so the ratchet's premise was backwards too.
        # Do not re-enable without splitting by strategy-version and counting PnL, not labels.

        adx_insight = f"ADX below {cur_adx}: {len(low_adx)} trades, WR={low_wr:.0f}%"
        insights.append(adx_insight)

        # TP_RATIO auto-adjust DISABLED 2026-07-01 (same bug class as MIN_SCORE above: used
        # blended "TP hit rate" = wins/decided across ALL-TIME trades, mixing the pre-2026-06-25
        # old scoring system with the CM Sling Shot rebuild. CM Sling Shot had 0 closed trades, so
        # every "TP hit rate" this block computed was 100% old-system data, yet it silently
        # ratcheted TP_RATIO down 0.25/night once decided>=15: 2.0→1.75 (06-29)→1.5 (06-30),
        # undoing 4+ nightly sessions of evidenced 1:2 RR calibration with no visibility to the
        # memory-based review process. Restored to 2.0 on 2026-07-01. Do not re-enable without
        # first splitting stats by strategy-version (pre/post rebuild).
        cur_tp = config.get("tp_ratio", 2.0)
        if decided >= 15:
            tp_rate = len(wins) / decided * 100
            insights.append(f"TP hit rate: {tp_rate:.0f}%  (TP_RATIO={cur_tp})")

        # ── 4. RISK_PCT ─────────────────────────────────────────────────────────
        cur_risk = config.get("risk_pct", 0.03)
        drawdown = state.get("stats", {}).get("current_drawdown_pct", 0)

        if drawdown > 15 and cur_risk > 0.02:
            config["risk_pct"] = 0.02
            changes.append(f"RISK_PCT {cur_risk*100:.0f}%→2%  (drawdown={drawdown:.1f}% > 15% — protect capital)")
        elif drawdown < 5 and wr > 65 and decided >= 10 and cur_risk < 0.04:
            config["risk_pct"] = round(min(cur_risk + 0.005, 0.04), 3)
            changes.append(f"RISK_PCT {cur_risk*100:.0f}%→{config['risk_pct']*100:.1f}%  (WR={wr:.0f}% > 65%, drawdown={drawdown:.1f}%)")
        elif 5 <= drawdown <= 10 and cur_risk > 0.03:
            config["risk_pct"] = 0.03
            changes.append(f"RISK_PCT reset to 3%  (drawdown recovered to {drawdown:.1f}%)")
        insights.append(f"Drawdown: {drawdown:.1f}%  |  S1 RISK_PCT: {config.get('risk_pct', cur_risk)*100:.1f}% (S1 disabled - S2 risk is in strategy2.S2_RISK_PCT, owner-locked)")

        # ── 5. MAX_TRADES (concurrent loss correlation) ──────────────────────────
        cur_max = config.get("max_trades", 2)
        if decided >= 20:
            days_trades = defaultdict(list)
            for t in all_trades:
                d = (t.get("close_time") or "")[:10]
                if d:
                    days_trades[d].append(t)
            loss_days        = [ts for ts in days_trades.values() if any(t["result"]=="sl" and t["lev_pct"]<0 for t in ts)]
            double_loss_days = [ts for ts in loss_days if sum(1 for t in ts if t["result"]=="sl" and t["lev_pct"]<0) >= 2]
            corr_rate        = len(double_loss_days) / len(loss_days) if loss_days else 0
            insights.append(f"Concurrent loss rate: {corr_rate*100:.0f}% ({len(double_loss_days)}/{len(loss_days)} loss-days)")
            if corr_rate > 0.4 and cur_max > 1:
                config["max_trades"] = 1
                changes.append(f"MAX_TRADES {cur_max}→1  (concurrent loss rate={corr_rate*100:.0f}% > 40%)")
            elif corr_rate < 0.15 and wr > 65 and cur_max < 3:
                config["max_trades"] = 3
                changes.append(f"MAX_TRADES {cur_max}→3  (low correlation losses + strong WR)")

        # ── 6. WATCHLIST (suspend bad coins, restore after 7 days) ──────────────
        coin_perf = defaultdict(lambda: {"n":0,"w":0,"pct":0.0})
        for t in all_trades:
            c = t["coin"]
            coin_perf[c]["n"] += 1
            coin_perf[c]["w"] += 1 if t["lev_pct"] > 0 else 0
            coin_perf[c]["pct"] += t.get("lev_pct", 0) or 0

        cur_list  = list(config.get("watchlist", []))
        suspended = config.setdefault("suspended_coins", {})
        today     = datetime.utcnow().date()

        # Re-enable coins that have been suspended long enough
        for coin, susp_date in list(suspended.items()):
            age = (today - datetime.fromisoformat(susp_date).date()).days
            if age >= 7:
                del suspended[coin]
                if coin not in cur_list:
                    cur_list.append(coin)
                    changes.append(f"WATCHLIST: re-added {coin} (7-day suspension ended)")

        # Suspend persistently bad performers
        for coin, cp in coin_perf.items():
            if cp["n"] >= 5 and coin in cur_list:
                c_wr  = cp["w"] / cp["n"] * 100
                c_avg = cp["pct"] / cp["n"]
                if c_wr < 25 and c_avg < -15:
                    cur_list.remove(coin)
                    suspended[coin] = today.isoformat()
                    changes.append(f"WATCHLIST: suspended {coin}  (WR={c_wr:.0f}%, AvgPnL={c_avg:.1f}%)")

        config["watchlist"]       = cur_list
        config["suspended_coins"] = suspended

        # ── 7. SESSION HOURS (trim edges with bad performance) ──────────────────
        hour_perf = defaultdict(lambda: {"w":0,"n":0})
        for t in all_trades:
            sig = _match_signal(t, signals)
            h   = sig.get("session_hour") if sig else None
            if h is not None:
                hour_perf[h]["n"] += 1
                if t["lev_pct"] > 0:
                    hour_perf[h]["w"] += 1

        cur_start = config.get("session_start_utc", 7)
        cur_end   = config.get("session_end_utc", 22)
        window    = cur_end - cur_start

        # SESSION HOURS auto-adjust DISABLED 2026-07-06 - third instance of the blended-metric
        # bug class (MIN_SCORE, TP_RATIO, now SESSION). Two flaws: (1) the win counter above
        # requires result==tp, but the trail engine exits winners with result==sl, so every
        # profitable trail close is counted as a loss; (2) hour stats blend pre/post-2026-06-25
        # strategy regimes. v1.9.3 (2026-07-05 23:05) trimmed hour 11 on WR=0% when the actual
        # hour-11 entries were BNB -20.0% and OP +34.0% - net POSITIVE, and OP was the best CM
        # Sling Shot trade so far, a +34% trail close misread as a loss. Reverted to 11:00 by
        # the 2026-07-06 nightly. Do not re-enable without (a) counting wins by lev_pct > 0 and
        # (b) splitting stats by strategy version. hour_perf above kept for insight only.
        _ = window  # retained for future insight use

        # ── 8. Confluence signal quality analysis (insight only) ─────────────────
        factor_stats = defaultdict(lambda: {"w":0,"n":0})
        for t in all_trades:
            sig = _match_signal(t, signals)
            if not sig:
                continue
            is_win = (t.get("lev_pct") or 0) > 0
            for reason in sig.get("reasons", []):
                factor_stats[reason]["n"] += 1
                if is_win:
                    factor_stats[reason]["w"] += 1

        best_signals  = sorted([(r, fs) for r, fs in factor_stats.items() if fs["n"] >= 3],
                                key=lambda x: -x[1]["w"] / x[1]["n"])
        worst_signals = list(reversed(best_signals))

        # ── Apply all changes ────────────────────────────────────────────────────
        config["updated_by"] = "auto-nightly"
        save_config(config)
        _patch_trader(config)

        # ── Build rich report ────────────────────────────────────────────────────
        now_s   = datetime.utcnow().strftime("%d %b %Y")
        pnl_s   = f"{'+' if total_pnl>=0 else ''}{total_pnl:.1f}%"
        avg_win  = sum(t["lev_pct"] for t in wins)  / len(wins)  if wins  else 0
        avg_loss = sum(t["lev_pct"] for t in losses) / len(losses) if losses else 0

        lines = [
            f"🧠 <b>Nightly Self-Improve — {now_s}</b>",
            f"━━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 {len(all_trades)} trades  ·  WR: <b>{wr:.0f}%</b>  ·  P&L: <b>{pnl_s}</b>",
            f"📈 Avg win: <b>+{avg_win:.1f}%</b>  ·  📉 Avg loss: <b>{avg_loss:.1f}%</b>",
        ]

        if changes:
            lines.append(f"\n✏️ <b>Changes ({len(changes)}):</b>")
            lines += [f"  · {c}" for c in changes]
        else:
            lines.append(f"\n✅ All parameters within target bounds — no changes.")

        if insights:
            lines.append(f"\n📋 <b>Insights:</b>")
            lines += [f"  · {i}" for i in insights]

        # Coin leaderboard
        if coin_perf:
            lines.append(f"\n🏆 <b>Coin leaderboard:</b>")
            for coin, cp in sorted(coin_perf.items(), key=lambda x: -x[1]["pct"])[:8]:
                c_wr  = cp["w"] / cp["n"] * 100
                c_avg = cp["pct"] / cp["n"]
                flag  = " ⛔" if coin in suspended else ""
                lines.append(f"  {coin:<8} {cp['n']}t  WR:{c_wr:.0f}%  Avg:{c_avg:+.1f}%{flag}")

        # Top confluence signals
        if best_signals:
            lines.append(f"\n🔍 <b>Best confluence signals:</b>")
            for reason, fs in best_signals[:5]:
                lines.append(f"  {fs['w']/fs['n']*100:.0f}% WR  {reason}  ({fs['n']}x)")
            if worst_signals and worst_signals[0][1]["w"]/worst_signals[0][1]["n"] < 0.4:
                worst_r, worst_fs = worst_signals[0]
                lines.append(f"  ⚠️ Weakest: {worst_r} — {worst_fs['w']/worst_fs['n']*100:.0f}% WR")

        tg.dm_owner("\n".join(lines))

        # ── Claude AI brain — deep code + strategy improvement ───────────────
        code_edits = []
        try:
            from ai_brain import run_ai_brain, format_dm as brain_format_dm
            brain_result = run_ai_brain(keepalive=KEEPALIVE)
            tg.dm_owner(brain_format_dm(brain_result))
            if brain_result["changes_applied"]:
                code_edits = [{"file": c["file"], "reason": c["reason"]}
                              for c in brain_result["changes_applied"]]
                changes += [f"[AI] {c['file']}: {c['reason']}" for c in code_edits]
        except Exception as brain_err:
            import traceback
            tg.dm_owner(f"⚠️ Claude brain error: {tg.esc(brain_err)}\n<code>{tg.esc(traceback.format_exc()[:400])}</code>")

        # ── Version bump + GitHub push ────────────────────────────────────────
        try:
            night_report = {
                "date":         datetime.utcnow().date().isoformat(),
                "rule_changes": changes,
                "code_edits":   code_edits,
                "stats": {
                    "trades":    len(all_trades),
                    "win_rate":  round(wr, 1),
                    "total_pnl": round(total_pnl, 1),
                    "wins":      len(wins),
                    "losses":    len(losses),
                },
            }
            atomic_write_json("/root/trade/.night_report.json", night_report, indent=None)
            version_push()   # push immediately while we have the full picture
        except Exception as rep_err:
            tg.dm_owner(f"⚠️ version push error: {rep_err}")

        # Auto-restart if any changes were made and no open positions
        if changes:
            open_pos = state.get("tracked", {})
            if not open_pos:
                tg.dm_owner("♻️ No open positions — restarting bot to apply all changes now...")
                import sys, os
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                coins = ", ".join(open_pos.keys())
                tg.dm_owner(f"⚠️ Open positions ({coins}) — restart bot manually after they close to apply changes.")

    except Exception as e:
        import traceback
        tg.dm_owner(f"⚠️ Self-improve error: {tg.esc(e)}\n<code>{tg.esc(traceback.format_exc()[:600])}</code>")


def weekly_review():
    s   = get_week_summary()
    now = datetime.now(timezone.utc).strftime("%d %b %Y")

    if not s:
        _post_or_edit(
            brand.header("📅", f"گزارش هفتگی — {now}", f"Weekly Report — {now}")
            + "\n\nهنوز معامله بسته شده ای نیست"
            + f"\n{brand.rule()}\n"
            + "No closed trades yet")
        return

    wr_e  = "🟢" if s["win_rate"] >= 50 else "🔴"
    tot_e = "💹" if s["total_lev_pct"] > 0 else "💀"
    w_sig = "\n".join(f"  · {r} ({c}x)" for r, c in s["top_win_signals"]) or "  · —"
    l_sig = "\n".join(f"  · {r} ({c}x)" for r, c in s["top_loss_signals"]) or "  · —"

    try:
        trust = health_score(save=True)["total"]
        trust_line = f"🏆 Trust Score: <b>{trust}/100</b>\n"
    except Exception:
        trust_line = ""

    # Same split as the nightly: headline to the channel, the signal-quality
    # breakdown and the trust score to the owner. Those exist to steer tuning,
    # not to inform a subscriber.
    msg = (
        brand.header("📅", f"گزارش هفتگی — {now}", f"Weekly Report — {now}")
        + "\n" + brand.numeric_block([
            ("Trades",   f"{s['total_trades']}  ({s['wins']}W / {s['losses']}L)"),
            ("Win rate", f"{s['win_rate']:.0f}%"),
            ("Week",     f"{'+' if s['total_lev_pct']>=0 else ''}{s['total_lev_pct']:.1f}%"),
        ])
    )
    tg.dm_owner(
        f"📅 <b>Weekly detail — {now}</b>\n"
        f"{wr_e} WR {s['win_rate']:.0f}%  ·  avg win +{s['avg_win']:.1f}%  ·  "
        f"avg loss {s['avg_loss']:.1f}%\n"
        f"{tot_e} Week {'+' if s['total_lev_pct']>=0 else ''}{s['total_lev_pct']:.1f}%\n"
        f"{trust_line}"
        f"\n✅ Best signals:\n{w_sig}\n"
        f"❌ Signals in losses:\n{l_sig}")
    _post_or_edit(msg)
    removed = compact(keep_days=30)
    tg.dm_owner(f"🧹 Weekly compact: removed {removed} old records")


def should_quiet(hour_utc):
    return 2 <= hour_utc < 4


# Both latches are WINDOWS, not instants, and both rely on the caller's
# once-per-period guard (live.py holds _nightly_done by date and _weekly_done by
# ISO week, and sets each BEFORE invoking the review) to fire exactly once.
#
# They were `minute == 0` / `minute == 30` until 2026-09-02. The main loop polls
# every POLL=20s plus however long that pass's network calls take, so an exact
# minute match only lands if a pass happens to begin inside those 60 seconds --
# and on 2026-09-02 the review was moved BELOW position management, putting three
# more Hyperliquid round-trips ahead of the check. Under the 502 bursts bot.log
# shows regularly, that is enough to step straight over minute 0 and silently
# skip the review for the whole day. A window costs nothing (the date latch still
# bounds it to one run) and removes the race entirely.
#
# Moved off the top of the hour on 2026-09-06. The review is the only blocking
# work left in the loop, and at 23:00 it blocked the 23:00 CANDLE: measured over
# 38 nights the 23:00 scan starts a median 30s late but a p90 of 19.4 min and a
# worst case of 27.7 min, with 16 nights over 5 min, against ~9s for every other
# hour of the day. Last night it was 14.0 min (23:00:07 -> 23:14:06 in bot.log).
#
# Since 2026-09-02 position management sits ABOVE this block, so the ratchet and
# the resting stops are no longer affected -- what is left is ENTRIES, and this
# is a mean-reversion engine whose signal is a transient stretch from the mean.
# Deciding the 23:00 candle on prices read at 23:14 is 14 minutes of signal-to-
# fill drift on one candle in every 24, self-inflicted and free to remove.
#
# 23:20 rather than 23:30, because the windows must stay disjoint from
# should_weekly_review's (see below, and test_review_order.py section 3): a pass
# that matched both would start the weekly against a loop the nightly had
# already blocked. The 23:00 scan finishes by ~23:02, so 23:20 clears it by 18
# minutes, and on Sundays the nightly still returns in time for the weekly to
# fire immediately after it in the normal order.
#
# This does NOT stress SCAN_STALE_ALERT_SEC=8100, which live.py sizes to clear
# exactly this review. It relaxes it: the review no longer consumes a scan slot,
# so the longest scan-free stretch falls from 22:01 -> (23:00 + review) to
# 23:01 -> (23:30 + review). At the BRAIN_TIMEOUT-bounded worst case that is 99
# min against 129 min today, i.e. more headroom under an unchanged threshold.
# The threshold itself is deliberately left alone -- it was only stabilised on
# 08-31 after a 3-of-4 false-alarm run, and re-deriving it on one night's data
# is how that run started.
def should_nightly_review(hour_utc, minute_utc):
    return hour_utc == 23 and 20 <= minute_utc < 30


def should_weekly_review(weekday, hour_utc, minute_utc):
    return weekday == 6 and hour_utc == 23 and minute_utc >= 30


def should_version_push(hour_utc, minute_utc):
    return hour_utc == 4 and minute_utc == 0


def _untracked_source(git):
    """Root-level *.py that git does not yet track — the files `add -u` misses.

    `git ls-files --others --exclude-standard` lists untracked files with
    .gitignore applied, which is the whole safety property here: config.py holds
    the API keys and is ignored on line 2 of .gitignore, so it can never appear
    in this list. Anything that weakens or removes --exclude-standard commits
    the secrets.

    Root level only, and .py only. The bot's importable surface and its tests
    all live in the repo root; everything nested is either __pycache__ or the
    avatar/ design scratch (43 generated PNGs and HTML mockups), and a nightly
    job should not decide on its own to start committing binaries.

    Returns [] on any git failure -- a push that stages nothing new is the
    status quo, while raising here would take down the commit that carries the
    night's real work.
    """
    try:
        res = git("ls-files", "--others", "--exclude-standard")
        if res.returncode != 0:
            return []
        return sorted(f for f in res.stdout.split()
                      if "/" not in f and f.endswith(".py"))
    except Exception:
        return []


def version_push():
    """Bump VERSION, append to CHANGELOG.md, commit and push to GitHub."""
    import subprocess

    REPORT  = "/root/trade/.night_report.json"
    VER_F   = "/root/trade/VERSION"
    CHNG_F  = "/root/trade/CHANGELOG.md"
    REPO    = "/root/trade"

    if not os.path.exists(REPORT):
        return  # no nightly run happened (e.g. <10 trades) — skip

    try:
        with open(REPORT) as f:
            report = json.load(f)

        # ── Bump version ─────────────────────────────────────────────────────
        ver = open(VER_F).read().strip() if os.path.exists(VER_F) else "1.0.0"
        major, minor, patch = (int(x) for x in ver.split("."))

        code_edits   = report.get("code_edits", [])
        rule_changes = report.get("rule_changes", [])

        if code_edits:
            minor += 1
            patch  = 0
        else:
            patch += 1

        new_ver = f"{major}.{minor}.{patch}"
        atomic_write_text(VER_F, new_ver + "\n")

        # ── Build CHANGELOG entry ─────────────────────────────────────────────
        stats    = report.get("stats", {})
        date_str = report.get("date", datetime.utcnow().date().isoformat())

        lines = [f"## v{new_ver} — {date_str}", ""]

        lines.append(
            f"**Stats:** {stats.get('trades', 0)} trades · "
            f"WR: {stats.get('win_rate', 0):.0f}% · "
            f"P&L: {'+' if stats.get('total_pnl', 0) >= 0 else ''}{stats.get('total_pnl', 0):.1f}%"
        )
        lines.append("")

        param_changes = [c for c in rule_changes if not c.startswith("[AI]")]
        ai_changes    = [c for c in rule_changes if c.startswith("[AI]")]

        if param_changes:
            lines.append(f"**Parameter changes ({len(param_changes)}):**")
            for c in param_changes:
                lines.append(f"- {c}")
            lines.append("")

        if ai_changes:
            lines.append(f"**Code improvements ({len(ai_changes)}):**")
            for c in ai_changes:
                lines.append(f"- {c.removeprefix('[AI] ')}")
            lines.append("")

        if not param_changes and not ai_changes:
            lines.append("No changes — all parameters within target bounds.")
            lines.append("")

        lines.append("---")
        lines.append("")
        entry = "\n".join(lines)

        # Prepend entry after the header block
        existing = open(CHNG_F).read() if os.path.exists(CHNG_F) else "# Changelog\n\n---\n"
        split    = existing.find("\n---\n")
        if split != -1:
            new_content = existing[:split + 5] + "\n" + entry + existing[split + 5:]
        else:
            new_content = existing + "\n" + entry
        atomic_write_text(CHNG_F, new_content)

        # ── Git commit + push ─────────────────────────────────────────────────
        def _git(*args):
            # timeout is not optional: `git push` talks to the network,
            # and a hang here parks the whole main loop. The watchdog in
            # live.py would eventually restart the bot, but a hang that
            # reproduces every night turns that into a restart loop.
            try:
                return subprocess.run(
                    ["git", "-C", REPO] + list(args),
                    capture_output=True, text=True, timeout=120
                )
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(
                    args, 1, "", "git timed out after 120s")

        _git("add", "-u")                       # stage all tracked modified files
        _git("add", "VERSION", "CHANGELOG.md")  # always include these two

        # `add -u` stages only files git ALREADY TRACKS, so for as long as this
        # push has existed, every NEW module a nightly session wrote has been
        # silently left out of every commit. Found 2026-09-06 by checking what
        # `git ls-files` actually returns: brand.py and io_safe.py were both
        # missing from the repository while being imported by tg.py, tracker.py
        # and review.py itself -- a fresh clone could not start the bot -- along
        # with five test files, i.e. the safety net was absent from the backup of
        # the thing it protects. Nothing surfaced it because the commit succeeds:
        # the loss is invisible until someone clones.
        #
        # Deliberately NOT `git add -A`. That would also sweep in 43 untracked
        # design assets under avatar/ (generated PNGs, HTML mockups) whose place
        # in the repo is Kamran's call, not this function's, and a nightly job
        # that quietly starts committing binaries is worse than one that misses
        # a file. Root-level *.py only: that is exactly the set the running
        # process imports and the tests it is checked by.
        #
        # --exclude-standard is what keeps this safe -- it honours .gitignore,
        # where config.py (API keys) sits on the first line. Dropping that flag
        # would commit the secrets. Pinned by test_git_add_new.py.
        _new = _untracked_source(_git)
        if _new:
            _git("add", *_new)

        commit_title = f"v{new_ver} — nightly {date_str}"
        if param_changes or ai_changes:
            total = len(param_changes) + len(ai_changes)
            commit_title += f": {total} improvement{'s' if total != 1 else ''}"

        _git("commit", "-m", commit_title)
        push = _git("push")

        if push.returncode == 0:
            tg.dm_owner(
                f"🚀 <b>v{new_ver}</b> pushed to GitHub\n"
                f"  {len(param_changes)} param change{'s' if len(param_changes)!=1 else ''} · "
                f"{len(ai_changes)} code improvement{'s' if len(ai_changes)!=1 else ''}"
            )
            # The public half. Subscribers never saw that the bot was being
            # improved -- version_push has always told only the owner. Bullets
            # are derived from WHICH FILES changed (brand.highlights_for), never
            # from the nightly model's own free text, so nothing unreviewed and
            # untranslated can reach the channel.
            try:
                changed = _git("diff", "--name-only", "HEAD~1", "HEAD").stdout.split()
            except Exception:
                changed = []
            try:
                tg.send_version_update(new_ver, changed_files=changed)
            except Exception as e:
                tg.dm_owner(f"⚠️ version update post failed: {tg.esc(e)}")
        else:
            tg.dm_owner(f"⚠️ Git push failed: <code>{tg.esc(push.stderr[:300])}</code>")

        os.remove(REPORT)

    except Exception as e:
        import traceback
        tg.dm_owner(f"⚠️ version_push error: {tg.esc(e)}\n<code>{tg.esc(traceback.format_exc()[:400])}</code>")
