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


def _concurrency(trades):
    """Do two open positions behave like two bets, or like one bet twice?

    MAX_TRADES=2 caps the NUMBER of positions. It says nothing about their
    correlation, and correlation is what actually sets the variance of the
    book. This was flagged qualitatively on 2026-08-23 (three shorts inside
    three hours on one market-wide pump, two of them concurrent and losing
    together) and never measured. This measures it.

    Method: every pair of closed trades whose [open, close] intervals overlap
    is one observation, scored CONCORDANT when both won or both lost. The
    yardstick is what independence would give at the book's own win rate --
    p^2 + (1-p)^2 -- not 50%.

    Two things this is NOT, and the caller prints both:

      * The pairs are not independent of each other. One long-lived position
        pairs with everything that opens inside it, so a single trade can carry
        several pairs and a single outcome can drive most of the concordance.
        `max_pairs_per_trade` is returned so the reader can see that.

      * Concurrency is a PROXY for regime, not a cause. Positions overlap
        BECAUSE setups cluster, and setups cluster when one move is dragging
        many coins at once -- exactly the regime a mean-reversion entry is
        worst in. The CAPACITY section already makes this argument from the
        rejection stream (qualifying rate inside a full book is 0.7x baseline).
        So a SOLO/CONCURRENT gap is not evidence that holding two positions
        causes losses; it is evidence that the conditions which fill the book
        are the conditions the edge dislikes.

    Returns None when there is nothing to say.
    """
    rows = []
    for t in trades:
        r = _r_of(t)
        o = _naive_utc(t.get("open_time"))
        c = _naive_utc(t.get("close_time"))
        if r is None or o is None or c is None or c < o:
            continue
        rows.append({"coin": t.get("coin", "?"),
                     "dir":  "LONG" if t.get("direction") == 1 else "SHORT",
                     "o": o, "c": c, "r": r})
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x["o"])

    pairs, in_pair = [], defaultdict(int)
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if b["o"] >= a["c"]:
                continue          # rows are sorted by open, but not by close
            ovl = (min(a["c"], b["c"]) - b["o"]).total_seconds() / 3600
            if ovl <= 0:
                continue
            pairs.append((a, b, ovl, (a["r"] > 0) == (b["r"] > 0)))
            in_pair[id(a)] += 1
            in_pair[id(b)] += 1

    wins = sum(1 for x in rows if x["r"] > 0)
    p    = wins / len(rows)
    solo = [x for x in rows if not in_pair[id(x)]]
    conc = [x for x in rows if in_pair[id(x)]]

    def _agg(g):
        if not g:
            return None
        s = sum(x["r"] for x in g)
        return {"n": len(g), "wr": sum(1 for x in g if x["r"] > 0) / len(g),
                "sum": s, "mean": s / len(g)}

    return {
        "pairs":      pairs,
        "n_pairs":    len(pairs),
        "concordant": sum(1 for x in pairs if x[3]),
        "opp_dir_concordant": sum(1 for a, b, _o, same in pairs
                                  if same and a["dir"] != b["dir"]),
        "independent": p * p + (1 - p) * (1 - p),
        "max_pairs_per_trade": max(in_pair.values()) if in_pair else 0,
        "solo": _agg(solo),
        "conc": _agg(conc),
    }


def _capacity(per, state, max_trades, rsi_lo, rsi_hi, max_adx):
    """What MAX_TRADES cost, joined from position intervals x the scan stream.

    This is the one entry-side constraint the journal cannot see from the
    inside. Every other gate (RSI, ADX, stretch) leaves a record: the scan line
    is printed, the setup is evaluated, `no mean-reversion setup` is logged. The
    capacity gate leaves NOTHING -- until 2026-09-10 live.py returned early on
    `s2_at_risk >= MAX_TRADES` without a single line, so "we looked and found
    nothing" and "we never looked" were byte-identical in the log. The only
    reason this function can reconstruct the history at all is that a SEPARATE
    loop prints RSI/ADX per coin every hour regardless.

    Method: rebuild each position's [opened_at, closed_at] interval from
    state.json, count how many were live at each scan observation, and split the
    observations on whether the book could have acted.

    The comparison is CONFOUNDED and the confound is the finding, not a defect:
    the book is full *because* setups just fired, so "book full" and "another
    setup qualifying" share a cause -- market-wide dislocation. That is exactly
    why the qualifying rate inside full hours runs several times baseline, and
    it is the same fact as the 2026-08-19 cluster (three shorts in three hours
    into one pump, two of them concurrent, both losers) seen from the other
    side. Capacity binds precisely when opportunity clusters, and what clusters
    is correlated. Read it as an argument about CORRELATION, not as a case for
    raising MAX_TRADES -- the trades it would buy are the correlated ones.

    RSI+ADX is an upper bound on qualifying (the live gate also needs
    |stretch| >= MIN_STRETCH_ATR, which the scan line does not carry), so the
    caller must deflate before quoting a trade count. It is the same upper bound
    on both sides of the split, so the RATIO is unaffected.

    Returns (hours_by_conc, blocked, blocked_q, reachable, reachable_q, runs)
    where runs is [(first_hour, last_hour, {holders})] for each full stretch.
    """
    iv = []
    for t in list(state.get("closed_trades", [])) + list(
            state.get("tracked", {}).values()):
        op = _naive_utc(t.get("opened_at"))
        if op is None:
            continue
        # An open position has no closed_at; it holds its slot up to now.
        cl = _naive_utc(t.get("closed_at")) or datetime.utcnow()
        iv.append((op, cl, t.get("coin", "?")))
    if not iv:
        return {}, 0, 0, 0, 0, []

    hours = defaultdict(lambda: [0, set()])
    blocked = blocked_q = reachable = reachable_q = 0
    for obs in per.values():
        for ts, _price, rsi, adx in obs:
            d = _naive_utc(ts.replace(" ", "T"))
            if d is None:
                continue
            who = [c for a, b, c in iv if a <= d <= b]
            hr = d.replace(minute=0, second=0, microsecond=0)
            # A single hour is one book state; take the max seen in it rather
            # than letting whichever coin printed last define the hour.
            if len(who) >= hours[hr][0]:
                hours[hr][0] = len(who)
                hours[hr][1] = set(who)
            qualifies = (rsi <= rsi_lo or rsi >= rsi_hi) and adx < max_adx
            if len(who) >= max_trades:
                blocked += 1
                blocked_q += 1 if qualifies else 0
            else:
                reachable += 1
                reachable_q += 1 if qualifies else 0

    full = sorted(h for h, v in hours.items() if v[0] >= max_trades)
    runs, cur = [], []
    for h in full:
        # Allow a one-hour hole: quiet hours (02-04 UTC) print no scan lines, so
        # a stretch spanning them is one event, not two.
        if cur and (h - cur[-1]) > timedelta(hours=2):
            runs.append(cur)
            cur = []
        cur.append(h)
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        who = set()
        for h in r:
            who |= hours[h][1]
        out.append((r[0], r[-1], who))
    return ({h: v[0] for h, v in hours.items()}, blocked, blocked_q,
            reachable, reachable_q, sorted(out, key=lambda x: x[0]))


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


def _restart_times(logs=None):
    """Every process start, as sorted datetimes.

    One source for both consumers -- the per-day restart count printed under
    LOOP LATENCY, and the process-down test in _stall_cause. They were briefly
    two independent parses of the same banner, which is how the report ends up
    disagreeing with itself about whether the bot was running (see
    [[reference_stale_instruments]]).
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
    seen = set()
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = _RESTART_RE.match(line)
                    if m:
                        seen.add(datetime(
                            int(m.group(1)[:4]), int(m.group(1)[5:7]),
                            int(m.group(1)[8:10]), int(m.group(2)),
                            int(m.group(3)), int(m.group(4))))
        except OSError:
            continue
    return sorted(seen)


def _restarts(logs=None, starts=None):
    """Process starts per day, newest last. A restart is cheap (~5s) but not
    free: it re-reads state.json, re-restores open trades, and re-prints the
    current candle header. Nine of them in one afternoon (2026-09-04) is a
    signal in its own right -- it just is not the signal the latency section
    used to report it as."""
    if starts is None:
        starts = _restart_times(logs)
    per = defaultdict(int)
    for t in starts:
        per[f"{t:%Y-%m-%d}"] += 1
    return dict(per)


# Timestamped lines that say the EXCHANGE was unreachable during a stall.
_VENUE_DOWN_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| (?:WARNING|ERROR) \| "
    r"(?:HL API \d+|Cycle error)")


def _stall_cause(due, wall, venue_events, starts=()):
    """Why the loop was late. Three causes, three different owners.

    "process down" -- a startup banner lands INSIDE the window, so there was no
    loop to block. The bot was not running and the first thing it did on waking
    was print this candle. 2026-08-22 17:00 (banner 17:50:45, the tail of the
    19h48m host blackout) and 2026-09-10 12:00 (banner 12:51:56, after a 1.9h
    outage the heartbeat itself reported). Owned by AVAILABILITY and the
    heartbeat, which already count it; counting it here too is double billing.

    "venue down" -- Hyperliquid 502s (2026-09-02 07:00, AAVE SHORT, 27.9 min).
    The ratchet cannot advance, but the STOP IS ALREADY RESTING ON THE
    EXCHANGE, so the position is protected and only the upside is stalled.
    Nothing on our side prevents it.

    "self-blocked" -- the venue was healthy, the process was up, and our own
    code held the loop (2026-08-22..24, the inline nightly review). That, and
    only that, is ours to fix.

    Checked in that order deliberately: a process that was not running cannot
    have been blocked by its own code, whatever else the log says during the
    gap. Until 2026-09-12 there were only two branches and "self-blocked" was
    the RESIDUAL -- anything without a 502 in the window was billed to us. That
    is a cause read off the absence of one alternative rather than off evidence
    ([[reference_cause_by_elimination]]), and it inflated "OURS to prevent"
    to 241 min against a true 84.
    """
    if any(due < t <= wall for t in starts):
        return ("process down", 0)
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


# Where the signal log actually begins. journal["signals"] was not written
# before this instant, so every trade opened earlier joins to nothing. This is
# a SCHEMA date, not a market fact -- see _entry_bands.
SIGNAL_LOG_FROM = "2026-08-10T17:01"


def _entry_bands(trades, signals):
    """Bucket closed trades by their ENTRY conditions, and RETURN the leftovers.

    Returns (banded, dropped). `dropped` is every trade that could not be
    banded, as (coin, opened, r, why) -- it is not the caller's option to
    ignore it, it is the coverage statement that has to be printed next to the
    result.

    The inline version this replaces did `if not sig or r is None: continue`
    with no counter, and silently computed the whole table on 14 of 20 closed
    trades. The six it discarded are ARB 07-27, ETH 07-31, AVAX 08-01,
    ETH 08-01, BTC 08-03 and INJ 08-06 -- and the reason none of them joined is
    that journal["signals"] does not start until 2026-08-10T17:01. The
    exclusion criterion was THE DATE SIGNAL LOGGING WAS SWITCHED ON, which has
    no connection whatsoever to entry quality. The two cohorts differ:

        kept     n=14  WR 36%  meanR +0.266
        dropped  n= 6  WR 50%  meanR -0.050

    so a reader comparing any cell against the report's headline (+0.171R, WR
    40%) was comparing it to a baseline drawn from a different sample than the
    table. Neither published figure describes the table's own population.

    The one useful consequence, which was accidental and is now deliberate:
    because the cut is by date and that date (08-10) falls AFTER the 08-05
    exit-regime boundary, every banded trade is a current-regime trade. The
    table is cleaner than its caption claimed -- but it was true by luck, and
    luck is not a property you get to keep silently.
    """
    banded  = defaultdict(lambda: {"n": 0, "w": 0, "r": 0.0})
    dropped = []
    for t in trades:
        sig = _sig_for(t, signals)
        r   = _r_of(t)
        if r is None:
            dropped.append((t.get("coin"), str(t.get("open_time", ""))[:16],
                            None, "no R (entry stop not recorded)"))
            continue
        if not sig:
            opened = str(t.get("open_time", ""))[:16]
            why = ("predates the signal log" if opened < SIGNAL_LOG_FROM
                   else "no signal within 2h of entry")
            dropped.append((t.get("coin"), opened, r, why))
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
    return banded, dropped


def _band(vals, val, labels):
    """First label whose edge `val` falls under; last label otherwise."""
    for edge, lab in zip(vals, labels):
        if val < edge:
            return lab
    return labels[-1]


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
    starts = []
    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    s = _RESTART_RE.match(line)
                    if s:
                        starts.append(datetime(
                            int(s.group(1)[:4]), int(s.group(1)[5:7]),
                            int(s.group(1)[8:10]), int(s.group(2)),
                            int(s.group(3)), int(s.group(4))))
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
    starts = sorted(set(starts))

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
            cause, nerr = _stall_cause(due, wall, venue, starts)
            for coin, side, still_open in _open_during(due, wall, state):
                frozen.append((due, coin, side, lag, cause, nerr))
    return by_hour, worst, frozen


_REVIEW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| INFO \| "
    r"(Nightly|Weekly) review (starting|complete)"
    r"(?: \(book managed (\d+)x during the brain wait\))?")

# review._self_improve logs this the moment the brain returns (from
# 2026-09-17), BEFORE the os.execv that ends a flat-book night with changes.
# live.py's "review complete" line carries the same count but is never reached
# on those nights: 2026-09-16 23:20->23:28 was the first review after the
# keepalive shipped, it ended in execv, and the report had nothing to read.
_BRAIN_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}) \| INFO \| "
    r"Brain wait done in (\d+)s \(book managed (\d+)x during the brain wait\)")


def _review_windows(logs=None):
    """Every nightly/weekly review as
    (start, end, kind, how_ended, passes, brain_secs).

    LOOP LATENCY measures a candle header against its own hour, which is the
    right instrument for a review that starts at 23:00 -- and the WRONG one
    for a review that starts at 23:20 and finishes before 00:00, which is
    where it has run since 2026-09-06. A 14-minute review on 2026-09-14 sat on
    two open shorts and produced no late candle at all, so the section above
    reported 0 minutes for it. The instrument had drifted off the thing it
    was built to see ([[reference_stale_instruments]]).

    A review that ends in os.execv (review._self_improve restarts the bot on a
    flat book) never logs "complete"; its end is the next startup banner. A
    review followed by another review-start with no complete (2026-09-13:
    nightly -> weekly) ended when the next one began. Windows with no visible
    end at all are dropped, not guessed.

    `passes` is the keepalive count. It is read from the review's own "Brain
    wait done" line when present (2026-09-17 on, written before any execv),
    else from the loop's "review complete" line (2026-09-16 on, only reached
    when the process survives the review). None on rows with neither. Zero
    with a brain wait of a minute or more means the hook was not installed,
    which is the regression this column exists to show; zero on a 5-second
    wait is the brain failing fast, not the hook. `brain_secs` is None when
    no brain line was seen.
    """
    if logs is None:
        logs = sorted(glob.glob("/root/trade/bot.2026-*.log")) + [BOTLOG_F]
    events = []          # (ts, kind, what, passes, secs)

    def _ts(m):
        return datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]),
                        int(m.group(1)[8:10]), int(m.group(2)),
                        int(m.group(3)), int(m.group(4)))

    for path in logs:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    m = _REVIEW_RE.match(line)
                    if m:
                        passes = int(m.group(7)) if m.group(7) is not None else None
                        events.append((_ts(m), m.group(5).lower(), m.group(6),
                                       passes, None))
                        continue
                    b = _BRAIN_RE.match(line)
                    if b:
                        events.append((_ts(b), "brain", "done",
                                       int(b.group(6)), int(b.group(5))))
                        continue
                    s = _RESTART_RE.match(line)
                    if s:
                        events.append((_ts(s), "process", "start", None, None))
        except OSError:
            continue
    events = sorted(set(events))
    out = []
    for i, (ts, kind, what, _p, _s) in enumerate(events):
        if what != "starting":
            continue
        brain = None          # (passes, secs) from the brain line, if seen
        for ts2, kind2, what2, passes2, secs2 in events[i + 1:]:
            if kind2 == "brain":
                brain = (passes2, secs2)
                continue
            if kind2 == kind and what2 == "complete":
                how = "complete"
            elif kind2 == "process":
                how = "restart"
            elif what2 == "starting":
                how = "next review"
            else:
                continue
            if brain is not None:
                out.append((ts, ts2, kind, how, brain[0], brain[1]))
            else:
                out.append((ts, ts2, kind, how,
                            passes2 if how == "complete" else None, None))
            break
    return out


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


def _mfe_seen(x):
    """Best available lower bound on a trade's true peak excursion, in R.

    `peak_roe_pct` is SAMPLED by the tracker poll loop, so it undershoots the
    real peak by up to one poll interval. `locked_r` is RECORDED: it is the
    level update_sl() moved the stop to, and the ratchet only moves a stop to a
    level price has actually traded through. So a recorded lock is a hard
    lower bound on peak excursion -- a physical fact, not an estimate -- and
    `max(mfe_r, locked_r)` is strictly closer to the truth than either alone.

    Why this exists as a shared helper rather than inline: the bias is
    ONE-SIDED BY CONSTRUCTION. Only trades that armed carry a lock, and only
    winners arm, so reading raw `mfe_r` understates the excursion of winners
    ONLY and leaves every loser correct. That is the same shape as the
    2026-08-17 stop-denominator bug: an error that lands exclusively on the
    fat tail that decides the book. Measured on 2026-09-21, four trades
    (SOL/SUI 08-18, ETH 08-21, OP 09-14) had their stop locked AT +2.50R while
    their recorded peak read 2.32-2.46R, so every >=2.5R test scored four real
    arms as misses: the 2.0R->2.5R continuation printed 38% when the recorded
    facts say 88%.

    `locked_r` is 0.0 for trades that recorded the field but never armed, and
    None for trades predating it (2026-08-16); max() handles both without a
    special case. Returns None when no excursion was recorded at all -- a
    measurement never taken is not a measurement of zero, see
    [[mfe-not-realised-r]].
    """
    m = x.get("mfe_r")
    if m is None:
        return None
    lk = x.get("locked_r")
    return m if lk is None else max(m, float(lk))


def _excursion_hazard(exc, levels=(0.5, 1.0, 1.5, 2.0, 2.5, 3.0)):
    """Conditional continuation: given a trade got to L, did it get to the next L?

    Every existing view of the excursion column is a set of MARGINAL survival
    counts ("26% reached 2.0R"). Those describe how many trades ended up in the
    tail; they do not describe WHERE the population thins out, and those are
    different questions with different answers. Marginal counts falling smoothly
    from 84% to 11% look like one continuous distribution. The conditional
    counts show whether the attrition is spread evenly across the range or
    concentrated in one band -- and if it is concentrated, that band is the
    level at which this entry's trades stop being one population and become two.

    Returns [{lo, hi, n_at_lo, n_at_hi, p, censored}, ...], plus the count of
    rows with no recorded excursion, which the caller must print.

    CENSORING -- the part that makes the top of this curve unreadable, and the
    reason `censored` exists as a field rather than a footnote. The ratchet arms
    at TRAIL_START_R and the trade is then closed AT that level, so no poll can
    ever observe an MFE materially above it for a trade that armed. MFE is
    therefore right-censored at TRAIL_START_R by our own exit rule: below it the
    column is an observation of the market, at and above it the column is an
    observation of the exit. "Only 11% reached 2.5R" is not a fact about the
    entry -- it is 2.5R being the level at which we stop watching. Rows at or
    above TRAIL_START_R are flagged so nobody reads a market claim off them.

    Counts through `_mfe_seen`, NOT raw `mfe_r`: a trade whose stop the ratchet
    locked at +2.50R demonstrably reached +2.50R, whatever the poll loop
    happened to sample. Reading the raw column here put the single largest
    distortion in this whole report on the one row that sits at the deployed
    arming level. A trade whose excursion was never recorded is not a trade
    whose excursion was zero -- see [[mfe-not-realised-r]].
    """
    meas = [x for x in exc if _mfe_seen(x) is not None]
    unrecorded = len(exc) - len(meas)
    try:
        import strategy2
        arm = float(strategy2.TRAIL_START_R)
    except Exception:
        arm = None
    rows, prev_n, prev_lo = [], len(meas), 0.0
    for L in levels:
        n = sum(1 for x in meas if _mfe_seen(x) >= L)
        rows.append({
            "lo": prev_lo, "hi": L,
            "n_at_lo": prev_n, "n_at_hi": n,
            "p": (n / prev_n) if prev_n else None,
            # `L > arm`, not `>=`. Reaching the arming level itself is now
            # FULLY observable: arming records `locked_r`, and _mfe_seen reads
            # it, so "did it get to 2.5R" is answered by a recorded fact rather
            # than by whether a poll happened to catch the tick. Only levels
            # strictly ABOVE the arm are censored, because that is where the
            # trade has already been closed and no poll can follow it. Leaving
            # this at `>=` flagged the corrected 2.0->2.5 row as unreadable and
            # would have buried the finding it exists to surface.
            "censored": arm is not None and L > arm,
        })
        prev_n, prev_lo = n, L
    return rows, unrecorded, arm


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


def _stop_fill_quality(state):
    """Did the ORIGINAL stop deliver the -1.00R it promised?

    The counterpart to _ratchet_slippage, and it exists because that section
    left an obvious question unasked. A stop placed at 1R from entry defines
    the loss: realised should EQUAL -1.00R. Like the ratchet's lock, that is an
    invariant, not a statistic -- the trade cannot choose to lose more than the
    distance to its own stop, only the FILL can. So every R by which realised
    falls below -1.00R is venue execution loss landing on losers, exactly as
    the ratchet leak lands on winners.

    Nothing measured this before 2026-09-06. EXIT MECHANISM pools all twelve
    original-stop exits into a single meanR, and a mean near -1.00R is
    indistinguishable there from twelve stops that each filled perfectly and
    from twelve that missed by +-0.2R in cancelling directions. Only the
    per-trade deviation separates those two worlds, and they imply opposite
    fixes.

    Classification is the whole difficulty. `locked_r is None and sl ==
    sl_orig` looks like it identifies an untouched stop, but it also matches
    the four pre-2026-08-16 winners whose trail moved without writing a lock
    back to state (AVAX/ETH 08-01, BTC 08-03, AVAX 08-11) -- those exited up to
    5.8% BEYOND their recorded stop, in profit. The disambiguator is sign: the
    original stop is always adverse, so rr > 0 proves the trade ratcheted no
    matter what the record says. Misclassifying those four would import +5.3R
    of ratchet profit into a loss-side execution figure and invert its verdict.

    `width_pct` is 1R expressed as a fraction of entry, and it is reported
    because it is the conversion factor between the two other columns and the
    reason they look inconsistent. A fill 0.09% past the trigger cost FIL
    0.19R while a fill 0.61% past it cost ARB 0.15R -- not a contradiction, but
    stops 0.48% and 4.37% wide. Venue slippage is priced in percent; edge is
    counted in R; a tight stop converts the first into the second at a far
    worse rate. Reporting the deviation in R alone invites tuning the stop
    distance to fix what is really a spread cost.

    Returns (measured, gap) where measured is
    [{coin, opened, real_r, dev_r, adv_pct, width_pct}, ...] with dev_r
    positive when the stop filled BETTER than it asked, and gap is
    [(coin, opened, real_r), ...] for original-stop losers whose prices are
    incomplete -- named rather than dropped, because a silent absence in an
    execution-quality table reads as an absence of the problem.
    """
    measured, gap = [], []
    for t in state.get("closed_trades", []):
        try:
            rr = t.get("rr")
            if rr is None:
                continue
            rr = float(rr)
            opened = str(t.get("opened_at", ""))[:10]

            # Positive rr can only have come from a stop that moved, whether or
            # not a lock was recorded. Excluded here and owned by
            # _ratchet_slippage instead.
            if rr >= 0:
                continue
            locked = t.get("locked_r")
            if locked is not None and float(locked) > 0:
                continue

            coin = t.get("coin", "?")
            try:
                entry = float(t["entry"])
                trig  = float(t["sl"])
                orig  = float(t.get("sl_orig", trig))
                fill  = float(t["exit"])
                d     = int(t["dir"])
            except (KeyError, TypeError, ValueError):
                gap.append((coin, opened, rr))
                continue
            # A stop the ratchet has already moved is not the original stop,
            # even when locked_r is missing.
            if abs(trig - orig) > 1e-12 or entry <= 0 or trig <= 0:
                gap.append((coin, opened, rr))
                continue

            width = abs(entry - orig) / entry
            if width <= 0:
                gap.append((coin, opened, rr))
                continue

            # Signed so that positive always means "filled worse than the stop
            # asked for", matching _ratchet_slippage's adv_pct convention.
            adv = ((trig - fill) if d == 1 else (fill - trig)) / trig

            measured.append({
                "coin": coin, "opened": opened, "real_r": rr,
                "dev_r": rr + 1.0, "adv_pct": adv, "width_pct": width,
            })
        except (TypeError, ValueError):
            continue
    return measured, gap


SELFLEARN_LOG = "/root/trade/selflearn.log"

# Lines self_improve.sh prints itself. Our own banner is never the reason a
# session died, and on the --retry paths it is the FIRST line of the body, so
# any positional guess at the cause lands on it by construction.
_OWN_BANNER = re.compile(r"^(MODE:|Session ended:|Session complete\.|=+$)")


def _failure_reason(body, benign):
    """Pick the line that explains a non-zero exit.

    Was `body.splitlines()[0]` until 2026-09-09. That is a guess about WHERE
    the harness prints a fatal error, and it held for 14 of 17 historical
    failures by luck rather than by rule. It broke on 2026-09-08 in both
    available ways at once: a benign permission warning appeared above the
    error, and the two retries put our own `MODE:` banner on line 1. All three
    of that night's failures were reported to the owner as a settings.json
    typo when the real cause was `Your organization has disabled Claude
    subscription access` -- a five-second fix pointed at instead of an
    account-level one, for the three nights the bot traded unsupervised.

    So classify on the invariant instead of the position: a line that also
    opens a session which exited 0 CANNOT be why a different session exited
    non-zero. `benign` carries those lines. Among what survives, take the
    first -- the harness prints the cause before its remediation hints
    (2026-08-20: "claude native binary not installed" above "Or reinstall
    without --ignore-scripts").
    """
    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    for l in lines:
        if l in benign or _OWN_BANNER.match(l):
            continue
        return l[:60]
    # Nothing survived: no successful session to learn from, or a body that is
    # all banner. Fall back to the old behaviour rather than claiming to know.
    return (lines or ["unknown"])[0][:60]


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

    parsed = []
    for i in range(1, len(chunks) - 2, 3):
        date, tm, body = chunks[i], chunks[i + 1], chunks[i + 2]
        m = re.search(r"Session ended: \S+ UTC \(exit (\d+)\)", body)
        if not m:
            continue                      # still running, or pre-07-24 format
        parsed.append((date, tm, int(m.group(1)), body))

    # Pass 1: what the harness prints ABOVE a session that then succeeded.
    # Deliberately the FIRST line only, not every line of the body -- a
    # successful body contains the whole session report, and that report
    # routinely quotes the very error strings we are trying to attribute
    # (this one does). Harvesting the full body would launder tonight's
    # diagnosis into tomorrow's benign set and re-break this function.
    benign = set()
    for _d, _t, rc, body in parsed:
        if rc != 0:
            continue
        for l in (x.strip() for x in body.strip().splitlines()):
            if l and not _OWN_BANNER.match(l):
                benign.add(l)
                break

    rows = []
    for date, tm, rc, body in parsed:
        lim = re.search(r"You've hit your (session|weekly|monthly)[^\n]*", body)
        if rc == 0:
            reason = ""
        elif lim:
            reason = lim.group(0).replace("You've hit your ", "").strip()
        elif rc == 124:
            reason = "killed at the 60-minute wall clock"
        else:
            reason = _failure_reason(body, benign)
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
                # The 2026-09-02 reorder put _reconcile_dust / _check_closed /
                # _check_trail_s2 / _verify_stops ABOVE the maintenance block,
                # so the ratchet runs ONCE at the top of the pass that starts
                # the review -- and then not again until the review returns.
                # This caption used to read "after 09-02: ratchet already ran",
                # which is true of one iteration and false of the stall. A
                # blocked loop is a frozen ratchet whatever the source order;
                # the fix that actually ends the freeze is the keepalive of
                # 2026-09-16 (see REVIEW BLOCKING below).
                lines.append("  ⚠️  candle >5min late WITH A POSITION OPEN "
                             "(ratchet ran once at the top of the pass, then "
                             "stood still for the whole stall):")
                # Totals are per STALL EVENT, not per row. _frozen carries one
                # row per position open during the stall, because which
                # positions were exposed is the point of the list -- but two
                # positions open across one 52.9 min window cannot freeze
                # 105.8 min of a 52.9 min hour. Summing the rows did exactly
                # that on 2026-09-10 (DOGE + ETH), inflating the wall-clock
                # total by the length of the worst event in the book.
                _by_ev = {}
                for _due, _coin, _side, _lag, _cause, _nerr in _frozen:
                    _by_ev[_due] = (_lag, _cause)
                _tot = sum(l for l, _ in _by_ev.values())
                _bucket = defaultdict(float)
                for _l, _c in _by_ev.values():
                    _bucket[_c] += _l
                _seen_ev = set()
                for _due, _coin, _side, _lag, _cause, _nerr in _frozen:
                    _tag = {
                        "venue down": f"venue down ({_nerr} API errors) — stop "
                                      f"was still resting on the exchange",
                        "process down": "PROCESS DOWN — the bot was not running; "
                                        "counted by AVAILABILITY, not ours here",
                    }.get(_cause, "SELF-BLOCKED — venue was healthy, we were "
                                  "not looking")
                    _dup = "  ↳ same stall" if _due in _seen_ev else ""
                    _seen_ev.add(_due)
                    lines.append(f"    {_due:%Y-%m-%d %H:%M}  {_coin} {_side}"
                                 f"  frozen {_lag/60:.1f} min  [{_tag}]{_dup}")
                lines.append(
                    f"    {len(_by_ev)} stall event(s), {_tot/60:.0f} min "
                    f"wall-clock: {_bucket['self-blocked']/60:.0f} min OURS to "
                    f"prevent, {_bucket['venue down']/60:.0f} min venue, "
                    f"{_bucket['process down']/60:.0f} min downtime")
            lines.append("  (blocking maintenance was moved BELOW position "
                         "management 2026-09-02 and to 23:20 on 09-06, so a "
                         "review no longer shows up here as a late candle at "
                         "all — see REVIEW BLOCKING)")
    except Exception as _e:
        lines.append(f"\n── LOOP LATENCY ──\n  latency check failed: {_e}")

    # ── Review blocking ────────────────────────────────────────────
    # The review is the one piece of blocking work the loop runs on purpose,
    # and since it moved to 23:20 it finishes before the next candle, so the
    # candle-latency instrument above cannot see it. This one reads the
    # review's own start/end lines and asks the only question that matters:
    # was a position open while the loop was not looking.
    try:
        _wins = _review_windows()
        _st = load_state()
        lines.append(f"\n── REVIEW BLOCKING (loop stopped inside nightly/weekly review) ──")
        if not _wins:
            lines.append("  no review windows found in the retained logs")
        else:
            _durs = sorted((e - s).total_seconds() for s, e, _k, _h, _p, _b in _wins)
            _n_night = sum(1 for w in _wins if w[2] == "nightly")
            lines.append(f"  windows: {len(_wins)} ({_n_night} nightly)  "
                         f"median {_durs[len(_durs)//2]/60:.1f} min  "
                         f"p90 {_durs[int(len(_durs)*0.9)]/60:.1f} min  "
                         f"max {_durs[-1]/60:.1f} min")
            # The latest nightly, whatever the book held: the keepalive count
            # is the nightly proof the hook is installed, and a flat-book
            # night is exactly when a missing hook must be caught -- by the
            # time a position is open during the review it is too late.
            _nights = [w for w in _wins if w[2] == "nightly"]
            if _nights:
                s, e, _k, how, passes, bsecs = _nights[-1]
                if bsecs is not None:
                    _kp = (f"brain wait {bsecs/60:.1f} min, book managed {passes}x"
                           + ("  ‼️  ZERO passes on a real wait — hook NOT installed"
                              if passes == 0 and bsecs >= 60 else ""))
                elif passes is not None:
                    _kp = f"book managed {passes}x (from the loop's completion line)"
                else:
                    _kp = ("no keepalive count in the log — review ended by "
                           f"{how} before the loop could print one"
                           + (" (predates the 2026-09-17 brain-wait line)"
                              if s < datetime(2026, 9, 17) else
                              "  ‼️  the brain line is missing: brain raised, or the log line is gone"))
                lines.append(f"  latest nightly: {s:%Y-%m-%d %H:%M} → {e:%H:%M}  "
                             f"{(e-s).total_seconds()/60:.1f} min  ended by {how}  [{_kp}]")
            _exposed = []
            for s, e, k, how, passes, _b in _wins:
                _pos = _open_during(s, e, _st)
                if _pos:
                    _exposed.append((s, e, k, how, passes, _pos))
            if not _exposed:
                lines.append("  no review crossed an open position ✓")
            else:
                _unman = sum((e - s).total_seconds() for s, e, _k, _h, p, _ in _exposed
                             if not p)
                lines.append(f"  ⚠️  {len(_exposed)} review(s) crossed an open position "
                             f"— {_unman/60:.0f} min with the ratchet FROZEN:")
                for s, e, k, how, passes, pos in _exposed:
                    _coins = ", ".join(f"{c} {d}" for c, d, _ in pos)
                    if passes:
                        _tag = f"managed — book polled {passes}x during the brain wait"
                    elif passes == 0:
                        _tag = "‼️  0 keepalive passes — hook NOT installed, ratchet frozen"
                    else:
                        _tag = "ratchet frozen (predates the 2026-09-16 keepalive)"
                    lines.append(f"    {s:%Y-%m-%d %H:%M} → {e:%H:%M}  "
                                 f"{(e-s).total_seconds()/60:5.1f} min  {k:<7} "
                                 f"ended by {how:<11} {_coins}  [{_tag}]")
            # Any ending counts: a flat-book night ends in execv, not
            # "complete", and 2026-09-16 showed that reading only completed
            # windows made the check unfireable on exactly those nights. A
            # brain wait under a minute with 0 passes is the brain failing
            # fast (usage limit, CLI error), not a missing hook.
            _later = [w for w in _wins if w[0] >= datetime(2026, 9, 16)]
            _nohook = [w for w in _later if w[2] == "nightly" and w[4] == 0
                       and (w[5] is None or w[5] >= 60)]
            if _nohook:
                lines.append(f"  ‼️  {len(_nohook)} nightly review(s) since 2026-09-16 "
                             f"ran with ZERO keepalive passes — the brain wait "
                             f"is blocking again")
            lines.append("  (since 2026-09-16 ai_brain._run_claude hands control to "
                         "live._manage_book every 20s; the rest of the review still "
                         "blocks, typically well under a minute)")
    except Exception as _e:
        lines.append(f"\n── REVIEW BLOCKING ──\n  check failed: {_e}")

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
            # WHERE IT IS, not just where it has been. Every other line in this
            # section is an excursion (MFE/MAE) or a consistency check between
            # recorded fields; none of them says what the position is worth
            # now. On 2026-09-07 the report showed ETH at "MFE +2.02R" and the
            # session read that as a trade near its arming threshold. Two days
            # later the same two lines were unchanged while the position had
            # round-tripped to +0.26R -- the give-back is invisible in a column
            # that only ever ratchets up. price_series is the tracker's own
            # poll record, so this stays a pure function of state.json.
            _ps  = [p for p in (_t.get("price_series") or []) if p]
            _now = None
            if _ps:
                _now = (float(_ps[-1]) - _ent) * (_t.get("dir") or 1) \
                       / abs(_ent - _stop)
            lines.append(f"    MFE {_mfe:+.2f}R   MAE {_mae:+.2f}R"
                         + (f"   NOW {_now:+.2f}R @ ${float(_ps[-1]):g}"
                            if _now is not None else
                            "   NOW n/a (no price_series yet)"))
            if _now is not None and _mfe >= 0.5 and (_mfe - _now) >= 0.5:
                lines.append(
                    f"    ↩️  GIVEN BACK {_mfe - _now:.2f}R of a {_mfe:+.2f}R "
                    f"peak ({100 * (_mfe - _now) / _mfe:.0f}%) — unrealised, "
                    f"and nothing is locked until the ratchet arms at "
                    f"{_arm_r:.2f}R")
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

    # ── Concurrency ────────────────────────────────────────────────
    # DIRECTION SPLIT asks what we bet on. This asks how many bets we were
    # really making. MAX_TRADES caps position COUNT; nothing caps correlation,
    # and correlation is what sets the book's variance. Flagged 2026-08-23,
    # measured 2026-09-20. Report-only: MAX_TRADES is owner-locked.
    try:
        _cc = _concurrency(trades)
    except Exception as _e:                       # never let a new section
        _cc = None                                # abort the sections below it
        lines.append(f"\n── CONCURRENCY ──\n  concurrency check failed: {_e}")
    if _cc and _cc["n_pairs"]:
        lines.append("\n── CONCURRENCY (is a second position a second bet?) ──")
        lines.append(
            f"  overlapping pairs: {_cc['n_pairs']}   "
            f"concordant (both won or both lost): {_cc['concordant']}"
            f" = {_cc['concordant'] / _cc['n_pairs'] * 100:.0f}%")
        lines.append(f"  if the two were independent at this book's WR: "
                     f"{_cc['independent'] * 100:.0f}%")
        for _lbl, _g in (("SOLO", _cc["solo"]), ("CONCURRENT", _cc["conc"])):
            if _g:
                lines.append(f"  {_lbl:<11} n={_g['n']:2d}  WR:{_g['wr'] * 100:3.0f}%  "
                             f"sumR:{_g['sum']:+.2f}  meanR:{_g['mean']:+.3f}")
        for _a, _b, _ovl, _same in _cc["pairs"]:
            lines.append(
                f"    {_a['coin']:<5}{_a['dir']:<6}{_a['r']:+.2f}R || "
                f"{_b['coin']:<5}{_b['dir']:<6}{_b['r']:+.2f}R  "
                f"overlap {_ovl:5.1f}h  "
                f"{'CONCORDANT' if _same else 'split     '} "
                f"{'sameDir' if _a['dir'] == _b['dir'] else 'OPPdir'}")
        if _cc["opp_dir_concordant"]:
            lines.append(
                f"  {_cc['opp_dir_concordant']} concordant pair(s) were OPPOSITE direction — "
                f"a long and a short losing together is not directional beta,")
            lines.append(
                "  it is the regime being wrong for mean reversion in both directions at once.")
        lines.append(
            f"  CAUTION: pairs are NOT independent observations — one long-lived position pairs with")
        lines.append(
            f"  everything opened inside it, and here one trade carries up to "
            f"{_cc['max_pairs_per_trade']} of the {_cc['n_pairs']} pairs.")
        lines.append(
            "  CAUTION: concurrency is a PROXY FOR REGIME, not a cause. The book fills because setups")
        lines.append(
            "  cluster, and setups cluster when one move drags many coins — see CAPACITY, where the")
        lines.append(
            "  qualifying rate inside a full book is 0.7x baseline. Do NOT read a SOLO/CONCURRENT gap")
        lines.append(
            "  as 'holding two positions loses money'; read it as 'the conditions that fill the book")
        lines.append(
            "  are the conditions this entry is worst in'. MAX_TRADES is owner-locked either way.")

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
            # STEP LAG -- the OTHER half of what the ratchet fails to capture,
            # and a different cost with a different cause.
            #
            #   leak = locked_r - realised    execution: the fill came in below
            #                                 the level the stop was resting at
            #   lag  = peak     - locked_r    the ratchet never STEPPED that high;
            #                                 price ran past it between polls
            #
            # Reporting only `leak` silently attributes the whole shortfall to
            # execution and makes TRAIL_STEP_R look free. It is not: AAVE ran to
            # 3.13R with the stop stepped to 3.00R. That 0.13R is not slippage,
            # it is step granularity, and tightening RATCHET_SLIP_CAP cannot
            # recover a single basis point of it.
            _exc_by_key = {(x["coin"], x["opened"]): x for x in _excursion_stats(state)}
            _lags = []
            for x in _slip:
                _e = _exc_by_key.get((x["coin"], x["opened"]))
                _peak = _mfe_seen(_e) if _e else None
                if _peak is not None:
                    _lags.append((x, max(0.0, _peak - x["locked_r"])))
            if _lags:
                _totlag = sum(l for _, l in _lags)
                _nonzero = [(x, l) for x, l in _lags if l > 0.005]
                lines.append(f"  step lag (peak above the level the stop reached): "
                             f"{_totlag:+.3f}R over {len(_lags)} arm(s)"
                             + (f" — all of it on "
                                + ", ".join(f"{x['coin']} {x['opened']} {l:+.3f}R"
                                            for x, l in _nonzero)
                                if _nonzero else " — none"))
                lines.append(f"  TOTAL un-harvested = leak {_tot:+.3f}R + lag {_totlag:+.3f}R "
                             f"= {_tot + _totlag:+.3f}R")
                lines.append("  the two have DIFFERENT fixes: leak is execution (cap/placement),")
                lines.append("  lag is TRAIL_STEP_R granularity. Both owner-locked; report only.")
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

    # ── Stop fill quality ──────────────────────────────────────────
    # The section above asks whether winners delivered what was locked. This
    # one asks the same question of losers, and the two answers are not the
    # same. See _stop_fill_quality.
    try:
        _sf, _sf_gap = _stop_fill_quality(state)
        if _sf or _sf_gap:
            lines.append("\n── STOP FILL QUALITY (original stop vs its -1.00R promise) ──")
        if _sf:
            for x in sorted(_sf, key=lambda v: v["opened"]):
                lines.append(
                    f"  {x['coin']:<5} {x['opened']}  realised {x['real_r']:+.3f}R  "
                    f"vs -1.000R  dev {x['dev_r']:+.3f}R   "
                    f"fill {x['adv_pct']*100:+.3f}% vs trigger   "
                    f"1R = {x['width_pct']*100:.2f}% of entry")
            _n    = len(_sf)
            _sum  = sum(x["dev_r"] for x in _sf)
            _abs  = sum(abs(x["dev_r"]) for x in _sf)
            _worst = min(_sf, key=lambda v: v["dev_r"])
            lines.append(f"  n={_n}  net {_sum:+.3f}R  mean {_sum/_n:+.4f}R/stop  "
                         f"gross dispersion {_abs:.3f}R")
            lines.append(f"  worst fill: {_worst['coin']} {_worst['opened']} "
                         f"{_worst['dev_r']:+.3f}R ({_worst['adv_pct']*100:+.3f}% past trigger)")

            # The verdict, stated in the direction that stops a future session
            # re-proposing a fix for a cost that is not there.
            _adverse = [x for x in _sf if x["dev_r"] < 0]
            lines.append(f"  {len(_adverse)}/{_n} filled adverse, {_n-len(_adverse)}/{_n} filled favourable")
            if abs(_sum / _n) < 0.02:
                lines.append("  VERDICT: the original stop is execution-UNBIASED. Net "
                             f"{_sum:+.3f}R over {_n} stops is not a tax, it is noise "
                             "cancelling;")
                lines.append("  the resting exchange stop fills either side of its trigger "
                             "about equally. Do NOT spend a session tightening entry")
                lines.append("  brackets or stop placement to recover it -- there is nothing "
                             "to recover.")
            else:
                lines.append(f"  VERDICT: net {_sum:+.3f}R over {_n} stops is a REAL "
                             f"{'cost' if _sum < 0 else 'credit'}, not cancelling noise "
                             "-- investigate before tuning anything else.")

            # The comparison that gives the number its meaning.
            try:
                if _slip:
                    _rmean = sum(x["slip_r"] for x in _slip) / len(_slip)
                    lines.append(f"  vs RATCHET: {_rmean:+.3f}R lost per arm (n={len(_slip)}) "
                                 f"against {_sum/_n:+.4f}R per original stop.")
                    lines.append("  Execution loss in this book is a RATCHET phenomenon, not a "
                                 "venue tax -- the ratchet arms at market and")
                    lines.append("  rests its stop on the price that just traded; the original "
                                 "stop rests far away and waits. Same venue,")
                    _ratio = (f"{abs(_rmean / (_sum / _n)):.0f}x"
                              if abs(_sum / _n) > 1e-9 else "unboundedly more")
                    lines.append(f"  same order type, {_ratio} the leak per event. "
                                 "That is placement, not liquidity.")
            except NameError:
                pass

            _wmin = min(x["width_pct"] for x in _sf)
            _wmax = max(x["width_pct"] for x in _sf)
            lines.append(f"  1R width spans {_wmin*100:.2f}%-{_wmax*100:.2f}% of entry "
                         f"({_wmax/_wmin:.1f}x) at a FIXED SL_ATR_MULT -- so an identical")
            lines.append("  percentage of slippage buys wildly different amounts of R. Read the "
                         "dev column against the width column,")
            lines.append("  never on its own.")
            lines.append("  NOTE: realised should EQUAL -1.00R -- an invariant, not a statistic. "
                         "The trade cannot lose more than the")
            lines.append("  distance to its own stop; only the fill can.")
        if _sf_gap:
            lines.append("  prices incomplete, excluded above: "
                         + ", ".join(f"{c} {o} {r:+.2f}R" for c, o, r in _sf_gap))
    except Exception as _e:
        lines.append(f"\n── STOP FILL QUALITY ──\n  stop fill check failed: {_e}")

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
        # _mfe_seen, not the raw poll sample -- a recorded ratchet lock proves
        # price reached that level. See the helper for what reading the raw
        # column cost this section.
        mfes = sorted(_mfe_seen(x) for x in meas)
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
        # The same column read conditionally instead of marginally. The counts
        # above say how many trades ended in the tail; these say where the
        # population thins, which is a different question.
        haz, haz_unrec, haz_arm = _excursion_hazard(exc)
        lines.append("  CONTINUATION (given it got to lo, did it reach hi?):")
        for h in haz:
            if h["n_at_lo"] == 0:
                continue
            mark = "  ← censored by our own exit" if h["censored"] else ""
            lines.append(f"    {h['lo']:.1f}R -> {h['hi']:.1f}R   "
                         f"{h['n_at_hi']:2}/{h['n_at_lo']:<2} = {h['p']*100:3.0f}%{mark}")
        if haz_unrec:
            lines.append(f"    (excludes {haz_unrec} trade(s) with no recorded "
                         f"excursion — not counted as zero)")
        if haz_arm is not None:
            lines.append(f"    CENSORING: the ratchet arms at {haz_arm:.2f}R and closes the trade")
            lines.append("    THERE, so no poll can observe an MFE ABOVE it on a trade that")
            lines.append(f"    armed. Up to and including {haz_arm:.2f}R this column measures the")
            lines.append("    market (arming records locked_r, which proves the level was")
            lines.append("    reached); strictly above it, it measures our exit. Do not read")
            lines.append("    the flagged rows as how far price would have run.")
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
            # Print the corrected peak, and say so on the rows where it differs,
            # so the column stays auditable against raw state.json rather than
            # silently disagreeing with it.
            seen = _mfe_seen(x)
            note = ("" if abs(seen - x["mfe_r"]) < 1e-9
                    else f"  [peak from locked_r; poll sampled {x['mfe_r']:+.2f}R]")
            give = seen - x["real_r"]
            lines.append(f"    {x['coin']:<5} {x['opened']}  MFE {seen:+5.2f}R  "
                         f"MAE {x['mae_r']:+5.2f}R  ->  {x['real_r']:+5.2f}R  "
                         f"(gave back {give:+.2f}R)  [{x['regime']}]{note}")

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
        # Checked against _mfe_seen for the same reason the counts are: a
        # recorded lock is a better bound than a sampled peak, so using it
        # makes this test STRICTER on losers (unchanged) and correct on
        # winners, instead of leaning on TOL to absorb the sampling gap.
        TOL = 0.10
        bad = [x for x in meas
               if x["real_r"] > _mfe_seen(x) + TOL or x["real_r"] < x["mae_r"] - TOL]
        if bad:
            lines.append(f"  ⚠️  INVARIANT VIOLATED (MAE <= realised <= MFE, tol {TOL}R):")
            for x in bad:
                lines.append(f"      {x['coin']} {x['opened']}: MAE {x['mae_r']:+.2f} "
                             f"realised {x['real_r']:+.2f} MFE {_mfe_seen(x):+.2f} "
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

    banded, band_dropped = _entry_bands(trades, signals)
    if banded:
        # Coverage FIRST, by name, before any cell is read. This table was
        # computed on 14 of 20 closed trades for weeks with no line saying so;
        # the cut was the date signal logging began, and the discarded cohort
        # had a different WR and a different meanR from the kept one.
        kept = [t for t in trades
                if _sig_for(t, signals) and _r_of(t) is not None]
        kr = [_r_of(t) for t in kept]
        lines.append(f"  COVERAGE: {len(kept)} of {len(trades)} closed trades banded "
                     f"({len(kept)/len(trades)*100:.0f}%)"
                     + (f"  — table baseline WR:{sum(1 for r in kr if r>0)/len(kr)*100:.0f}% "
                        f"meanR:{sum(kr)/len(kr):+.3f}" if kr else ""))
        if band_dropped:
            dr = [d[2] for d in band_dropped if d[2] is not None]
            lines.append(f"  NOT BANDED n={len(band_dropped)}"
                         + (f"  WR:{sum(1 for r in dr if r>0)/len(dr)*100:.0f}% "
                            f"meanR:{sum(dr)/len(dr):+.3f}" if dr else ""))
            for coin, opened, r, why in band_dropped:
                rs = f"{r:+.2f}R" if r is not None else "  n/a"
                lines.append(f"      {coin:<5} {opened}  {rs}  — {why}")
            lines.append("  ⚠️  do NOT compare a cell below against the report's headline EV/WR —")
            lines.append("      the headline is drawn from all "
                         f"{len(trades)} trades, this table from {len(kept)}.")
        for label, bs in sorted(banded.items()):
            lines.append(f"  {label:<16} n={bs['n']:2}  WR:{bs['w']/bs['n']*100:3.0f}%  "
                         f"meanR:{bs['r']/bs['n']:+.3f}")
        lines.append("  (n is far too small to act on; this is an accumulator)")
        # Stated deliberately because it was previously true by accident: the
        # drop is by date, and that date (08-10) falls after the 08-05 exit
        # regime change, so this table is all current-regime trades.
        lines.append(f"  (every banded trade opened on/after {SIGNAL_LOG_FROM[:10]}, which is")
        lines.append(f"   after the {EXIT_REGIME_FROM} exit-regime change — so this table is")
        lines.append("   single-regime. That is a consequence of the cut, not a filter.)")
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
            # The RSI column above changed DEFINITION at v1.50.0 (2026-09-08
            # 23:28): before that, live.py used HA-close RSI; after, real-close.
            # HA smoothing suppresses extremes, so the older rows understate how
            # often RSI_OVERSOLD/RSI_OVERBOUGHT is touched. Once the real-close
            # side exceeds ~1000 obs the two periods are comparable; below that
            # the split is underpowered and invites a false read.
            _RSI_SPLIT_TS = "2026-09-08T23:28"
            _RSI_SPLIT_MIN = 1000
            _post = [o for o in flat if o[0] >= _RSI_SPLIT_TS]
            _pre  = [o for o in flat if o[0] <  _RSI_SPLIT_TS]
            if _post and _pre:
                if len(_post) >= _RSI_SPLIT_MIN:
                    lines.append(
                        f"  ⚠️  RSI DEFINITION CHANGED mid-window (v1.50.0, "
                        f"2026-09-08 23:28): {len(_pre)} rows HA-close RSI, "
                        f"{len(_post)} rows real-close RSI (18.8%). "
                        f"HA smoothing suppresses extremes, so HA rows "
                        f"UNDERSTATE the RSI-qualified counts above.")
                    lines.append(f"  RSI DEFINITION SPLIT (n≥{_RSI_SPLIT_MIN} "
                                 f"threshold met — comparing admitted rates):")
                    for period_label, period_obs in (
                        ("HA-close  (pre-v1.50.0) ", _pre),
                        ("real-close (post-v1.50.0)", _post),
                    ):
                        for direction, extreme_filter in (
                            (f"long  (RSI<={_s2c.RSI_OVERSOLD})",
                             lambda o: o[2] <= _s2c.RSI_OVERSOLD),
                            (f"short (RSI>={_s2c.RSI_OVERBOUGHT})",
                             lambda o: o[2] >= _s2c.RSI_OVERBOUGHT),
                        ):
                            cand = [o for o in period_obs if extreme_filter(o)]
                            ok   = sum(1 for o in cand if o[3] < _s2c.MAX_ADX)
                            if cand:
                                lines.append(
                                    f"    {period_label} {direction}: "
                                    f"{len(cand):4} RSI-qual of {len(period_obs):5} "
                                    f"({100*len(cand)/len(period_obs):.2f}%)  "
                                    f"{ok:3} ADX<{_s2c.MAX_ADX} "
                                    f"= {100*ok/len(cand):.1f}% admitted")
                            else:
                                lines.append(
                                    f"    {period_label} {direction}: "
                                    f"no RSI-qualified obs")
                else:
                    lines.append(
                        f"  ⚠️  RSI DEFINITION CHANGED mid-window (v1.50.0, "
                        f"2026-09-08 23:28): {len(_pre)} rows are "
                        f"HA-close RSI, {len(_post)} are real-close RSI "
                        f"({100 * len(_post) / len(flat):.1f}%). HA smoothing "
                        f"suppresses extremes, so the older rows UNDERSTATE the "
                        f"RSI-qualified counts above. Do not read the split until "
                        f"the real-close side is large enough to stand alone.")
            lines.append("  NOTE: RSI extremes are CAUSED by strong directional")
            lines.append("  moves, which is exactly what raises ADX. The oversold")
            lines.append("  and ranging conditions are anti-correlated by")
            lines.append("  construction -- this is the binding constraint on")
            lines.append("  trade frequency, not a tuning detail.")

            # ── Capacity ───────────────────────────────────────────────────
            # Placed here because it answers the question the census raises
            # next: of the setups that DID clear the gate, how many arrived at
            # a moment the book had no room? Every other constraint in this
            # system is visible in the log; this one was not (see _capacity).
            try:
                hours_c, blk, blk_q, rch, rch_q, runs = _capacity(
                    per, state, _s2c.MAX_TRADES, _s2c.RSI_OVERSOLD,
                    _s2c.RSI_OVERBOUGHT, _s2c.MAX_ADX)
            except Exception as cap_err:      # never let this kill the report
                hours_c, runs = {}, []
                lines.append(f"\n── CAPACITY ── unavailable: {cap_err}")
            if hours_c:
                tot_h = len(hours_c)
                lines.append(f"\n── CAPACITY (MAX_TRADES={_s2c.MAX_TRADES}) ──")
                for lvl in sorted(set(hours_c.values())):
                    n = sum(1 for v in hours_c.values() if v == lvl)
                    tag = "  <-- BLOCKED" if lvl >= _s2c.MAX_TRADES else ""
                    lines.append(f"  {lvl} position(s) open: {n:>4} scan-hours "
                                 f"({n/tot_h*100:>4.1f}%){tag}")
                b_rate = blk_q / blk * 100 if blk else 0.0
                r_rate = rch_q / rch * 100 if rch else 0.0
                lines.append(f"  gate-qualifying obs while BLOCKED : {blk_q:>3} of "
                             f"{blk:>6}  ({b_rate:.2f}%)")
                lines.append(f"  gate-qualifying obs while OPEN    : {rch_q:>3} of "
                             f"{rch:>6}  ({r_rate:.2f}%)")
                if r_rate > 0:
                    lines.append(f"  qualifying rate inside a full book is "
                                 f"{b_rate/r_rate:.1f}x baseline — the book fills "
                                 f"BECAUSE setups cluster, so this is a statement")
                    lines.append(f"  about CORRELATION, not a case for raising the cap: "
                                 f"the trades it would buy are the correlated ones")
                # Deflate before quoting a trade count. RSI+ADX over-counts the
                # live gate (no stretch term, one setup per candle, coin
                # dedup); the observed ratio of trades actually opened to
                # qualifying observations in REACHABLE hours is the honest
                # conversion, and it is measured, not assumed.
                lo_ts = min(o[0] for o in flat)
                opened_in_window = sum(
                    1 for t in list(state.get("closed_trades", [])) +
                    list(state.get("tracked", {}).values())
                    if (_naive_utc(t.get("opened_at")) or datetime.min)
                    >= (_naive_utc(lo_ts.replace(" ", "T")) or datetime.min))
                if rch_q and opened_in_window:
                    defl = opened_in_window / rch_q
                    mean_r = (sum(r for _, r in r_vals) / len(r_vals)) if r_vals else 0.0
                    lines.append(f"  deflator: {opened_in_window} trades opened per "
                                 f"{rch_q} reachable qualifying obs = {defl:.2f} "
                                 f"trades/obs")
                    lines.append(f"  => MAX_TRADES cost ~{blk_q*defl:.1f} trades over "
                                 f"{tot_h} scan-hours. At meanR {mean_r:+.3f} that is "
                                 f"~{blk_q*defl*mean_r:+.2f}R.")
                    lines.append(f"  VERDICT: the cap is NOT the frequency bottleneck. "
                                 f"The gate is — see the admitted rates above.")
                if runs:
                    lines.append(f"  full-book stretches (longest first):")
                    for a, b, who in sorted(runs, key=lambda x: -( (x[1]-x[0]).total_seconds() ))[:5]:
                        span = int((b - a).total_seconds() // 3600) + 1
                        lines.append(f"    {a:%m-%d %H:%M} → {b:%m-%d %H:%M}  "
                                     f"{span:>2}h  holders: {', '.join(sorted(who))}")
                lines.append("  NOTE: RSI+ADX is an UPPER bound on qualifying (no")
                lines.append("  stretch term in the scan line). Same bound both sides,")
                lines.append("  so the ratio holds; the absolute counts do not.")

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
            # Classify on the INVARIANT, not on the bookkeeping. Under S2 the
            # only path that opens a position is strategy2.signal returning a
            # setup, so the existence of a closed trade IS the evidence that a
            # signal fired -- the journal row is a record of that fact, not the
            # fact itself. The previous test (`sig and sig.get("fired")`)
            # scored the six trades opened before journal["signals"] began on
            # 2026-08-10 as non-qualifying, so the sample component read 14/20
            # instead of 20/20 and understated Trust Score by 7.5 points on
            # Kamran's dashboard. Same root cause as the ENTRY CONDITION BANDS
            # drop; this is the second reader to mistake a schema date for a
            # market fact. (This function was already fixed once, on 08-11, for
            # the same class of error via a different route: it filtered on
            # score>=7, an S1 concept, and pinned the score at 0 forever.)
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
        bad_lines = sum(1 for l in recent if "Traceback" in l)
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
