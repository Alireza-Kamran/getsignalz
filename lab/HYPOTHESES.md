# Pre-registered hypotheses

Written 2026-11-12, BEFORE any of these was run on the multi-year data.
A hypothesis is added here first and tested second. Results are appended
under each one, including the failures. Nothing is deleted.

## Ground rules

- Periods are fixed in `lab/validate.py`: train 2021-2024, validate 2025,
  holdout 2026. Research runs use train. A candidate is frozen, then judged
  once on validate. The holdout is opened once, for the final portfolio.
- Every run: real OHLC, entry at next bar open, `fill="pess"`, fees +
  slippage + funding on. Headline numbers are never quoted from `opt`.
- Each family has ONE primary specification, fixed below, plus a small
  neighbourhood used only to check that the primary is not a lonely peak.
  The primary is what gets judged. Picking the best neighbour afterwards is
  a new trial and is counted as one.
- Trial count is kept at the bottom of this file. `validate.deflated_t`
  discounts every reported t by it.
- Universe is today's 25 liquid majors: long-only results are flattered by
  survivorship. Read them accordingly.
- No coin is dropped because of its own backtest result.

## H0 — baseline: S2 exactly as deployed

RSI(14) <= 25 / >= 75 on Heikin Ashi 1h, ADX < 25, |stretch| >= 1.5 ATR from
EMA100, stop 1.5 ATR, ratchet arms at 2.5R step 0.25R, backstop TP 5R, book
cap 2.

Expectation: no edge. The entry was already measured as a coin flip, the
seven-month backtest reached t=1.86, live delivered t=0.20 over 50 trades.
Purpose: calibrate the lab against something whose live result is known.

## A — time-series trend following

Rationale: the best documented effect in crypto. Returns are positively
autocorrelated at multi-week horizons, moves are fat-tailed, and a breakout
rule with a wide trailing stop is long that tail by construction.

Primary: 1d bars. Long when the close exceeds the highest high of the
previous 40 days; short when it breaks the lowest low. Initial stop 3 x
ATR(20). Exit on a 3 x ATR chandelier trail. Book cap 8, at most 6 per side.
Rank simultaneous signals by breakout strength in ATRs.

Neighbourhood: lookback 20 / 40 / 80 days; trail 2.5 / 3 / 4 ATR; 4h bars
with lookback 120 / 240 / 480 (same horizons in bars).

Variants, each one extra trial: long-only; longs only while BTC is above its
200-day average, shorts only below.

## B — cross-sectional momentum (market neutral)

Rationale: relative strength persists across coins for a few weeks, and a
long-short book is insulated from the market direction that dominates every
other family here. If it works it is the natural diversifier.

Primary: weekly rebalance. Rank coins by 28-day return, skipping the most
recent day. Long the top fifth, short the bottom fifth, equal weight, gross
exposure 1.0, dollar neutral. Minimum 10 listed coins.

Neighbourhood: lookback 14 / 28 / 56 days; top/bottom third.

## C — the owner's indicator (ind.txt)

Rationale: it is the tool the owner actually reads a chart with. It has never
been measured on real prices over more than a few months.

- C1, SSL-200 as a trend engine. Primary: 4h Heikin Ashi. Long when the
  SSL(200) state flips to +1, short on -1, exit on the opposite flip, initial
  stop 3 ATR. Neighbourhood: 1h, 1d; length 100.
- C2, the indicator's own combined alert. 1h Heikin Ashi. Long when a bullish
  order block forms with RSI(14) <= 30; short on a bearish block with
  RSI >= 70. Stop 1.5 ATR. Primary exit: 2R target. Neighbourhood: ratchet
  2.5R/0.25R; 4h.
- C3, order block in the direction of the SSL trend. Same trigger as C2
  without the RSI condition, taken only with the 4h SSL(200) state.
- Liquidity-pool and FVG rules are deferred until C1-C3 are read; they need a
  written entry rule first, and one will not be invented after seeing data.

## D — mean reversion with the trend, not against it

Rationale: live S2 longs lost (n=34, mean -0.17R) while its shorts won (n=16,
+0.51R) during a falling market. A dip in an uptrend is bought by trend
followers; a dip in a downtrend is the trend. The existing ADX gate measures
trend STRENGTH and is blind to DIRECTION.

Primary: S2's trigger on 1h (RSI <= 25 / >= 75, |stretch| >= 1.5 ATR, no ADX
gate), but longs only when the 1d close is above its 100-day EMA and shorts
only below. Stop 1.5 ATR. Exit: 2R target. Book cap 4.

Neighbourhood: exit at 1.5R / 3R / ratchet 2.5R; trend filter EMA 50 / 200;
RSI 20 / 30.

Control, counted as a trial: the same trigger taken only AGAINST the trend.
If the control does as well as the primary, the filter is not the reason.

## E — funding as a crowding signal

Rationale: persistently high funding means longs are paying to stay in a
crowded trade; extremes tend to precede flushes. Lowest priority, weakest
prior.

Primary: used as a FILTER on family A -- skip longs when the coin's 3-day
average funding is above its 90th percentile of the past year, skip shorts
below the 10th. Judged by whether it improves A, not standalone.

## F — funding carry, delta neutral  (ROUND 2, registered 2026-11-12)

Registered AFTER round 1 was read, and for a stated reason: every round-1
family bets on price direction, and the frozen round-1 book failed its
validation year. This one does not bet on direction at all.

Rationale: perpetual longs pay shorts a funding rate whenever the perp trades
above spot, which in crypto is most of the time because leveraged demand is
structurally long. Holding the coin spot and shorting the same notional of the
perp earns that rate with no exposure to price. The return is a payment for
providing leverage, not a forecast, which is why it should be far steadier
than anything directional -- and why it is small.

Primary: weekly rebalance. Eligible coins are those whose trailing 7-day
funding, annualised, exceeds 10% (enough to pay for getting in and out). Hold
the 5 highest, equal weight, long spot / short perp; hold cash when fewer
qualify. Costs per unit of notional traded: perp taker 0.045% + 0.03% slip,
spot 0.07% + 0.03% slip, both legs, both ways. Capital = 1.5x the perp
notional (spot fully paid, 50% margin behind the short).

Neighbourhood: threshold 5% / 20%; top 3 / 8; trailing window 3 / 14 days.

Known gaps, stated before running: basis moves between the legs are not
marked (they net to zero over a round trip but add month-to-month noise);
funding is Binance's 8-hour print, Hyperliquid's hourly rate differs in
detail; a short squeeze can force margin top-ups. And it needs a spot leg,
which the current bot does not trade.

---

## Results

All figures: TRAIN 2021-2024 unless marked, `fill="pess"`, costs and funding
on, 25 coins. Run 2026-11-12.

### Engine and port checks (not trials)
- `lab.check_parity`: lab.engine reproduces sweep_trail_mode exactly in all
  three legacy modes (max |dR| ~1e-14, same trades).
- `lab.check_s2_port`: lab S2 signals == strategy2.signal, 215 of 215, once a
  live rounding artefact is reproduced (below).

### H0 — S2 as deployed: REJECTED, negative expectancy
- n=1026, WR 25.2%, mean -0.178R, t=-3.60, negative in every year 2021-2025.
  Still negative with costs removed (-0.084R).
- LIVE BUG found on the way: `strategy2.signal` computes leverage =
  round(20/sl_pct, 1) and rejects when sl_pct*leverage > 20, i.e. whenever the
  rounding went up. That discards ~47% of valid signals at random.
- On the last 7 months (HL bars) the randomly KEPT half scores +0.336R/trade,
  t=2.29; ALL signals score -0.010R. The backtest that justified the deployed
  parameters was measuring which half the rounding happened to keep.

### D — mean reversion with the daily trend: REJECTED
- Primary: n=3978, WR 32.2% against a 2R target (breakeven 33.3% before
  costs), mean -0.115R, t=-5.18. Negative every year, both sides.
- Control (against trend) -0.127R; no filter -0.130R. The filter does nothing.
- All 7 neighbours negative. Fading 1h RSI extremes is a coin flip that pays
  0.08R in costs each time.

### C — the owner's indicator
- Port check: vectorised order blocks and SSL equal the live port bar for bar.
- C1 SSL-200 flip, 4h (primary): n=735, WR 24.5%, mean +0.618R, t=2.97.
  2022 negative. Best 5% of trades removed: -165R -- the whole result is a
  handful of very long trades. Neighbours all positive (1h t=3.06, 1d t=1.26,
  SSL-100 t=2.69).
- C2 OB + RSI alert: REJECTED. n=1198, mean -0.149R, t=-3.71; -0.083R even
  with no costs.
- C3 OB with the 4h SSL trend: REJECTED. mean -0.055R, t=-2.90 (+0.031R
  before costs: no edge, and costs decide it).

### A — Donchian trend: weak
- Primary 1d N=40 trail 3: n=392, mean +0.116R, t=2.00, 51% positive months,
  2022 negative, 2024 flat. Shorts lose (-0.047R). All 14 variants positive.

### B — cross-sectional momentum: weak
- Primary: +29%/yr at 38% vol, Sharpe 0.76, t=1.52, 48% positive months.
  All 7 variants positive (Sharpe 0.74-1.24).

### Frozen book (C1 + A + B primaries, equal vol, 15% target)
- TRAIN: +19.7%/yr, Sharpe 1.31, maxDD -11.4%, 54% positive months, 2022 -2.6%.
  Sleeve correlations 0.34-0.59 once trades are marked to market daily.
- VALIDATE 2025, judged once: **-2.6%, Sharpe -0.28, maxDD -10.5%, 50%
  positive months. FAILED.** C1 itself: n=179, mean -0.207R.
- Verdict: no round-1 candidate passes. Nothing is deployed. The holdout
  (2026) has not been opened for any candidate.

### E — funding filter on A: NOT RUN
- Its only purpose was to improve A, and A did not survive as part of the
  frozen book. Running it now would be tuning a rejected candidate.

### F — funding carry: REJECTED on train, validation not spent on it
- Primary: +8.5%/yr on capital, maxDD -1.3%, worst month -0.35% -- as steady
  as expected. But 2021 is +27.7% of it; 2022 -0.6%, 2023 +1.4%, 2024 +5.6%.
  Only 50% of months positive: outside a leverage boom there is nothing to
  collect, and weekly rotation costs more than it earns.
- Under 2x costs 2022 and 2023 are negative. All 6 neighbours tell the same
  story (+7.8 to +9.0%/yr, 35-56% positive months).
- Not a failure of the idea so much as of its size: it is real, tiny, and
  regime-dependent. Not sent to validation.

## Trial count

Round 1: 38 variants read on train (A 14, D 10, C 7, B 7). One frozen book
judged on validate, once.
Round 2: 7 variants read on train (F). None sent to validate.
