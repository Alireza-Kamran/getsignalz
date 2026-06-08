"""
Nightly AI brain — uses Claude Code CLI (claude -p) to analyse the full project
and generate real, validated improvements to any file.

Claude reads everything: all strategy logic, indicator math, execution code,
signal scoring, trade history, logs. It proposes concrete, syntax-validated
code edits. Changes are applied automatically; bot restarts if safe to do so.
"""
import os
import json
import py_compile
import subprocess
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path("/root/trade")
MODEL       = "claude-sonnet-4-6"

from config import HYPERLIQUID_PRIVATE_KEY as _PK, HYPERLIQUID_ACCOUNT as _ACCT

EDITABLE_FILES = [
    "trader.py",
    "executor.py",
    "tracker.py",
    "live.py",
    "review.py",
    "tg.py",
    "journal.py",
    "analyze.py",
    "indicators.py",
    "backtest.py",
]

PROTECTED_STRINGS = [_PK, _ACCT]

MAX_CHANGES = 12


def _read(name: str) -> str:
    p = PROJECT_DIR / name
    return p.read_text() if p.exists() else f"[not found: {name}]"


def _recent_logs(n: int = 150) -> str:
    log = PROJECT_DIR / "bot.log"
    if not log.exists():
        return "[no log]"
    return "\n".join(log.read_text().splitlines()[-n:])


def _build_prompt() -> str:
    parts = []

    # ── Instructions ──────────────────────────────────────────────────────────
    parts.append("""
You are the nightly AI brain of a Hyperliquid perpetuals trading bot called GetSignalz.
You receive the complete project source code, trade history, and performance data.
Your job: improve the bot every night by proposing concrete, validated code changes.

SCOPE — you can improve ANYTHING:
- trader.py: signal scoring, SL/TP logic, session filters, confluence weights, new indicators
- indicators.py: better indicator math, new technical signals, Heikin Ashi improvements
- executor.py: order placement precision, slippage handling (but NOT the 25% SL safety cap)
- tracker.py: live message formatting, PnL display, trailing logic
- analyze.py / review.py: better self-learning rules, smarter parameter tuning
- journal.py: additional data fields to track for better future analysis
- Any bug you find anywhere

SAFETY RULES — never break these:
- Never change PRIVATE_KEY or ACCOUNT_ADDRESS values
- Never weaken the MAX_LEV_LOSS = 0.25 cap in executor.py
- Never break state.json structure (tracked / closed_trades / stats)
- No blocking calls or sleep() in the main trading loop
- Changes must be surgical — targeted replacements, not full rewrites

OUTPUT — respond with ONLY raw JSON (no markdown, no code fences):
{
  "analysis": "2-4 sentences: current state assessment and key improvement opportunity",
  "changes": [
    {
      "file": "trader.py",
      "old": "exact string currently in file (copy-paste precise)",
      "new": "replacement string (valid Python)",
      "reason": "specific reason this makes the bot better"
    }
  ],
  "summary": "one paragraph for the channel owner: what you changed and the expected impact"
}

Rules for "old" field:
- Must be an EXACT copy of text currently in the file — character-for-character
- Include enough context (2-3 lines) to be unique within the file
- If no worthwhile improvement exists, return "changes": []
""")

    # ── Performance data ──────────────────────────────────────────────────────
    parts.append("\n" + "=" * 70)
    parts.append("PERFORMANCE DATA")
    parts.append("=" * 70)

    try:
        state   = json.loads(_read("state.json"))
        journal = json.loads(_read("journal.json"))
        config  = json.loads(_read("strategy_config.json"))
        stats   = state.get("stats", {})
        trades  = [t for t in journal.get("trades", []) if t.get("result")]
        signals = journal.get("signals", [])
    except Exception as e:
        state, journal, config, stats, trades, signals = {}, {}, {}, {}, [], []
        parts.append(f"[data load error: {e}]")

    parts.append(f"Total closed trades : {len(trades)}")
    parts.append(f"Win rate            : {stats.get('win_rate', 0):.1f}%")
    parts.append(f"Total P&L (levered) : {stats.get('total_pct', 0):+.1f}%")
    parts.append(f"Max drawdown        : -{stats.get('max_drawdown_pct', 0):.1f}%")
    parts.append(f"Current drawdown    : -{stats.get('current_drawdown_pct', 0):.1f}%")
    parts.append(f"Open positions      : {list(state.get('tracked', {}).keys())}")

    parts.append("\nLAST 25 TRADES (newest first):")
    for t in reversed(trades[-25:]):
        pct = t.get("lev_pct", 0) or 0
        parts.append(
            f"  {t.get('coin','?'):<8}  {t.get('result','?').upper():<3}  "
            f"{'+' if pct>=0 else ''}{pct:.1f}%  "
            f"{t.get('duration_h') or 0:.1f}h  "
            f"lev={t.get('leverage',1)}x  "
            f"sl={t.get('sl',0):.5g}  tp={t.get('tp',0):.5g}"
        )

    parts.append("\nLAST 20 SIGNALS (for confluence quality analysis):")
    for s in signals[-20:]:
        parts.append(
            f"  {s.get('coin','?'):<8}  score={s.get('score',0)}/14  "
            f"adx={s.get('adx',0):.0f}  rsi={s.get('rsi',0):.0f}  "
            f"reasons={s.get('reasons',[])}"
        )

    parts.append("\nSTRATEGY CONFIG:")
    parts.append(json.dumps(config, indent=2))

    # ── Bot logs ──────────────────────────────────────────────────────────────
    parts.append("\n" + "=" * 70)
    parts.append("RECENT BOT LOGS (last 150 lines):")
    parts.append(_recent_logs(150))

    # ── All project source code ───────────────────────────────────────────────
    parts.append("\n" + "=" * 70)
    parts.append("COMPLETE PROJECT SOURCE CODE:")
    for fname in EDITABLE_FILES:
        parts.append(f"\n{'─'*60}")
        parts.append(f"FILE: {fname}")
        parts.append("─" * 60)
        parts.append(_read(fname))

    parts.append("\n" + "=" * 70)
    parts.append(f"Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    parts.append("Analyse everything above and return your improvement JSON now.")

    return "\n".join(parts)


def _validate(change: dict, code: str) -> str | None:
    """Return None if valid, or an error string."""
    old = change.get("old", "")
    new = change.get("new", "")

    if not old:
        return "empty old string"
    if old not in code:
        return f"old string not found in file"
    for p in PROTECTED_STRINGS:
        if p in old or p in new:
            return "touches protected string"

    # Syntax check the result
    candidate = code.replace(old, new, 1)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
        tf.write(candidate)
        tf_name = tf.name
    try:
        py_compile.compile(tf_name, doraise=True)
    except py_compile.PyCompileError as e:
        return f"syntax error: {e}"
    finally:
        os.unlink(tf_name)
    return None


def run_ai_brain() -> dict:
    result = {
        "changes_applied":  [],
        "changes_rejected": [],
        "summary":          "",
        "analysis":         "",
        "error":            None,
    }

    try:
        prompt = _build_prompt()

        proc = subprocess.run(
            ["claude", "--print", "--model", MODEL],
            input=prompt, capture_output=True, text=True, timeout=300,
            env={**os.environ}
        )

        if proc.returncode != 0:
            result["error"] = f"claude CLI error (rc={proc.returncode}): {proc.stderr[:300]}"
            return result

        raw = proc.stdout.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)

    except json.JSONDecodeError as e:
        result["error"] = f"Claude returned invalid JSON: {e} — raw: {proc.stdout[:300]}"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "Claude brain timed out (>5 min)"
        return result
    except Exception as e:
        result["error"] = f"Brain error: {e}\n{traceback.format_exc()[:400]}"
        return result

    result["analysis"] = parsed.get("analysis", "")
    result["summary"]  = parsed.get("summary", "")
    changes = parsed.get("changes", [])[:MAX_CHANGES]

    # Load file contents (changes in same file stack correctly)
    file_contents = {f: _read(f) for f in EDITABLE_FILES}

    for change in changes:
        fname = change.get("file", "")
        if fname not in EDITABLE_FILES:
            result["changes_rejected"].append({**change, "reject_reason": "file not editable"})
            continue

        current = file_contents.get(fname, "")
        err = _validate(change, current)
        if err:
            result["changes_rejected"].append({**change, "reject_reason": err})
            continue

        file_contents[fname] = current.replace(change["old"], change["new"], 1)
        result["changes_applied"].append(change)

    # Write to disk
    written = set()
    for change in result["changes_applied"]:
        fname = change["file"]
        if fname not in written:
            (PROJECT_DIR / fname).write_text(file_contents[fname])
            written.add(fname)

    return result


def format_dm(result: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b %Y")
    lines = [
        f"🤖 <b>Claude Brain — {now}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if result.get("error"):
        lines.append(f"⚠️ Error: <code>{result['error'][:400]}</code>")
        return "\n".join(lines)

    if result.get("analysis"):
        lines.append(f"\n🔍 {result['analysis']}")

    applied  = result["changes_applied"]
    rejected = result["changes_rejected"]

    if applied:
        lines.append(f"\n✏️ <b>Code improvements ({len(applied)}):</b>")
        for c in applied:
            lines.append(f"  ✅ <b>{c['file']}</b>: {c['reason']}")
    else:
        lines.append(f"\n✅ No code changes — bot is well optimised for current data.")

    if rejected:
        lines.append(f"\n❌ <b>Rejected ({len(rejected)}):</b>")
        for c in rejected[:5]:
            lines.append(f"  · {c.get('file','?')}: {c.get('reject_reason','?')}")

    if result.get("summary"):
        lines.append(f"\n📋 {result['summary']}")

    return "\n".join(lines)
