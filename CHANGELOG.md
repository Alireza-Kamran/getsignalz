# Changelog

All nightly improvements are logged here automatically.

> **Gap notice:** v1.22.0 (2026-07-30) and v1.23.0 (2026-07-31) exist as commits but were
> never given entries here — the nightly sessions bumped the version in the commit subject
> only. Their full write-ups are in the memory file's session log for those dates.

## v1.47.0 — 2026-09-04 — CHANNEL VOICE, NAKED POSITIONS, AND NON-ATOMIC STATE

Owner-driven session, not a nightly. Three pieces of work.

**Result card rebuilt on Pillow.** The dollar figure was computed as
`size * abs(exit_px - entry) * direction` — `abs()` destroys the outcome, so the sign that
survived was only the sign of `direction`: every LONG printed "+$" and every SHORT "-$"
regardless of result. **9 of 19 published cards carried the wrong sign**, including AAVE #95
showing "+57.6%" beside "-$20.32", and losing longs advertised as profits. Fixed, and all 18
cards with a stored `card_msg_id` were replaced in the channel with `editMessageMedia` (ids
unchanged, nothing deleted). Card is now a 16:9 supersampled render with a price ladder;
dust trades (|P&L| < $1) lead with dollars so a $0.20 loss no longer headlines as -37.4%.

**Critical: a rejected entry stop left the position naked.** `open_trade` logged the
rejection as a warning and returned the trade dict, which `live.py` took as proof of
protection. `update_sl` had been hardened to raise for exactly this on the ratchet path; the
entry path never was. Now retries twice, then flattens the position and DMs the owner.
Added `_verify_stops` in the main loop (one check per coin per 5 min) — `get_stop_price` now
raises on a failed read and returns None only for "nothing resting", so a missing stop is
detectable at all, which it previously was not.

**Critical: every state file was written non-atomically.** `open(path, "w")` truncates before
writing, so a crash in that window empties `state.json` — the file holding all open
positions, `closed_trades` and the stats. This had already happened once (the OP record).
New `io_safe.py`: serialise, write to a same-directory tmp, fsync, `os.replace`, keeping one
`.bak` generation. `load_state` now recovers from `.bak` and raises rather than silently
returning an empty skeleton. Routed `state.json`, both journals, `strategy_config.json`,
VERSION, CHANGELOG and the two sites that rewrite `trader.py` through it.

**Channel voice.** New `brand.py`: one house style, Persian and English never sharing a line
(Telegram runs bidi per line and reorders mixed content), figures in `<pre>` blocks. Version
updates now post publicly — bullets derived from *which files changed*, never from the
nightly model's free text, so nothing unreviewed reaches subscribers. Daily/weekly reviews
trimmed to a headline; their detail moved to the owner DM.

**Also:** tests that import `live` were writing into the production `bot.log` — a new test
asserting on a missing stop emitted `[BTC] NO STOP RESTING` at ERROR into the file the
nightly review reads for incidents. The file sink is now skipped when the entrypoint is a
`test_*` script. `test_null_record`'s live-trade-count guard was pinned to the literal 18 and
failed on every real close; it now snapshots at start. Fixed a permission regression where
`mkstemp` silently took `state.json` from 644 to 600.

**Tests:** 10 files green. New: `test_naked_stop.py` (22), `test_atomic_state.py` (12),
`test_brand.py` (31).

---

## v1.52.1 — 2026-09-14

**Stats:** 24 trades · WR: 33% · P&L: -30.3%

No changes — all parameters within target bounds.

---

## v1.52.0 — 2026-09-13

**Stats:** 23 trades · WR: 35% · P&L: -12.8%

**Code improvements (1):**
- tracker.py: Align the closed-trade card's USD display with the archived pnl_usd: both now draw from size_orig (the size as filled at entry, immune to restart overwrites after a partial fill) rather than t['size'], which is set to the residue on the first restart after a partial stop. The _num helper handles None/str inputs safely and is already defined in this file; the fallback to t['size'] preserves behaviour for records opened before size_orig was introduced.

---

## v1.51.3 — 2026-09-12

**Stats:** 22 trades · WR: 36% · P&L: +11.0%

No changes — all parameters within target bounds.

---

## v1.51.2 — 2026-09-11

**Stats:** 22 trades · WR: 36% · P&L: +11.0%

No changes — all parameters within target bounds.

---

## v1.51.1 — 2026-09-10

**Stats:** 21 trades · WR: 38% · P&L: +60.3%

No changes — all parameters within target bounds.

---

## v1.51.0 — 2026-09-09

**Stats:** 20 trades · WR: 40% · P&L: +83.4%

**Code improvements (1):**
- trader.py: v1.50.0 (2026-09-08) switched the S2-only observability scan in live.py to real_close for RSI because HA smoothing suppresses extremes and understates how often RSI_OVERSOLD/RSI_OVERBOUGHT is touched. build_df was not updated alongside it, so the 12 S1-watchlist coins continued logging HA-RSI while the 8 S2-only coins logged real-close RSI. analyze._scan_census pools all bot.log scan observations and computes RSI qualification rates; with two different RSI definitions in the same pool the rates are internally inconsistent. real_close is also what strategy2.find_setup uses for its actual entry decisions. S1 is disabled so scoring is unaffected; the only change is a consistent RSI definition across the full 20-coin scan log and cleaner census statistics.

---

## v1.50.0 — 2026-09-08

**Stats:** 20 trades · WR: 40% · P&L: +83.4%

**Code improvements (1):**
- live.py: The observability scan logs real_close for the price display (via _pc) but computes RSI from the HA-transformed close. HA smoothing suppresses RSI extremes, producing moderate readings (38-56) where real-close RSI would show the oversold/overbought values (13-24, 75-86) that strategy2 actually gates on. analyze._scan_census() reads these log lines to estimate how often RSI_OVERSOLD/RSI_OVERBOUGHT thresholds are met — with HA-RSI those thresholds are almost never crossed, so the census dramatically underestimates signal frequency and the ADX conversion rate is reported as far higher than reality. real_close is guaranteed to exist (fetch_candles always sets df['real_close'] = df['close'] before the HA transform). ADX is left on HA bars intentionally — HA-ADX is a valid smoothing choice for trend-strength filtering and changing it would affect the gate census in a less certain direction.

---

## v1.49.3 — 2026-09-07

**Stats:** 20 trades · WR: 40% · P&L: +83.4%

No changes — all parameters within target bounds.

---

## v1.49.2 — 2026-09-06

**Stats:** 20 trades · WR: 40% · P&L: +83.4%

No changes — all parameters within target bounds.

---

## v1.49.1 — 2026-09-05

**Stats:** 20 trades · WR: 40% · P&L: +83.4%

No changes — all parameters within target bounds.

---

## v1.49.0 — 2026-09-05

**Stats:** 19 trades · WR: 42% · P&L: +133.6%

**Code improvements (4):**
- live.py: observability pass for the 8 S2 coins the scan loop never logged (it iterated the retired S1 watchlist) - placed below the entry decision so it adds no signal-to-fill drift
- analyze.py: the feed-quality split no longer discards trades silently - it was dropping 6 of 19 closed trades (32% of the book, 5 of them losses) on a criterion unrelated to feed quality
- analyze.py: a process restart is no longer counted as a loop stall - 10 restarts on 09-04 produced 9 phantom SELF-BLOCKED rows and inflated 'ours to prevent' from 121 to 494 min
- test_scan_coverage.py: 45 assertions pinning watchlist coverage, log-format round-trip and scan ordering

---

## v1.48.0 — 2026-09-04

**Stats:** 19 trades · WR: 42% · P&L: +133.5%

**Code improvements (1):**
- live.py: NEAR SHORT re-signalled on two consecutive candles despite a 6-hour entry-fail cooldown being set in _cooldown_until and threaded into s2_open. strategy2.find_setup() returned the coin anyway. This guard checks _cooldown_until directly in live.py — where it is authoritative and in-memory — immediately after find_setup() returns and before log_signal or open_trade is called, so no cooldown bypass can burn a signal number or send an order the exchange will reject.

---

## v1.46.0 — 2026-09-04 — THE STOP FIRED, FILLED 97%, AND THE TRADE STAYED OPEN

**Stats:** live n=19 (was reported as 18), WR 42.1%, sumR +4.57, avgWin +1.922R,
avgLoss -0.982R, EV +0.2405R, breakeven WR 33.8%, t=+0.67. **No S2 parameter changed** —
every constant is owner-locked and t=+0.67 justifies moving none.

**Last night's fixes verified.** The pinned dashboard is ALIVE: 0 `dashboard error` lines in
the 711 log lines since the 09-03 02:09 restart, against 176 before it. The review ran exactly
once (23:00:09 → 23:02:15). No new SELF-BLOCKED latency row.

**THE FINDING — `sz != 0` is not the same as "position open".**
BTC 2026-09-03 SHORT stopped out at 19:21:24 and the bot did not notice for **6.7 hours**.
Hyperliquid's "Stop Market" is a stop-limit with a ~0.3% band filled IOC; the book inside that
band was thinner than the order, so it swept five levels (83472 → 83514) for **0.00473 of
0.00486** and left **0.00013 ($10.74)** resting. `executor.get_positions()` filters `if sz != 0`
and `live._check_closed()` fires only when the coin is ABSENT from that dict — a residue is
neither zero nor a position, so it falls through both and the trade becomes immortal.
Confirmed against real candles: BTC's real high was **83750** vs a stop at **83456.68**,
breached for four consecutive hours. All 18 prior closes filled to exactly zero.

**The cost was never the $10:** a MAX_TRADES slot held forever (effective MAX_TRADES=1 for
6.7h, during the first-ever concurrent pair); a -1.01R loss missing from every published
statistic (n=18/WR 44.4%/EV +0.310R was really n=19/WR 42.1%/EV +0.2405R); a residue with no
stop under it; and a report describing a $400 protected position that did not exist.

**THE WORSE HALF.** `live.py`'s restore sets `size = abs(hl["size"])` and writes it back to
state.json — **one restart** would have laundered the residue into "the size we opened"
(0.00486 → 0.00013), making the ratio 1.00 and the dust undetectable forever. Same disease the
repo already cured for the stop (the ratchet overwrote `sl`, hence `sl_orig`); `size` needed
the identical immutable twin.

**FIXED.** `tracker.register_position` and both `live.py` open sites record an immutable
`size_orig`; the restore path reads it before `size` is overwritten and persists it. New
`live._reconcile_dust()` runs ABOVE `_check_closed` (absence from `positions` is the only
signal it reads) and above `_check_trail_s2`. Classifies by **fraction of size opened, never
by notional** — $10 is dust on BTC and a whole position on DOGE. ≤10%: flatten and record the
close (`get_close_fill` already size-weights every closing fill). 10–90%: a real position with
no stop under it — logged and DMed, **deliberately not auto-resolved**.

**ADDED.** `analyze.py` OPEN BOOK now asserts `MAE >= -1.00R`, the mirror of the existing
locked-vs-MFE invariant on the loss side. Every closed-trade invariant PASSED that night
because the trade was never recorded as closed; the open book had no invariant at all. It
back-derives the breach price and prints $83,750.15 against a real candle high of 83,750.
`test_dust_close.py` new, 40 assertions incl. the `size_orig` laundering regression.

**Proven in production:** restart → `BTC: stop filled short — 0.00013 of 0.00486 (2.7%)` →
`Closed BTC` → `[S2] CLOSED BTC | -37.4% | SL`. BTC flat, its stale TP auto-cancelled by the
venue, ETH untouched with both stop and TP resting. Seven suites green; state files
md5-identical across the run.

## v1.42.0 — 2026-09-01 — THE SESSION THAT SUPERVISES THE BOT FAILED 14 OF 40 TIMES

**Stats:** live n=17, WR 41.2%, sumR +2.61, EV +0.153R, t=+0.42. Book flat since 08-25.
No parameter changed — every S2 constant is owner-locked and t=+0.42 justifies moving none.
Seven days silent is the gate working as measured (census admits 8.7% of long / 2.1% of short
RSI candidates). ARB sat RSI 91→94 at ADX 39→45 tonight: a textbook short, correctly gated out.

**Last night's fix verified: ZERO blind-bot alerts.** The 01:17 off-session and 04:00
quiet-hours windows, which produced a false 🚨-then-✅ pair every night 08-28→08-30, have each
passed twice in silence.

**THE FINDING.** Mined `selflearn.log` over 07-24 → 08-31: **40 sessions, 26 completed, 14
failed = 65%**. Seven of the failures were usage limits whose reset was *hours* away, and
nothing ever re-ran them — which is why the bot traded unsupervised for four days.

**CORRECTION.** The 08-31 note ranked "no alert exists for a missed session" as its top item.
An alert *does* exist — `self_improve.sh` DMs on every non-zero exit, and all three of
08-28/29/30 exited 1. Detection was never the gap; **recovery** was. A detector tells you the
bot ran unsupervised; a retry means it didn't.

**FIXED — automatic same-day retries.** `session limit` always resets 02:10–04:00 UTC and
`weekly limit` at 14:00, so cron now adds `--retry` at 04:30 and `--retry-last` at 15:00.
Replayed against the record: **7 of 14 lost nights recovered, 65% → 82%**; the recent four-day
run would have been one day. A human already proved it by hand — on 07-26 the 02:00 run died on
a spend limit and a manual 04:41 rerun succeeded. Safe because a retry is a **no-op** once
`.last_session` carries today's date, and it doubles as the missing-session detector because it
keys on "did today succeed", not on "did 02:00 report a failure".

**Three bugs found by writing the test, not by reading the code:** the escalation `case` matched
`retry-last` while the argument is `--retry-last` (the end-of-day alert would never have fired);
`self_improve.sh` pins `PATH` and discarded the test's stub, **launching a real nightly session
that orphaned to ppid=1 with acceptEdits** (killed by exact PID, no damage); and the failure
detector grepped the whole transcript for `"hit your session limit"` — text this very report
contains, so a successful session would have scored as failed. **An instrument must not match
its own output.**

**Also fixed:** the failure DM interpolated raw output into a `parse_mode=HTML` send without
`tg.esc()`, so a traceback containing `<urllib3...>` would be rejected and the alert lost
exactly when it mattered. And `self_improve.sh` now re-execs from an immutable snapshot of
itself — bash reads scripts by byte offset, and the session it launches edits this repo.

**Added:** `analyze.py` SUPERVISION section, printed second after AVAILABILITY — completion
rate plus named recent failures. Same reasoning as AVAILABILITY on 08-23: a control that only
works when someone remembers it is not a control. `test_session_retry.py`, 17 assertions.

---

## v1.45.0 — 2026-09-03

**Stats:** 18 trades · WR: 44% · P&L: +170.9%

**Code improvements (1):**
- tracker.py: The retry loop `for attempt in range(3)` was effectively a one-shot: a ConnectionResetError (printed at 22:01 in today's logs) hit the except block which called `return`, exiting _edit() immediately rather than continuing to the next attempt. With BTC and ETH live on Telegram, a dropped edit leaves subscribers reading a stale position message. Adding exponential backoff (2s then 4s) before retrying on transient network failures makes all three attempts meaningful without touching any trading logic.

---

## v1.43.0 — 2026-09-02

**Stats:** 18 trades · WR: 44% · P&L: +170.9%

**Code improvements (1):**
- live.py: journal_s2.json omits sl_orig, so any direct R analysis on that file — including _s2_report and future queries — computes R against the ratcheted stop, systematically understating every winner that armed the ratchet. analyze.py documents the error precisely. t always has sl_orig (written at trade open); t.get with t['sl'] fallback is safe for any pre-fix entries already in _open_trades at restart.

---

## v1.42.1 — 2026-09-01

**Stats:** 17 trades · WR: 41% · P&L: +113.3%

No changes — all parameters within target bounds.

---

## v1.39.0 — 2026-08-27 — THE BOT WENT BLIND AND EVERY HEALTH CHECK SAID GREEN

**Stats:** live n=17, WR 41.2%, sumR +2.61, meanR +0.153, t=+0.42. Book flat. No parameter
changed — every S2 constant is owner-locked and the book is still not distinguishable from a
coin flip.

**THE INCIDENT.** At 02:00 UTC the last completed candle scan was **00:01** — two hours blind.
257 ConnectTimeout warnings, 50 Cycle errors. Cause is **external**, confirmed independently:
DNS resolves, Telegram and `1.1.1.1` connect in 0.16s, but all 8 A-records behind
`api.hyperliquid.xyz` *and* `api.hyperliquid-testnet.xyz` sit in `99.86.171.0/24` and raw TCP to
that entire /24 times out. One shared CloudFront edge PoP — mainnet and testnet both behind it,
so there is no failover endpoint and retrying a different A-record cannot help.

**A THIRD LIVENESS FAILURE MODE.** Neither existing guard covers it. `_watchdog()` watches
`_last_tick` and the loop was ticking normally — cycle, raise, sleep, repeat. `.heartbeat` was
fresh to the second. systemd said `active (running)`. All three green while the bot had not seen
a price in two hours, because `get_positions()` raises at the *top* of the cycle before the
candle check is reached, and **nothing measured whether a scan completed**. Liveness was measured
on the loop, never on the loop's job. `tg.send_error()` did fire but dedups to
`"upstream gateway error x25 in the last 30 min"` — noise, not "you are blind".

**FIXED.** `live.py` tracks `_last_scan_ok`, set only on reaching the end of a full candle pass.
`_check_scan_stale()` runs in the existing watchdog thread, DMs once per episode past
`SCAN_STALE_ALERT_SEC = 8100` (2h15m — two consecutive misses, and clears the nightly review's
~1h10m legitimate scan-free stretch), skips quiet hours, names any open position, warns the
ratchet cannot arm, and DMs again on recovery. **Deliberately does not restart:** a restart
cannot reach a dead API, and the trade-restore block needs `get_positions()` to succeed — when it
does not, `_open_trades` stays permanently empty and any open position is orphaned from the bot's
exit management for the life of that process. New `test_scan_stale.py`, 15 assertions, all pass.

**CORRECTION — v1.38.0's cap recommendation was backwards.** That entry states ETH under a 0.3%
cap "would have been capped at roughly +2.40R instead of +2.17R", implying ~+0.23R free from
narrowing `RATCHET_SLIP_CAP`. **Wrong** — and it contradicts itself two sentences later: a
Hyperliquid trigger with `isMarket:True` fires an IOC whose unfilled remainder is **CANCELLED,
not rested**. An order that cannot fill inside its cap does not fill at a worse-but-capped price;
**it does not fill at all.** ETH's adverse fill was **1.047% of price**, so at 0.30% that stop
would not have executed — and `update_sl` cancels the old stop *and* the TP before placing the
new one. The real trade is 0.33R against an unprotected short mid-move.
→ **Do not narrow `RATCHET_SLIP_CAP` to 0.30%.** Owner call; values unchanged.

**WHY IT WAS INVISIBLE: a unit mismatch.** The leak was reported only in **R** — right for edge
(normalises across coins), wrong for an execution cap enforced as a **fraction of price**, and
R/entry differs 3.6x across these trades (SOL 0.81%, SUI 1.30%, ETH 2.93%). The old line said the
worst fill "sits inside the 2.00% cap" without ever printing what it was.
`analyze._ratchet_slippage` now returns `adv_pct` per arm; the report prints fill-vs-trigger in
percent, the worst fill as a share of the cap, and names any arm a tightened cap would have
failed to fill.

**THE LEAK IS ONE BAD FILL, NOT A BROAD TAX.** New concentration line: **ETH 2026-08-21 alone is
80% of the 0.414R leak.** Adverse fills: SOL **0.019%**, SUI **0.074%**, ETH **1.047%** — the two
longs cost essentially nothing, the one SHORT cost 14x the worst long. Hypothesis, n=1, not acted
on: a short's ratchet stop is a BUY, and buying into a squeeze is where slippage lives.

**Known limitation:** `_last_scan_ok` initialises at import, so a restart resets the blind-clock
and an outage spanning the nightly restart is re-alerted only after another 2h15m.

## v1.35.0 — 2026-08-24 — THE TEST SUITE WAS WRITING INTO PRODUCTION STATE

**Stats:** live n=16, WR 37.5%, sumR +0.44, meanR +0.027, t=+0.08. One ETH SHORT open,
52h, MFE +2.27R. No parameter changed — every S2 constant is owner-locked.

**THE BUG.** `test_ratchet.py` was writing into the live `/root/trade/state.json`. It muted
the loguru sink and stubbed `update_sl` and `tg.dm_owner` — but `_check_trail_s2` has a
**third writer one level down**: `tracker.update_trail` → `save_state()` → the real state
file. Every fixture trade in that suite is named **`"ETH"`**, the coin most likely to be
open. It ran during the 08-23 02:00 session with the ETH short open and wrote its
`entry=100 / R=1` ladder over that position's record: `sl` 2684.36 → **103.0**, `locked_r`
0.0 → **3.0**, plus six fabricated "Trail +NR" activity lines.

**Cost: no money, no naked risk — but the edge was switched off.** `update_sl` was stubbed,
so no exchange order ever moved; `frontend_open_orders` confirmed the real stop resting at
2684.4 and the TP at 2225.2 throughout. The damage: `_check_trail_s2`'s monotonic guard is
`new_sl < t["sl"]` for a short and **nothing is below 103**, so every candidate stop failed
`improves` and **the ratchet was dead on that position for 24 hours** — silently, no error.
The ratchet is where 100% of the measured edge lives (original stop 10 trades meanR
**−0.979** vs ratcheted 6 trades **+1.705**). And because `tracker._live_text` renders
`activity[-4:]` into the position's **channel** message and the dashboard renders the
`locked_r` badge, the public record advertised **"🔒+3R locked"** on a trade whose stop was
still at −1.00R.

**FIXED IN THREE LAYERS**, because one layer is what failed.

1. **Prevention** (`test_ratchet.py`) — `tracker.STATE_F` is redirected to a temp file for
   the run. Redirect the *resource*, don't stub the *caller*: stubbing call sites is
   whack-a-mole and this bug **is** the third call site. New case 8 sha256-hashes the real
   state.json before and after and fails the suite if a byte moves. **11/11 pass.**
2. **Correction** (`live._reconcile_stop`, `executor.get_stop_price`) — on restore the
   tracked stop is compared against the resting reduce-only stop on HL, which is the order
   that will actually fire and is therefore right by definition. Disagreement beyond 0.5%
   adopts the exchange price, re-derives `locked_r` from it, repairs state.json on disk and
   DMs the owner. The 0.5% band absorbs trigger tick rounding (2684.3609 books as 2684.4).
   Never raises — a restore that dies there would orphan the position, a strictly worse
   failure. **Verified live:** the 02:07 restart logged `[ETH] tracked SL $103 disagrees
   with the resting exchange stop $2684.4 — adopting the exchange price`; 02:08 was a clean
   no-op.
3. **Detection** (`analyze.py` OPEN BOOK) — asserts `locked_r ≤ MFE` and that the
   **working** `sl` implies the recorded `locked_r`. The report read as a healthy armed
   ratchet for two nights because it printed `sl_orig` (clean) and `locked_r` (corrupt) and
   **never printed `sl`** (corrupt). *A report that only shows the fields you expect to be
   right cannot catch the field that is wrong.*

**THE SWEEP OVERRULED THE ANECDOTE.** ETH peaking at +2.27R and missing the 2.50R arm by
0.23R reads as an argument for lowering `TRAIL_START_R`. `sweep_trail_mode.py` says the
opposite at every bound — but its `tsr` grid was **hardcoded 0.50–1.50** and had silently
stopped containing the live constant when it moved to 2.50 on 08-05, the *identical*
stale-pin failure `test_ratchet.py` was fixed for on 08-19. The grid now derives from
`s2.TRAIL_START_R` with an assert. Real prices, 92 trades, 20 coins, cap 2 — net acct % by
(close / touch_opt / touch_pess):

```
1.00  +40.05 / +43.52 / +19.04       2.50  +55.05 / +62.67 / +41.69   <- live
1.50  +39.34 / +61.28 / +37.11       3.00  +51.85 / +65.18 / +51.64
2.00  +51.03 / +58.01 / +40.72
```

Model edge is far stronger than the live book: pessimistic bound t=2.56, out-of-sample
n=58 EV +0.500%/trade, against live n=16 t=+0.08.

**FOR KAMRAN (owner-locked — reported, not touched):**
1. **`trail_gap_r = 0.25`** — forbidding the new stop from resting within 0.25R of price
   improves **both** bounds at the deployed 2.50: touch_opt +62.67 → **+70.85**, touch_pess
   +41.69 → **+48.11**, WR unchanged. It is the direct fix for the ratchet placing its stop
   essentially *at market* (BTC 08-03 armed at +0.75R, filled 22s later at +0.706R). It
   **hurts** at tsr=2.00, so it is specific to the deployed setting. Not implemented — it
   would be a **new** exit parameter, i.e. deployed exit behaviour changed by the back door.
2. **TRAIL_START_R 2.50 → 3.00** improves the pessimistic bound **+24%**. Only `close`
   prefers 2.50, and `close` is the model the sweep exists to show is unrealistic.
3. Items from 08-23 stand: short leg 0/3, correlation cap, DOGE 20.9% frozen, external
   dead-man switch.

**Health:** `test_ratchet.py` 11/11, `test_exit_price.py` 5/5, `py_compile` clean on every
touched file. `trader.py` reformatted by `apply_config_to_trader()` (12-coin WATCHLIST onto
one line — cosmetic, same coins, and the disabled S1 engine). Three clean restarts; ETH
restored each time with stop and exchange in agreement. AVAILABILITY section clean since
the 08-21 outage.

---

## v1.41.0 — 2026-08-31

**Stats:** 17 trades · WR: 41% · P&L: +113.3%

**Code improvements (1):**
- live.py: The cooldown exists to avoid re-entering a coin whose market just moved against us. But S2 cancels the TP when the ratchet arms, so every S2 exit — winners included — arrives with hit='sl'. The branch only checked hit=='sl', not whether the trade was actually a loss, so it fired after every profitable trail exit. Adding `and lev_pct < 0` restricts cooldowns to genuine losses; coins that close profitable are immediately available for re-entry, which is correct behavior given the measured entry frequency.

---

## v1.40.1 — 2026-08-30

**Stats:** 17 trades · WR: 41% · P&L: +113.3%

No changes — all parameters within target bounds.

---

## v1.37.0 — 2026-08-25

**Stats:** 17 trades · WR: 41% · P&L: +113.3%

**Code improvements (1):**
- analyze.py: The MAX_LEV_LOSS cap only limits losses, not gains. abs() incorrectly treats every large profitable trade (ETH +127.3%, AVAX +41.1%, SUI +31.8%) as a breach of the 25% loss cap, permanently zeroing the 5-point breach component even when risk management is functioning correctly. Restricting breach detection to negative outcomes means the indicator accurately reflects whether the loss-side safety mechanism has ever failed — which it has (OP -43.3% at 20x genuinely exceeded the 25% cap), so breach_pts stays 0 for the right reason rather than the wrong one.

---

## v1.36.0 — 2026-08-24

**Stats:** 16 trades · WR: 38% · P&L: -14.0%

**Code improvements (1):**
- tracker.py: Network timeouts (the failure mode in tonight's logs) block the tracker thread for up to 34 seconds per _edit call with no benefit — the tracker loop re-attempts the edit naturally on the next 60-second tick. Returning immediately on non-429 exceptions eliminates the dead wait while leaving the 429 rate-limit wait-and-retry path (the only case where retrying within the same call actually helps) completely untouched.

---

## v1.34.0 — 2026-08-23

**Stats:** 16 trades · WR: 38% · P&L: -14.0%

**Code improvements (1):**
- live.py: tracker.py's _loop() updates state['tracked'][coin]['peak_roe_pct'] and 'max_adverse_pct' every 60s, but _open_trades in live.py is a separate in-memory dict initialised once at trade open and never updated. Reading max_adverse from _open_trades therefore always returns 0.0 (the initialisation value), so the 'Max drawdown' line never appears in the owner DM. Reading _last from closed_trades before close_position appends the current trade means peak_roe_pct and max_drawdown_pct always contain the PREVIOUS trade's figures. Reading from tracker.load_state()['tracked'][coin] — which the tracker thread keeps current — before close_position removes the entry gives the correct per-trade values for all three fields.

---

## v1.33.0 — 2026-08-23 — THE BOT WAS GONE FOR 19 HOURS AND NOTHING SAID SO

**Stats:** live n=16, WR 37.5%, sumR +0.44, meanR +0.027, EV +0.027R/trade, t=+0.08.
One ETH SHORT open. No parameter changed — every S2 constant is owner-locked and n=16
justifies moving none of them. Tonight's finding was operational.

**THE OUTAGE.** The **host** was down 2026-08-21 22:02 → 08-22 17:50 UTC — **19h48m**.
Confirmed three independent ways: bot.log candle continuity, a missing `selflearn.log`
entry for 08-22 02:00, and `uptime` + `ExecMainStartTimestamp=2026-08-22 17:50:36` with
**`NRestarts=0`** — systemd never saw a failure because the machine itself was gone. There
is no nightly commit for 08-21 either; the same fact showing up in git.

**The watchdog is not at fault and could never have caught it.** `live.py:_watchdog` is a
liveness check living inside the thing whose liveness it checks. It died with the process.

**What it cost.** An **ETH SHORT opened 08-21 22:01:46 — 76 seconds before the host died** —
sat through the whole blackout. The resting exchange stop still protected it, so this was
not naked risk, but the **ratchet was frozen for 19h48m** and the ratchet is where 100% of
the measured edge lives. Still open at time of writing: entry $2607.83, stop $2684.36
(2.93% = 1R), **MFE +2.20R**, currently ~+1.74R, **stop still at the original −1.00R**
because TRAIL_START_R=2.5 was never reached.

**FIXED (live.py) — on-disk heartbeat.** `.heartbeat` written from `_beat()` at most once
per 60s, read at startup *before* the first beat overwrites it. Deliberately **its own file,
not a `state.json` key**: routing it through `tracker.save_state` would widen exactly the
non-atomic read-modify-write race that erased OP on 08-13. A restart after a 30s systemd
bounce and a restart after a 19h blackout previously sent the owner a **byte-identical**
"Bot started" line. Downtime ≥30 min now escalates and names every position that sat through
the gap unmanaged. Verified across no-file / fresh-beat / 19h48m / 30s-bounce / corrupt-file.

**ADDED (analyze.py) — AVAILABILITY, printed first.** Mines `━━━ Candle` lines from every bot
log, excludes quiet hours (UTC 2–3), merges gaps *across* them so the blackout reads as one
18h event rather than two 9h ones, and names any position open through a gap. Justification:
the standing manual step "check candle continuity FIRST" **demonstrably failed** — the 08-22
session ran after the outage ended, read a full report, shipped two improvements, and never
noticed. A control that only works when someone remembers it is not a control. Over **827
candles / 39 days it finds exactly 2 unexplained gaps** (this one and the known 07-28 socket
freeze) with **no false positives**.

**ADDED (analyze.py) — OPEN BOOK.** Every other section reads `closed_trades`, so the most
important trade on record was invisible to the entire report while it was open. Prints live
MFE/MAE and, when unarmed, exactly how far from arming the position is.

**THE SHORT LEG OPENED.** First shorts ever: LONG 13 trades WR 46% sumR **+2.99R** vs SHORT
3 trades WR 0% sumR **−2.55R** (DOGE −0.95, BNB −0.83, APT −0.78). Shorts gave back 85% of
the long book's profit. But all three fired within two hours on 08-19 (15:01/16:01/17:01)
into one market-wide pump — **one correlated event sampled three times, not n=3**.
`MAX_TRADES=2` caps position *count*, not *correlation*; BNB and APT were concurrent and lost
together. DOGE also traded on a **21.3% frozen feed** and was dead on arrival (MFE +0.00R,
stopped in 4 minutes). The executor SL guard fired for the first time as a **clamp**, not an
abort (APT: `SL clamped to 1.52% from fill`); the abort branch has still never fired.

**Reported, not changed (owner-locked):** TRAIL_START_R=2.5 now has a live in-flight
counterexample (ETH peaked +2.20R without arming — first trade to reach ≥2.0R and not arm);
the short leg needs a decision rather than a tune; correlation is the uncapped risk; DOGE
should leave the WATCHLIST; and host-level monitoring needs an external dead-man switch,
because nothing on this box can alert *during* an outage.

**Health:** `test_ratchet.py` 10/10, `test_exit_price.py` 5/5, standing `result=="tp"` grep
clean (4 live sites, all backstop-first with P&L-sign fallbacks).

## v1.26.0 — 2026-08-04 — THE EXIT PARAMETERS WERE TUNED ON A MODEL LIVE CANNOT REPRODUCE

**Stats:** live n=5 (BTC LONG closed 08-03 at +13.3% / +0.706R). WR 60%, meanR +0.156,
total P&L +7.2%, maxDD -2.8%. Book flat. One parameter reverted, three bugs fixed, one new tool.

**`TRAIL_START_R` 0.75 → 1.00, reverting 08-02.** `portfolio2.simulate` drives the ratchet off
the bar **close** and does not test the freshly-raised stop until the **next bar** — it grants
every raised stop a full bar of immunity that no real stop has. `live._check_trail_s2` polls the
mid every ~20s and places the stop **at the market** the instant price prints the level. First
live proof: **BTC armed at +0.75R at 13:49:56 and filled 22 seconds later at +0.706R.**

New `sweep_trail_mode.py` measures both achievable bounds (asserts equivalence with portfolio2
at `mode="close"`, printed OK). 20 coins, 5000 bars, real prices, fees, cap 2:

| model | n | WR | net | EV | t | ex-top5 | ≥1.5R |
|---|---|---|---|---|---|---|---|
| close (portfolio2) | 121 | 57.0% | +42.49% | +0.351% | 2.62 | +18.56% | 19 |
| touch_opt | 122 | 58.2% | +34.65% | +0.284% | 2.45 | +15.02% | 15 |
| touch_pess | 122 | 59.0% | +8.74% | +0.072% | 0.83 | -2.81% | 7 |

**The close row sits above the optimistic bound — outside the range live can occupy.** Note the
signature: win rate goes *up* as EV collapses. A stop placed on the price rescues near-misses and
truncates runners, and this strategy's EV lives entirely in the runners.

Under both achievable models the 0.75-vs-1.00 ordering **inverts**, monotonically — 0.75 is a
local *minimum* under the pessimistic bound. 1.00 beats it at cap 2, cap 4 and under alphabetical
ranking, is positive in both out-of-sample halves under both models (0.75 is negative in-sample),
and is the only setting with non-negative `ex-top5`. Chose 1.00 over the better-scoring 1.25/1.50
because the curve runs monotone to the edge of the swept range, and picking a monotone boundary is
a mechanical fact, not a measured edge. Recorded honestly: at cap 1 the two **tie**. The cost is
deliberate — win rate drops ~5pp while EV rises. A per-rung "gap" fix was built, measured, and
**rejected** as non-monotonic noise.

**Live corroboration (n=5):** best trade **+0.99R, zero above 1.5R**, all three winners pinned at
the arming level. Winners average 4.5h, losers 17.2h.

**Bugs fixed:**
- `analyze.full_report` classified wins as `result=="tp"` — unreachable under S2, which cancels the
  TP and exits *every* trade via the stop. Every coin, score band and factor read **WR 0% while
  P&L was positive** (AVAX printed `WR:0% AvgPnL:+15.7%`). The standing coin-removal bar is
  "<30% WR AND negative P&L over 5+ trades", so its WR half was stuck at 0 for everything.
- Added an **R-multiple distribution** section to the report — in R, not leveraged %, because the
  exchange leverage clamp distorts the published number.
- **The Telegram channel has been unreachable since 08-03 22:01 UTC** (`@GetSignalz` →
  `chat not found`; token fine, owner DM fine). It produced 194 identical log lines in 4 hours and
  escalated nothing — `tg.send` was silently returning `None` on rejection. Added
  `tg.note_channel_failure()`: DMs the owner, suppresses the spam. **Restoring the channel needs
  Mr G.**
- `test_ratchet` case 2b hard-coded a 0.9R peak, silently encoding `TRAIL_START_R < 0.9`; now
  derived from the constants. 10/10 pass.

`portfolio2.py`'s docstring now carries an explicit warning that it is **invalid for exit-side
parameters** and valid for entry-side ones. `trader.py` byte-identical for the 6th night.

## v1.25.0 — 2026-08-03 — THE TAKE-PROFIT HAD BECOME A TRUNCATION DEVICE

**Stats:** live n unchanged at 4 (no signal since 08-01 20:01, ~30h quiet). Book flat.
One parameter changed, one publishing bug fixed, one new tool.

**`TP_R` 1.0 → 3.0** — the last constant still resting on the void Heikin-Ashi numbers.
Re-deriving it showed it had stopped being an exit parameter at all: since 08-02 the ratchet
arms at 0.75R, *below* the take-profit, and cancels it, so `TP_R` only decides where a
dead-bot backstop sits. At 1.0 that backstop sat 0.25R above the arming threshold, so a move
travelling 0.75R → 1.0R inside one ~20s poll window filled the TP before the ratchet could
cancel it — capping the trade at exactly 1R, on precisely the fast trending trades the ratchet
exists to harvest. **46 of 121 trades (38%) touched 1.0R while unarmed, giving up 36.75R.**

Worst case (20 coins, 5000 bars, capped book, fees in): TP_R 1.0 → net +15.59%, EV +0.129%,
t=1.32. TP_R 3.0 → net +39.03%, EV +0.325%, t=2.61, at the same win rate and no worse
drawdown. Raising the backstop cannot turn a winner into a loser — anything reaching 1.0R
already armed at 0.75R, so its stop is locked at ≥ +0.75R. Passed all standing rejection tests
(concurrency cap 1/2/4, alphabetical ranking, out-of-sample positive in both halves, and
independence from the 08-02 change). 3.0 rather than 5.0/none deliberately: the curve is
monotonic toward no-TP, and picking the boundary of a monotone relationship is a mechanical
fact, not a measured edge.

**New: `sweep_tp.py`** — `portfolio2.simulate` does not model the take-profit at all, so
`TP_R` was invisible to it and could not be swept there. The new tool asserts equivalence with
`portfolio2` at `tp_r=None` before reporting anything, and gives bounds rather than an estimate.

**Fixed: signals advertised a target the bot is designed to miss.** `tg.send_signal` rendered
the TP as the goal with an `R:R 1 : X` line; with a 3R backstop that would have published a
number every S2 trade intends never to reach. S2 signals now state the real mechanism —
trailing stop arms at +0.75R, floor once armed, backstop TP marked cancelled-on-arming.
S1 rendering byte-unchanged.

## v1.24.0 — 2026-08-02 — THE RATCHET RAN LIVE, AND ITS TRIGGER WAS SET TOO HIGH

**Stats:** live n went 1 → 4 (2W/2L). In R: +0.075R total, i.e. flat. Max drawdown 2.77%
of account. One parameter changed.

**The ratchet executed live for the first time — twice — and behaved exactly as specified.**
AVAX locked +1R at $6.2034 (exit +15.67%, rr 0.993) and ETH locked +1R at $1852.08 (exit
+17.38%, rr 0.985). Take-profit cancelled on both, no `position UNPROTECTED` line. The
naked-stop guard added on 07-30 was never needed but is now a live-exercised path. This is
the code carrying 100% of the strategy's measured edge, and it had never run before.

**Read live P&L in R, not leveraged %.** The dashboard's -6.14% is a reporting artifact:
`strategy2.signal` sizes leverage as `MAX_LEV_LOSS / sl_pct` so 1R always equals 20%, but
the exchange clamps it. ETH computed 15.9x and got 20x, so a 1R stop printed -26.1% instead
of -20%. Account risk was correct at ~1% throughout (size = `risk_usd` / stop distance), so
this is not a capital-risk bug — but it makes the published number move with whatever
leverage the exchange grants.

**`TRAIL_START_R` 1.0 → 0.75.** The comment justifying 1.0 quoted the Heikin-Ashi numbers
voided on 07-30 (+628.5%, maxDD -4.23%, 8-of-8 months). Same class of finding as 07-31: the
results were restated, the reasons underneath them were not. Re-derived in `portfolio2.py`
(capped book, real prices, fees, 20 coins, 5000 bars):

| TRAIL_START_R | n | WR | net | maxDD | EV | t |
|---|---|---|---|---|---|---|
| 0.65 | 120 | 57.5% | +34.49% | -6.10% | +0.287% | 2.30 |
| **0.75 (new)** | 118 | **55.9%** | +33.73% | **-9.02%** | +0.286% | **2.28** |
| **1.00 (old)** | 117 | 49.6% | +32.41% | -12.49% | +0.277% | 2.04 |
| 1.25 | 116 | 44.0% | +30.75% | -16.45% | +0.265% | 1.81 |

Net return barely moves; the gain is risk profile — **win rate +6.3pp, max drawdown ~28%
smaller**, return/maxDD 2.6 → 3.7. Mechanism: 18 of 118 trades (15.3%) peak between 0.5R and
1.0R and under a 1.0R trigger every one is a maximum loss. Arming below the take-profit
converts them, paid for by capping near-1.0R peaks at 0.75R.

Passed the standing rejection tests before deployment: better than 1.0 at concurrency cap 1,
2 and 4; better under alphabetical instead of `|stretch|` selection; out-of-sample t=2.36 vs
1.92; and the 0.65–0.85 band is flat, so it is a plateau not a spike. 0.70 scored best
(+36.72, t=2.43) and was deliberately **not** taken — picking the peak inside a flat band is
fitting noise. Caveats kept in plain sight: 4-of-7 positive months vs 5-of-7, and in-sample
ex-top5 is negative at every setting (the 07-30 tail-dependence is untouched).

**`TRAIL_STEP_R` deliberately unchanged.** Smaller is monotonically better (0.1 → +34.4%,
1.0 → +25.4%) with no plateau, so the optimum is at the boundary — a mechanical relationship,
not an edge, and chasing it adds stop-modification calls on the path whose failure mode was
only fixed on 07-30.

**Void-number cleanup.** `MAX_ADX` and `TP_R` comments still quoted HA-path figures as settled
fact; both now carry explicit warnings. **`TP_R = 1.0` has never been re-derived on real
prices** and is the largest remaining unvalidated constant.

**`test_ratchet.py`** now derives its arming cases from the constants instead of hard-coding
1.0 (its deliberate `assert ... update it if those change` guard did its job), plus a new
assertion that a sub-target peak leaves a profitable stop. 10/10 pass.

**New:** `sweep_ratchet.py` and `rej_check.py` — the derivation record, re-runnable.

## v1.21.0 — 2026-07-29 — THE BOT WAS FROZEN FOR 4 HOURS AND SYSTEMD SAID IT WAS FINE

**Stats:** 1 live trade (unchanged). Max drawdown 1.58%. No strategy parameter changed.

**Critical fix — a silent, unbounded freeze.**
`bot.log` had no entry between 2026-07-28 22:01:36 and 2026-07-29 02:07 UTC, while
`systemctl status` reported `active (running)` throughout. Both the main loop and the
tracker thread were parked in `wchan=wait_woken` — a blocking socket read with no timer,
which can never return on its own.

Root cause: `hyperliquid.api.API` defaults `timeout=None` and passes it straight into
`requests.post`, so a half-open socket blocks forever. `executor.py` built its `Info`
and `Exchange` without a timeout, and `indicators.py` rebuilt an untimed `Info` on every
candle fetch (20 coins x hourly — the most exposed surface in the codebase). The API sits
behind an nginx that 502s regularly, which is exactly what leaves half-open sockets behind.
- `HTTP_TIMEOUT = (5, 20)` (connect, read) now passed to every `Info`/`Exchange`.
- `executor._hl_call()` retries `requests` `Timeout`/`ConnectionError` with the same
  backoff it already used for 429/502/503/500 — that is the failure the timeout surfaces.

**New — hang watchdog (`live.py`).** The main loop calls `_beat()` every iteration; a
daemon thread checks every 60s and, if the loop has gone silent past its allowance, logs,
DMs the owner and exits hard so systemd (`Restart=always`, `RestartSec=30`) restarts it
with open trades restored from `state.json`. Allowance is 900s normally and is widened
around the calls that legitimately block: `nightly_review()` 4800s (`ai_brain` runs to
`BRAIN_TIMEOUT=3600`), `weekly_review()` and `version_push()` 1800s. Verified in both
directions before deploying: fires and exits 1 on a simulated 20-minute stall, and
survives 75s of healthy heartbeats with no false positive.

**Fix — `review.py`'s `_git()` had no timeout.** `git push` talks to the network. On its
own the new watchdog would catch a hang there, but `version_push()` runs at the same time
every night, so it would have become a restart loop rather than a one-off recovery.
Now `timeout=120` with a synthetic failure result.

**Fix — `_release_lock()` is ownership-aware.** It now only unlinks
`/tmp/getsignalz.pid` when the file still holds this process's own PID, so a dying
instance cannot disarm the lock of a newer one. Found while testing the watchdog, which
deleted the live bot's lockfile from a test subprocess.

**Impact of the outage:** 3 missed candle scans (~0.07 expected signals at the measured
0.53/day) and the 23:00 UTC nightly review, whose channel daily summary did not post on
07-28. The book was flat, so no capital was exposed — but strategy 2 exits exclusively
through its stop ratchet, so an open position would have had that ratchet frozen for the
full four hours.

**Unchanged, deliberately.** Live evidence is still one closed trade. Per-score win rate,
confluence attribution, trailing behaviour and coin EV all remain unanswerable, and the
07-28 bucket study over 110 backtested trades already showed no actionable gradient in
stretch, ADX or RSI depth. The 20-coin strategy-2 drift check reproduced cleanly
(24/7 n=121 WR 66.1% +67.73%; deployed n=109 WR 64.2% +57.52%), and
`apply_config_to_trader()` left `trader.py` byte-identical.

## v1.20.0 — 2026-07-28 — TRUE FILL PRICES + 24/7 COVERAGE FOR STRATEGY 2

**Stats:** 1 live trade (ARB, SL). Recorded P&L corrected -19.4% -> -13.1% by this release.

**Critical fix — closed trades were recorded at the wrong price.**
`live._check_closed()` took the exit price from `get_price(coin)`: the current mid at
the moment the bot noticed the position had gone, up to POLL=20s after the exchange
actually filled it. The first live strategy-2 trade exposed it. ARB's stop filled at
0.07817 (confirmed against Hyperliquid `user_fills`), but the mid 22 seconds later was
0.07767 — so the journal, the owner DM and the public channel post all reported a
-19.4% loss on a trade that really lost -13.1%, a 48% overstatement. The bias is
systematically worst on losses, because price keeps running after a stop is taken.
- New `executor.get_close_fill(coin, since_ms)` — size-weighted average of the fills
  that actually closed the position, or `None` when none can be attributed.
- New `live._exit_price()` uses it and falls back to the old mid behaviour on any
  failure, so a fills-API outage degrades to the previous accuracy instead of erroring.
- The stored ARB record was corrected in `journal.json`, `state.json` and
  `journal_s2.json` (each row keeps an `exit_corrected` note). Balance-derived stats
  (max drawdown 1.58%) were already right, which confirms the bug was narrowly the
  exit price and not the position accounting.

**Strategy 2 now scans on every hour, not just the session window.**
S2 was validated on every 1h bar 24/7, but its block sat behind `in_session()` — a
strategy-1 inheritance — so it only ran 11:00-23:59 UTC and produced 0.39 signals/day
against the >=1-signal-per-2-days requirement that is the entire reason S2 exists.
Moved above the gate: 0.53/day. Stated plainly, because it matters: the newly enabled
hours are the *weakest* block measured (n=30, WR 56.7%, EV +0.199%/trade, t=0.99 —
positive but statistically indistinguishable from zero) versus 11-24 (n=80, WR 67.5%,
EV +0.655%, t=4.43). This buys frequency at roughly break-even expectancy; it is not
an edge improvement, and it is the first thing to reconsider if live results disappoint.

**Position management no longer freezes during quiet hours.**
`_check_closed`/`_check_trail`/`_check_trail_s2` sat behind the 02:00-04:00 gate, and
that gate slept 600s. S2 exits *exclusively* through its stop ratchet, so an open trade
could not lock in profit for two hours a night. Management now runs ahead of the gate at
the normal 20s poll; the gate still pauses scanning, as intended, and logs once per night
instead of every cycle. Capital was never at risk here — the resting exchange stop always
sits underneath — this was lost upside only.

**Startup banner reported the retired engine.** It printed strategy 1's coin count, min
score and session window even with S1 disabled, so the log advertised a session window
the live engine no longer obeys. Now reports both engines and their real state.

**No strategy parameter changed.** Stretch / ADX / RSI-depth buckets over 110 backtested
trades show no actionable gradient. Notably the 4.0+ ATR stretch bucket that the live ARB
loss came from is the *best* one (+0.818%/trade), so that loss does not indict the entry
filter — it lost exactly its 1R budget (-0.98% of account against 1.0% risked), which is
the sizing working correctly.

---

## v1.32.0 — 2026-08-22

**Stats:** 16 trades · WR: 38% · P&L: -14.0%

**Code improvements (2):**
- live.py: S2 scanning bypassed the 3-hour post-SL cooldown that S1 already respects via already_open. A mean-reversion coin that is trending keeps printing extreme RSI readings after a stop-out and without this gate S2 can chain entries on the same coin in the same session, compounding losses on a directional move.
- analyze.py: SHORT signals fire at overbought RSI (75-85); _band([15,20,23], 80, ...) returns the last label '23-25' for any value above 23, bucketing all SHORT entries alongside nearly-oversold LONG entries. Adding direction awareness routes overbought SHORTs to their own bands so the accumulating per-condition EV stats remain interpretable as n grows.

---

## v1.31.1 — 2026-08-20

**Stats:** 16 trades · WR: 38% · P&L: -14.0%

No changes — all parameters within target bounds.

---

## v1.31.0 — 2026-08-18

**Stats:** 12 trades · WR: 42% · P&L: -31.0%

**Code improvements (2):**
- tracker.py: duration_h was never written to state.json closed_trades, so the public dashboard always showed 'Avg hold: 0.0h' and filtered it out of every avg-hold computation. The field is correctly populated in journal.json by log_trade_close but that path is separate from the state record the dashboard reads. Fix computes duration from opened_at (always a JSON string by this point) to the same utcnow() used for closed_at, so both timestamps are consistent.
- analyze.py: The [-1:] slice limited the scan census to only the newest rotated weekly log plus the current file — roughly 2 weeks of scan observations. With loguru retention='4 weeks' there are up to 4 rotated logs available; reading all of them multiplies the sample for feed-health frozen-bar percentages, gate admission rates, direction-bias counts, and contamination checks, all of which are accumulator statistics where every extra observation reduces noise. The overhead is a few extra sequential file reads once per nightly cycle.

---

## v1.30.2 — 2026-08-17

**Stats:** 12 trades · WR: 42% · P&L: -31.0%

No changes — all parameters within target bounds.

---

## v1.30.1 — 2026-08-16

**Stats:** 11 trades · WR: 36% · P&L: -51.1%

No changes — all parameters within target bounds.

---

## v1.29.0 — 2026-08-15

**Stats:** 11 trades · WR: 36% · P&L: -51.1%

**Code improvements (4):**
- tracker.py: If an S2 trade ever reaches its 5R backstop TP, _trail_tp fires in the tracker thread, cancels ALL reduce-only orders (including the ratcheted stop from _check_trail_s2), and places a fixed 8%-from-peak trailing stop — overriding the calibrated S2 exit entirely. The guard eliminates that interference with one line.
- analyze.py: _edge_confidence used a date-only join to match trades to signals while full_report already uses _sig_for's 2h timestamp-nearest window. A date-only join misattributes when two signals for the same coin land on the same calendar day, producing a wrong edge-confidence score in the Trust Score.
- journal.py: journal.json trades have no strategy tag, so full_report's per-coin EV table, R-multiple distribution, and direction split all blend S1 and S2 data silently once both engines trade. Adding a strategy parameter with default 'S1' is fully backward-compatible with the existing S1 caller; the companion live.py change tags S2 trades correctly going forward.
- live.py: Companion to the journal.py change: the S2 trade-open path must pass strategy='S2' so all future journal records are tagged by engine. The S1 call site already defaults to 'S1' and requires no change.

---

## v1.28.0 — 2026-08-14

**Stats:** 11 trades · WR: 36% · P&L: -51.1%

**Code improvements (1):**
- live.py: _check_trail_s2 has two sequential guards that both fail on a restored position: (1) t.get('strategy') != 'S2' short-circuits to skip when strategy is absent from the dict, and (2) t.get('R') or 0 returns 0 because R is never persisted to state.json (it lives only in memory). Both guards must pass for the ratchet to run. strategy and sl_orig are already saved to state.json by register_position so t.get() recovers them correctly; R must be recomputed as abs(entry - sl_orig) since it was never stored. locked_r is also saved by update_trail whenever a ratchet step fires, so it restores cleanly with a 0.0 default for pre-ratchet positions. Without this fix, every S2 trade that survives a nightly auto-restart loses its ratchet management silently for the rest of its life.

---

## v1.27.0 — 2026-08-13

**Stats:** 11 trades · WR: 36% · P&L: -51.1%

**Code improvements (2):**
- tracker.py: Adds a backward-compatible locked_r parameter so S2 can persist both the new SL and the exact locked-R amount in one call. All existing S1 call sites pass nothing and are unaffected. Without this, state.json never received the ratcheted SL, so the dashboard always showed the original stop as the live protection level.
- live.py: Wires S2 ratchet fires into tracker.update_trail — the call S1 already makes but S2 was missing. This persists the ratcheted SL and locked_r to state.json immediately on every successful update_sl(), fixing: (1) the live Telegram message showing the wrong stop price and no lock indicator, (2) the dashboard lock badge never appearing for S2 positions, and (3) bot restarts restoring _open_trades from the stale pre-ratchet SL rather than the actual ratcheted level.

---

## v1.2.7 -- 2026-06-19

**Stats:** 33 trades, WR: 32%, P&L: +339.7%

**Critical fix (2):**
- ROOT CAUSE FOUND: review.py _self_improve() was auto-bumping MIN_SCORE +1 each night when overall WR < 50%. This ran at 23:00 UTC, overriding every manual fix. CHANGELOG v1.2.5/v1.2.6 entries (MIN_SCORE 8->9, WR=31%) were generated by this heuristic. 8 consecutive sessions all claimed to fix MIN_SCORE to 8 and were silently reverted overnight.
- FIX 1: review.py MIN_SCORE auto-adjust DISABLED. Blended WR is wrong metric. Per-score EV only: score8 EV=+31.8% vs score9 EV=+8.7%.
- FIX 2: MIN_SCORE=8 in both strategy_config.json and trader.py. Verified in bot.log 02:04:41 UTC: Min score: 8/13.

---

## v1.18.0 — 2026-07-24 — STRATEGY REBUILD

**Stats:** 0 trades (fresh regime, reset 2026-07-23) · previous regime: 68 trades, WR 33%, +205.1%

**Full core strategy replacement.** Kamran described his actual manual trading process in detail — wait for a liquidity pool (LP) with high probability of being reached, confirm the path to it is clean of obstacles that could react first and stop-hunt, enter precisely off a nearby order block or FVG, size risk for a ~20% max stop, minimum 1:2 R:R, trail through progressive targets toward the far edge of an FVG as the final target. This did not match the live CM Sling Shot (EMA cloud) system at all — that system was built 2026-06-25 from a shorter, less accurate description.

**Code changes:**
- `indicators.py`: new `liquidity_pools()` — a bar-by-bar port of the Liquidity Pools zone-tracking logic in `ind.txt` (Kamran's own Pine Script), never previously ported. Wick-rejection contact counting, zone confirmation, 2-consecutive-close mitigation.
- `trader.py`: full internal rewrite (same public interface — `find_best_setup`, `score_setup`, `build_df`, `WATCHLIST`, etc. all unchanged, so `live.py`/`tracker.py`/`ai_brain.py` needed zero changes). Entry = confirmed rejection off a nearby order block/LP zone (price wicked in, closed back out — not bare proximity, see bug note below). Target = an opposite-type liquidity pool farther out, with a path-clean check against other unmitigated OB/FVG zones in between. Trend filter switched from EMA-cloud direction to market-structure direction (`order_blocks()`'s `struct`). `MAX_LEV_LOSS` tightened 25%→20% (Kamran's stated number; `executor.py`'s independent 25% safety clamp is untouched, unrelated defense-in-depth).
- `backtest.py`: updated to match (macro-trend calc switched from EMA-cloud to structure-based, matching `trader.py`'s new `get_macro_trend()`).

**Bugs found and fixed during backtesting (before going live):**
- Entries were firing on bare proximity to a zone with no confirmation it was actually holding — the exact "aggressive entry" failure mode already proven to lose ~-10.4%/trade in the old system. Fixed to require a rejection candle (wicked into the zone, closed back out) before entering.
- `order_blocks()` only flags the bar an OB formed on with no mitigation tracking — order blocks from months ago that price had long since closed through were still being treated as live entry triggers. Added mitigation filtering.
- FVG target-extension had the direction backwards (was targeting a same-type gap instead of the opposing-type "unfinished business" gap Kamran described) and had the same missing-mitigation-check bug as the order blocks. Fixed both.
- The path-clean/confluence check was only looking at other liquidity pool zones, not order blocks or FVGs — despite Kamran explicitly describing OB/FVG confluence as part of what raises a target's credibility. Fixed to include both.
- `backtest.py`'s trade-qualification check compared the raw `score_setup()` output against `MIN_SCORE`, but the live path adds a timeframe-strength bonus before that comparison — the two were silently using different bars. Promoted the bonus table to a shared `trader.TF_BONUS` constant both paths import, so it can't drift again.

**Verification:** backtest across the full watchlist after fixes: 15 simulated trades, blended positive EV (concentrated in 2 coins, thin sample — not proof of an edge, but not broken either). Live watchlist scan completed cleanly with no qualifying setup at deploy time (expected — not every scan should fire). Book was flat at restart.

---

## v1.17.0 — 2026-07-23

**Stats:** 68 trades · WR: 33% · P&L: +205.1%

**Code improvements (6):**
- tracker.py: dashboard's pair count was hardcoded ("13 pairs") — this exact bug class has already recurred twice before (14→13, 15→14 stale counts), and regressed again after tonight's BNB removal. Now derives from `len(WATCHLIST)` so it can't go stale again.
- ai_brain.py: the nightly AI code-improvement prompt was reasoning from a stale, wrong snapshot — hardcoded `MIN_SCORE=8 ... DO NOT change it` (real value is 7) and an EV table from the pre-06-25 scoring system, plus a `score/14` display when the real max is 8. Now reads MIN_SCORE live from strategy_config.json and instructs the AI to compute fresh EV from actual journal data instead of trusting a frozen example.
- Removed `claude_brain.py` and `runner.py` — both dead code, confirmed unreferenced anywhere (grep across all `.py`/`.sh`, cron, and the systemd unit). `claude_brain.py` was already marked superseded in `.gitignore`; `runner.py` predates `live.py` and imports functions that no longer exist.
- Wired up `result_card.py` (a fully-built, previously-unused Hyperliquid-style PnL card generator) into `tracker.py`'s close flow — closed trades now post a polished visual card to the channel alongside the existing text notification, via a new `_send_photo()` helper. Wrapped in try/except so a card failure can never block the text close notification.
- backtest.py rebuilt from scratch: the old version tested a completely different, no-longer-live scoring system (OB/RSI/SSL/UT-Bot/FVG combos, pre-06-25). It now walks historical candles through the actual live `trader.build_df()`/`score_setup()` directly — no reimplemented logic to drift out of sync — turning "wait weeks for live n≥5 per bucket" into a same-day historical read across months of data. Verified against ETH/SOL: all simulated trades correctly bracket entry/SL/TP by direction and respect TP_RATIO.
- New Trust Score (0-100) in analyze.py: five-pillar composite (statistical edge confidence, risk control, system reliability, strategy stability, watchlist health) computed from data already logged (journal.json, state.json, CHANGELOG.md, selflearn.log, bot.log). Surfaced on the pinned dashboard and in the weekly review post; history saved to journal.json's previously-unused `reviews` list.

---

## v1.16.0 — 2026-07-23

**Stats:** 68 trades · WR: 33% · P&L: +205.1%

**Parameter changes (1):**
- WATCHLIST: removed BNB (5T/0%WR/-10.3% avg — crossed the 5-trade removal bar tracked since 07-18, same evidence bar used to remove RUNE on 07-15)

**Code improvements (1):**
- self_improve.sh: the nightly 02:00 UTC self-learn session — the one that tracks and acts on watchlist removals like BNB above — had been silently failing for 3 consecutive nights (`claude: command not found` Jul 21/22 from a bare cron PATH, then `OAuth session expired` Jul 23), which is why BNB's removal went unactioned until caught manually tonight. Pinned an explicit PATH, resolved the claude binary via `command -v` with a hard failure message if still missing, and added a Telegram DM alert to the owner on any future failure (bad exit code or auth/PATH/session-limit error pattern in the output) so a multi-night silent gap can't happen again unnoticed.

---

## v1.15.4 — 2026-07-22

**Stats:** 68 trades · WR: 39% · P&L: +209.0%

No changes — all parameters within target bounds.

---

## v1.15.3 — 2026-07-21

**Stats:** 66 trades · WR: 38% · P&L: +195.3%

No changes — all parameters within target bounds.

---

## v1.15.2 — 2026-07-20

**Stats:** 66 trades · WR: 38% · P&L: +195.3%

No changes — all parameters within target bounds.

---

## v1.15.1 — 2026-07-19

**Stats:** 64 trades · WR: 40% · P&L: +216.5%

No changes — all parameters within target bounds.

---

## v1.15.0 — 2026-07-18

**Stats:** 64 trades · WR: 40% · P&L: +216.5%

**Code improvements (1):**
- executor.py: query_order_by_oid likely throws AttributeError in the current HL SDK, so the except block fires on every SL trail update. With pass, execution falls through to exchange.cancel() and removes ALL reduce-only orders including the resting TP — leaving positions unprotected at 1:1R and 1.5:1R trail stages. With continue, an unidentifiable order is skipped: the worst outcome is a stale wide-SL order that never triggers (the tighter new SL fires first), whereas the current worst outcome is silently deleting the TP and letting a winning position reverse through breakeven.

---

## v1.14.1 — 2026-07-17

**Stats:** 63 trades · WR: 39% · P&L: +205.2%

No changes — all parameters within target bounds.

---

## v1.14.0 — 2026-07-16

**Stats:** 62 trades · WR: 38% · P&L: +204.8%

**Code improvements (4):**
- live.py: During signal droughts every candle log shows only RSI/ADX/SSL with no indication of how close each coin is to a signal. Adding '[6/8]' for near-misses immediately distinguishes 'ADX too low so score=0' from 'coin scored 6, one factor short of MIN_SCORE=7' — enabling correct diagnosis without manual indicator inspection.
- journal.py: Trailing-stop exits are stored as result='sl' with positive lev_pct. The result=='tp' filter silently drops every trail-win from the daily review W count, showing an understated WR in the channel. Matches the fix already applied in tracker.py close_position which uses lev_pct>0 as the authoritative win definition.
- journal.py: Same trail-win misclassification bug in get_week_summary — weekly review WR, avg_win, and avg_loss shown in the channel are all wrong whenever trailing stops exit profitably. Also fixes the win_reasons / loss_reasons signal attribution so the 'Best signals' section correctly includes trail-wins when identifying which confluence factors actually performed.
- tracker.py: RUNE was removed from WATCHLIST on 2026-07-10, reducing the watched coins from 14 to 13. The pinned dashboard has shown a stale count for 6 days.

---

## v1.13.2 — 2026-07-15

**Stats:** 62 trades · WR: 38% · P&L: +204.8%

No changes — all parameters within target bounds.

---

## v1.13.1 — 2026-07-14

**Stats:** 62 trades · WR: 38% · P&L: +204.8%

No changes — all parameters within target bounds.

---

## v1.13.0 — 2026-07-13

**Stats:** 62 trades · WR: 22% · P&L: +204.8%

**Code improvements (3):**
- review.py: Profitable trail-SL exits carry result='sl' and were excluded from both wins and losses, deflating the reported WR by ~15pp and corrupting the RISK_PCT ratchet logic and nightly DM stats. Matches the already-correct lev_pct>0 logic in tracker.close_position.
- review.py: Same misclassification in the confluence signal quality analysis: factors that appeared in profitable trail exits were miscounted as losses, corrupting the 'Best confluence signals' insight table in the nightly DM.
- tracker.py: WATCHLIST has 14 coins; dashboard displayed 15 since before APT was added or a coin was removed.

---

## v1.12.1 — 2026-07-12

**Stats:** 61 trades · WR: 23% · P&L: +222.3%

No changes — all parameters within target bounds.

---

## v1.12.0 — 2026-07-11

**Stats:** 58 trades · WR: 24% · P&L: +232.9%

**Code improvements (1):**
- executor.py: 2% SL limit buffer directly caused ETH -54.5% loss: SL trigger was correctly capped at 1.29% from entry (25% MAX_LEV_LOSS ÷ 20x), but the fill-limit was set 2% below the trigger, allowing execution up to 3.29% from entry × 20x = 65.8% leveraged loss. Price fell only 1.44% past trigger — within the 2% window — producing the observed 54.6% loss. ETH/BNB/INJ spreads are 0.01-0.05%; 0.3% buffer provides ample room for normal fills while keeping worst-case SL execution at (cap_pct + 0.3%) × leverage ≈ 31% max instead of 65%. Also tightens TP fills to capture closer to the intended reward price.

---

## v1.11.1 — 2026-07-10

**Stats:** 58 trades · WR: 24% · P&L: +232.9%

No changes — all parameters within target bounds.

---

## v1.11.0 — 2026-07-09

**Stats:** 55 trades · WR: 23% · P&L: +219.6%

**Parameter changes (1):**
- MIN_ADX 35→40  (low-ADX WR=13% < 35% on 15 trades)

**Code improvements (1):**
- trader.py: RUNE showed frozen data (identical $0.3982, RSI 84, ADX 38) for 3 consecutive hourly candles in today's logs (20:00–22:00 UTC), reproducing the Jul 5 staleness incident. The 5-bar guard misses freezes shorter than 5 candles. Three consecutive identical real closes is impossible in a liquid market; tightening to 3 bars reliably rejects frozen feeds without false-positives on legitimate consolidation.

---

## v1.10.1 — 2026-07-08

**Stats:** 52 trades · WR: 25% · P&L: +240.7%

**Parameter changes (1):**
- MIN_ADX 30→35  (low-ADX WR=17% < 35% on 6 trades)

---

## v1.10.0 — 2026-07-07

**Stats:** 47 trades · WR: 24% · P&L: +263.9%

**Code improvements (2):**
- live.py: abs(price - entry) is symmetric: with TP_RATIO=2.0 the risk distance equals the SL distance exactly, so the breakeven trail fires when price drops to the SL level just as it would when price rises to 1R profit. Today's SOL trade confirmed this precisely — trail fired at 20:36 when price reached ~$80.237 (= entry - 0.810 = SL), cancelled the original SL and placed a new one at entry $81.047, which immediately filled with slippage at ~$80.08, producing -11.9% instead of a clean -10% SL exit. Replacing abs() with (price - entry) * direction ensures the trail only activates when price has moved in the profitable direction.
- trader.py: Guards against frozen testnet feeds. RUNE showed identical price $0.3874, RSI 25, ADX 57 at both 20:01 and 21:01 today — repeating the Jul 5 freeze that ran 16:00-23:00. Frozen candles produce no new EMA crossings so signal risk is low, but they waste scan cycles and can produce misleading indicator readings if the feed resumes mid-bar. Returning None on 5 consecutive identical real closes skips the coin silently, consistent with how fetch_candles already handles missing data.

---

## v1.9.4 — 2026-07-06

**Stats:** 42 trades · WR: 27% · P&L: +321.6%

No changes — all parameters within target bounds.

---

## v1.9.3 — 2026-07-05

**Stats:** 38 trades · WR: 28% · P&L: +330.4%

**Parameter changes (1):**
- SESSION start 11:00→12:00 UTC  (WR=0% < 25%)

---

## v1.9.2 — 2026-07-04

**Stats:** 35 trades · WR: 29% · P&L: +309.2%

No changes — all parameters within target bounds.

---

## v1.9.1 — 2026-07-03

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

No changes — all parameters within target bounds.

---

## v1.9.0 — 2026-07-02

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Code improvements (1):**
- live.py: Extends per-coin SL cooldown from 1 candle (1h) to 3 candles (3h). Trade history shows RUNE SL×3 in sequence, ATOM SL×2 with the second at 0h duration, kPEPE SL×2 in rapid succession — all are re-entries within the 1h window that compounded the same directional loss. Three candles of separation gives the market time to print a new structure before the scoring system can re-qualify the coin, without tightening any signal quality criteria.

---

## v1.8.1 — 2026-07-01

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

No changes — all parameters within target bounds.

---

## v1.8.0 — 2026-06-30

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 1.75→1.5  (TP hit rate=30% < 30% — target too far)

**Code improvements (2):**
- trader.py: score_setup computed TP at hardcoded 2.0× SL while executor places the real order at TP_RATIO=1.5×. The signal Telegram message advertised a TP that the exchange never targeted, then the live position message showed a different (closer) TP — visibly confusing for channel followers. Aligning to TP_RATIO fixes the discrepancy so signal TP equals actual TP. The leverage formula simplifies to MAX_LEV_LOSS/sl_pct which is mathematically identical to the old 50/tp_pct when tp_pct=sl_pct×2.0, so leverage values are unchanged — only the TP price and displayed R:R become accurate.
- tracker.py: Telegram returns 429 with a retry_after parameter when edits are too rapid; the trail engine can fire multiple SL updates per minute across positions. Without retry these edits silently fail, leaving the channel message showing a stale SL price even though the exchange order was correctly updated. The fix adds up to 3 retries respecting the server backoff (capped at 10s), runs entirely inside the background tracker thread so sleep() does not touch the main trading loop.

---

## v1.7.0 — 2026-06-29

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)

**Code improvements (1):**
- trader.py: score_setup() already computes tp_pct using a hard-coded 2.0x SL multiple (raw_tp_pct = sl_pct * 2.0), and live.py's _check_trail() assumes risk = abs(tp-entry)/2.0 for its breakeven/0.5R trail logic. With TP_RATIO=1.75, executor.open_trade() recalculates the live TP at 1.75x instead of 2.0x, mismatching the scored setup and feeding _check_trail() a TP that doesn't match its 2R assumption — shifting the breakeven/0.5R trail triggers off their intended price.

---

## v1.6.1 — 2026-06-28

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)

---

## v1.6.0 — 2026-06-27

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)

**Code improvements (1):**
- trader.py: score_setup signals and gates on 2.0× RR; executor was placing actual TP orders at 1.75×, causing every TP hit to earn 14.3% less R than displayed and making breakeven trail fire 0.125R early

---

## v1.5.1 — 2026-06-26

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)

---

## v1.5.0 — 2026-06-25

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)

**Code improvements (1):**
- trader.py: Recent losses are concentrated at extreme RSI: RUNE shorted at RSI 17 and RSI 20 (five consecutive hours), SOL at RSI 24, ARB at RSI 29 — all immediate SL hits as price bounced from oversold exhaustion. Williams %R at -80 did not catch these because it can diverge from RSI in strongly trending but exhausted price action. Rejecting shorts when RSI < 25 removes the highest-reversal-risk entries without materially reducing signal frequency on the current watchlist.

---

## v1.4.0 — 2026-06-23

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Code improvements (2):**
- trader.py: Merges the two separate short RSI gates (25 unconditional, 32 with ADX≤40) into a single cleaner threshold of 35 with no ADX exception. The ADX exception was the loophole: high-ADX oversold shorts (ARB RSI=29 ADX=55, kPEPE RSI=32 ADX=79, AVAX RSI=34 ADX=34) are consistently the worst performers — strong trends produce the largest counter-bounces when RSI is already deeply depressed. All profitable shorts had RSI ≥50. Raising the floor to 35 blocks the losing class without touching any winner.
- tg.py: UT Bot (weight 0) and trendline break (weight 0) were removed from scoring last session, dropping the maximum achievable score from 14 to 11. The signal card still displayed /14, making a score of 9 look weaker than it is (64% of max vs the true 82%). The logger in live.py already shows /11 correctly; this syncs the public Telegram post.

---

## v1.3.1 — 2026-06-22

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

No changes — all parameters within target bounds.

---

## v1.3.0 — 2026-06-21

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (1):**
- TP_RATIO 1.75→1.5  (TP hit rate=30% < 30% — target too far)

**Code improvements (1):**
- executor.py: The old formula produced a constant ~12.5% account loss per SL hit because leverage and position size were coupled: with the leverage formula in trader.py (leverage = 50/tp_pct, tp_pct = sl_pct×2), the product sl_pct × leverage × 0.5 always ≈ 12.5% regardless of stop distance. The fix uses risk_usd (already computed as account×RISK_PCT=2% in live.py) divided by sl_pct to target exactly 2% account risk per trade — an 84% reduction. The cap at account×leverage×0.5 ensures no position exceeds the old maximum, preserving safety on very tight stops.

---

## v1.2.9 — 2026-06-20

**Stats:** 34 trades · WR: 30% · P&L: +329.1%

**Parameter changes (2):**
- TP_RATIO 2.0→1.75  (TP hit rate=30% < 30% — target too far)
- SESSION start 10:00→11:00 UTC  (WR=0% < 25%)

---

## v1.2.8 — 2026-06-19

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---


## v1.2.3 — 2026-06-16

**Stats:** 33 trades · WR: 32% · P&L: +339.7%

**Critical fix (1):**
- MIN_SCORE 10->8. Root cause: a flawed nightly heuristic (global WR<50% -> bump MIN_SCORE) had ratcheted the threshold up for 6 straight sessions (8->9->9->9->9->10->10), directly contradicting the per-score EV table that every session recomputed and confirmed: score 8 EV +31.8% vs score 9 +8.7% vs score 10 +4.6% (3-7x worse). Combined with the live.py losing-streak circuit breaker (+2 active), effective MIN_SCORE had reached ~12/13, explaining zero new trades for several days despite daily ADX>30 conditions. Reverted to the evidence-backed value. New rule: MIN_SCORE changes must be justified by the per-score EV table, never by overall win rate alone.

---

## v1.2.6 — 2026-06-18

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---

## v1.2.5 — 2026-06-17

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---

## v1.2.4 — 2026-06-16

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---

## v1.2.2 — 2026-06-15

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

No changes — all parameters within target bounds.

---

## v1.2.1 — 2026-06-14

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 9→10  (WR=31% < 50% on 26 trades)

---

## v1.2.0 — 2026-06-13

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

**Code improvements (1):**
- trader.py: With 8 consecutive losses and -78.2% drawdown the bot needs a circuit-breaker. This reads the last 8 closed trades from state.json once per candle (hourly, negligible I/O) and raises the required score by +1 when 4+ are losses and +2 when 6+. At the current 8/8 loss streak it requires score≥11 to fire, cutting out marginal 9-10 point setups until the market regime proves itself. The bump self-heals automatically as winning trades accumulate — no manual reset needed.

---

## v1.1.3 — 2026-06-11

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---

## v1.1.2 — 2026-06-10

**Stats:** 33 trades · WR: 31% · P&L: +343.6%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=31% < 50% on 26 trades)

---

## v1.1.1 — 2026-06-09

**Stats:** 32 trades · WR: 32% · P&L: +356.1%

**Parameter changes (1):**
- MIN_SCORE 8→9  (WR=32% < 50% on 25 trades)

---

## v1.1.0 — 2026-06-08

**Stats:** 32 trades · WR: 32% · P&L: +356.1%

**Parameter changes (2):**
- MIN_SCORE 8→9  (WR=32% < 50% on 25 trades)
- MAX_TRADES 2→1  (concurrent loss rate=100% > 40%)

**Code improvements (5):**
- trader.py: The old gate blocked oversold shorts only when ADX<=40, so RUNE RSI=17 ADX=66 slipped through and hit SL. RSI<=25 means the downward move is already severely extended regardless of trend strength — blocking unconditionally removes these low-probability continuation entries.
- trader.py: RUNE's 25-second SL hit had a stop only 0.28% from fill — any spread, slippage, or single tick against the position guaranteed immediate closure. A 0.4% minimum raw distance ensures the trade has room to breathe; below this threshold the setup is indistinguishable from noise.
- live.py: Adds the module-level dict used by the two changes below to track per-coin SL cooldown expiry times.
- live.py: Records a 60-minute cooldown whenever a coin hits SL. This directly breaks the RUNE×4, kPEPE×4, and ATOM×2 consecutive-loss chains where the bot re-entered the same failing setup on the very next candle.
- live.py: Treats cooled-down coins as if they were open positions so find_best_setup skips them for the full 60-minute window after a SL hit.

---

## v1.0.1 — 2026-06-07

**Stats:** 0 trades · WR: 0% · P&L: +0.0%

**Parameter changes (2):**
- MIN_SCORE 7→8 (score-7 trades avg -14.4%, score-8 EV=+44%)
- WATCHLIST: suspended kPEPE (4 trades, 0% WR, -12.2% avg)

---

## v1.0.0 — 2026-06-06

Initial release. Autonomous ETH/crypto scalper on Hyperliquid with:
- Telegram signal channel integration
- 25% leveraged risk cap per trade
- Trailing stop-loss (breakeven → 0.5R lock)
- 8-parameter nightly rule-based tuner
- AI-powered nightly code improvement (Claude)
- Correlation guard, session filter, watchlist suspension

---
