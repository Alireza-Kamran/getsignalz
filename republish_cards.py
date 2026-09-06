"""
Regenerate every published result card and replace it in the channel in place.

One-off maintenance script; the bot never imports it.

Why it exists: result_card.py computed the dollar figure as
    size * abs(exit_px - entry) * direction
where abs() destroys the outcome, leaving only the sign of `direction`. Every
LONG printed "+$" and every SHORT "-$" whatever the result, so nine of nineteen
published cards contradicted their own headline percentage -- AAVE #95 showed
"+57.6%" beside "-$20.32", and losing longs were advertised as profits.

Cards are replaced with editMessageMedia, never delete+repost: closed signal
messages are the channel's permanent public trade journal and keep their ids.

    python3 republish_cards.py --dry-run     # render to /tmp, touch nothing
    python3 republish_cards.py               # replace in the channel
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import result_card
import tracker

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
PAUSE = 1.5          # Telegram throttles media edits harder than text edits


def _sign(v):
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def check_signs(rows):
    """The whole bug in one assertion: dollars, R and ROI% describe the same
    trade and must never disagree about whether it won. Runs before anything is
    published, and fails the run rather than shipping a contradiction again."""
    bad = []
    for c in rows:
        s = {_sign(c.get("pnl_usd") or 0), _sign(c.get("rr") or 0),
             _sign(c.get("lev_pct") or 0)}
        if len(s - {0}) > 1:
            bad.append(c)
    return bad


def render(c):
    return result_card.generate(
        coin=c["coin"], direction=c["dir"], entry=c["entry"], exit_px=c["exit"],
        sl=c["sl"], tp=c["tp"], lev_pct=c["lev_pct"], result=c["result"],
        sig_num=c.get("signal_num") or 0,
        opened_at=datetime.fromisoformat(c["opened_at"]),
        closed_at=datetime.fromisoformat(c["closed_at"]),
        duration_h=c.get("duration_h") or 0,
        max_adverse=c.get("max_drawdown_pct") or 0.0,
        leverage=c.get("leverage", 10), size=c["size"],
        rr=c.get("rr"), pnl_usd=c.get("pnl_usd"),
        balance_before=c.get("balance_before"), sl_orig=c.get("sl_orig"),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="/tmp/cards")
    args = ap.parse_args()

    rows = sorted(json.load(open(STATE))["closed_trades"],
                  key=lambda c: c["closed_at"])

    bad = check_signs(rows)
    if bad:
        print("ABORT — sign disagreement in state.json, fix the data first:")
        for c in bad:
            print(f"  {c['coin']} #{c.get('signal_num')}  "
                  f"pnl={c.get('pnl_usd')}  rr={c.get('rr')}  lev_pct={c.get('lev_pct')}")
        return 1
    print(f"sign check OK across {len(rows)} trades\n")

    if args.dry_run:
        os.makedirs(args.out, exist_ok=True)

    ok = fail = skip = 0
    for c in rows:
        tag = f"#{str(c.get('signal_num') or '--'):>4} {c['coin']:<5}"
        usd = c.get("pnl_usd") or 0
        card = render(c)

        if args.dry_run:
            path = os.path.join(args.out, f"{c.get('signal_num')}_{c['coin']}.png")
            open(path, "wb").write(card.read())
            print(f"  {tag} {usd:+8.2f}  ->  {path}")
            ok += 1
            continue

        mid = c.get("card_msg_id")
        if not mid:
            # The OP trade was reconstructed from journal.json during the stats
            # audit; its card id was never recorded, so there is nothing to
            # replace. Adding a new card would post it out of chronological
            # order at the end of the channel, so leave the channel alone.
            print(f"  {tag} {usd:+8.2f}  SKIP — no card_msg_id")
            skip += 1
            continue

        caption = f"#Signal{c.get('signal_num')}  {c['coin']}"
        if tracker._edit_photo(mid, card, caption):
            print(f"  {tag} {usd:+8.2f}  OK   msg {mid}")
            ok += 1
        else:
            print(f"  {tag} {usd:+8.2f}  FAIL msg {mid}")
            fail += 1
        time.sleep(PAUSE)

    print(f"\n{ok} ok · {fail} failed · {skip} skipped")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
