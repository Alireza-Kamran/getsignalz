"""
Generate a trade result card styled after Hyperliquid's PnL share card.
Returns a BytesIO PNG ready to send to Telegram.
"""
import io
import math
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import matplotlib.patheffects as pe

# ── HL-style colour palette ────────────────────────────────────────────────────
BG      = "#0d1117"      # HL dark background
CARD    = "#161b22"      # card background
BORDER  = "#30363d"      # border
GREEN   = "#3fb950"      # profit green (HL exact)
RED     = "#f85149"      # loss red (HL exact)
YELLOW  = "#e3b341"      # warning
WHITE   = "#e6edf3"      # primary text
MUTED   = "#8b949e"      # secondary text
BLUE    = "#58a6ff"      # accent
DARKGN  = "#0d4429"      # profit bg tint
DARKRD  = "#3d1a18"      # loss bg tint


def _fmt(val: float, decimals: int = 4) -> str:
    return f"{val:,.{decimals}f}"


def generate(coin: str, direction: int, entry: float, exit_px: float,
             sl: float, tp: float, lev_pct: float, result: str,
             sig_num: int, opened_at: datetime, closed_at: datetime,
             duration_h: float, max_adverse: float = 0.0,
             leverage: int = 10, size: float = 0) -> io.BytesIO:

    won   = result == "tp" and lev_pct > 0
    side  = "SHORT" if direction == -1 else "LONG"
    color = GREEN if won else RED
    bg_tint = DARKGN if won else DARKRD

    pct_s = f"+{lev_pct:.1f}%" if lev_pct >= 0 else f"{lev_pct:.1f}%"
    usd_val = size * abs(exit_px - entry) * direction
    usd_s   = f"{'+' if usd_val >= 0 else '-'}${abs(usd_val):,.2f}"

    dur_h_int = int(duration_h)
    dur_m_int = int((duration_h - dur_h_int) * 60)
    dur_str   = f"{dur_h_int}h {dur_m_int}m" if dur_h_int else f"{dur_m_int}m"

    close_dt  = closed_at.astimezone(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    status    = "TP HIT" if won else "SL HIT"
    status_icon = "✓" if won else "✗"

    # ── Figure ─────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(9, 5.4), facecolor=BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 9)
    ax.set_ylim(0, 5.4)
    ax.set_facecolor(BG)
    ax.axis("off")

    # ── Card background ─────────────────────────────────────────────────────────
    card = FancyBboxPatch((0.25, 0.25), 8.5, 4.9,
                          boxstyle="round,pad=0.05",
                          facecolor=CARD, edgecolor=BORDER, linewidth=1.5)
    ax.add_patch(card)

    # Subtle profit/loss tint strip at top of card
    tint = FancyBboxPatch((0.25, 4.65), 8.5, 0.5,
                          boxstyle="square,pad=0",
                          facecolor=bg_tint, edgecolor="none", alpha=0.6)
    ax.add_patch(tint)

    # ── Top bar: HL branding + signal number ───────────────────────────────────
    ax.text(0.55, 5.05, "⬡ Hyperliquid", fontsize=10, color=BLUE,
            va="center", ha="left", fontfamily="DejaVu Sans", fontweight="bold")
    ax.text(8.75, 5.05, f"#Signal{sig_num}", fontsize=9, color=MUTED,
            va="center", ha="right", fontfamily="monospace")

    # Divider under HL bar
    ax.axhline(y=4.65, xmin=0.028, xmax=0.972, color=BORDER, linewidth=0.8)

    # ── Coin + direction header ─────────────────────────────────────────────────
    side_color = GREEN if side == "LONG" else RED
    ax.text(0.55, 4.3, coin, fontsize=20, color=WHITE, va="center", ha="left",
            fontweight="bold")
    ax.text(0.55 + len(coin) * 0.235 + 0.1, 4.3, side, fontsize=11, color=side_color,
            va="center", ha="left", fontweight="bold")

    # Status badge
    badge_x, badge_w = 7.4, 1.3
    badge = FancyBboxPatch((badge_x, 4.05), badge_w, 0.5,
                           boxstyle="round,pad=0.05",
                           facecolor=bg_tint, edgecolor=color, linewidth=1.5)
    ax.add_patch(badge)
    ax.text(badge_x + badge_w / 2, 4.3, f"{status_icon} {status}",
            fontsize=10, color=color, va="center", ha="center", fontweight="bold")

    # ── Main P&L ───────────────────────────────────────────────────────────────
    ax.text(0.55, 3.45, pct_s, fontsize=40, color=color, va="center", ha="left",
            fontweight="bold",
            path_effects=[pe.withStroke(linewidth=2, foreground=BG)])
    ax.text(0.55, 2.85, usd_s, fontsize=15, color=color, va="center", ha="left",
            alpha=0.85)

    # ── Entry / Exit row ───────────────────────────────────────────────────────
    ax.axhline(y=2.55, xmin=0.028, xmax=0.972, color=BORDER, linewidth=0.6, alpha=0.6)
    for label, val, xpos in [
        ("Entry", f"${_fmt(entry)}", 1.1),
        ("→",     "",               2.8),
        ("Exit",  f"${_fmt(exit_px)}", 3.4),
        ("Leverage", f"{leverage}×",  5.8),
        ("Duration", dur_str,          7.3),
    ]:
        if label == "→":
            ax.text(xpos, 2.2, "→", fontsize=14, color=MUTED, va="center", ha="center")
            continue
        ax.text(xpos, 2.45, label, fontsize=7.5, color=MUTED, va="center", ha="center")
        ax.text(xpos, 2.05, val,   fontsize=10,  color=WHITE, va="center", ha="center",
                fontfamily="monospace")

    # ── Bottom stats row ───────────────────────────────────────────────────────
    ax.axhline(y=1.75, xmin=0.028, xmax=0.972, color=BORDER, linewidth=0.6, alpha=0.6)
    stats = [
        ("SL",          f"${_fmt(sl)}",          RED),
        ("TP",          f"${_fmt(tp)}",           GREEN),
        ("Max adverse", f"{max_adverse:.1f}%",    YELLOW if max_adverse < -10 else MUTED),
        ("Closed",      close_dt,                 MUTED),
    ]
    for i, (label, val, col) in enumerate(stats):
        xpos = 1.0 + i * 2.0
        ax.text(xpos, 1.55, label, fontsize=7.5, color=MUTED, va="center", ha="center")
        ax.text(xpos, 1.18, val,   fontsize=8.5,  color=col,   va="center", ha="center",
                fontfamily="monospace")

    # ── Footer ─────────────────────────────────────────────────────────────────
    ax.axhline(y=0.75, xmin=0.028, xmax=0.972, color=BORDER, linewidth=0.6, alpha=0.4)
    ax.text(4.5, 0.5, "@GetSignalz  ·  Testnet",
            fontsize=8, color=MUTED, va="center", ha="center", alpha=0.7)

    # ── Render ─────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    buf.seek(0)
    plt.close("all")
    return buf
