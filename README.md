<div align="right">
  <a href="README.fa.md"><img src="https://img.shields.io/badge/Docs-Persian-blue?style=for-the-badge" alt="Persian / فارسی"></a>
</div>

# GetSignalz — Hyperliquid Trading Agent

**Channel:** [@GetSignalz](https://t.me/GetSignalz)  
**Exchange:** Hyperliquid (perpetuals)

---

## Overview

An autonomous AI trading agent that runs 24/7 on Hyperliquid, posts live trade signals to a public Telegram channel, and rewrites its own strategy and code every night based on performance data.

**Key features:**
- Automated entry, stop-loss, and take-profit on Hyperliquid
- Real-time signal posts to Telegram with live P&L updates
- Leveraged risk capped at 25% per trade (enforced in code)
- Nightly self-learning: agent reviews all trades and rewrites its own parameters and code
- Trailing stop-loss to lock in profits
- Correlation guard: no two correlated positions open simultaneously

---

## Architecture

```
live.py          — main loop, session gating, trailing stop, nightly trigger
trader.py        — signal generation, indicator scoring, trade sizing
executor.py      — Hyperliquid SDK wrapper, order placement
tracker.py       — open-trade monitor, close detection, P&L calculation
tg.py            — Telegram message formatting and posting
journal.py       — trade history persistence
review.py        — nightly/weekly review + 8-rule parameter tuner
ai_brain.py      — AI-powered nightly code and strategy self-improvement
analyze.py       — historical data analysis
indicators.py    — technical indicators (RSI, MACD, ADX, BB, EMA, volume)
backtest.py      — backtesting engine
```

---

## Setup

### 1. Prerequisites

```bash
pip install hyperliquid-python-sdk eth-account loguru requests
```

### 2. Configuration

```bash
cp config.example.py config.py
# Edit config.py and fill in your keys
```

`config.py` is gitignored and will never be committed.

```python
# config.py
HYPERLIQUID_PRIVATE_KEY = "0xYOUR_PRIVATE_KEY"
HYPERLIQUID_ACCOUNT     = "0xYOUR_ACCOUNT_ADDRESS"
USE_TESTNET             = True   # False for mainnet

TELEGRAM_TOKEN          = "YOUR_BOT_TOKEN"
TELEGRAM_CHANNEL        = "@YourChannel"
TELEGRAM_OWNER_ID       = "YOUR_TELEGRAM_USER_ID"
```

### 3. Run

```bash
python live.py
```

---

## Risk Management

| Parameter | Value |
|-----------|-------|
| Max leveraged loss per trade | 25% |
| Max concurrent trades | 2 |
| Min signal score | 7 / 14 |
| Min ADX | 30 |
| Session hours (UTC) | 07:00 – 22:00 |

---

## Nightly Self-Learning

Every night at 23:00 UTC the agent:

1. Reviews all trades from the past 7 days
2. Runs 8 rules to tune core parameters (score threshold, ADX, TP ratio, risk %, session hours, watchlist)
3. Passes its full source code + trade data to an AI model for deeper improvements
4. Validates every proposed code change (syntax check, no secrets touched) before applying
5. Pushes a versioned update to GitHub at 04:00 UTC with a full changelog
6. Restarts itself if no positions are open
