"""
House style for everything posted to the public channel.

The channel IS the product, and until now every message site invented its own
header and its own divider run -- three different rules (━, ─, ═) across the
dashboard, the reviews and the signals, all English-only.

Two rules shape everything here:

1. PERSIAN AND ENGLISH NEVER SHARE A LINE. Telegram runs the Unicode bidi
   algorithm per line, so "سود: +20.32 دلار" is reordered at render time and the
   sign can end up at the wrong end of the number. Separate blocks render
   correctly on every client. No ZWNJ either: it is invisible, it breaks
   copy-paste, and half the fonts on Android render it as a gap.

2. FIGURES GO IN A <pre> BLOCK. A monospace run is unambiguously left-to-right,
   so one numeric table serves both languages instead of being duplicated -- and
   the columns actually line up, which they never do in a proportional font.

Pure string building: no I/O, no bot imports, so it is trivially testable.
"""

MARK = "⬡"
RULE = "─" * 26
BULLET = "•"

# Zero-width non-joiner. Must never appear in output; the test asserts this.
ZWNJ = "‌"


def rule() -> str:
    return RULE


def mark_line(right: str = "") -> str:
    """The brand mark alone, optionally with something right-hand side (a signal
    number). Telegram has no right-alignment, so the pieces are simply spaced."""
    left = f"{MARK} <b>GetSignal AI</b>"
    return f"{left}        <code>{right}</code>" if right else left


def header(emoji: str, fa: str, en: str) -> str:
    """Brand mark, then the Persian title, then the English one.

    The mark matches the hexagon on the result cards so the channel and the
    images read as one product rather than two tools.
    """
    return f"{MARK} <b>GetSignal AI</b>\n\n{emoji} <b>{fa}</b>\n{emoji} <b>{en}</b>"


def numeric_block(rows) -> str:
    """A <pre> table from (label, value) pairs, labels padded to align."""
    rows = [(str(k), str(v)) for k, v in rows if v is not None]
    if not rows:
        return ""
    w = max(len(k) for k, _ in rows)
    body = "\n".join(f"{k.ljust(w)}   {v}" for k, v in rows)
    return f"<pre>{body}</pre>"


def bilingual(emoji: str, fa_title: str, en_title: str,
              fa_lines=(), en_lines=()) -> str:
    """Full Persian block, divider, full English block.

    Used for prose messages such as the version update. Messages that are mostly
    figures should instead use header() + numeric_block(), which prints both
    titles once and shares a single table.
    """
    fa = "\n".join(f"{BULLET} {l}" for l in fa_lines)
    en = "\n".join(f"{BULLET} {l}" for l in en_lines)
    parts = [f"{emoji} <b>{fa_title}</b>"]
    if fa:
        parts.append("\n" + fa)
    parts.append(f"\n{RULE}\n")
    parts.append(f"{emoji} <b>{en_title}</b>")
    if en:
        parts.append("\n" + en)
    return "\n".join(parts)


def footer() -> str:
    return f"<i>@GetSignalAI</i>"


# ── Version-update highlights ────────────────────────────────────────────────
# Derived from WHICH FILES changed, never from the nightly model's own words.
# ai_brain returns free-text English reasons; piping those into a public channel
# would publish whatever the model happened to write that night, untranslated
# and unreviewed. A fixed table is boring, bilingual and always safe.
_CATEGORIES = [
    (("strategy2.py", "trader.py", "indicators.py"),
     "بهبود استراتژی ورود و خروج", "Entry and exit strategy improved"),
    (("executor.py",),
     "محافظت از پوزیشن قوی تر شد", "Stronger position protection"),
    (("result_card.py", "tracker.py", "tg.py", "brand.py"),
     "نمایش پیام ها و کارت ها بهتر شد", "Messages and cards improved"),
    (("io_safe.py", "live.py", "review.py", "analyze.py"),
     "پایداری و ایمنی داده بهتر شد", "Reliability and data safety improved"),
]
_FALLBACK = ("بهبود پایداری و دقت", "Stability and accuracy improvements")
MAX_HIGHLIGHTS = 4


def highlights_for(changed_files) -> list:
    """Map changed filenames onto bilingual (fa, en) one-liners.

    Deterministic, deduplicated, capped, and never empty -- an update post with
    no bullets would read as a broken message.
    """
    names = {str(f).strip().split("/")[-1] for f in (changed_files or [])}
    # Tests are an implementation detail; they should not produce their own
    # bullet, but a test-only change still counts as a reliability change.
    if any(n.startswith("test_") for n in names):
        names.add("io_safe.py")

    out = []
    for files, fa, en in _CATEGORIES:
        if names & set(files) and (fa, en) not in out:
            out.append((fa, en))
    return (out or [_FALLBACK])[:MAX_HIGHLIGHTS]


def version_update(version: str, changed_files=None, highlights=None,
                   date: str = None) -> str:
    """The channel's version-update post.

    `date` back-dates a post. Used to rebuild the update history: the channel
    had several one-off announcements scattered through it, so they were
    rewritten as the version posts they should always have been, each carrying
    the date it actually shipped.

    Pass it NUMERICALLY (2026-08-23, not "23 Aug 2026"). The title line is
    Persian, and a month abbreviation puts Latin letters inside an RTL run --
    the exact bidi hazard the house style exists to prevent. Digits, dots and
    dashes are script-neutral and safe there.
    """
    hl = highlights or highlights_for(changed_files)
    suffix = f"  ·  {date}" if date else ""
    return bilingual(
        "🚀",
        f"آپدیت نسخه {version}{suffix}", f"Version {version} Update{suffix}",
        [fa for fa, _ in hl], [en for _, en in hl],
    )


def fmt_px(v: float) -> str:
    """Price decimals by magnitude, with thousands separators.

    Lived in result_card.py; moved here because the channel text needs it too
    and importing result_card into tracker would drag PIL and numpy into every
    60-second message refresh. The old text path used "%.5g", which printed ETH
    as "$2444" (no decimals, no separator) and FIL as "$0.66933" -- a different
    shape per coin, and none of them aligned in a column.
    """
    a = abs(v)
    if a >= 10000: return f"{v:,.1f}"
    if a >= 1000:  return f"{v:,.2f}"
    if a >= 10:    return f"{v:,.3f}"
    if a >= 1:     return f"{v:,.4f}"
    return f"{v:.5f}"


def dur(seconds) -> str:
    """Human duration. Past a day, say so -- "24h 14m" made a day-old trade look
    like an intraday one."""
    seconds = max(int(seconds or 0), 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


HERO_W = 30


def hero(*lines) -> str:
    """The headline figure, centred inside a <pre> run.

    Telegram renders message text in a proportional font, where centring by
    padding does not work -- the same number of spaces is a different width on
    every client. Inside <pre> every glyph is one cell, so it does.
    """
    body = "\n".join(str(l).center(HERO_W).rstrip() for l in lines if l is not None)
    return f"<pre>{body}</pre>"


# ── Text-mode instrumentation ────────────────────────────────────────────────
# Telegram gives exactly one useful affordance: <pre>, which is monospace,
# left-to-right and space-preserving. Everything below is built on that, to get
# a text message as close to what a web or app UI would show as the medium
# allows.
#
# Braille (U+2800..28FF) would give 2x4 sub-cell resolution -- four times what
# the block characters manage, and it is what terminal plotting libraries use.
# It is deliberately NOT used: it renders as tofu or with broken metrics on
# several Android font stacks, and this goes to a public channel where a
# fraction of readers seeing garbage is worse than everyone seeing something
# slightly coarser.
EIGHTHS = "▏▎▍▌▋▊▉█"     # 1/8 .. 8/8 of a cell, for smooth horizontal fill
SPARK   = "▁▂▃▄▅▆▇█"     # 1/8 .. 8/8 height, for the price path
EMPTY   = "░"


def bar(frac, width: int = 20) -> str:
    """A progress bar with 8 sub-steps per cell -- 160 states across 20 cells,
    where the old implementation had 10.

    Always exactly `width` characters: at a whole-cell boundary the partial cell
    must be omitted entirely, not rendered as a space, or the bar comes out one
    column short and the right edge of every message wobbles.
    """
    width = max(1, int(width))
    frac  = max(0.0, min(1.0, float(frac or 0.0)))
    total = frac * width
    full  = int(total)
    eighth = int(round((total - full) * 8))
    if eighth == 8:          # rounded up to a whole cell
        full, eighth = full + 1, 0
    full = min(full, width)
    cell = EIGHTHS[eighth - 1] if eighth and full < width else ""
    return (("█" * full) + cell).ljust(width, EMPTY)[:width]


def spark(series, width: int = 24) -> str:
    """Sparkline of a numeric series, normalised to its own min/max.

    Returns "" for anything too short to be a line -- an empty string is a
    section the caller can drop, whereas a one-character "chart" is a bug that
    looks like a feature.
    """
    pts = [float(v) for v in (series or []) if v is not None]
    if len(pts) < 2:
        return ""
    if len(pts) > width:
        step = len(pts) / width
        pts = [pts[min(len(pts) - 1, int(i * step))] for i in range(width)]
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    return "".join(SPARK[min(7, max(0, int((v - lo) / rng * 7.999)))] for v in pts)


def track(lo, hi, marks, width: int = 22) -> str:
    """A proportional rule with markers: {value: char}.

    Guards lo == hi, which happens on a flat or malformed record and would
    otherwise divide by zero or index off the end.
    """
    width = max(3, int(width))
    cells = ["─"] * width
    span = (hi - lo) or 1.0
    for value, ch in marks.items():
        i = int((float(value) - lo) / span * (width - 1))
        cells[max(0, min(width - 1, i))] = ch
    return "".join(cells)


# ── Public strategy names ────────────────────────────────────────────────────
# "Mean reversion" is a textbook term: printing it on every signal tells any
# reader exactly what the entry is looking for, which is the one part of this
# system worth not advertising. The exit is already public -- the whole pitch is
# the risk-free ratchet -- but the entry condition is not.
#
# These are product names only. Docstrings, logs and the journal keep the real
# description, because whoever maintains the code needs to know what it does.
STRATEGY_NAMES = {
    "S1": "Bedrock",      # retired structural engine
    "S2": "Keystone",     # live. A keystone is the stone that locks an arch --
                          # which is what this strategy's exit does to a profit.
}


def strategy_name(code) -> str:
    return STRATEGY_NAMES.get(code, str(code))
