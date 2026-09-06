"""
Re-render every published signal message in the current house style.

One-off maintenance script; the bot never imports it.

The closed signal messages are the channel's permanent public trade journal, so
they are EDITED IN PLACE with editMessageText -- never deleted, never reposted.
Message ids and their position in the channel history are preserved.

The open trade's message needs nothing: tracker re-renders it on its normal
60-second refresh and picks the new format up by itself.

    python3 restyle_signal_messages.py --dry-run
    python3 restyle_signal_messages.py
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tg
import tracker

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
PAUSE = 1.2

ZWNJ = "‌"
PERSIAN = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")
TAG = re.compile(r"<[^>]+>")


def render(c):
    return tracker._live_text(
        dict(c), c["exit"], closed=True,
        close_result=c.get("result"), final_pct=c.get("lev_pct") or 0.0)


def lint(msg):
    """The same rules test_brand.py enforces, applied to the real records before
    anything is published. A record with an odd shape could produce a line that
    the fixtures never exercise."""
    problems = []
    if ZWNJ in msg:
        problems.append("contains ZWNJ")
    for line in TAG.sub("", msg).split("\n"):
        if PERSIAN.search(line) and LATIN.search(line):
            problems.append(f"mixed script: {line!r}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = sorted(json.load(open(STATE))["closed_trades"],
                  key=lambda c: c["closed_at"])

    # Lint everything BEFORE publishing anything -- a broken render is much
    # cheaper to find here than in 19 edited channel messages.
    faults = {}
    for c in rows:
        p = lint(render(c))
        if p:
            faults[f"{c['coin']} #{c.get('signal_num')}"] = p
    if faults:
        print("ABORT — rendering problems:")
        for k, v in faults.items():
            print(f"  {k}: {v}")
        return 1
    print(f"lint OK across {len(rows)} messages\n")

    ok = fail = skip = 0
    for c in rows:
        tag = f"#{str(c.get('signal_num') or '--'):>4} {c['coin']:<5}"
        mid = c.get("msg_id")
        if not mid:
            print(f"  {tag} SKIP — no msg_id")
            skip += 1
            continue
        if args.dry_run:
            print(f"  {tag} would edit msg {mid}")
            ok += 1
            continue
        if tg.edit(mid, render(c)) is not False:
            print(f"  {tag} OK   msg {mid}")
            ok += 1
        else:
            print(f"  {tag} FAIL msg {mid}")
            fail += 1
        time.sleep(PAUSE)

    print(f"\n{ok} ok · {fail} failed · {skip} skipped")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
