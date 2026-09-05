"""
Performance analysis engine for the self-learn session.
Produces clean statistics the agent uses to make strategy decisions.
"""
import json, os, re, glob
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from io_safe import atomic_write_json, atomic_write_text

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
    atomic_write_json(CONFIG_F, cfg, default=None)


def _entry_stop(t):
    """The stop as it stood at ENTRY -- the denominator of every R figure.

    `sl` is rewritten in place every time the ratchet fires, and it only ever
    fires on trades that went far enough to arm it. So reading `sl` does not
    add noise, it adds a bias that lands exclusively on the winners: SOL
    (2026-08-16) armed at +2.5R, had its stop rewritten from 73.781 to 75.897,
    and every R computed off that stop came out at ~1.0R instead of ~2.5R.
    Every loser reads correctly, so the error is invisible in aggregate and
    silently flattens the fat tail the whole strategy depends on.

    Both R consumers must go through here. `_r_of` already knew this rule;
    `_excursion_stats` re-derived it independently and got it wrong for a day.
    """
    return t.get("sl_orig") or t.get("sl")


def _r_of(t):
    """Realised R for a closed trade, measured off the ORIGINAL stop."""
    entry, ex, d = t.get("entry"), t.get("exit"), t.get("direction")
    sl = _entry_stop(t)
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

    Every hour the bot prints RSI/ADX per coin and then throws the reading
    away unless it becomes a signal. That discarded stream is the only
    part of this system with real statistical power: 11 closed trades give a
    win-rate sigma of ~15pp, while the same window holds thousands of
    coin-observations. Both measurements below are things the trade journal
    physically cannot answer, because they are about the setups that never
    became trades.

    HISTORY NOTE, and it matters when comparing coins: before 2026-09-05 the
    bot's scan loop iterated the RETIRED S1 watchlist (12 coins) while the
    engine traded strategy2.WATCHLIST (20), so ADA, BNB, BTC, FIL, LDO, NEAR,
    TIA and XLM have NO observations at all before that date. Any per-coin rate
    computed here is therefore a much shorter series for those eight, and any
    cross-coin comparison spanning the boundary is comparing unequal windows.

    Returns {coin: [(timestamp, price, rsi, adx), ...]}.
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
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


# The bot prints one of these per hour, immediately after the quiet-hours gate,
# so their presence is a direct record of "the main loop was alive and working".
_CANDLE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):\d{2}:\d{2} \| INFO \| .*Candle ")

# review.should_quiet(h) is `2 <= h < 4`, and the candle header is logged after
# that gate, so UTC hours 2 and 3 are legitimately silent every day.
QUIET_HOURS = {2, 3}


def _availability(logs=None):
    """Hours in which the bot logged a candle, and the gaps where it did not.

    Added 2026-08-23 after the standing 'check bot.log candle continuity FIRST'
    checklist step failed in practice: the host was down 2026-08-21 22:02 ->
    08-22 17:50 (19h48m) and the 08-22 nightly session, which ran after the
    outage had already ended, reported two unrelated improvements and never
    noticed. A manual step that only works when someone remembers it is not a
    control. An open ETH SHORT sat through the whole gap with its ratchet frozen.

    Returns (seen_hours, gaps, window) where gaps is a list of
    (start_dt, end_dt, n_missing_hours) covering only unexplained absences --
    quiet hours are excluded, because they are supposed to be empty.
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
    seen = set()
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = _CANDLE_RE.match(line)
                    if m:
                        seen.add(datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                                   "%Y-%m-%d %H"))
        except OSError:
            continue
    if not seen:
        return set(), [], None
    lo, hi = min(seen), max(seen)
    missing, cur = [], lo
    while cur <= hi:
        if cur.hour not in QUIET_HOURS and cur not in seen:
            missing.append(cur)
        cur += timedelta(hours=1)
    # Collapse consecutive missing hours into single gaps. A one-hour blip and a
    # twenty-hour blackout are different events and must not be counted alike.
    #
    # Two missing hours separated only by quiet hours are still ONE outage: the
    # 2026-08-21 blackout ran 08-21 23:00 -> 08-22 16:00 straight through the
    # 02-03 quiet window, and splitting it there would report two ~9h gaps and
    # understate the worst event on record.
    def _only_quiet_between(a, b):
        cur = a + timedelta(hours=1)
        while cur < b:
            if cur.hour not in QUIET_HOURS:
                return False
            cur += timedelta(hours=1)
        return True

    gaps = []
    for ts in missing:
        if gaps and _only_quiet_between(gaps[-1][1], ts):
            gaps[-1][1] = ts
            gaps[-1][2] += 1
        else:
            gaps.append([ts, ts, 1])
    return seen, [(g[0], g[1], g[2]) for g in gaps], (lo, hi)


def _naive_utc(ts):
    """Parse a timestamp from the journals to a NAIVE UTC datetime, or None.

    Every timestamp this system writes is UTC, but not every one says so the
    same way. Records written by the live loop are naive ("...T00:01:48.9"),
    while the OP 2026-08-13 record -- rebuilt by an ad-hoc repair script after
    save_state erased it, and flagged `reconstructed_from_journal` -- carries an
    explicit "+00:00". Mixing the two in a single comparison raises
    TypeError("can't compare offset-naive and offset-aware datetimes"), and on
    2026-09-03 that ONE record was aborting both the AVAILABILITY
    cross-reference and the whole LOOP LATENCY section -- the section added the
    previous night specifically to answer that night's primary question.

    So: strip the offset rather than trust it, and never let a single
    unparseable row decide what the rest of the report is allowed to measure.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _open_during(gap_start, gap_end, state):
    """Positions that were open across a downtime gap, from state.json.

    This is the half that makes an availability gap actionable: scanning
    downtime costs missed signals, but downtime with a live position costs
    ratchet management on money already at risk.
    """
    out = []
    for t in list(state.get("closed_trades", [])) + list(
            state.get("tracked", {}).values()):
        op = _naive_utc(t.get("opened_at"))
        if op is None:
            continue
        cl = _naive_utc(t.get("closed_at"))
        if op <= gap_end and (cl is None or cl >= gap_start):
            out.append((t.get("coin", "?"), "SHORT" if t.get("dir") == -1 else "LONG",
                        cl is None))
    return out


# Wall-clock timestamp AND the candle label, which _CANDLE_RE deliberately does
# not separate (availability only cares that an hour was seen at all).
_CANDLE_LAG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| INFO \| .*Candle (\d{2}):00 UTC")

# A candle header this far behind its own hour means the loop was blocked, not
# merely busy: an ordinary pass logs the header ~10s past the hour.
_LAG_BLOCKED_SEC = 300

# The startup banner. One per process start, so counting these counts restarts
# -- which the latency section previously mistook for 50-minute loop stalls.
_RESTART_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| INFO \| \s*GETSIGNAL AI")


def _restarts(logs=None):
    """Process starts per day, newest last. A restart is cheap (~5s) but not
    free: it re-reads state.json, re-restores open trades, and re-prints the
    current candle header. Nine of them in one afternoon (2026-09-04) is a
    signal in its own right -- it just is not the signal the latency section
    used to report it as."""
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
    seen = set()
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = _RESTART_RE.match(line)
                    if m:
                        seen.add((m.group(1), m.group(2), m.group(3), m.group(4)))
        except OSError:
            continue
    per = defaultdict(int)
    for d, *_ in seen:
        per[d] += 1
    return dict(per)


# Timestamped lines that say the EXCHANGE was unreachable during a stall.
_VENUE_DOWN_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| (?:WARNING|ERROR) \| "
    r"(?:HL API \d+|Cycle error)")


def _stall_cause(due, wall, venue_events):
    """Why the loop was late: the venue was down, or we blocked ourselves.

    These are opposite risks and the report used to print both as "ratchet
    frozen". When Hyperliquid 502s (2026-09-02 07:00, AAVE SHORT, 27.9 min) the
    ratchet cannot advance -- but the STOP IS ALREADY RESTING ON THE EXCHANGE,
    so the position is protected and only the upside is stalled, and there is
    no code change on our side that prevents it. When we block ourselves
    (2026-08-22..24, the inline nightly review) the venue is healthy, the loop
    is simply not looking, and that IS ours to fix.

    Conflating them invites a future session to "fix" an outage it does not own.
    """
    hits = sum(1 for t in venue_events if due <= t <= wall)
    return ("venue down", hits) if hits else ("self-blocked", 0)


def _feed_quality_split(trades, stale):
    """Join closed trades to their own coin's frozen-bar rate.

    Returns (clean, dirty, unmeasured) where unmeasured is [(coin, R), ...] for
    trades whose coin never appeared in the scan log.

    Extracted from full_report and made to RETURN the leftovers rather than
    drop them. The inline version did `s = stale.get(coin)` and skipped the
    trade when it was None, with no counter: that silently discarded 6 of 19
    closed trades (32% of the book, 5 of them losses, meanR -0.717) because
    their coins were absent from the RETIRED S1 watchlist the scan loop used to
    iterate. The exclusion had nothing to do with feed quality, and the
    discarded cohort was worse than either published bucket -- so the reported
    clean-vs-dirty gap was part real effect, part selection artifact.
    """
    clean, dirty, unmeasured = [], [], []
    for t in trades:
        r = _r_of(t)
        if r is None:
            continue
        s = stale.get(t.get("coin"))
        if not s:
            unmeasured.append((t.get("coin"), r))
        elif s[2] < 0.03:
            clean.append(r)
        else:
            dirty.append(r)
    return clean, dirty, unmeasured


def _loop_latency(logs=None, state=None):
    """How long after each hour the loop actually got round to that candle.

    AVAILABILITY answers 'did the bot scan this hour', which is binary and
    therefore blind to a loop that scanned every hour but twenty minutes late.
    Found 2026-09-02 by throwaway code, and made permanent here for the same
    reason AVAILABILITY and SUPERVISION were: the last three times a defect was
    found by a script written that night and then thrown away, the next session
    had no way to see whether it had come back.

    The number that matters is not the delay itself but WHAT WAS BLOCKED. Until
    2026-09-02 nightly_review() -- which runs ai_brain and _self_improve()
    inline -- sat above position management in live.py's loop, so every minute
    of 23:00 delay was a minute _check_trail_s2 did not run. The ratchet is the
    only mechanism in this system that produces profit.

    Returns (by_hour, worst, frozen) where by_hour maps hour-of-day to
    (n, median, p90, max, n_over_5min) and frozen lists the review-nights that
    crossed an open position.
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
    rows = set()
    venue = []
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    v = _VENUE_DOWN_RE.match(line)
                    if v:
                        venue.append(datetime(
                            int(v.group(1)[:4]), int(v.group(1)[5:7]),
                            int(v.group(1)[8:10]), int(v.group(2)),
                            int(v.group(3)), int(v.group(4))))
                    m = _CANDLE_LAG_RE.match(line)
                    if not m:
                        continue
                    wall = datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]),
                                    int(m.group(1)[8:10]), int(m.group(2)),
                                    int(m.group(3)), int(m.group(4)))
                    due = wall.replace(hour=int(m.group(5)), minute=0, second=0)
                    # A candle logged after midnight belongs to the previous day.
                    if (wall - due).total_seconds() < -3600:
                        due -= timedelta(days=1)
                    rows.add((wall, due, (wall - due).total_seconds()))
        except OSError:
            continue
    if not rows:
        return {}, [], []

    # Keep only the FIRST time the loop reached each candle hour.
    #
    # A process restart re-prints the header for the hour it starts in, so an
    # hour served on time at HH:00:15 and then restarted at HH:49 produced a
    # SECOND row reading "49 minutes late". Nothing was blocked for 49 minutes;
    # the loop had already done that candle, and the restart itself costs ~5s.
    #
    # On 2026-09-04 the bot restarted seven times between 15:49 and 17:36 and
    # twice more at 22:40/22:53, and this section reported NINE phantom stalls
    # of 6-54 minutes each, every one labelled "SELF-BLOCKED — venue was
    # healthy, we were not looking", inflating "OURS to prevent" to 494 min
    # against a true figure of ~28. That matters beyond arithmetic: per the
    # 2026-08-31 session, a health alarm that cries wolf gets read past, and
    # this one sits directly above the numbers a session is meant to act on.
    #
    # First-arrival is also the definition this function documents ("how long
    # after each hour the loop actually got round to that candle") -- the
    # answer is when it first got there. Deduped here rather than at the
    # regex so `venue` and the raw parse stay untouched.
    first = {}
    for wall, due, lag in rows:
        if due not in first or wall < first[due][0]:
            first[due] = (wall, due, lag)
    rows = set(first.values())

    by_hour, buckets = {}, {}
    for wall, due, lag in rows:
        buckets.setdefault(due.hour, []).append(lag)
    for hr, v in buckets.items():
        v.sort()
        by_hour[hr] = (len(v), v[len(v) // 2], v[int(len(v) * 0.9)], v[-1],
                       sum(1 for x in v if x > _LAG_BLOCKED_SEC))

    worst = sorted(rows, key=lambda r: -r[2])[:5]

    # Cross-reference: a blocked loop only costs money with a position open.
    frozen = []
    if state:
        for wall, due, lag in sorted(rows):
            if lag <= _LAG_BLOCKED_SEC:
                continue
            cause, nerr = _stall_cause(due, wall, venue)
            for coin, side, still_open in _open_during(due, wall, state):
                frozen.append((due, coin, side, lag, cause, nerr))
    return by_hour, worst, frozen


# Last commit that touched an exit constant in strategy2.py (TP_R 3.0 -> 5.0,
# TRAIL_START_R -> 2.50). Trades opened before this ran a materially different
# exit and must not be pooled with the ones after it -- see _excursion_stats.
EXIT_REGIME_FROM = "2026-08-05"


def _maybe_r(roe_pct, lev, risk_pct):
    """Leveraged ROE% -> R, preserving 'not recorded' as None rather than 0.0."""
    if roe_pct is None:
        return None
    try:
        return (float(roe_pct) / lev) / risk_pct
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _excursion_stats(state):
    """Per-trade maximum favourable/adverse excursion, expressed in R.

    Win rate and realised R only describe what the EXIT rule did. MFE describes
    what the ENTRY offered before any exit rule touched it, and the two answer
    opposite questions: an entry whose favourable excursion rarely reaches the
    stop distance is unprofitable under every exit rule, while an entry with
    large MFE and small realised R is an exit problem. Nothing in this system
    read that apart until 2026-08-17, so "should the trail be tighter" was being
    argued from realised R -- which is the trail's own output.

    `peak_roe_pct` and `max_adverse_pct` are already recorded per trade as
    LEVERAGED return-on-equity percentages, so both are divided by leverage to
    recover the underlying price move before normalising by the stop distance.

    Returns [{coin, opened, mfe_r, mae_r, real_r, regime}, ...], newest last.
    """
    out = []
    for t in state.get("closed_trades", []):
        try:
            entry = float(t["entry"])
            sl    = float(_entry_stop(t))
            lev   = float(t.get("leverage") or 0)
            risk_pct = abs(entry - sl) / entry * 100
            if not (risk_pct > 0 and lev > 0):
                continue          # degenerate stop or missing leverage: unusable
            opened = str(t.get("opened_at", ""))[:10]
            out.append({
                "coin":   t.get("coin", "?"),
                "opened": opened,
                # None, NOT 0.0, when the tracker never recorded an excursion.
                # `or 0.0` here silently asserted "this entry never went
                # favourable" for the reconstructed OP 2026-08-13 record, which
                # dragged the median MFE down, scored OP as a miss at every
                # threshold, and printed a FALSE invariant violation against its
                # real -1.09R exit. A measurement that was never taken is not a
                # measurement of zero -- see [[mfe-not-realised-r]].
                "mfe_r":  _maybe_r(t.get("peak_roe_pct"),    lev, risk_pct),
                "mae_r":  _maybe_r(t.get("max_adverse_pct"), lev, risk_pct),
                "real_r": (float(t.get("lev_pct")        or 0.0) / lev) / risk_pct,
                # What the ratchet ACTUALLY locked, when the trade recorded it.
                # Preferred over inferring arming from MFE: peak_roe_pct is
                # sampled by the poll loop, so it undershoots. SOL armed at
                # exactly +2.5R (bot.log "[S2] SOL stop -> +2.5R") but its
                # recorded peak is 2.46R, so an MFE>=2.5 test scores a real
                # arm as a miss. Only trades from 2026-08-16 carry this field.
                "locked_r": (None if t.get("locked_r") is None
                             else float(t["locked_r"])),
                "regime": "new" if opened >= EXIT_REGIME_FROM else "old",
            })
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def _ratchet_slippage(state):
    """How much of the profit the ratchet LOCKED was actually delivered.

    `locked_r` is the R level update_sl() moved the stop to; `rr` is what the
    trade realised. The two should be equal -- that is the whole promise of a
    ratchet, and it is an invariant, not a statistic. Every R by which realised
    falls short of locked is pure execution loss, and it lands exclusively on
    winners, which is where this book's entire sumR lives.

    Nothing measured this until 2026-08-26, and it is not visible in any metric
    that already existed: EXIT MECHANISM pools ratcheted trades into one meanR,
    and EXCURSION's "gave back" is MFE minus realised, which mixes execution
    loss together with the ordinary retrace between the peak and the stop. Only
    locked-vs-realised isolates the part that should be zero.

    The mechanism is the ratchet placing its stop essentially AT market: it arms
    at the moment price reaches TRAIL_START_R, so the new stop sits at the price
    that just traded, and any adverse tick fires it immediately into an IOC
    capped at executor.RATCHET_SLIP_CAP.

    Trades that armed before `locked_r` began being recorded (2026-08-16) carry
    no lock level and are reported as a named coverage gap rather than dropped
    silently -- the same treatment OP gets in the excursion section, and for the
    same reason: an unexplained absence reads as an absence of the problem.

    `adv_pct` is the same loss expressed as a fraction of price, and it is the
    only unit in which the cap question can actually be answered: R normalises
    across coins (which is why it is right for edge), but RATCHET_SLIP_CAP is
    enforced as a fraction of price, and the conversion factor R/entry differs
    per trade -- 0.81% for SOL, 2.93% for ETH. Reporting the leak only in R and
    then remarking that it "sits inside the cap" invites the conclusion that the
    cap is the culprit, without ever printing the number that would test it.
    None when the trade lacks the prices to compute it.

    Returns (measured, gap) where measured is
    [{coin, opened, locked_r, real_r, slip_r, adv_pct}, ...] and gap is
    [(coin, opened, real_r), ...] for ratcheted exits with no recorded lock.
    """
    measured, gap = [], []
    for t in state.get("closed_trades", []):
        try:
            rr = t.get("rr")
            if rr is None:
                continue
            rr = float(rr)
            opened = str(t.get("opened_at", ""))[:10]
            locked = t.get("locked_r")
            if locked is None or float(locked) <= 0:
                # A stop that filled in the trade's favour can only have got
                # there by ratcheting; the original stop is always adverse.
                if rr > 0:
                    gap.append((t.get("coin", "?"), opened, rr))
                continue
            locked = float(locked)

            # Adverse fill vs the ratcheted trigger, signed so that positive
            # always means "filled worse than the stop asked for".
            adv_pct = None
            try:
                trig, fill, d = float(t["sl"]), float(t["exit"]), int(t["dir"])
                if trig > 0:
                    adv_pct = ((trig - fill) if d == 1 else (fill - trig)) / trig
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                adv_pct = None

            measured.append({
                "coin": t.get("coin", "?"), "opened": opened,
                "locked_r": locked, "real_r": rr, "slip_r": locked - rr,
                "adv_pct": adv_pct,
            })
        except (TypeError, ValueError):
            continue
    return measured, gap


SELFLEARN_LOG = "/root/trade/selflearn.log"


def _session_history(path=SELFLEARN_LOG, limit=14):
    """Mine selflearn.log for whether the NIGHTLY SESSION itself ran.

    This is an availability metric, not a trading one, and it belongs next to
    candle continuity for the same reason: on 2026-08-28/29/30 the 02:00 session
    died on usage limits three nights running and the bot traded unsupervised
    for four days. Nothing in the report showed it -- the 08-31 session only
    found out by reading this log by hand.

    Returns (rows, stats) where rows are (date, time, rc, reason) newest-first.
    Only sessions from 2026-07-24 are counted: the "Session ended (exit N)" line
    did not exist before then, so earlier runs cannot be scored and must not be
    silently reported as failures.
    """
    try:
        txt = open(path, errors="replace").read()
    except OSError:
        return [], {}

    chunks = re.split(
        r"={40}\nSELF-LEARN: (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC\n={40}\n", txt)
    rows = []
    for i in range(1, len(chunks) - 2, 3):
        date, tm, body = chunks[i], chunks[i + 1], chunks[i + 2]
        m = re.search(r"Session ended: \S+ UTC \(exit (\d+)\)", body)
        if not m:
            continue                      # still running, or pre-07-24 format
        rc = int(m.group(1))
        lim = re.search(r"You've hit your (session|weekly|monthly)[^\n]*", body)
        if rc == 0:
            reason = ""
        elif lim:
            reason = lim.group(0).replace("You've hit your ", "").strip()
        elif rc == 124:
            reason = "killed at the 60-minute wall clock"
        else:
            reason = (body.strip().splitlines() or ["unknown"])[0][:60]
        rows.append((date, tm, rc, reason))

    ok = sum(1 for r in rows if r[2] == 0)
    stats = {"n": len(rows), "ok": ok, "failed": len(rows) - ok,
             "rate": (100.0 * ok / len(rows)) if rows else 0.0}
    return rows[::-1][:limit], stats


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

    # ── Availability ───────────────────────────────────────────────
    # Deliberately FIRST. Every other number in this report is conditional on
    # the bot having been running, and on 2026-08-22 a session read a full
    # report without noticing that the host had been dead for 19 of the
    # previous 20 hours with a live position on the book.
    try:
        _seen, _gaps, _win = _availability()
        lines.append(f"\n── AVAILABILITY (candle continuity) ──")
        if _win:
            _exp = int((_win[1] - _win[0]).total_seconds() // 3600) + 1
            _quiet = sum(1 for i in range(_exp)
                         if (_win[0] + timedelta(hours=i)).hour in QUIET_HOURS)
            lines.append(f"  window: {_win[0]:%Y-%m-%d %H:%M} → {_win[1]:%Y-%m-%d %H:%M} UTC")
            lines.append(f"  candles logged: {len(_seen)} of {_exp - _quiet} "
                         f"expected ({_quiet} quiet hours excluded)")
        if not _gaps:
            lines.append("  no unexplained gaps ✓")
        for _a, _b, _n in sorted(_gaps, key=lambda g: -g[2])[:5]:
            _span = (_b - _a).total_seconds() / 3600 + 1
            lines.append(f"  ⚠️  {_a:%Y-%m-%d %H:%M} → {_b:%Y-%m-%d %H:%M} UTC  "
                         f"{_n}h missing (span {_span:.0f}h)")
            for _c, _side, _still in _open_during(_a, _b, state):
                lines.append(f"       ‼️  {_c} {_side} was OPEN and unmanaged "
                             f"through this gap"
                             + ("  (STILL OPEN)" if _still else ""))
        lines.append("  (quiet hours 02:00-03:59 UTC are expected silent)")
    except Exception as _e:
        lines.append(f"\n── AVAILABILITY ──\n  availability check failed: {_e}")

    # ── Supervision (did the nightly session itself run?) ──────────
    # Second, for the same reason availability is first: an unsupervised bot is
    # not a measured bot. 08-28/29/30 all died on usage limits and four days of
    # trading went unreviewed before anyone noticed.
    try:
        _srows, _sst = _session_history()
        if _sst.get("n"):
            lines.append(f"\n── SUPERVISION (nightly session, n={_sst['n']}) ──")
            lines.append(f"  completed: {_sst['ok']}/{_sst['n']} "
                         f"({_sst['rate']:.0f}%)   failed: {_sst['failed']}")
            _miss = [r for r in _srows if r[2] != 0]
            if _miss:
                lines.append("  recent failures (newest first):")
                for _d, _t, _rc, _why in _miss:
                    lines.append(f"    ✗ {_d} {_t}  exit {_rc}  {_why}")
            else:
                lines.append("  no failed session in the last "
                             f"{len(_srows)} runs ✓")
            lines.append("  retries: 04:30 (session limits reset 02:10-04:00) "
                         "and 15:00 UTC (weekly limits reset 14:00)")
    except Exception as _e:
        lines.append(f"\n── SUPERVISION ──\n  session history failed: {_e}")

    # ── Loop latency (did the loop get to each candle ON TIME?) ────
    # Third, because AVAILABILITY above is binary: it asks whether an hour was
    # scanned, so a loop that scanned every hour twenty minutes late reads as
    # perfectly healthy. That is exactly what the 23:00 review looked like for
    # 41 nights.
    try:
        _bh, _worst, _frozen = _loop_latency(state=load_state())
        if _bh:
            _all = sorted(_bh.items())
            _slow = [(h, v) for h, v in _all if v[1] > 120 or v[4] > 3]
            _med = sorted(v[1] for _, v in _all)[len(_all) // 2]
            lines.append(f"\n── LOOP LATENCY (candle start vs its own hour) ──")
            lines.append(f"  typical hour: median {_med:.0f}s behind the hour")
            if _slow:
                lines.append("  hours running late:")
                for h, (n, md, p90, mx, over) in _slow:
                    lines.append(f"    {h:02d}:00  n={n:3d}  median {md:5.0f}s  "
                                 f"p90 {p90:6.0f}s  max {mx:6.0f}s  "
                                 f"{over} nights >5min")
            else:
                lines.append("  no hour runs systematically late ✓")
            try:
                _rs = _restarts()
                _busy = sorted((d, n) for d, n in _rs.items() if n >= 3)[-5:]
                if _busy:
                    lines.append("  process restarts (a restart re-prints the "
                                 "current candle header; not a stall):")
                    for _d, _n in _busy:
                        lines.append(f"    {_d}  {_n} restarts")
            except Exception:
                pass
            if _frozen:
                # NOT "ratchet frozen" since 2026-09-02: _reconcile_dust /
                # _check_closed / _check_trail_s2 / _verify_stops all run ABOVE
                # the maintenance block and above this scan, so a late candle
                # header no longer means the ratchet stood still. What it still
                # costs is ENTRY latency on that candle (live.py:1240-1248).
                # Rows dated before 2026-09-02 did freeze the ratchet.
                lines.append("  ⚠️  candle >5min late WITH A POSITION OPEN "
                             "(before 2026-09-02: ratchet frozen; after: "
                             "entries delayed, ratchet already ran):")
                _tot = _ours = 0.0
                for _due, _coin, _side, _lag, _cause, _nerr in _frozen:
                    _tot += _lag
                    if _cause == "self-blocked":
                        _ours += _lag
                    _tag = (f"venue down ({_nerr} API errors) — stop was still "
                            f"resting on the exchange" if _cause == "venue down"
                            else "SELF-BLOCKED — venue was healthy, we were not "
                                 "looking")
                    lines.append(f"    {_due:%Y-%m-%d %H:%M}  {_coin} {_side}"
                                 f"  frozen {_lag/60:.1f} min  [{_tag}]")
                lines.append(f"    total {_tot/60:.0f} min frozen, of which "
                             f"{_ours/60:.0f} min was OURS to prevent")
            lines.append("  (blocking maintenance was moved BELOW position "
                         "management 2026-09-02; entries on the delayed candle "
                         "are still affected — see test_review_order.py)")
    except Exception as _e:
        lines.append(f"\n── LOOP LATENCY ──\n  latency check failed: {_e}")

    # ── Open book ──────────────────────────────────────────────────
    # Every other section reads closed_trades, so an open position is invisible
    # to the entire report no matter how much it matters. On 2026-08-23 the most
    # important trade on record -- the first SHORT ever to run favourably, which
    # peaked at +2.20R against a 2.5R arming threshold -- was open, and could
    # only be found by reading state.json by hand.
    try:
        import strategy2 as _s2o
        _arm_r = float(_s2o.TRAIL_START_R)
    except Exception:
        _arm_r = 2.5
    _tracked = state.get("tracked", {})
    lines.append(f"\n── OPEN BOOK ({len(_tracked)}) ──")
    if not _tracked:
        lines.append("  flat")
    for _c, _t in _tracked.items():
        try:
            _lev  = float(_t.get("leverage") or 1) or 1
            _ent  = float(_t["entry"])
            _stop = float(_t.get("sl_orig") or _t["sl"])
            _risk = abs(_ent - _stop) / _ent * 100
            _side = "SHORT" if _t.get("dir") == -1 else "LONG"
            _mfe  = (float(_t.get("peak_roe_pct")    or 0.0) / _lev) / _risk
            _mae  = (float(_t.get("max_adverse_pct") or 0.0) / _lev) / _risk
            _age  = (datetime.utcnow() - datetime.fromisoformat(
                str(_t["opened_at"]).replace("Z", ""))).total_seconds() / 3600
            lines.append(f"  {_c} {_side} @ ${_ent:g}  stop ${_stop:g} "
                         f"({_risk:.2f}% = 1R)  open {_age:.1f}h")
            lines.append(f"    MFE {_mfe:+.2f}R   MAE {_mae:+.2f}R")
            _locked = _t.get("locked_r")
            if _locked:
                lines.append(f"    ratchet ARMED, locked +{float(_locked):.2f}R")
                # A stop cannot lock in more than the trade ever reached. When
                # it claims to, the record is corrupt, not the trade -- and a
                # corrupt sl is worse than a cosmetic error, because live.py's
                # ratchet compares every candidate stop against it and a bad
                # value freezes the ratchet silently for the life of the
                # position. This section printed sl_orig and locked_r but never
                # the working sl, which is why the 2026-08-23 corruption
                # (sl 2684.36 -> 103.0, locked_r 0 -> 3.0, written by
                # test_ratchet.py into the live state file) sat in the report
                # for two nights reading as a healthy armed ratchet.
                _live_sl = float(_t.get("sl") or _stop)
                _impl    = (_live_sl - _ent) * (_t.get("dir") or 1) / abs(_ent - _stop)
                if float(_locked) > _mfe + 0.10:
                    lines.append(f"    ⚠️  IMPOSSIBLE: locked "
                                 f"+{float(_locked):.2f}R exceeds MFE "
                                 f"{_mfe:+.2f}R — record is corrupt")
                if abs(_impl - float(_locked)) > 0.10:
                    lines.append(f"    ⚠️  working stop ${_live_sl:g} implies "
                                 f"{_impl:+.2f}R, not the recorded "
                                 f"+{float(_locked):.2f}R — record is corrupt")
            else:
                lines.append(f"    ratchet NOT armed — needs "
                             f"{_arm_r:.2f}R, peaked {_mfe:+.2f}R "
                             f"({_arm_r - _mfe:+.2f}R short); stop still "
                             f"at -1.00R")
            # The mirror of the locked-vs-MFE invariant above, on the loss side,
            # and it applies armed or not: the ratchet only ever moves a stop in
            # our favour, so -1.00R is the worst any OPEN position can be showing.
            # An open trade reporting MAE past its own stop means the stop was
            # traded through and did not fully fill.
            #
            # This is exactly what BTC 2026-09-03 did -- MAE -1.19R against a
            # stop at -1.00R -- and the report printed it as a healthy protected
            # position for 6.7 hours. The stop had swept the book for 97.3% of
            # the size at 19:21:24 and left 0.00013 BTC resting; `sz != 0` kept
            # the coin in get_positions(), so _check_closed never fired. Every
            # closed-trade invariant in this file passed, because the trade was
            # never recorded as closed. The open book had no invariant at all.
            if _mae < -1.0 - 0.05:
                _breach = _ent + (_stop - _ent) * abs(_mae)
                lines.append(
                    f"    ‼️  INVARIANT VIOLATED: MAE {_mae:.2f}R is past the "
                    f"stop at -1.00R (price reached ~${_breach:,.2f} vs stop "
                    f"${_stop:,.2f}) yet the position is still open after "
                    f"{_age:.1f}h — the stop filled SHORT. Check the residual "
                    f"size against state.json and the resting orders.")
        except Exception as _e:
            lines.append(f"  {_c}: could not summarise ({_e})")

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

        # ── Expected value ─────────────────────────────────────────
        # The number the whole system lives or dies on, and it was never
        # printed. Win rate alone is meaningless here: this book pairs a
        # ~-1.03R loss with a ~+1.56R win, so it clears breakeven well under
        # 50%. Stated with its own error bar, because at this n the honest
        # answer is almost always "indistinguishable from zero" and a report
        # that hides that invites tuning on noise.
        w = [r for r in rs if r > 0]
        l = [r for r in rs if r <= 0]
        if w and l:
            aw, al = sum(w) / len(w), abs(sum(l) / len(l))
            wr, be = len(w) / n, al / (aw + al)
            ev = wr * aw - (1 - wr) * al
            mean = sum(rs) / n
            sd = (sum((v - mean) ** 2 for v in rs) / (n - 1)) ** 0.5 if n > 1 else 0.0
            tstat = mean / (sd / n ** 0.5) if sd > 0 else 0.0
            sig = (wr * (1 - wr) / n) ** 0.5 * 100
            lines.append(f"  EV = {wr:.3f} x {aw:+.3f}R - {1-wr:.3f} x {al:.3f}R "
                         f"= {ev:+.4f}R per trade")
            lines.append(f"  breakeven WR {be*100:.1f}% (avgLoss/(avgWin+avgLoss)); "
                         f"actual {wr*100:.1f}% = {(wr-be)*100:+.1f}pp")
            lines.append(f"  sd {sd:.2f}R, t={tstat:+.2f}, sigma(WR)={sig:.1f}pp "
                         f"-> {(wr-be)/(sig/100) if sig else 0:+.2f} sigma from breakeven")
            lines.append("  (|t| < 2 means this is not yet distinguishable from a "
                         "coin flip -- do not tune on it)")
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

    # ── Ratchet slippage ───────────────────────────────────────────
    # EXIT MECHANISM says ratcheted stops are profitable. This says how much of
    # that profit never arrives. See _ratchet_slippage.
    try:
        _slip, _slip_gap = _ratchet_slippage(state)
        if _slip or _slip_gap:
            lines.append(f"\n── RATCHET SLIPPAGE (locked vs delivered) ──")
        if _slip:
            for x in sorted(_slip, key=lambda v: v["opened"]):
                _pct = 100 * x["slip_r"] / x["locked_r"] if x["locked_r"] else 0.0
                _adv = ("  fill %+.3f%% vs trigger" % (100 * x["adv_pct"])
                        if x["adv_pct"] is not None else "  fill n/a")
                lines.append(
                    f"  {x['coin']:<5} {x['opened']}  locked {x['locked_r']:+.2f}R"
                    f"  ->  got {x['real_r']:+.3f}R"
                    f"   leak {x['slip_r']:+.3f}R ({_pct:+.1f}% of lock)"
                    f"{_adv}")
            _tot  = sum(x["slip_r"] for x in _slip)
            _lock = sum(x["locked_r"] for x in _slip)
            lines.append(f"  n={len(_slip)}  total leak {_tot:+.3f}R of {_lock:.2f}R locked "
                         f"({100*_tot/_lock:+.1f}%)  mean {_tot/len(_slip):+.3f}R/arm")
            # Concentration check. A mean over 3 arms hides whether this is a
            # broad tax or one bad fill, and those have different fixes.
            _worst_arm = max(_slip, key=lambda v: v["slip_r"])
            if _tot > 0:
                lines.append(f"  concentration: {_worst_arm['coin']} "
                             f"{_worst_arm['opened']} alone is "
                             f"{100*_worst_arm['slip_r']/_tot:.0f}% of the leak "
                             f"— this is one bad fill, not a broad tax")
            # Framed against the book, because that is the number that decides
            # whether this is a rounding error or a first-order leak.
            _book = sum(r for r in (_r_of(t) for t in trades) if r is not None)
            if _book + _tot > 0:
                lines.append(f"  book sumR {_book:+.2f}; without this leak {_book + _tot:+.2f} "
                             f"— slippage is {100*_tot/(_book + _tot):.0f}% of gross")
            # The cap verdict, stated in the cap's own unit. Without this the
            # obvious-looking fix (narrow RATCHET_SLIP_CAP to match the entry
            # bracket) reads as free money; it is not, and the number says why.
            try:
                import executor as _ex
                _advs = [x["adv_pct"] for x in _slip if x["adv_pct"] is not None]
                if _advs:
                    _worst = max(_advs)
                    lines.append(f"  worst fill {_worst*100:.3f}% vs trigger — "
                                 f"{100*_worst/_ex.RATCHET_SLIP_CAP:.0f}% of the "
                                 f"{_ex.RATCHET_SLIP_CAP*100:.2f}% RATCHET_SLIP_CAP")
                    _would_miss = [x for x in _slip
                                   if x["adv_pct"] is not None
                                   and x["adv_pct"] > _ex.BRACKET_SLIP_CAP]
                    if _would_miss:
                        lines.append(
                            f"  ⚠️  narrowing the cap to the entry bracket's "
                            f"{_ex.BRACKET_SLIP_CAP*100:.2f}% would NOT have filled: "
                            + ", ".join(f"{x['coin']} {x['opened']}" for x in _would_miss)
                            + " — update_sl cancels the old stop AND the TP before")
                        lines.append(
                            "      placing the new one, so a rejected fill leaves the "
                            "position with nothing resting against it. Tightening this")
                        lines.append(
                            "      cap buys back fractions of an R by risking an "
                            "unprotected position. Not a free win.")
                    else:
                        lines.append(f"  every observed fill is inside the entry "
                                     f"bracket's {_ex.BRACKET_SLIP_CAP*100:.2f}% — "
                                     f"no evidence the cap is binding at all")
                else:
                    lines.append(f"  cap {_ex.RATCHET_SLIP_CAP*100:.2f}% "
                                 f"(entry bracket {_ex.BRACKET_SLIP_CAP*100:.2f}%) "
                                 f"— no fill prices recorded to compare")
            except Exception:
                pass
            lines.append("  NOTE: realised should EQUAL locked -- this is an invariant, not")
            lines.append("  a statistic. Any leak is execution loss falling on winners only,")
            lines.append("  because the ratchet arms AT market: the new stop rests at the")
            lines.append("  price that just triggered it, so one adverse tick fires it.")
        if _slip_gap:
            lines.append("  no lock recorded (pre-2026-08-16, excluded above): "
                         + ", ".join(f"{c} {o} {r:+.2f}R" for c, o, r in _slip_gap))
    except Exception as _e:
        lines.append(f"\n── RATCHET SLIPPAGE ──\n  slippage check failed: {_e}")

    # ── Excursion (MFE/MAE) ────────────────────────────────────────
    # The section above measures the exit. This one measures the entry, which
    # is the only way to tell the two apart. See _excursion_stats.
    exc = _excursion_stats(state)
    if exc and any(x["mfe_r"] is not None for x in exc):
        try:
            import strategy2 as _s2x
            arm_r = float(_s2x.TRAIL_START_R)
        except Exception:
            arm_r = None
        # Trades whose excursion was actually RECORDED. A trade with a real
        # realised R but a null peak_roe_pct still belongs in this section --
        # dropping it would recreate the very under-count the coverage warning
        # below exists to catch -- but it cannot contribute to a statistic
        # about excursion it never measured.
        meas   = [x for x in exc if x["mfe_r"] is not None and x["mae_r"] is not None]
        unmeas = [x for x in exc if x not in meas]
        mfes = sorted(x["mfe_r"] for x in meas)
        n    = len(mfes)
        lines.append(f"\n── EXCURSION: WHAT THE ENTRY OFFERED (n={n}) ──")
        med  = mfes[n // 2] if n % 2 else (mfes[n // 2 - 1] + mfes[n // 2]) / 2
        if unmeas:
            lines.append("  ⚠️  excursion NOT RECORDED for "
                         + ", ".join(f"{x['coin']} {x['opened']} "
                                     f"(realised {x['real_r']:+.2f}R)"
                                     for x in unmeas)
                         + " — shown below but excluded from every statistic in "
                           "this section; a null excursion is not a zero one")

        # Coverage reconciliation. This section reads state.json while every
        # section above reads journal.json, and the two can disagree: OP
        # (2026-08-13) was written to the journal but never reached state's
        # closed_trades, so it is absent here. That is not cosmetic -- OP was
        # a -1.09R loss, and dropping it lifted the current-regime meanR shown
        # below from -0.03R to +0.14R. A silently smaller n in a section that
        # sits under a bigger headline n is exactly how a book flatters itself.
        jkeys = {(t.get("coin"), str(t.get("open_time", ""))[:10]) for t in trades}
        ekeys = {(x["coin"], x["opened"]) for x in exc}
        missing = sorted(jkeys - ekeys)
        if missing:
            lines.append(f"  ⚠️  covers {len(exc)} of {len(trades)} closed trades. "
                         f"Missing (no state record, excluded from every figure "
                         f"in this section): "
                         + ", ".join(f"{c} {d}" for c, d in missing))
        lines.append(f"  median MFE: {med:+.2f}R   best: {mfes[-1]:+.2f}R   "
                     f"worst: {mfes[0]:+.2f}R")
        for thr in (0.5, 1.0, 1.5, 2.0, 2.5):
            hit = sum(1 for m in mfes if m >= thr)
            lines.append(f"    reached >={thr:.1f}R favourable:  {hit:2}/{n}  "
                         f"({hit/n*100:4.0f}%)")
        if arm_r is not None:
            # Recorded fact first, sampled proxy only where no record exists.
            def _armed(x):
                if x["locked_r"] is not None:
                    return x["locked_r"] > 0
                # No lock recorded AND no MFE recorded: unknowable, not "no".
                return x["mfe_r"] is not None and x["mfe_r"] >= arm_r
            armed    = [x for x in meas if _armed(x)]
            inferred = sum(1 for x in armed if x["locked_r"] is None)
            new      = [x for x in meas if x["regime"] == "new"]
            new_arm  = [x for x in new if _armed(x)]
            lines.append(f"  live TRAIL_START_R={arm_r:g} armed in {len(armed)}/{n} "
                         f"trades ({len(armed)/n*100:.0f}%)"
                         + (f" — {inferred} inferred from MFE, no locked_r recorded"
                            if inferred else ""))
            if new:
                lines.append(f"    under the current exit regime only: "
                             f"{len(new_arm)}/{len(new)} armed "
                             f"({len(new_arm)/len(new)*100:.0f}%) — this is the "
                             f"rate that describes the deployed system")
        lines.append("  per trade (MFE -> realised):")
        for x in exc:
            if x["mfe_r"] is None or x["mae_r"] is None:
                lines.append(f"    {x['coin']:<5} {x['opened']}  MFE   n/a  "
                             f"MAE   n/a  ->  {x['real_r']:+5.2f}R  "
                             f"(excursion never recorded)  [{x['regime']}]")
                continue
            give = x["mfe_r"] - x["real_r"]
            lines.append(f"    {x['coin']:<5} {x['opened']}  MFE {x['mfe_r']:+5.2f}R  "
                         f"MAE {x['mae_r']:+5.2f}R  ->  {x['real_r']:+5.2f}R  "
                         f"(gave back {give:+.2f}R)  [{x['regime']}]")

        # Invariant. A trade cannot exit above its own peak or below its own
        # trough, so MAE <= realised <= MFE holds by construction for every
        # row -- any violation means the three columns were not measured
        # against the same denominator. This is not hypothetical: from
        # 2026-08-17 to 08-18 this table normalised by the ratcheted stop
        # while realised R used the entry stop, and SOL printed MFE +0.98R
        # against a true +2.46R for a full day without anyone noticing.
        # Deriving a metric and never asserting its own arithmetic is how a
        # measurement bug survives a nightly review that is looking straight
        # at it.
        # Tolerance, not zero: peak_roe_pct and max_adverse_pct are SAMPLED by
        # the tracker poll loop while the exit is an exact fill, so a trade
        # that closes right at its extreme routinely overshoots the recorded
        # peak by a poll interval. Observed overshoot on the current book is
        # 0.01-0.02R. 0.10R sits well clear of that and still catches the real
        # thing by a mile -- the stop-denominator bug put SOL 1.50R over.
        # Only rows that HAVE both bounds can violate a bound. Checking an
        # unrecorded excursion coerced to 0.0 is how OP 2026-08-13 printed a
        # violation for a year-normal stop-out: MAE +0.00, realised -1.09,
        # MFE +0.00. The invariant was working; the input was fabricated.
        TOL = 0.10
        bad = [x for x in meas
               if x["real_r"] > x["mfe_r"] + TOL or x["real_r"] < x["mae_r"] - TOL]
        if bad:
            lines.append(f"  ⚠️  INVARIANT VIOLATED (MAE <= realised <= MFE, tol {TOL}R):")
            for x in bad:
                lines.append(f"      {x['coin']} {x['opened']}: MAE {x['mae_r']:+.2f} "
                             f"realised {x['real_r']:+.2f} MFE {x['mfe_r']:+.2f} "
                             f"— excursion and realised R disagree on the stop")
        else:
            lines.append(f"  invariant MAE <= realised <= MFE: OK on all {len(meas)} "
                         f"measured (tol {TOL}R for poll sampling)")
        # Exit-regime split. Pooling these hides that the constants changed
        # underneath the record on 2026-08-05.
        for lab, key in (("pre-" + EXIT_REGIME_FROM, "old"),
                         (EXIT_REGIME_FROM + " onward", "new")):
            g = [x for x in exc if x["regime"] == key]
            if not g:
                continue
            rs = [x["real_r"] for x in g]
            w  = sum(1 for r in rs if r > 0)
            lines.append(f"  exit regime {lab:<16} n={len(g)}  WR:{w/len(g)*100:3.0f}%  "
                         f"meanR:{sum(rs)/len(g):+.3f}")
        lines.append("  CAUTION: retrofitting a take-profit onto an MFE column is the")
        lines.append("  most overfit-prone sum in trading — every trade 'would have'")
        lines.append("  hit any target below its own peak, by construction.")

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
            (f"RSI {_band([15,20,23], rsi_v, ['<15','15-20','20-23','23-25'])}"
             if sig.get("direction", 1) == 1
             else f"RSI {_band([77,80,85], rsi_v, ['75-77','77-80','80-85','85+'])}")
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
        # These bands have implied "tighten the gate" for four sessions running
        # (low-ADX and high-stretch cells carry the winners). That reading was
        # TESTED on 2026-08-26 with portfolio2 -- the instrument that is valid
        # for entry-side questions -- and REJECTED in both directions. 20 coins,
        # 5000 1h bars, live exit params, net% / ex-top5%:
        #     MAX_ADX   25(live) +49.18/+25.67   22 +17.92/-0.19
        #               20        +6.04/ -9.26   18  +2.32/-6.38
        #     stretch  1.5(live) +49.18/+25.67  2.5 +38.99/+15.97
        #              3.0       +29.38/ +8.54  3.5 +16.21/-4.63
        # Monotonic degradation both ways, and ex-top5 -- the edge with its five
        # best trades deleted -- goes NEGATIVE at every tightening. The live gate
        # is the only setting tested whose edge survives losing its tail.
        #
        # The bands disagree because they slice n<20 post-hoc and are conditioned
        # on exactly the tail ex-top5 removes: "ADX 15-20 WR 100%" is three
        # trades, against n=23 and ex-top5 -9.26% for the same cell in the sweep.
        # Reproduce, do not trust these numbers as they age:
        #   python3 -c "import portfolio2 as p;b,c=p.load();print(p.summarize(
        #       p.simulate(b,c,max_adx=20,trail_start_r=2.5),'adx20'))"
        lines.append("  CAUTION: the low-ADX / high-stretch cells look best because they")
        lines.append("  hold the tail. portfolio2 swept both on 2026-08-26 and tightening")
        lines.append("  either one degrades net, EV and t MONOTONICALLY, and drives ex-top5")
        lines.append("  negative. Do not re-propose a tighter gate off this table alone.")
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
            # Coverage is stated by NAME, not as a ratio. Until 2026-09-05 the
            # scan loop iterated the retired S1 watchlist (12 coins) while the
            # engine traded 20, and this line read "12 of 20" -- true, quiet,
            # and read past for three weeks. What it was hiding: the missing 8
            # included BTC (4 trades, the worst record in the book) and NEAR
            # (whose feed went stale on 09-04 and was caught by an exchange
            # rejection rather than by FEED HEALTH below). live.py now logs the
            # S2-only coins after the entry decision, so this gap closes going
            # forward -- but the HISTORY stays one-sided, and a reader
            # comparing coins needs to know which ones only started being
            # observed tonight.
            missing = [c for c in _s2c.WATCHLIST if c not in per]
            lines.append(f"  coverage: {len(per)} logged coins of "
                         f"{len(_s2c.WATCHLIST)} in the S2 watchlist")
            if missing:
                traded_missing = sorted({t.get("coin") for t in trades
                                         if t.get("coin") in set(missing)})
                lines.append(f"  ⚠️  NEVER OBSERVED: {', '.join(missing)}")
                if traded_missing:
                    lines.append(f"      of which these have TRADED: "
                                 f"{', '.join(traded_missing)} — every band, "
                                 f"census and feed figure below excludes them")
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

                # Does a dirty feed actually cost money, or is it only ugly?
                # The trade record can answer that by joining each closed trade
                # to its own coin's frozen rate. WATCHLIST is owner-locked, so
                # this exists to hand Kamran evidence, not to act on it.
                # `stale.get(coin)` returns None for any coin the scan loop
                # never printed, and until 2026-09-05 the trade then fell out
                # of `paired` with no counter and no warning. That dropped 6 of
                # 19 closed trades -- 32% of the book, 5 of them losses,
                # meanR -0.717 -- on a criterion (membership of the retired S1
                # watchlist) with no connection whatsoever to feed quality. The
                # discarded cohort was WORSE than either published bucket, so
                # the clean-vs-dirty gap below was part real and part selection
                # artifact. Never let a slice discard rows silently: report the
                # remainder next to the result, so the reader can see how much
                # of the separation the sample could have manufactured.
                clean, dirty, unmeasured = _feed_quality_split(trades, stale)
                paired = clean + dirty
                if len(paired) >= 4:
                    tot = len(paired) + len(unmeasured)
                    lines.append(f"  REALISED R BY FEED QUALITY "
                                 f"({len(paired)} of {tot} closed trades measurable):")
                    if clean:
                        lines.append(f"    clean (<3% frozen)   n={len(clean)}  "
                                     f"meanR:{sum(clean)/len(clean):+.3f}")
                    if dirty:
                        lines.append(f"    dirty (>=3% frozen)  n={len(dirty)}  "
                                     f"meanR:{sum(dirty)/len(dirty):+.3f}")
                    if unmeasured:
                        urs = [r for _, r in unmeasured]
                        nl = sum(1 for r in urs if r <= 0)
                        lines.append(
                            f"    UNMEASURABLE (coin never logged) n={len(urs)}  "
                            f"meanR:{sum(urs)/len(urs):+.3f}  "
                            f"({nl} of {len(urs)} are losses): "
                            f"{', '.join(f'{c} {r:+.2f}' for c, r in unmeasured)}")
                        lines.append(
                            f"    ⚠️  that is {len(urs)/tot*100:.0f}% of the book "
                            f"excluded for a reason unrelated to feed quality — "
                            f"the split above is contaminated by selection")
                    lines.append("    (n tiny and this is a post-hoc slice -- an "
                                 "accumulator, not a verdict)")

                # Control test: are frozen bars MANUFACTURING entry signals?
                # If a stalled price could fake the oversold+ranging combination
                # the gate looks for, the whole record would be built on
                # fictional setups. Measured, not assumed.
                sq = sn = fq = fn = 0
                for obs in per.values():
                    for i in range(1, len(obs)):
                        _, p, rsi, adx = obs[i]
                        qual = (rsi <= 25 or rsi >= 75) and adx < 25
                        if p == obs[i - 1][1]:
                            sn += 1; sq += qual
                        else:
                            fn += 1; fq += qual
                if sn and fn:
                    lines.append(f"  GATE CONTAMINATION CHECK: qualifying rate on "
                                 f"frozen bars {sq}/{sn} ({sq/sn*100:.2f}%) vs live "
                                 f"bars {fq}/{fn} ({fq/fn*100:.2f}%)")
                    lines.append("    (a frozen price cannot print a NEW extreme, so "
                                 "staleness suppresses signals rather than faking them)")
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
    breach = any((t.get("lev_pct") or 0) < -26 for t in trades)
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

    # trader.py is being rewritten in place: a torn write here leaves the bot
    # unable to import its own strategy module on the next restart.
    atomic_write_text(trader_path, code)

    return f"Applied: MIN_SCORE={config['min_score']}, WATCHLIST={len(config['watchlist'])} coins"


if __name__ == "__main__":
    print(full_report())
