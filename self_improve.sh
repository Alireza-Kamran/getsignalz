#!/bin/bash
# GetSignalz AI — nightly self-improvement session
# Runs at 2:00 AM UTC daily via cron.

# Cron uses a minimal default PATH that doesn't reliably include the claude
# CLI's install location — pin the same PATH the other root cron jobs use.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG="/root/trade/selflearn.log"
echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "SELF-LEARN: $(date -u '+%Y-%m-%d %H:%M UTC')" >> "$LOG"
echo "========================================" >> "$LOG"

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

PROMPT='You are the brain of GetSignalz AI — a self-improving crypto trading bot.

Tonight is your nightly improvement session. You think like a professional trader who has been trading for 10 years. You study your own performance ruthlessly, identify weaknesses, and fix them. You do not wait for instructions. You evolve.

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
- What ADX range did winning trades happen in? Consider raising MIN_ADX if low-ADX trades lose.
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
"$CLAUDE_BIN" -p "$PROMPT" > "$OUT_TMP" 2>&1
RC=$?
cat "$OUT_TMP" >> "$LOG"
echo "Session ended: $(date -u '+%H:%M UTC') (exit $RC)" >> "$LOG"

if [ $RC -ne 0 ] || grep -qiE "command not found|oauth session expired|session limit|failed to authenticate" "$OUT_TMP"; then
    SNIPPET="$(tail -c 500 "$OUT_TMP")"
    python3 - "$SNIPPET" <<'PYEOF' >> "$LOG" 2>&1
import sys
sys.path.insert(0, "/root/trade")
import tg
tg.dm_owner("⚠️ Nightly self-learn (02:00 UTC) likely failed:\n\n" + sys.argv[1])
PYEOF
fi
rm -f "$OUT_TMP"
