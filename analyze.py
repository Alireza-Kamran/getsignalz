"""
Performance analysis engine for the self-learn session.
Produces clean statistics the agent uses to make strategy decisions.
"""
import json, os
from datetime import datetime, timezone
from collections import defaultdict

JOURNAL_F = "/root/trade/journal.json"
STATE_F   = "/root/trade/state.json"
CONFIG_F  = "/root/trade/strategy_config.json"


def load_journal():
    if not os.path.exists(JOURNAL_F):
        return {"trades": [], "signals": []}
    with open(JOURNAL_F) as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_F):
        return {}
    with open(STATE_F) as f:
        return json.load(f)


def load_config():
    if not os.path.exists(CONFIG_F):
        return {}
    with open(CONFIG_F) as f:
        return json.load(f)


def save_config(cfg):
    cfg["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(CONFIG_F, "w") as f:
        json.dump(cfg, f, indent=2)


def full_report():
    """
    Produce a full performance report as a string.
    This is what the self-learn agent reads to make decisions.
    """
    journal = load_journal()
    state   = load_state()
    config  = load_config()

    trades  = [t for t in journal.get("trades", []) if t.get("result")]
    signals = journal.get("signals", [])

    lines = []
    lines.append("=" * 60)
    lines.append("PERFORMANCE ANALYSIS REPORT")
    lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    # ── Overall stats ──────────────────────────────────────────────
    stats = state.get("stats", {})
    lines.append(f"\n── OVERALL STATS ──")
    lines.append(f"Total trades:    {len(trades)}")
    lines.append(f"Win rate:        {stats.get('win_rate', 0):.1f}%")
    lines.append(f"Total P&L:       {stats.get('total_pct', 0):+.1f}% (leveraged)")
    lines.append(f"Max drawdown:    -{stats.get('max_drawdown_pct', 0):.1f}%")
    lines.append(f"Peak equity:     ${stats.get('peak_balance', 0):.2f}")

    if len(trades) < 5:
        lines.append(f"\n⚠️  Only {len(trades)} closed trades — insufficient for statistical analysis.")
        lines.append("    Recommendation: observe patterns, fix bugs, prepare for when data grows.")
        lines.append("=" * 60)
        return "\n".join(lines)

    wins   = [t for t in trades if t["result"] == "tp"]
    losses = [t for t in trades if t["result"] == "sl"]

    # ── Per-coin performance ───────────────────────────────────────
    lines.append(f"\n── PER-COIN PERFORMANCE ──")
    coin_stats = defaultdict(lambda: {"n":0,"w":0,"pct_sum":0.0})
    for t in trades:
        c = t["coin"]
        coin_stats[c]["n"] += 1
        coin_stats[c]["w"] += 1 if t["result"]=="tp" else 0
        coin_stats[c]["pct_sum"] += t.get("lev_pct", 0) or 0

    for coin, cs in sorted(coin_stats.items(), key=lambda x: -x[1]["pct_sum"]):
        wr = cs["w"]/cs["n"]*100
        ev = cs["pct_sum"]/cs["n"]
        lines.append(f"  {coin:<8} {cs['n']} trades  WR:{wr:.0f}%  AvgPnL:{ev:+.1f}%")

    # ── Confluence factor analysis ─────────────────────────────────
    lines.append(f"\n── CONFLUENCE FACTOR WIN RATES ──")
    factor_stats = defaultdict(lambda: {"win":0,"lose":0})

    for t in trades:
        sig = next((s for s in signals
                    if s["coin"]==t["coin"]
                    and s["time"][:10]==t["open_time"][:10]), None)
        if not sig:
            continue
        result = t["result"]
        for reason in sig.get("reasons", []):
            # Normalize reason to factor key
            key = reason.split(" ")[0].lower() if reason else "unknown"
            if result == "tp":
                factor_stats[key]["win"] += 1
            else:
                factor_stats[key]["lose"] += 1

    for factor, fs in sorted(factor_stats.items(),
                              key=lambda x: -(x[1]["win"]/(x[1]["win"]+x[1]["lose"]))):
        total = fs["win"] + fs["lose"]
        if total < 2:
            continue
        wr = fs["win"] / total * 100
        lines.append(f"  {factor:<20} WR:{wr:.0f}%  ({fs['win']}W/{fs['lose']}L)")

    # ── Score threshold analysis ───────────────────────────────────
    lines.append(f"\n── PERFORMANCE BY SIGNAL SCORE ──")
    score_stats = defaultdict(lambda: {"win":0,"lose":0,"pct":0.0})
    for t in trades:
        sig = next((s for s in signals
                    if s["coin"]==t["coin"]
                    and s["time"][:10]==t["open_time"][:10]), None)
        score = sig["score"] if sig else 0
        score_stats[score]["win"]  += 1 if t["result"]=="tp" else 0
        score_stats[score]["lose"] += 1 if t["result"]=="sl" else 0
        score_stats[score]["pct"]  += t.get("lev_pct", 0) or 0

    for score in sorted(score_stats.keys()):
        ss = score_stats[score]
        total = ss["win"] + ss["lose"]
        wr = ss["win"]/total*100
        ev = ss["pct"]/total
        lines.append(f"  Score {score}: {total} trades  WR:{wr:.0f}%  AvgPnL:{ev:+.1f}%")

    # ── Duration analysis ──────────────────────────────────────────
    lines.append(f"\n── TRADE DURATION ──")
    if wins:
        avg_win_dur  = sum(t.get("duration_h",0) or 0 for t in wins) / len(wins)
        lines.append(f"  Avg winning trade duration:  {avg_win_dur:.1f}h")
    if losses:
        avg_loss_dur = sum(t.get("duration_h",0) or 0 for t in losses) / len(losses)
        lines.append(f"  Avg losing trade duration:   {avg_loss_dur:.1f}h")

    # ── Current config ─────────────────────────────────────────────
    lines.append(f"\n── CURRENT STRATEGY CONFIG ──")
    lines.append(f"  MIN_SCORE:      {config.get('min_score', 6)}")
    lines.append(f"  MAX_TRADES:     {config.get('max_trades', 2)}")
    lines.append(f"  TRAIL_PCT:      {config.get('trail_pct', 0.08)*100:.0f}%")
    lines.append(f"  SESSION:        {config.get('session_start_utc',7)}:00-{config.get('session_end_utc',22)}:00 UTC")
    lines.append(f"  MIN_ADX:        {config.get('min_adx', 20)}")
    lines.append(f"  WATCHLIST:      {', '.join(config.get('watchlist', []))}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def apply_config_to_trader():
    """
    Read strategy_config.json and apply values to trader.py.
    The agent calls this after updating strategy_config.json.
    """
    config = load_config()
    trader_path = "/root/trade/trader.py"

    with open(trader_path) as f:
        code = f.read()

    replacements = {
        f"MIN_SCORE   = {old}": f"MIN_SCORE   = {config['min_score']}"
        for old in range(1, 15)
        if f"MIN_SCORE   = {old}" in code
    }

    # Apply WATCHLIST
    wl_str = '[\n    "' + '", "'.join(config['watchlist']) + '"\n]'

    import re
    # Replace WATCHLIST
    code = re.sub(
        r'WATCHLIST = \[.*?\]',
        f'WATCHLIST = {wl_str}',
        code, flags=re.DOTALL
    )

    # Replace MIN_SCORE
    code = re.sub(r'MIN_SCORE\s*=\s*\d+', f'MIN_SCORE   = {config["min_score"]}', code)

    with open(trader_path, "w") as f:
        f.write(code)

    return f"Applied: MIN_SCORE={config['min_score']}, WATCHLIST={len(config['watchlist'])} coins"


if __name__ == "__main__":
    print(full_report())
