<div align="center">

[![English](https://img.shields.io/badge/🇬🇧-English-2ea44f?style=for-the-badge)](README.md)
[![فارسی](https://img.shields.io/badge/🇮🇷-فارسی-555?style=for-the-badge)](README.fa.md)

# GetSignal AI

**Autonomous trading agent for Hyperliquid perpetuals**

[![Channel](https://img.shields.io/badge/Telegram-@GetSignalAI-229ED9?style=flat-square&logo=telegram)](https://t.me/GetSignalAI)
![Exchange](https://img.shields.io/badge/Exchange-Hyperliquid-000?style=flat-square)
![Mode](https://img.shields.io/badge/Mode-Testnet-orange?style=flat-square)

</div>

---

## What it does

Scans 20 liquid perpetuals every hour, opens positions with automatic stop-loss
and take-profit, posts each signal to a public Telegram channel, and updates
that message live until the trade closes. Every night it reviews its own
results and can rewrite its parameters and code.

---

## Strategy

**Mean reversion at extremes.** Crypto overshoots on liquidation cascades; the
snap back is tradeable, but only when no trend is driving the move.

A position opens when all three hold on a closed 1h candle:

| Condition | Threshold |
|---|---|
| RSI at an extreme | ≤ 25 (long) · ≥ 75 (short) |
| Price stretched from its mean | ≥ 1.5 ATR from EMA(100) |
| Market not trending | ADX < 25 |

**Exit — progressive risk-free ratchet.** At +1R the resting take-profit is
cancelled, the stop locks at +1R, and from there it ratchets up 0.25R for every
0.25R gained. The position exits only when that stop is taken out, so once armed
the trade cannot return less than +1R. A take-profit still rests at 3R purely as
a backstop in case the bot dies mid-trade — it is not the intended exit.

---

## Risk

| Parameter | Value |
|---|---|
| Risk per trade | 1% of account |
| Max concurrent positions | 2 |
| Max leveraged loss per trade | 20% |
| Take-profit backstop | 3R |
| Max leverage | 25x |
| Stop-loss | 1.5 × ATR |
| Timeframe | 1h |

Position size is derived from stop distance (`notional = risk / stop%`), so
every stop-out costs the same 1% regardless of how wide the stop is.

---

## Live results

Real trades on the exchange. Every one is posted to the channel when it opens,
updated while it runs, and left there when it closes — including the losers.

| Metric | Value |
|---|---|
| Closed trades | 5 |
| Win rate | 60% (3W / 2L) |
| Total | +7.14% leveraged · +$5.46 |
| Avg R:R | +0.16R |
| Since | 2026-07-27 |

**Five trades proves nothing.** It is posted because it is real, not because it
is significant. Judge this again at 30+.

## Backtest

20 coins, ~7 months, exchange fees included, one position per coin, capped at 2
concurrent — the same limits the live bot runs under.

| Model | Net (account) | Win rate |
|---|---|---|
| Optimistic fill | +40.5% | ~58% |
| Pessimistic fill | +14.5% | ~54% |

The two rows bracket how a trailing stop can fill in reality: the ratchet raises
the stop the moment price prints a level, and whether that stop then survives
the same candle is not knowable from hourly bars. Live execution sits somewhere
between them.

> Earlier versions of this file quoted 67.7% win rate and −4.2% drawdown. Those
> came from a backtest that drove the ratchet off bar highs, which credits wicks
> the bot could never have filled, and from Heikin-Ashi rather than real prices.
> Both were wrong and the figures are void. The numbers above are what survived
> the correction.

---

## Architecture

```
live.py         main loop, entries, stop ratchet, nightly triggers
strategy2.py    signal logic and parameters
executor.py     Hyperliquid order placement
tracker.py      live message updates, dashboard, closed-trade stats
backtest2.py    single-coin backtest engine
portfolio2.py   portfolio simulation with the live concurrency cap
sweep_*.py      parameter sweeps (TP, ratchet, fill model)
test_ratchet.py unit tests for the exit ladder
s2_report.py    per-trade CSV + monthly report export
indicators.py   RSI, ATR, ADX, EMA, order blocks, liquidity pools
tg.py           Telegram formatting
journal.py      trade history
review.py       nightly/weekly review and parameter tuner
ai_brain.py     nightly AI code review
```

`trader.py` and `backtest.py` hold a retired liquidity-pool strategy, kept
switchable via `S1_ENABLED` in `live.py`.

---

## Setup

```bash
pip install hyperliquid-python-sdk eth-account loguru requests pandas numpy

cp config.example.py config.py    # then fill in your keys
python live.py
```

`config.py` is gitignored and never committed.

```python
HYPERLIQUID_PRIVATE_KEY = "0x..."
HYPERLIQUID_ACCOUNT     = "0x..."
USE_TESTNET             = True

TELEGRAM_TOKEN          = "..."
TELEGRAM_CHANNEL        = "@YourChannel"
TELEGRAM_OWNER_ID       = "..."
```

---

## Nightly self-learning

At 02:00 UTC the agent reviews its own performance, tunes parameters against
its trade history, and validates every proposed code change (syntax check,
secrets untouched) before applying it. Changes are versioned and pushed with a
changelog; the bot restarts itself only when no position is open.

---

<div align="center">
<sub>Testnet. Not financial advice.</sub>
</div>
