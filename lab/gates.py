"""
The acceptance gate: what a book must show before it may trade the channel.

This file is the one place where "good enough" is written down, so that it is
decided BEFORE a result is looked at and cannot quietly move afterwards.

    python -m lab.gates          # judge the frozen book on train + validate

==========================  OWNER DECIDES  ==================================
THRESHOLDS below are a PROPOSAL from the 2026-11-12 session. They encode a
product decision, not a statistical one: how much evidence, how much drawdown
and how many losing months the channel is willing to carry. Kamran sets them.

Things to weigh when changing a number:
  - min_train_t      3.0 is roughly "one chance in 700 of being luck" for a
                     single test; ~40 variants were tried, which is why it is
                     not 2.0. Lowering it admits more candidates and more of
                     them will be noise.
  - min_val_sharpe   The unseen year must be clearly positive, not merely
                     above zero. 0.5 is modest; 1.0 is what a paid product
                     would want.
  - min_pos_months   The stated goal was "always rising". No directional book
                     tested here exceeded 60% even in-sample; 65 already asks
                     for more than round 1 could deliver.
  - max_drawdown     At the 15% volatility target. Scales with TARGET_VOL in
                     lab/portfolio.py.
=============================================================================
"""
THRESHOLDS = {
    "min_train_t": 3.0,          # t-stat of daily P&L over 2021-2024
    "min_val_sharpe": 0.5,       # Sharpe over the unseen validation year
    "min_pos_months": 65.0,      # % of months positive, in BOTH periods
    "max_drawdown": -12.0,       # worst peak-to-trough, account %, either period
    "max_worst_month": -6.0,     # worst single month, account %, either period
}


def passes_gate(train, val, th=None):
    """Judge a book. `train` / `val` are portfolio.stats() dicts.

    Returns (passed, rows) where each row is (criterion, value, need, ok).
    Every criterion is evaluated and reported even after one fails, so a
    rejection always says everything that was wrong, not just the first thing.
    """
    th = th or THRESHOLDS
    rows = [
        ("train t-stat", train["t"], ">= %.1f" % th["min_train_t"],
         train["t"] >= th["min_train_t"]),
        ("validate Sharpe", val["sharpe"], ">= %.1f" % th["min_val_sharpe"],
         val["sharpe"] >= th["min_val_sharpe"]),
        ("validate return %", val["ann"], "> 0", val["ann"] > 0),
        ("train positive months %", train["pos"], ">= %.0f" % th["min_pos_months"],
         train["pos"] >= th["min_pos_months"]),
        ("validate positive months %", val["pos"], ">= %.0f" % th["min_pos_months"],
         val["pos"] >= th["min_pos_months"]),
        ("worst drawdown %", min(train["dd"], val["dd"]), ">= %.0f" % th["max_drawdown"],
         min(train["dd"], val["dd"]) >= th["max_drawdown"]),
        ("worst month %", min(train["worst"], val["worst"]), ">= %.0f" % th["max_worst_month"],
         min(train["worst"], val["worst"]) >= th["max_worst_month"]),
    ]
    return all(r[3] for r in rows), rows


def main():
    import contextlib
    import io
    from lab import portfolio
    with contextlib.redirect_stdout(io.StringIO()):
        tr = portfolio.stats(portfolio.report("train")["BOOK"])
        va = portfolio.stats(portfolio.report("validate")["BOOK"])
    ok, rows = passes_gate(tr, va)
    print("%-28s %9s   %-8s %s" % ("criterion", "value", "need", ""))
    for name, val, need, good in rows:
        print("%-28s %+9.2f   %-8s %s" % (name, val, need, "pass" if good else "FAIL"))
    print("\nGATE:", "PASSED" if ok else "REJECTED -- nothing is deployed")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
