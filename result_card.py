"""
Trade result card, drawn in the visual language of an exchange PnL share card.
Returns a BytesIO PNG ready to send to Telegram.

Rendered with Pillow rather than matplotlib: this is a share card, not a chart,
and matplotlib gave no control over gradients, rounded pills or text metrics.
Everything is drawn at SCALE x and downsampled with LANCZOS -- Pillow has no
antialiased primitives, so supersampling is what keeps the corners clean.
"""
import io
import os
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Geometry ──────────────────────────────────────────────────────────────────
W, H  = 1200, 675          # 16:9 fills Telegram's photo width without cropping
PAD   = 64
SCALE = 2

# ── Palette ───────────────────────────────────────────────────────────────────
BG_TOP   = (11, 14, 17)      # #0b0e11
BG_BOT   = (19, 24, 32)      # #131820
GREEN    = (63, 185, 80)     # #3fb950
RED      = (248, 81, 73)     # #f85149
WHITE    = (230, 237, 243)   # #e6edf3
MUTED    = (139, 148, 158)   # #8b949e
DIM      = (90, 102, 115)    # #5a6673
HAIRLINE = (34, 43, 54)      # #222b36
BLUE     = (88, 166, 255)    # #58a6ff

_FONTS = {
    "bold": ["/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "reg":  ["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"],
    "monor": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"],
}


def _f(kind: str, size: int):
    for path in _FONTS[kind]:
        if os.path.exists(path):
            return ImageFont.truetype(path, int(size * SCALE))
    return ImageFont.load_default()


# Shared with the channel text so a price reads identically on the card and in
# the message beside it. BTC at 111,000 and OP at 0.086 both have to look right.
from brand import fmt_px as _fmt_px


def _signed(v: float, suffix: str = "", decimals: int = 2) -> str:
    return f"{'+' if v >= 0 else '-'}{abs(v):,.{decimals}f}{suffix}"


def _signed_usd(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _background(draw_size):
    """Vertical gradient ground."""
    w, h = draw_size
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    arr = (np.array(BG_TOP, np.float32) * (1 - t) +
           np.array(BG_BOT, np.float32) * t)
    return Image.fromarray(np.repeat(arr, w, axis=1).astype(np.uint8), "RGB").convert("RGBA")


def _glow(size, center, radius, color, max_alpha=54):
    """Soft radial wash in the result colour, sitting behind the headline."""
    w, h = size
    yy, xx = np.ogrid[:h, :w]
    d = np.sqrt((xx - center[0]) ** 2 + (yy - center[1]) ** 2)
    a = np.clip(1.0 - d / radius, 0.0, 1.0) ** 2
    arr = np.zeros((h, w, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = color
    arr[..., 3] = (a * max_alpha).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _pill(draw, x, y, text, font, fg, bg=None, border=None, padx=16, pady=8, radius=999):
    """Rounded label. Returns the x just past its right edge, so a row of pills
    can be laid out by chaining rather than by guessing offsets."""
    s = SCALE
    tw = draw.textlength(text, font=font)
    th = font.size
    box = [x * s, y * s - th * 0.5 - pady * s,
           x * s + tw + padx * 2 * s, y * s + th * 0.5 + pady * s]
    draw.rounded_rectangle(box, radius=min(radius * s, (box[3] - box[1]) / 2),
                           fill=bg, outline=border, width=max(1, int(1.5 * s)))
    draw.text(((box[0] + box[2]) / 2, y * s), text, font=font, fill=fg, anchor="mm")
    return (box[2] / s) + 10


def _tracked(draw, x, y, text, font, fill, spacing=2.2, anchor_y="mm"):
    """Letter-spaced small-caps label. Pillow has no tracking, so step manually."""
    s = SCALE
    cx = x * s
    for ch in text:
        draw.text((cx, y * s), ch, font=font, fill=fill, anchor="l" + anchor_y[1])
        cx += draw.textlength(ch, font=font) + spacing * s
    return cx / s


def _hairline(draw, y, x0=PAD, x1=W - PAD, color=HAIRLINE):
    s = SCALE
    draw.rectangle([x0 * s, y * s, x1 * s, y * s + max(1, int(s * 0.75))], fill=color)


def _hexmark(draw, cx, cy, r, color):
    """The brand hexagon, drawn rather than typed -- U+2B21 is absent from Noto
    Sans and rendered as tofu on the previous card."""
    s = SCALE
    pts = [(cx * s + r * s * np.cos(np.pi / 6 + i * np.pi / 3),
            cy * s + r * s * np.sin(np.pi / 6 + i * np.pi / 3)) for i in range(6)]
    draw.polygon(pts, outline=color, width=max(1, int(2 * s)))


def _ladder(d, x, y0, y1, entry, exit_px, stop, color, f_lab, f_val):
    """Vertical price track on the right half: stop / entry / exit placed at their
    true relative prices. The right ~40% of the card was empty, and the three
    prices were previously just numbers in the footer row -- here they also show
    how far the trade actually travelled and how close the stop sat."""
    s = SCALE
    pts = [("STOP", stop, MUTED), ("ENTRY", entry, WHITE), ("EXIT", exit_px, color)]
    lo, hi = min(p for _, p, _ in pts), max(p for _, p, _ in pts)
    span = (hi - lo) or (abs(entry) * 1e-4) or 1.0
    lo, hi = lo - span * 0.14, hi + span * 0.14

    def ypos(p):
        return y0 + (hi - p) / (hi - lo) * (y1 - y0)

    d.rounded_rectangle([(x - 2.5) * s, y0 * s, (x + 2.5) * s, y1 * s],
                        radius=3 * s, fill=(28, 34, 43, 255))
    ye, yx = ypos(entry), ypos(exit_px)
    d.rounded_rectangle([(x - 2.5) * s, min(ye, yx) * s, (x + 2.5) * s, max(ye, yx) * s],
                        radius=3 * s, fill=color + (235,))

    # Entry and stop sit 0.4-1.2% apart, which is a few pixels -- without this
    # their labels overprint each other on almost every card.
    rows = sorted([(ypos(p), lab, p, col) for lab, p, col in pts])
    placed, prev = [], -1e9
    for yy, lab, p, col in rows:
        yy = max(yy, prev + 44)
        placed.append((yy, lab, p, col))
        prev = yy
    shift = max(0.0, placed[-1][0] - y1)

    for yy, lab, p, col in placed:
        ty = yy - shift
        d.ellipse([(x - 7) * s, (ypos(p) - 7) * s, (x + 7) * s, (ypos(p) + 7) * s],
                  fill=col + (255,) if len(col) == 3 else col)
        if abs(ty - ypos(p)) > 2:
            d.line([(x + 9) * s, ypos(p) * s, (x + 22) * s, ty * s],
                   fill=HAIRLINE, width=max(1, int(s)))
        _tracked(d, x + 28, ty - 11, lab, f_lab, DIM, spacing=1.6)
        d.text(((x + 28) * s, (ty + 12) * s), _fmt_px(p), font=f_val, fill=col, anchor="lm")


def generate(coin: str, direction: int, entry: float, exit_px: float,
             sl: float, tp: float, lev_pct: float, result: str,
             sig_num: int, opened_at: datetime, closed_at: datetime,
             duration_h: float, max_adverse: float = 0.0,
             leverage: int = 10, size: float = 0,
             rr: float = None, pnl_usd: float = None,
             balance_before: float = None, sl_orig: float = None) -> io.BytesIO:

    # ── Numbers ───────────────────────────────────────────────────────────────
    # The sign has to come from the P&L itself. The previous card computed
    #     usd_val = size * abs(exit_px - entry) * direction
    # where abs() destroys the outcome and leaves only the sign of `direction`:
    # every LONG printed "+$" and every SHORT "-$" regardless of result. Nine of
    # nineteen published cards carried the wrong sign, including AAVE #95 showing
    # "+57.6%" and "-$20.32" on the same card. This is the same expression
    # tracker.py already uses for raw_dollar and pnl_usd.
    if pnl_usd is None:
        pnl_usd = (exit_px - entry) * direction * abs(size)
    usd_val = pnl_usd

    if rr is None:
        risk_px = abs(entry - (sl_orig if sl_orig is not None else sl))
        rr = ((exit_px - entry) * direction / risk_px) if risk_px else 0.0

    # Classify on realised P&L, never on which ORDER closed the trade. The
    # ratchet cancels the take-profit at +1R, so under strategy 2 every exit --
    # winners included -- fires the stop and arrives here as result="sl". Reading
    # the label painted every profitable card red and captioned it "SL HIT":
    # AVAX +15.7%, ETH +17.4% and BTC +13.3% all shipped as losses.
    won   = usd_val > 0
    flat  = abs(usd_val) < 0.005
    color = MUTED if flat else (GREEN if won else RED)
    side  = "SHORT" if direction == -1 else "LONG"

    pct_s = _signed(lev_pct, "%", 1)
    usd_s = _signed_usd(usd_val)
    rr_s  = _signed(rr, "R", 2)

    # Every card leads with ROI on margin, without exception -- owner's call, so
    # that the headline figure means the same thing on every card and cannot be
    # compared wrongly. The tradeoff is real and stays visible: BTC #96 lost
    # $0.20 on $11 of notional, which is a true -37.4% on margin and reads far
    # worse than it was. The dollars sit directly underneath for that reason.
    head_s, head_label = pct_s, "ROI ON MARGIN"
    sub = [usd_s, rr_s]
    if balance_before:
        sub.append(_signed(usd_val / balance_before * 100, "% account", 2))

    if result == "tp":
        badge, note = "TP HIT", "Take-profit target reached"
    elif flat:
        badge, note = "BREAKEVEN", "Closed at entry"
    elif won:
        badge, note = "WIN", "Trailing stop · risk-free exit"
    else:
        badge, note = "LOSS", "Stop-loss · risk capped at 1R"

    # duration_h was only added to the record later; the earliest closed trades
    # have it as None and rendered a flat "0m". The timestamps are always there.
    if not duration_h:
        try:
            duration_h = (closed_at - opened_at).total_seconds() / 3600
        except Exception:
            duration_h = 0
    dh, dm = int(duration_h or 0), int(((duration_h or 0) % 1) * 60)
    dur_s  = f"{dh}h {dm}m" if dh else f"{dm}m"
    _stamp  = lambda t: (t.astimezone(timezone.utc) if t.tzinfo else t).strftime("%d %b · %H:%M")
    open_s, close_s = _stamp(opened_at), _stamp(closed_at)

    # ── Canvas ────────────────────────────────────────────────────────────────
    s  = SCALE
    px = (W * s, H * s)
    img = _background(px)
    img.alpha_composite(_glow(px, (int(PAD * s + 180 * s), int(268 * s)),
                              620 * s, color, 0 if flat else 58))
    d = ImageDraw.Draw(img)

    f_brand  = _f("bold", 22)
    f_sig    = _f("monor", 19)
    f_coin   = _f("bold", 52)
    f_pill   = _f("bold", 17)
    f_head   = _f("bold", 104)
    f_label  = _f("bold", 15)
    f_sub    = _f("bold", 27)
    f_note   = _f("reg", 20)
    f_stat_l = _f("bold", 14)
    f_stat_v = _f("mono", 26)
    f_foot   = _f("reg", 17)

    # ── Top bar ───────────────────────────────────────────────────────────────
    _hexmark(d, PAD + 11, 52, 11, BLUE)
    d.text(((PAD + 32) * s, 52 * s), "GetSignal AI", font=f_brand, fill=WHITE, anchor="lm")
    d.text(((W - PAD) * s, 52 * s), f"#Signal{sig_num}", font=f_sig, fill=DIM, anchor="rm")
    _hairline(d, 92)

    # ── Coin row ──────────────────────────────────────────────────────────────
    d.text((PAD * s, 148 * s), coin.upper(), font=f_coin, fill=WHITE, anchor="lm")
    x = PAD + d.textlength(coin.upper(), font=f_coin) / s + 20
    side_col = GREEN if side == "LONG" else RED
    x = _pill(d, x, 150, side, f_pill, side_col,
              bg=tuple(int(c * 0.22) for c in side_col) + (255,))
    _pill(d, x, 150, f"{int(leverage)}×", f_pill, MUTED, bg=(28, 34, 43, 255))

    bw = d.textlength(badge, font=f_pill) / s + 32
    _pill(d, W - PAD - bw, 150, badge, f_pill, color,
          bg=tuple(int(c * 0.20) for c in color) + (255,), border=color + (255,))

    # ── Headline ──────────────────────────────────────────────────────────────
    d.text((PAD * s, 262 * s), head_s, font=f_head, fill=color, anchor="lm")
    _tracked(d, PAD + 3, 336, head_label, f_label, DIM)

    x = PAD
    for i, part in enumerate(sub):
        if i:
            d.text((x * s, 388 * s), "·", font=f_sub, fill=DIM, anchor="lm")
            x += 26
        d.text((x * s, 388 * s), part, font=f_sub,
               fill=WHITE if i == 0 else MUTED, anchor="lm")
        x += d.textlength(part, font=f_sub) / s + 26
    d.text((PAD * s, 430 * s), note, font=f_note, fill=DIM, anchor="lm")

    _ladder(d, 742, 208, 438, entry, exit_px,
            sl_orig if sl_orig is not None else sl, color, f_stat_l, f_stat_v)

    # ── Stats ─────────────────────────────────────────────────────────────────
    _hairline(d, 472)
    stats = [("MAX DRAWDOWN", f"{max_adverse:.1f}%", RED if max_adverse < -10 else MUTED),
             ("DURATION",     dur_s,                  MUTED),
             ("OPENED",       open_s,                 MUTED),
             ("CLOSED",       close_s,                MUTED)]
    colw = (W - 2 * PAD) / len(stats)
    for i, (label, val, col) in enumerate(stats):
        cx = PAD + i * colw
        _tracked(d, cx, 508, label, f_stat_l, DIM, spacing=1.6)
        d.text((cx * s, 548 * s), val, font=f_stat_v, fill=col, anchor="lm")

    # ── Footer ────────────────────────────────────────────────────────────────
    _hairline(d, 596)
    d.text((PAD * s, 630 * s), "@GetSignalAI  ·  Hyperliquid Testnet",
           font=f_foot, fill=DIM, anchor="lm")
    d.text(((W - PAD) * s, 630 * s), f"{coin.upper()} · {side} · {int(leverage)}× · {rr_s}",
           font=f_foot, fill=DIM, anchor="rm")

    # ── Render ────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").resize((W, H), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
