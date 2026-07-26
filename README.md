<div align="center">

[![English](https://img.shields.io/badge/🇬🇧-English-2ea44f?style=for-the-badge)](README.md)
[![فارسی](https://img.shields.io/badge/🇮🇷-فارسی-555?style=for-the-badge)](README.fa.md)

# GetSignalz

**Autonomous trading agent for Hyperliquid perpetuals**

[![Channel](https://img.shields.io/badge/Telegram-@GetSignalz-229ED9?style=flat-square&logo=telegram)](https://t.me/GetSignalz)
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

**Exit — progressive risk-free ratchet.** Take-profit sits at 1R. On reaching
it the TP is cancelled, the stop locks at 1R, and it ratchets up another 0.25R
for every 0.25R the trade gains. The position then exits only when that stop is
taken out. The floor never moves below 1R once armed, so this adds upside
without giving back certain profit.

---

## Risk

| Parameter | Value |
|---|---|
| Risk per trade | 1% of account |
| Max concurrent positions | 2 |
| Max leveraged loss per trade | 20% |
| Max leverage | 25x |
| Stop-loss | 1.5 × ATR |
| Timeframe | 1h |

Position size is derived from stop distance (`notional = risk / stop%`), so
every stop-out costs the same 1% regardless of how wide the stop is.

---

## Measured performance

Backtest over ~8 months, 20 coins, exchange fees included:

| Metric | Value |
|---|---|
| Trades | 133 |
| Win rate | 67.7% |
| Frequency | ~19 / month |
| Max drawdown | −4.2% |
| Months positive | 8 / 8 |
| Out-of-sample win rate | 63.6% |

All 13 parameter variations tested positive, so the result is not fitted to a
single lucky value.

> **This is backtest data.** Slippage is not modelled and there is no live
> track record yet. Treat it as a validated hypothesis, not a proven edge.

---

## Architecture

```
live.py         main loop, entries, stop ratchet, nightly triggers
strategy2.py    signal logic and parameters
executor.py     Hyperliquid order placement
tracker.py      live message updates, dashboard, closed-trade stats
backtest2.py    backtest engine for the live strategy
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
