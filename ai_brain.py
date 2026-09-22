"""
Nightly AI brain — uses Claude Code CLI (claude -p) to analyse the full project
and generate real, validated improvements to any file.

Claude reads everything: all strategy logic, indicator math, execution code,
signal scoring, trade history, logs. It proposes concrete, syntax-validated
code edits. Changes are applied automatically; bot restarts if safe to do so.
"""
import os
import html
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

# Structural max of score_setup() in trader.py (CM Sling Shot: cloud 2 + entry 2 +
# 4H 2 + ADX 1 + cloud-width 1). Not a tunable — only changes if scoring itself
# is restructured, unlike MIN_SCORE which strategy_config.json controls nightly.
MAX_SCORE = 8

# Full brain runs (~37K-token prompt + detailed JSON reply) legitimately take
# ~5 min; 300s and 900s both proved too tight under load (timed out
# 2026-07-26 with no partial output). 1 hour gives generous headroom.
BRAIN_TIMEOUT = 3600


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

    # Load config/state/journal FIRST so the instructions below can reference
    # live values instead of a frozen snapshot from whatever regime was current
    # when this prompt was last hand-edited.
    try:
        state   = json.loads(_read("state.json"))
        journal = json.loads(_read("journal.json"))
        config  = json.loads(_read("strategy_config.json"))
        stats   = state.get("stats", {})
        trades  = [t for t in journal.get("trades", []) if t.get("result")]
        signals = journal.get("signals", [])
    except Exception as e:
        state, journal, config, stats, trades, signals = {}, {}, {}, {}, [], []

    current_min_score = config.get("min_score", 7)

    # ── Instructions ──────────────────────────────────────────────────────────
    parts.append(f"""
You are the nightly AI brain of a Hyperliquid perpetuals trading bot called GetSignal AI.
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
- NEVER raise MIN_SCORE based on blended overall win rate — that number mixes every
  scoring regime this bot has ever run and is meaningless for this decision. Only act
  on a MIN_SCORE change if you compute per-score EV fresh from the LAST 25 TRADES /
  LAST 20 SIGNALS data below (or ask for more history) and the current regime's own
  bands separate cleanly, with n≥5 trades in the bands being compared.
- Current MIN_SCORE is {current_min_score} (read live from strategy_config.json,
  max achievable score is {MAX_SCORE}) — this is set by the nightly self-learn
  process, not by you unilaterally; only change it with the fresh evidence above.

OUTPUT — respond with ONLY raw JSON (no markdown, no code fences):
{{
  "analysis": "2-4 sentences: current state assessment and key improvement opportunity",
  "changes": [
    {{
      "file": "trader.py",
      "old": "exact string currently in file (copy-paste precise)",
      "new": "replacement string (valid Python)",
      "reason": "specific reason this makes the bot better"
    }}
  ],
  "summary": "one paragraph for the channel owner: what you changed and the expected impact"
}}

Rules for "old" field:
- Must be an EXACT copy of text currently in the file — character-for-character
- Include enough context (2-3 lines) to be unique within the file
- If no worthwhile improvement exists, return "changes": []
""")

    # ── Performance data ──────────────────────────────────────────────────────
    parts.append("\n" + "=" * 70)
    parts.append("PERFORMANCE DATA")
    parts.append("=" * 70)

    if not state and not journal:
        parts.append("[data load error — state.json/journal.json unavailable]")

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
            f"  {s.get('coin','?'):<8}  score={s.get('score',0)}/{MAX_SCORE}  "
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
    # The last line of a long prompt is the most attended position, and the
    # OUTPUT contract stated ~37K tokens earlier was ignored on 2026-09-21:
    # the reply was a prose paragraph and the whole review was discarded.
    # `_extract_json` now recovers most of that, but not replying in prose is
    # still cheaper than repairing it.
    parts.append("Analyse everything above and return your improvement JSON now.")
    parts.append(
        'Your ENTIRE reply must be one JSON object, starting with { and ending '
        'with }. No prose before or after it, no code fences, no commentary. '
        'If nothing is worth changing tonight, that is a valid and expected '
        'answer -- say it INSIDE the object as {"analysis": "...", "changes": '
        '[], "summary": "..."}, not as a sentence instead of it.')

    return "\n".join(parts)


class _NoJSON(Exception):
    """The brain produced output, but no JSON object could be found in it."""

    def __init__(self, text: str):
        super().__init__("no JSON object in reply")
        self.text = text


def _fenced_blocks(text: str):
    """Yield the contents of every ``` fence, language tag stripped."""
    for block in text.split("```")[1::2]:
        nl = block.find("\n")
        if nl != -1 and block[:nl].strip().lower() in ("", "json", "json5"):
            block = block[nl + 1:]
        yield block.strip()


def _brace_spans(text: str):
    """Yield every top-level {...} span, counting braces OUTSIDE string literals.

    The obvious one-liner -- `text[text.find("{"):text.rfind("}") + 1]` -- is
    wrong in both directions. It spans ACROSS two separate objects when the
    model emits an example followed by its real answer, and it miscounts on a
    brace inside a string value. That second case is not exotic here: this
    brain's own schema puts free prose in "reason" and "summary", and the
    prompt it reads is full of `{{` literals, so a brace inside a string is
    the expected shape of a correct reply.
    """
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start:i + 1]


def _extract_json(out: str) -> tuple[dict, str]:
    """Return (obj, how) for the first parseable JSON OBJECT in `out`.

    Until 2026-09-22 this was `json.loads(raw)` behind a fence-stripper that
    only fired when the reply STARTED with ```. Every other shape was fatal,
    and the whole review was discarded. On 2026-09-21 the brain ran for 772
    seconds, answered in prose ("The `_mfe_seen` helper and the excursion floor
    fix are already fully in place..."), and all of it was thrown away into a
    300-char error string -- in a file (`.night_report.json`) that
    `version_push` then deletes.

    Three layers, cheapest first, because the failure is free to guard against
    and expensive to hit:
      1. the whole reply (the contract, and what a compliant model sends)
      2. any fenced block, wherever it sits -- not just at offset 0
      3. any balanced {...} span, for a reply with a prose preamble

    Only a dict is accepted. `json.loads` returns an int for "5", None for
    "null" and a list for "[1,2]"; the old code passed all three straight into
    `parsed.get(...)`, where they raised AttributeError into the generic
    handler and were reported to the owner as `Brain error: 'int' object has
    no attribute 'get'`. That is a real error reported as the wrong error --
    a schema violation dressed up as an internal crash. Rejecting non-dicts
    here names the actual problem.
    """
    raw = (out or "").strip()
    if not raw:
        raise _NoJSON("")

    cands = [(raw, "whole reply")]
    cands += [(b, "fenced block") for b in _fenced_blocks(raw)]
    cands += [(s, "embedded object") for s in _brace_spans(raw)]

    for cand, how in cands:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj, how
    raise _NoJSON(raw)


# A repair pass carries only the model's own words plus the schema, so it is
# ~1% of the 37K-token main prompt and returns in seconds. Its timeout is
# short on purpose: it exists to rescue a review, never to extend one.
REPAIR_TIMEOUT = 300

_REPAIR_PROMPT = """A code-review assistant was asked to reply with ONLY a JSON
object and instead replied in prose. Below is its reply verbatim. Re-express it
as the required JSON object and output NOTHING else -- no prose, no code fences.

Schema:
{{"analysis": "<its assessment, 2-4 sentences>", "changes": [], "summary": "<one paragraph>"}}

If the reply proposes concrete file edits, add one object per edit to "changes"
with keys "file", "old", "new", "reason", copying the old/new text EXACTLY as
the reply gives it. If it proposes no edit, leave "changes" as [].
Invent nothing that is not in the reply below.

REPLY TO CONVERT:
{text}
"""


def _repair_json(text: str, keepalive=None) -> tuple[dict, str]:
    """Ask the model to re-express its own prose reply as the schema.

    Cheaper and more honest than re-running the full review: the analysis has
    already been done and paid for, and a second full run would re-derive it
    from scratch with no guarantee of a different output format. This pass
    cannot invent a code change -- `_validate` still has to find `old` in the
    file verbatim -- so the worst case is an empty `changes` list, which is
    also the correct reading of a prose "nothing to change tonight".
    """
    rc, out, err, _passes = _run_claude(
        ["claude", "--print", "--model", MODEL],
        _REPAIR_PROMPT.format(text=text[:12000]),
        keepalive=keepalive, timeout=REPAIR_TIMEOUT)
    if rc != 0:
        raise _NoJSON(text)
    return _extract_json(out)


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


# How often the brain wait hands control back to `keepalive`. Matches live.POLL
# so a position gets exactly the management cadence it has the rest of the day.
KEEPALIVE_SLICE_S = 20


def _run_claude(cmd, prompt, keepalive=None, slice_s=KEEPALIVE_SLICE_S,
                timeout=None):
    """Run `cmd` with `prompt` on stdin; call `keepalive()` every `slice_s`
    seconds while it runs.

    subprocess.run() blocks the calling thread for the whole call. That thread
    is live.py's main loop, and for the 5-36 minutes a brain run takes nothing
    manages the book: on 2026-09-14 the review sat on TWO open shorts for 14
    minutes, and on 2026-09-09 it ran 36 minutes. The 2026-09-02 fix moved the
    review BELOW the ratchet in the loop, which guarantees the ratchet runs once
    before the review starts -- and then stands still for its whole duration.
    Ordering was never the problem; blocking was.

    Popen + communicate(timeout=) in slices is the single-threaded answer: the
    wait returns every `slice_s`, the caller manages the book, and the wait
    resumes. No thread, so no race on state.json (the lost-update race that
    erased OP on 2026-08-13 is exactly what a management thread would reopen).
    communicate() documents that retrying after TimeoutExpired loses no output;
    `input` may only be passed on the first call, hence `pending`.

    Returns (returncode, stdout, stderr, keepalive_passes). Raises
    subprocess.TimeoutExpired if `timeout` elapses; the child is killed first.
    """
    import time as _time
    if timeout is None:
        timeout = BRAIN_TIMEOUT
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ},
    )
    t0 = _time.monotonic()
    pending = prompt
    passes = 0
    while True:
        remaining = timeout - (_time.monotonic() - t0)
        if remaining <= 0:
            proc.kill()
            proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout)
        try:
            out, err = proc.communicate(input=pending,
                                        timeout=min(slice_s, remaining))
            return proc.returncode, out, err, passes
        except subprocess.TimeoutExpired:
            pending = None
            if keepalive is None:
                continue
            passes += 1
            try:
                keepalive()
            except Exception:
                # Management errors are logged by the callback's own owner
                # (live.py); the brain wait must never die because a poll
                # failed, or the review would be worse than the blocking it
                # replaces.
                pass


def run_ai_brain(keepalive=None) -> dict:
    result = {
        "changes_applied":  [],
        "changes_rejected": [],
        "summary":          "",
        "analysis":         "",
        "error":            None,
        "keepalive_passes": 0,
    }

    try:
        prompt = _build_prompt()

        out = ""
        rc, out, err, passes = _run_claude(
            ["claude", "--print", "--model", MODEL], prompt, keepalive=keepalive)
        result["keepalive_passes"] = passes

        if rc != 0:
            result["error"] = f"claude CLI error (rc={rc}): {err[:300]}"
            return result

        try:
            parsed, how = _extract_json(out)
            result["json_source"] = how
        except _NoJSON as no_json:
            # The reply is prose. PRESERVE IT. The analysis is the expensive
            # half of a brain run and it has already been paid for -- 772
            # seconds of it on 2026-09-21, thrown away because of its wrapper.
            result["analysis"] = no_json.text[:4000]
            if not no_json.text:
                result["error"] = ("Claude returned an EMPTY reply "
                                   f"(rc=0, stderr: {err[:200]})")
                return result
            try:
                parsed, how = _repair_json(no_json.text, keepalive=keepalive)
                result["json_source"] = f"repaired via {how}"
            except (_NoJSON, subprocess.TimeoutExpired) as rep_err:
                result["error"] = (
                    "Claude answered in prose, not JSON, and the repair pass "
                    f"could not convert it ({type(rep_err).__name__}). The "
                    "analysis it did produce is preserved and reported; no "
                    "change was applied.")
                return result

    except json.JSONDecodeError as e:
        result["error"] = f"Claude returned invalid JSON: {e} — raw: {out[:300]}"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = f"Claude brain timed out (>{BRAIN_TIMEOUT // 60} min)"
        return result
    except Exception as e:
        result["error"] = f"Brain error: {e}\n{traceback.format_exc()[:400]}"
        return result

    # `or` not `get(..., "")`: on the repair path result["analysis"] already
    # holds the model's own prose, and a repair that returns an empty analysis
    # field must not erase it. See [[reference_null_not_zero]] -- a key that is
    # present and empty is not the same as an absent one.
    result["analysis"] = parsed.get("analysis") or result["analysis"]
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
        # result["error"] can carry raw subprocess stderr/stdout or a full
        # traceback (see the four assignment sites in this file) -- almost
        # guaranteed to contain '<' (e.g. "in <module>"), which breaks
        # Telegram's HTML parser inside this <code> block if not escaped.
        lines.append(f"⚠️ Error: <code>{html.escape(str(result['error'])[:400])}</code>")
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
