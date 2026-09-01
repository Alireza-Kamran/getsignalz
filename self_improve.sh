#!/bin/bash
# GetSignal AI — nightly self-improvement session
# Runs at 2:00 AM UTC daily via cron.

# Cron uses a minimal default PATH that doesn't reliably include the claude
# CLI's install location — pin the same PATH the other root cron jobs use.
#
# SELF_IMPROVE_BIN_PREFIX is a test seam: it lets test_session_retry.py put a
# stub `claude` ahead of the real one. Without it this pin silently DISCARDS any
# PATH the caller exported, so a test that redirects PATH to a stub still
# launches a real nightly session — which is what happened while writing that
# suite, leaving an orphaned session running unsupervised with acceptEdits.
export PATH="${SELF_IMPROVE_BIN_PREFIX:+$SELF_IMPROVE_BIN_PREFIX:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Paths are env-overridable so test_session_retry.sh can exercise the guards
# against temp files. Redirect the RESOURCE, don't stub the CALLER -- stubbing
# call sites is how test_ratchet.py ended up writing into the live state.json
# on 2026-08-24 and switching the ratchet off for 24h.
# Bash reads a script incrementally by byte offset and seeks back after each
# command, so EDITING THIS FILE WHILE IT RUNS makes the live shell resume
# mid-token. The nightly session this script launches edits files in this repo
# — including this one — so always run from an immutable snapshot.
if [ -z "$SELF_IMPROVE_SNAPSHOT" ]; then
    SNAP="$(mktemp)"
    cat "$0" > "$SNAP"
    export SELF_IMPROVE_SNAPSHOT=1
    bash "$SNAP" "$@"
    RC=$?
    rm -f "$SNAP"
    exit $RC
fi

LOG="${SELF_IMPROVE_LOG:-/root/trade/selflearn.log}"
MARKER="${SELF_IMPROVE_MARKER:-/root/trade/.last_session}"
LOCK="${SELF_IMPROVE_LOCK:-/root/trade/.self_improve.lock}"

# scheduled | retry | retry-last  (see the cron block at the bottom of this file)
MODE="${1:-scheduled}"
TODAY="$(date -u +%F)"

# A retry is a NO-OP once today's session has succeeded. This single check is
# what makes the extra cron entries safe: on a normal night they read one file
# and exit without burning a session or writing a log line.
if [ "$MODE" != "scheduled" ]; then
    if [ -f "$MARKER" ] && [ "$(cut -d' ' -f1 "$MARKER" 2>/dev/null)" = "$TODAY" ]; then
        exit 0
    fi
fi

# Never allow two sessions at once (a retry racing a still-running 02:00 run, or
# a manual invocation). -n fails immediately rather than queueing behind it.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -u '+%Y-%m-%d %H:%M UTC') [$MODE] another self_improve run holds the lock — skipping" >> "$LOG"
    exit 0
fi

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
# Header format is parsed by the session-history analysis; keep it byte-stable
# and put the mode on its own line below.
echo "SELF-LEARN: $(date -u '+%Y-%m-%d %H:%M UTC')" >> "$LOG"
echo "========================================" >> "$LOG"
if [ "$MODE" != "scheduled" ]; then
    echo "MODE: $MODE (no successful session yet today)" >> "$LOG"
fi

cd /root/trade

CLAUDE_BIN="$(command -v claude)"
if [ -z "$CLAUDE_BIN" ]; then
    echo "FATAL: claude CLI not found on PATH ($PATH)" >> "$LOG"
    python3 -c "
import sys; sys.path.insert(0, '/root/trade')
import tg
tg.dm_owner('⚠️ Nightly self-learn (02:00 UTC) FAILED: claude CLI not found on PATH. Cron job needs attention.')
" >> "$LOG" 2>&1
    exit 1
fi

PROMPT='You are the brain of GetSignal AI — a self-improving crypto trading bot.

Tonight is your nightly improvement session. You think like a professional trader who has been trading for 10 years. You study your own performance ruthlessly, identify weaknesses, and fix them. You do not wait for instructions. You evolve.

CRITICAL — this is a non-interactive, one-shot session (claude -p). Nobody is
watching it and it cannot be resumed after your turn ends. NEVER run a bash
command in the background (no run_in_background, no `&`, no "I will resume
once this finishes"). If something like a backtest is slow, run it
synchronously and wait for it to finish before continuing — a command that
takes a few minutes is fine; a command you never wait for means the session
ends with nothing done. Always complete STEP 6 (the report) before your turn
ends.

## STEP 1 — Generate your performance report

Run this first:
```python
import sys; sys.path.insert(0, "/root/trade")
from analyze import full_report
print(full_report())
```

Also read the last 300 lines of /root/trade/bot.log for errors and patterns.
Also read /root/trade/strategy_config.json for current parameters.
Also read /root/trade/trader.py for current scoring logic.
Also read /root/.claude/projects/-root-trade/memory/project_trading_bot.md for past sessions.

## STEP 2 — Think like a professional trader

Based on the data, answer every relevant question from this list:

**Signal quality:**
- What is my win rate per score level? Is score 6 reliable or noise? Should MIN_SCORE go to 7?
- Which confluence factors have the highest win rate in my actual trades?
- Which have the lowest? Should I reduce their weight?
- Is any factor consistently present in losses but absent in wins? Consider removing it.
- What is my expected value (EV) = WR × avg_win - (1-WR) × avg_loss? Is it positive?

**Position management:**
- Did trailing TP (8% trail) serve me well? Did it catch moves I would have missed?
  Or did it give back too much? Should trail be tighter (5%) or wider (10%)?
- Did any TP orders fail to place? Any SL placement errors? Fix them.
- Were any positions stopped out by noise before the real move? SL too tight?
- Did any position run much further than my TP without me catching it?

**Market regime:**
- What ADX range did winning trades happen in? (Report only -- MAX_ADX is owner-locked.)
- What RSI range at entry? Are there optimal entry RSI bands per direction?
- What session hours produced the best results? Could I narrow the session window?
- Did 4H macro trend filter help? Did any winning trade have a neutral/opposing 4H trend?

**Coin selection:**
- Which coins in WATCHLIST produced positive EV? Which negative?
- Remove any coin with: <30% win rate AND negative total PnL over 5+ trades.
- Add any observation about coins that consistently score high and follow through.

**Risk management:**
- What is the current max drawdown? Is it acceptable?
- With current leverage, are the % swings appropriate for the account size?
- Should I vary leverage by ADX strength? (e.g., ADX>40 → higher leverage)

## OWNER-LOCKED CONSTANTS — do not modify these under any circumstances

These live in /root/trade/strategy2.py and define the deployed entry, exit
and risk behaviour:

    RSI_OVERSOLD, RSI_OVERBOUGHT, MAX_ADX, MIN_STRETCH_ATR, SL_ATR_MULT,
    TP_R, TRAIL_START_R, TRAIL_STEP_R, MAX_TRADES, S2_RISK_PCT, WATCHLIST

Every one of them was derived on Hyperliquid TESTNET candles, and on 2026-08-05
the feed was found to carry 18.5% frozen bars against 0.4% on mainnet -- so all
of their supporting figures are void and are being re-derived on real prices.
Tuning them against 5 live trades, on numbers already known to be wrong, cannot
produce a better value; it can only overwrite work in progress.

If your analysis suggests one of them should change, SAY SO in the report and
leave the code alone. Kamran decides these.

You may still freely: fix bugs anywhere, improve reporting and analysis, adjust
strategy_config.json (it belongs to the DISABLED S1 engine), and change any file
other than strategy2.py.

## STEP 3 — Make actual changes

Based on your analysis, update /root/trade/strategy_config.json with improved values.

Then apply them:
```python
import sys; sys.path.insert(0, "/root/trade")
from analyze import apply_config_to_trader, save_config, load_config
config = load_config()

# Make your changes to config dict here
# config["min_score"] = 7  # example
# config["trail_pct"] = 0.06  # example
# config["signal_weights"]["ut_bot"] = 3  # example
# config["notes"] = "Changed X because Y based on Z data"

save_config(config)
result = apply_config_to_trader()
print(result)
```

If signal weights changed in strategy_config.json, also update the actual scoring logic in /root/trade/trader.py to match.

Fix any bugs you found in bot.log. Be surgical — one bug, one fix.

## STEP 4 — Update memory

Update /root/.claude/projects/-root-trade/memory/project_trading_bot.md:
- Add todays date section
- What the data showed
- What you changed and the exact evidence (e.g. "FVG win rate 71% vs OB 54% → increased FVG weight from 2 to 3")
- What you decided NOT to change and why
- Your current hypothesis about what makes signals work
- What to observe tomorrow

## STEP 5 — Restart if any .py file changed

```bash
systemctl restart getsignalz
sleep 5
systemctl status getsignalz --no-pager | head -3
```

## STEP 6 — Report

```python
import sys; sys.path.insert(0, "/root/trade")
import tg, json

# Always DM the owner with the full analysis
report = """
[Write your complete analysis here — every finding, every decision, your reasoning.
This is your trading journal entry. Be specific with numbers.]
"""
tg.dm_owner(report)

# Post to channel ONLY if something meaningful happened:
# - Win rate insight that changed a parameter
# - A new feature added
# - A significant bug fixed
# Do NOT post if it was just observation. Keep the channel clean.
```

You are a professional trader who happened to also be a software engineer. You think in expected value, not just win rate. You look for edge, not perfection. You know that 55% win rate with 2:1 RR is excellent. You know that 70% win rate with 0.5:1 RR is a losing system. Make decisions accordingly.
'

OUT_TMP="$(mktemp)"
# Fingerprint the owner-locked file so an edit is detectable even if the session
# does not mention it. acceptEdits means the session can write strategy2.py; the
# prompt forbids it, this proves whether the prompt was honoured.
LOCKED_BEFORE="$(md5sum /root/trade/strategy2.py | cut -d" " -f1)"
timeout 3600 "$CLAUDE_BIN" -p --permission-mode acceptEdits "$PROMPT" > "$OUT_TMP" 2>&1
RC=$?
LOCKED_AFTER="$(md5sum /root/trade/strategy2.py | cut -d" " -f1)"
if [ "$LOCKED_BEFORE" != "$LOCKED_AFTER" ]; then
    echo "WARNING: strategy2.py was modified despite being owner-locked" >> "$LOG"
    git -C /root/trade diff --stat strategy2.py >> "$LOG" 2>&1
    python3 - <<'LOCKEOF' >> "$LOG" 2>&1
import sys, subprocess
sys.path.insert(0, "/root/trade")
import tg
d = subprocess.run(["git","-C","/root/trade","diff","strategy2.py"],
                   capture_output=True, text=True).stdout[:1200]
tg.dm_owner("\u26a0\ufe0f <b>Nightly session edited owner-locked strategy2.py</b>\n"
            "<pre>" + (d or "(no git diff — file may be untracked)") + "</pre>")
LOCKEOF
fi
cat "$OUT_TMP" >> "$LOG"
echo "Session ended: $(date -u '+%H:%M UTC') (exit $RC)" >> "$LOG"

# The exit code is the authority. The string check is a fallback for a CLI that
# refuses and still exits 0 -- and it reads only the FIRST 200 bytes, because a
# hard refusal is the entire output ("You've hit your session limit ...") and is
# printed before any transcript. Scanning the whole file would match a session
# that merely DISCUSSES these strings -- which is exactly what tonight's report
# about usage-limit failures does. An instrument must not match its own output.
FAILED=0
if [ $RC -ne 0 ] || head -c 200 "$OUT_TMP" | grep -qiE "command not found|oauth session expired|hit your (session|weekly|monthly)|failed to authenticate"; then
    FAILED=1
fi

if [ $FAILED -eq 0 ]; then
    # The success marker is what later retries read to decide they are a no-op.
    echo "$TODAY $(date -u +%H:%M) $MODE" > "$MARKER"
    if [ "$MODE" != "scheduled" ]; then
        python3 - "$MODE" <<'PYEOF' >> "$LOG" 2>&1
import sys
sys.path.insert(0, "/root/trade")
import tg
tg.dm_owner("✅ <b>Nightly session recovered on " + tg.esc(sys.argv[1]) +
            "</b>\nThe 02:00 UTC run failed; this attempt completed. "
            "No supervision was lost today.")
PYEOF
    fi
else
    if [ $RC -eq 124 ]; then
        SNIPPET="Session hit the 60-minute wall clock and was killed (exit 124)."
    else
        SNIPPET="$(tail -c 500 "$OUT_TMP")"
    fi
    # MODE decides whether this is worth a DM. A plain "retry" failure is a
    # second notice about a failure already reported at 02:00 -- logging it is
    # enough. Re-sending it is the alarm fatigue that made the real 08-27
    # outage read as noise.
    case "$MODE" in
      scheduled)
        HEAD="⚠️ <b>Nightly self-learn (02:00 UTC) likely failed</b>
Automatic retries are scheduled for 04:30 and 15:00 UTC." ;;
      --retry-last)
        HEAD="🚨 <b>No nightly session completed today</b>
All attempts failed (02:00, 04:30, 15:00 UTC). The bot is running unsupervised — no analysis, no commit, no report for $TODAY." ;;
      *)
        HEAD="" ;;
    esac
    if [ -n "$HEAD" ]; then
        python3 - "$HEAD" "$SNIPPET" <<'PYEOF' >> "$LOG" 2>&1
import sys
sys.path.insert(0, "/root/trade")
import tg
# esc() the raw session output: it is arbitrary text going into a parse_mode=HTML
# send, and a traceback containing "<urllib3.connection.HTTPSConnection object>"
# would be rejected as bad entities -- losing the very alert being sent.
tg.dm_owner(sys.argv[1] + "\n\n<pre>" + tg.esc(sys.argv[2]) + "</pre>")
PYEOF
    fi
fi
rm -f "$OUT_TMP"

# Cron (see `crontab -l`):
#   0  2 * * * /root/trade/self_improve.sh
#   30 4 * * * /root/trade/self_improve.sh --retry        # session limits reset 02:10-04:00
#   0 15 * * * /root/trade/self_improve.sh --retry-last   # weekly limits reset 14:00
# Both retries exit immediately unless today's run failed, so on a normal night
# they cost one stat() each. Measured against selflearn.log 07-24 -> 08-31:
# 7 of 14 lost nights would have been recovered (65% -> 82% session success).
