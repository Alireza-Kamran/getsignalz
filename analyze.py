"""
Performance analysis engine for the self-learn session.
Produces clean statistics the agent uses to make strategy decisions.
"""
import json, os, re, glob
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


def _r_of(t):
    """Realised R for a closed trade, measured off the ORIGINAL stop.

    `sl` is rewritten in place every time the ratchet fires, so measuring
    against it would report every ratcheted winner as roughly 0R. sl_orig is
    the risk actually taken at entry, which is what every backtest reports.
    """
    entry, ex, d = t.get("entry"), t.get("exit"), t.get("direction")
    sl = t.get("sl_orig") or t.get("sl")
    if None in (entry, sl, ex) or not d or entry == sl:
        return None
    return (ex - entry) * d / abs(entry - sl)


def _sig_for(trade, signals):
    """Join a trade back to the signal that opened it.

    Matched on the nearest signal timestamp within 2h, not on the calendar
    date. The date-only join this replaces silently mismatched whenever the
    same coin traded twice in one day -- possible at MAX_TRADES=2 over a
    20-coin watchlist, and it attributes one trade's entry conditions to the
    other's outcome.
    """
    ot = trade.get("open_time") or ""
    best, best_gap = None, None
    for s in signals:
        if s.get("coin") != trade.get("coin"):
            continue
        try:
            gap = abs((datetime.fromisoformat(s["time"].replace("Z", "+00:00"))
                       - datetime.fromisoformat(ot.replace("Z", "+00:00"))).total_seconds())
        except Exception:
            continue
        if gap <= 7200 and (best_gap is None or gap < best_gap):
            best, best_gap = s, gap
    return best


def _stretch_of(sig):
    """Pull the ATR stretch out of a signal.

    S2 does not store it as a field -- it only survives inside the human
    reason string "4.2 ATR from mean" (live.py:733), so it is parsed back out
    rather than lost. Returns None rather than guessing if the shape changes.
    """
    if sig.get("stretch") is not None:
        return abs(float(sig["stretch"]))
    for reason in sig.get("reasons", []):
        if "ATR from mean" in reason:
            try:
                return abs(float(reason.split()[0]))
            except (ValueError, IndexError):
                return None
    return None


_SCAN_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| INFO \| ([A-Z]{2,6})\s+\$([\d.]+)"
    r"\s+RSI (\d+)\s+ADX (\d+)"
)


def _scan_census(logs=None):
    """Per-coin scan observations recovered from the bot logs.

    Every hour the bot prints RSI/ADX for all 20 coins and then throws the
    reading away unless it becomes a signal. That discarded stream is the only
    part of this system with real statistical power: 11 closed trades give a
    win-rate sigma of ~15pp, while the same window holds thousands of
    coin-observations. Both measurements below are things the trade journal
    physically cannot answer, because they are about the setups that never
    became trades.

    Returns {coin: [(timestamp, price, rsi, adx), ...]}.
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log"))[-1:] + [BOTLOG_F]
    per = defaultdict(list)
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = _SCAN_RE.match(line)
                    if m:
                        per[m.group(2)].append((m.group(1), float(m.group(3)),
                                                int(m.group(4)), int(m.group(5))))
        except OSError:
            continue
    return per


def _feed_staleness(per):
    """Fraction of consecutive hourly bars that repeated the previous price.

    A frozen bar is not a cosmetic problem: RSI and ADX computed across
    repeated prices are fiction, and a signal derived from them is fiction
    priced with real money. On 2026-08-05 this feed carried 18.5% frozen bars
    against 0.4% on mainnet, which is why every strategy2 constant is currently
    held void pending re-derivation. Tracking it nightly is how we know whether
    that re-derivation is even possible yet.
    """
    out = {}
    for coin, obs in per.items():
        if len(obs) < 2:
            continue
        frozen = run = longest = 0
        for i in range(1, len(obs)):
            if obs[i][1] == obs[i - 1][1]:
                frozen += 1
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        out[coin] = (len(obs), frozen, frozen / (len(obs) - 1), longest)
    return out


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

    # A trade is a win if it MADE MONEY, not if it exited via the take-profit.
    # Strategy 2 cancels its TP the moment the ratchet arms and exits every
    # single trade -- winners included -- through the stop, so result=="tp" is
    # unreachable under the live engine and classifying on it reported 0% win
    # rate on every coin, every score band and every factor while total P&L was
    # positive. That is not cosmetic: the standing coin-removal bar is "<30% WR
    # AND negative total P&L over 5+ trades", and the WR half of it was stuck at
    # 0 for all coins. (Found 2026-08-04, with AVAX showing WR:0% AvgPnL:+15.7%.)
    def _won(t):
        return (t.get("lev_pct") or 0) > 0

    wins   = [t for t in trades if _won(t)]
    losses = [t for t in trades if not _won(t)]

    # ── Per-coin performance ───────────────────────────────────────
    lines.append(f"\n── PER-COIN PERFORMANCE ──")
    coin_stats = defaultdict(lambda: {"n":0,"w":0,"pct_sum":0.0})
    for t in trades:
        c = t["coin"]
        coin_stats[c]["n"] += 1
        coin_stats[c]["w"] += 1 if _won(t) else 0
        coin_stats[c]["pct_sum"] += t.get("lev_pct", 0) or 0

    for coin, cs in sorted(coin_stats.items(), key=lambda x: -x[1]["pct_sum"]):
        wr = cs["w"]/cs["n"]*100
        ev = cs["pct_sum"]/cs["n"]
        lines.append(f"  {coin:<8} {cs['n']} trades  WR:{wr:.0f}%  AvgPnL:{ev:+.1f}%")

    # ── R-multiple distribution ────────────────────────────────────
    # The published leveraged % is distorted by whatever leverage Hyperliquid
    # actually grants: strategy2.signal sizes so a 1R stop costs MAX_LEV_LOSS
    # (20%), but the exchange clamps it, so two near-symmetric +-1R ETH trades
    # printed as -26.1% and +17.4% (2026-08-02). Account risk is unaffected --
    # size comes from risk_usd / stop distance -- but only R is comparable
    # between trades, and R is what every backtest reports. Read this table.
    #
    # NOTE on sources: journal.json preserves the entry stop, so R off it is
    # correct. journal_s2.json does NOT -- the ratchet overwrites `sl` in
    # place, so AVAX's +2.63R reads as +0.88R there. Compute R from this
    # journal, or from sl_orig, never from journal_s2's `sl`.
    r_vals = []
    for t in trades:
        r = _r_of(t)
        if r is not None:
            r_vals.append((t["coin"], r))

    lines.append(f"\n── R-MULTIPLE DISTRIBUTION ──")
    if r_vals:
        rs = [r for _, r in r_vals]
        n  = len(rs)
        lines.append(f"  n={n}  sumR:{sum(rs):+.2f}  meanR:{sum(rs)/n:+.3f}  "
                     f"WR:{sum(1 for r in rs if r>0)/n*100:.0f}%")
        lines.append(f"  best:{max(rs):+.2f}R  worst:{min(rs):+.2f}R  "
                     f"  >=1.5R:{sum(1 for r in rs if r>=1.5)}  >=3R:{sum(1 for r in rs if r>=3)}")
        lines.append("  " + "  ".join(f"{c}:{r:+.2f}" for c, r in r_vals[-12:]))
    else:
        lines.append("  (no trades with complete entry/sl/exit/direction)")

    # ── Direction split ────────────────────────────────────────────
    # S2's trigger is symmetric in code (strategy2.signal:219-222), but the
    # MAX_ADX gate is not symmetric in effect: measured over 20 coins x ~1500
    # 1h bars on 2026-08-14, the raw RSI+stretch trigger is near-balanced
    # (888 long / 820 short) but survival past ADX<25 is not -- the gate kills
    # 92.1% of longs and 96.7% of shorts, leaving 70 long / 27 short. Overbought
    # excursions in crypto arrive *with* trend; oversold dips happen in quiet
    # ranges. So the deployed system is structurally ~72/28 long-biased.
    #
    # Printed every night because the live book has been 100% long for 10
    # trades, and the only way to tell "expected skew" from "the short leg is
    # broken" is to watch the ratio accumulate against that 72/28 baseline.
    lines.append(f"\n── DIRECTION SPLIT ──")
    dir_stats = defaultdict(lambda: {"n":0,"w":0,"r":0.0})
    for t in trades:
        d = t.get("direction")
        if not d:
            continue
        k = "LONG" if d == 1 else "SHORT"
        dir_stats[k]["n"] += 1
        dir_stats[k]["w"] += 1 if _won(t) else 0
        dir_stats[k]["r"] += _r_of(t) or 0.0
    for k in ("LONG", "SHORT"):
        ds = dir_stats.get(k)
        if not ds or not ds["n"]:
            lines.append(f"  {k:<6} 0 trades")
            continue
        lines.append(f"  {k:<6} {ds['n']} trades  WR:{ds['w']/ds['n']*100:.0f}%  "
                     f"sumR:{ds['r']:+.2f}  meanR:{ds['r']/ds['n']:+.3f}")
    lines.append("  (structural baseline from backtest: ~72% long / ~28% short)")

    # ── Exit mechanism ─────────────────────────────────────────────
    # The single most important operating metric under the current regime, and
    # it was absent from this report until 2026-08-14. TP_R=5.0 is a backstop,
    # not a target: in 10 S2 trades the take-profit has never once filled, so
    # 100% of exits are stop-based. What separates a good night from a bad one
    # is whether the stop that filled was the ORIGINAL stop (a full -1R loss)
    # or a RATCHETED stop (locked-in profit). journal_s2 records result=="sl"
    # for both, which is why raw result codes tell you nothing.
    lines.append(f"\n── EXIT MECHANISM ──")
    exit_stats = defaultdict(lambda: {"n":0,"r":0.0})
    for t in trades:
        r = _r_of(t)
        if r is None:
            continue
        # A stop that filled beyond entry in the trade's favour can only have
        # got there by ratcheting; the original stop is always adverse.
        if t.get("result") == "tp":
            k = "TP backstop (5R)"
        elif r > 0:
            k = "ratcheted stop"
        else:
            k = "original stop"
        exit_stats[k]["n"] += 1
        exit_stats[k]["r"] += r
    for k, es in sorted(exit_stats.items(), key=lambda x: -x[1]["n"]):
        lines.append(f"  {k:<20} {es['n']:2} trades  sumR:{es['r']:+.2f}  "
                     f"meanR:{es['r']/es['n']:+.3f}")

    # ── Entry condition bands ──────────────────────────────────────
    # Replaces the old "confluence factor win rate" table, which was noise
    # twice over. It keyed on reason.split(" ")[0], so "4.2 ATR from mean"
    # became a factor literally named "4.2" and every distinct stretch value
    # got its own one-row bucket. Worse, S2 emits the SAME three reasons on
    # every signal, so the surviving keys ("rsi", "adx") could only ever report
    # the overall win rate back -- 36% against 36%, dressed up as a finding.
    #
    # What actually discriminates is the VALUE, not the presence: how deep the
    # RSI went, how stretched from the mean, how close to the ADX ceiling.
    lines.append(f"\n── ENTRY CONDITION BANDS ──")

    def _band(vals, val, labels):
        for edge, lab in zip(vals, labels):
            if val < edge:
                return lab
        return labels[-1]

    banded = defaultdict(lambda: {"n":0,"w":0,"r":0.0})
    for t in trades:
        sig = _sig_for(t, signals)
        r   = _r_of(t)
        if not sig or r is None:
            continue
        rsi_v, adx_v = sig.get("rsi"), sig.get("adx")
        stretch_v = _stretch_of(sig)
        for label in (
            f"RSI {_band([15,20,23], rsi_v, ['<15','15-20','20-23','23-25'])}"
            if rsi_v is not None else None,
            f"ADX {_band([15,20], adx_v, ['<15','15-20','20-25'])}"
            if adx_v is not None else None,
            f"stretch {_band([2.0,3.0], stretch_v, ['1.5-2','2-3','3+'])}"
            if stretch_v is not None else None,
        ):
            if label is None:
                continue
            banded[label]["n"] += 1
            banded[label]["w"] += 1 if r > 0 else 0
            banded[label]["r"] += r
    if banded:
        for label, bs in sorted(banded.items()):
            lines.append(f"  {label:<16} n={bs['n']:2}  WR:{bs['w']/bs['n']*100:3.0f}%  "
                         f"meanR:{bs['r']/bs['n']:+.3f}")
        lines.append("  (n is far too small to act on; this is an accumulator)")
    else:
        lines.append("  (no trades joined to a signal)")

    # ── Duration analysis ──────────────────────────────────────────
    lines.append(f"\n── TRADE DURATION ──")
    if wins:
        avg_win_dur  = sum(t.get("duration_h",0) or 0 for t in wins) / len(wins)
        lines.append(f"  Avg winning trade duration:  {avg_win_dur:.1f}h")
    if losses:
        avg_loss_dur = sum(t.get("duration_h",0) or 0 for t in losses) / len(losses)
        lines.append(f"  Avg losing trade duration:   {avg_loss_dur:.1f}h")

    # ── Rejection census + feed health ─────────────────────────────
    # Both are measured off the scan log rather than the journal, because both
    # ask about setups that never became trades. See _scan_census.
    try:
        import strategy2 as _s2c
        per = _scan_census()
        obs_n = sum(len(v) for v in per.values())
        if obs_n:
            lines.append(f"\n── ENTRY GATE CENSUS (scan log, n={obs_n} obs) ──")
            flat = [o for v in per.values() for o in v]
            span = f"{min(o[0] for o in flat)[:10]} → {max(o[0] for o in flat)[:10]}"
            lines.append(f"  window: {span}")
            # The scan loop prints the S1 WATCHLIST only, so this covers those
            # coins -- not all of strategy2's. Stated rather than glossed: the
            # admitted-rate below is a sample of S2's universe, not a census.
            lines.append(f"  coverage: {len(per)} logged coins of "
                         f"{len(_s2c.WATCHLIST)} in the S2 watchlist")
            for label, cand in (
                ("long  (RSI<=%d)" % _s2c.RSI_OVERSOLD,
                 [o for o in flat if o[2] <= _s2c.RSI_OVERSOLD]),
                ("short (RSI>=%d)" % _s2c.RSI_OVERBOUGHT,
                 [o for o in flat if o[2] >= _s2c.RSI_OVERBOUGHT]),
            ):
                if not cand:
                    lines.append(f"  {label}: none observed")
                    continue
                ok = sum(1 for o in cand if o[3] < _s2c.MAX_ADX)
                lines.append(
                    f"  {label}: {len(cand):4} RSI-qualified, "
                    f"{ok:3} also ADX<{_s2c.MAX_ADX} = {ok/len(cand)*100:.1f}% admitted"
                )
                for lo, hi in ((0, 20), (20, 25), (25, 30), (30, 40), (40, 999)):
                    n = sum(1 for o in cand if lo <= o[3] < hi)
                    if n:
                        lines.append(
                            f"      ADX {lo:>3}-{hi:<3} n={n:>4} "
                            f"({n/len(cand)*100:>5.1f}%) "
                            f"{'pass' if hi <= _s2c.MAX_ADX else 'BLOCKED'}"
                        )
            lines.append("  NOTE: RSI extremes are CAUSED by strong directional")
            lines.append("  moves, which is exactly what raises ADX. The oversold")
            lines.append("  and ranging conditions are anti-correlated by")
            lines.append("  construction -- this is the binding constraint on")
            lines.append("  trade frequency, not a tuning detail.")

            stale = _feed_staleness(per)
            if stale:
                lines.append(f"\n── FEED HEALTH (frozen bars) ──")
                overall_f = sum(s[1] for s in stale.values())
                overall_n = sum(s[0] - 1 for s in stale.values())
                for coin, (n, fz, pct, longest) in sorted(
                        stale.items(), key=lambda x: -x[1][2]):
                    flag = "  <-- unusable" if pct > 0.10 else ""
                    lines.append(f"  {coin:<6} {pct*100:5.1f}% frozen  "
                                 f"(max {longest} bars stalled){flag}")
                lines.append(f"  OVERALL: {overall_f}/{overall_n} = "
                             f"{overall_f/overall_n*100:.1f}%")
                lines.append("  (baseline: 18.5% testnet 2026-08-05 vs 0.4% mainnet)")
    except Exception as e:
        lines.append(f"\n  (scan census unavailable: {e})")

    # ── Current config ─────────────────────────────────────────────
    #
    # This block used to print strategy_config.json -- MIN_SCORE, TRAIL_PCT 8%,
    # SESSION 11-24, MIN_ADX 30 and a 12-coin watchlist -- under the heading
    # "CURRENT STRATEGY CONFIG". Every one of those values is inert: they
    # belong to the retired S1 engine and have had no effect since S1_ENABLED
    # went False. Labelling them "current" is how a reader ends up tuning a
    # trail that has never executed a trade, so the live constants are printed
    # instead and the dead ones are marked as dead.
    lines.append(f"\n── CURRENT STRATEGY CONFIG (LIVE = strategy2.py) ──")
    try:
        import strategy2 as _s2
        lines.append(f"  RSI:            {_s2.RSI_OVERSOLD} / {_s2.RSI_OVERBOUGHT}")
        lines.append(f"  MAX_ADX:        {_s2.MAX_ADX}")
        lines.append(f"  MIN_STRETCH_ATR:{_s2.MIN_STRETCH_ATR}")
        lines.append(f"  SL_ATR_MULT:    {_s2.SL_ATR_MULT}")
        lines.append(f"  TP_R:           {_s2.TP_R}  (backstop -- has never filled)")
        lines.append(f"  TRAIL_START_R:  {_s2.TRAIL_START_R}   STEP_R: {_s2.TRAIL_STEP_R}")
        lines.append(f"  MAX_TRADES:     {_s2.MAX_TRADES}")
        lines.append(f"  S2_RISK_PCT:    {_s2.S2_RISK_PCT*100:.2f}% of account")
        lines.append(f"  WATCHLIST:      {len(_s2.WATCHLIST)} coins")
        lines.append(f"  (all owner-locked -- report, do not tune)")
    except Exception as e:
        lines.append(f"  (could not read strategy2 constants: {e})")
    lines.append(f"\n── DEAD S1 CONFIG (strategy_config.json -- no effect) ──")
    lines.append(f"  MIN_SCORE:      {config.get('min_score', 6)}")
    lines.append(f"  TRAIL_PCT:      {config.get('trail_pct', 0.08)*100:.0f}%")
    lines.append(f"  SESSION:        {config.get('session_start_utc',7)}:00-{config.get('session_end_utc',22)}:00 UTC")
    lines.append(f"  MIN_ADX:        {config.get('min_adx', 20)}")
    lines.append(f"  WATCHLIST:      {', '.join(config.get('watchlist', []))}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


# ── Trust Score ──────────────────────────────────────────────────────────────
# A single 0-100 number answering "how much has this bot actually earned our
# trust so far" — five pillars, each computed from data already logged
# elsewhere (journal.json, state.json, CHANGELOG.md, selflearn.log, bot.log).
# No new tracking required. Cached briefly since this gets called from the
# live dashboard-refresh hot path (tracker.py), which can fire every ~60s.

CHANGELOG_F  = "/root/trade/CHANGELOG.md"
SELFLEARN_F  = "/root/trade/selflearn.log"
BOTLOG_F     = "/root/trade/bot.log"

_health_cache = {"time": None, "result": None}
_HEALTH_TTL_S = 900  # 15 min


def _edge_confidence(journal, config):
    """Pillar 1 (40 pts): sample size + sign of EV in the core edge band (score >= MIN_SCORE).

    S2 (mean-reversion engine) logs every signal with score=0 because it has
    no scoring system — the entry gate is purely RSI/ADX/stretch. Filtering
    score >= 7 excluded every S2 trade, making Edge Confidence stuck at 0/40
    even with a fully live strategy. When S2 is the engine, treat any trade
    that has a corresponding fired signal as qualifying.
    """
    min_score = config.get("min_score", 7)
    trades = [t for t in journal.get("trades", []) if t.get("result")]
    signals = journal.get("signals", [])

    # S2 engine: identified by the disabled-S1 marker in the config's engine field.
    s2_engine = "S1_DISABLED" in config.get("engine", "")

    qualifying = []
    for t in trades:
        sig = _sig_for(t, signals)
        if s2_engine:
            if sig and sig.get("fired"):
                qualifying.append(t)
        else:
            score = sig["score"] if sig else 0
            if score >= min_score:
                qualifying.append(t)

    n = len(qualifying)
    avg_pct = sum(t.get("lev_pct", 0) or 0 for t in qualifying) / n if n else 0.0

    sample_pts = min(n, 20) / 20 * 25
    edge_pts   = 15.0 if (n >= 5 and avg_pct > 0) else 0.0
    return round(sample_pts + edge_pts, 1), {"n": n, "avg_pct": round(avg_pct, 1)}


def _risk_control(journal, state):
    """Pillar 2 (20 pts): current drawdown containment + no MAX_LEV_LOSS breaches."""
    stats = state.get("stats", {})
    current_dd = stats.get("current_drawdown_pct", 0.0)

    dd_pts = max(0.0, 15 * (1 - current_dd / 40))
    dd_pts = min(dd_pts, 15.0)

    trades = journal.get("trades", [])
    breach = any(abs(t.get("lev_pct") or 0) > 26 for t in trades)
    breach_pts = 0.0 if breach else 5.0

    return round(dd_pts + breach_pts, 1), {"current_drawdown_pct": current_dd, "cap_breach": breach}


def _system_reliability():
    """Pillar 3 (20 pts): nightly cron health (last 7 sessions) + recent bot.log cleanliness."""
    cron_pts = 10.0
    try:
        with open(SELFLEARN_F) as f:
            text = f.read()
        sessions = text.split("SELF-LEARN: ")[1:][-7:]
        if sessions:
            bad = sum(1 for s in sessions if any(
                m in s for m in ("FATAL", "command not found", "OAuth session expired",
                                  "Failed to authenticate")))
            cron_pts = round((1 - bad / len(sessions)) * 10, 1)
    except FileNotFoundError:
        pass

    log_pts = 10.0
    try:
        with open(BOTLOG_F) as f:
            recent = f.readlines()[-3000:]
        bad_lines = sum(1 for l in recent if "Traceback" in l or "Ghost close" in l)
        log_pts = max(0.0, 10 - bad_lines * 2)
    except FileNotFoundError:
        pass

    return round(cron_pts + log_pts, 1), {"cron_pts": cron_pts, "log_pts": log_pts}


def _strategy_stability():
    """Pillar 4 (10 pts): days since the last CHANGELOG entry flagged as a critical fix/rogue bug."""
    try:
        with open(CHANGELOG_F) as f:
            text = f.read()
    except FileNotFoundError:
        return 10.0, {"days_since_critical": None}

    import re
    entries = re.findall(r"## v[\d.]+\s*[—-]+\s*(\d{4}-\d{2}-\d{2}).*?(?=\n## v|\Z)", text, re.DOTALL)
    critical_dates = []
    for block in re.finditer(r"## v[\d.]+\s*[—-]+\s*(\d{4}-\d{2}-\d{2})(.*?)(?=\n## v|\Z)", text, re.DOTALL):
        date_str, body = block.groups()
        if re.search(r"critical fix|root cause", body, re.IGNORECASE):
            critical_dates.append(date_str)

    if not critical_dates:
        return 10.0, {"days_since_critical": None}

    most_recent = max(critical_dates)
    days_since = (datetime.utcnow().date() - datetime.strptime(most_recent, "%Y-%m-%d").date()).days
    pts = round(10 * min(days_since / 14, 1), 1)
    return pts, {"days_since_critical": days_since, "last_critical_date": most_recent}


def _watchlist_health(journal, config):
    """Pillar 5 (10 pts): no active-watchlist coin sitting past its removal bar, plus data coverage."""
    watchlist = config.get("watchlist", [])
    trades = [t for t in journal.get("trades", []) if t.get("result")]

    coin_n = defaultdict(int)
    coin_wins = defaultdict(int)
    coin_pct = defaultdict(float)
    for t in trades:
        c = t["coin"]
        coin_n[c] += 1
        coin_wins[c] += 1 if (t.get("lev_pct") or 0) > 0 else 0
        coin_pct[c] += t.get("lev_pct", 0) or 0

    overdue = [c for c in watchlist
               if coin_n[c] >= 5 and coin_wins[c] == 0 and coin_pct[c] < 0]
    overdue_pts = 0.0 if overdue else 6.0

    covered = sum(1 for c in watchlist if coin_n[c] >= 3)
    coverage_pts = round(4 * (covered / len(watchlist)), 1) if watchlist else 0.0

    return round(overdue_pts + coverage_pts, 1), {"overdue_removal": overdue, "coverage": f"{covered}/{len(watchlist)}"}


def health_score(force=False, save=False):
    """Compute the 0-100 Trust Score. Cached 15 min unless force=True.
    If save=True, appends the result to journal.json's `reviews` list."""
    now = datetime.utcnow()
    if not force and _health_cache["time"] and (now - _health_cache["time"]).total_seconds() < _HEALTH_TTL_S:
        return _health_cache["result"]

    journal = load_journal()
    state   = load_state()
    config  = load_config()

    p1, d1 = _edge_confidence(journal, config)
    p2, d2 = _risk_control(journal, state)
    p3, d3 = _system_reliability()
    p4, d4 = _strategy_stability()
    p5, d5 = _watchlist_health(journal, config)

    total = round(p1 + p2 + p3 + p4 + p5)

    result = {
        "time": now.isoformat(),
        "total": total,
        "pillars": {
            "edge_confidence":     {"points": p1, "max": 40, **d1},
            "risk_control":        {"points": p2, "max": 20, **d2},
            "system_reliability":  {"points": p3, "max": 20, **d3},
            "strategy_stability":  {"points": p4, "max": 10, **d4},
            "watchlist_health":    {"points": p5, "max": 10, **d5},
        },
    }

    _health_cache["time"]   = now
    _health_cache["result"] = result

    if save:
        try:
            import journal as journal_mod
            journal_mod.log_review(result)
        except Exception as e:
            print(f"[analyze] health_score save error: {e}")

    return result


def format_health_score(result=None) -> str:
    r = result or health_score()
    lines = [f"🏆 Trust Score: {r['total']}/100", ""]
    for name, p in r["pillars"].items():
        lines.append(f"  {name.replace('_',' ').title():<20} {p['points']:.1f}/{p['max']}")
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
        r'^WATCHLIST = \[.*?\]',
        f'WATCHLIST = {wl_str}',
        code, flags=re.DOTALL | re.MULTILINE
    )

    # Replace MIN_SCORE
    # Anchored to the start of a line: an unanchored pattern also rewrites
    # mentions of MIN_SCORE inside comments and docstrings, which is how a
    # 2026-07-28 run silently mangled _scannable_timeframes()'s docstring.
    code = re.sub(r'^MIN_SCORE\s*=\s*\d+', f'MIN_SCORE   = {config["min_score"]}',
                  code, flags=re.MULTILINE)

    with open(trader_path, "w") as f:
        f.write(code)

    return f"Applied: MIN_SCORE={config['min_score']}, WATCHLIST={len(config['watchlist'])} coins"


if __name__ == "__main__":
    print(full_report())
