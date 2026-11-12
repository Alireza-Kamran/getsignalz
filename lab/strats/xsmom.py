"""
Family B: cross-sectional momentum, dollar neutral.

Every rebalance, coins are ranked by trailing return; the strongest slice is
held long and the weakest short, in equal weights that sum to zero. What is
left is relative strength -- the market's own direction cancels out, which is
exactly what every other family in this lab is exposed to.
"""
import numpy as np
import pandas as pd


def weights(closes, lookback=28, skip=1, frac=0.2, rebalance=7, min_coins=10,
            gross=1.0):
    """Target weights per day, decided at that day's close.

    The ranking return runs from t-skip-lookback to t-skip. Skipping the most
    recent day keeps the one-day reversal effect out of a momentum signal.
    A coin is eligible only once it has the full lookback of history.
    """
    mom = closes.shift(skip) / closes.shift(skip + lookback) - 1
    w = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    days = np.arange(len(closes))
    for i in days[::rebalance]:
        row = mom.iloc[i].dropna()
        w.iloc[i] = 0.0
        if len(row) < min_coins:
            continue
        k = max(1, int(round(frac * len(row))))
        order = row.sort_values()
        w.iloc[i, w.columns.get_indexer(order.index[-k:])] = gross / 2 / k
        w.iloc[i, w.columns.get_indexer(order.index[:k])] = -gross / 2 / k
    return w.ffill().fillna(0.0)
