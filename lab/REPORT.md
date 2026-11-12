# Lab report — round 1 and 2 (2026-11-12)

Detail, numbers and the pre-registered specifications: `HYPOTHESES.md`.

## Verdict

**No candidate passes the acceptance gate. Nothing is deployed.**
The 2026 holdout has not been opened for any candidate.

| family | train 2021-2024 | status |
|---|---|---|
| H0  S2 as deployed (live engine) | -0.18R/trade, t=-3.6, negative every year | negative expectancy |
| D   RSI fade with the daily trend | -0.12R/trade, t=-5.2 | rejected |
| C2  OB + RSI alert (ind.txt) | -0.15R/trade, t=-3.7 | rejected |
| C3  OB with the SSL trend | -0.06R/trade, t=-2.9 | rejected |
| C1  SSL-200 flip, 4h (ind.txt) | +0.62R/trade, t=3.0, tail-driven | failed 2025 (-0.21R/trade) |
| A   40-day breakout | +0.12R/trade, t=2.0 | weak |
| B   cross-sectional momentum | Sharpe 0.76 | weak, negative in 2025 |
| F   funding carry | +8.5%/yr, of which 2021 is +27.7% | rejected on train |

Frozen book (C1 + A + B, equal volatility, 15% target):
train +19.7%/yr, Sharpe 1.31, 54% of months positive;
**validate 2025: -2.6%, Sharpe -0.28.** `python -m lab.gates` fails 6 of 7.

## What was learned

1. The live engine loses money over five years. Its seven-month backtest was
   positive because a rounding artefact in `strategy2.signal` discards about
   half the signals at random, and the half it kept happened to win.
2. Fading 1h RSI extremes has no edge in either direction; costs decide it.
3. The only thing that worked in-sample was trend following, most strongly
   through the owner's own SSL-200 channel. It is real in 2021, 2023 and
   2024, absent in 2022 and 2025, and lives in a few very long trades.
4. Nothing directional on 25 majors reached 60% positive months even
   in-sample. "Rising every month" is not available from this kind of rule.

## Reproduce

    /opt/bots-venv/bin/python -m lab.data fetch      # ~3 min, once
    /opt/bots-venv/bin/python -m lab.check_parity    # engine == legacy model
    /opt/bots-venv/bin/python -m lab.check_s2_port   # port == strategy2.signal
    /opt/bots-venv/bin/python -m lab.run_h0          # and run_a, run_b, run_c, run_d
    /opt/bots-venv/bin/python -m lab.run_f train
    /opt/bots-venv/bin/python -m lab.portfolio train
    /opt/bots-venv/bin/python -m lab.portfolio validate
    /opt/bots-venv/bin/python -m lab.gates
    /opt/bots-venv/bin/python test_lab_engine.py
