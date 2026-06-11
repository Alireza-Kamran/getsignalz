# Changelog

All nightly improvements are logged here automatically.

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
