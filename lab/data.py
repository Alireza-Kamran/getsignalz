"""
Multi-year market data for the research lab.

WHY THIS EXISTS
    Every number this project quoted before 2026-11-12 was measured on ~5000
    hourly bars (about seven months), because that is all the Hyperliquid
    candle endpoint returns. Seven months and ~120 trades cannot separate a
    small edge from luck -- the deployed S2 backtest topped out at t=1.86 and
    the live book then delivered t=0.20. This module fetches 2020 -> today
    from Binance USDT-M futures, the deepest public history for the same
    coins, so a hypothesis can be fitted on one period and judged on another
    it has never seen.

WHAT IT GUARANTEES
    - REAL exchange OHLC only. No Heikin Ashi anywhere in this module; see
      the 2026-07-30 bug, where a fill simulated on the HA path overstated S2
      by more than 2x. A strategy that wants HA derives it as extra columns.
    - Closed bars only. The still-forming bar is dropped.
    - Nothing is silently repaired. Missing bars are NOT forward-filled and
      zero-volume bars are NOT dropped here; `quality()` counts them so the
      caller decides, and so a slice that discards rows has to say so.

KNOWN BIAS, recorded rather than hidden
    UNIVERSE is today's list of liquid majors. Coins that were liquid in 2021
    and have since died are not in it, so any long-biased result measured
    here is flattered by survivorship. Short-side and market-neutral results
    are flattered less. Read every long-only number with that in mind.

Execution venue is Hyperliquid, not Binance. `venue_gap()` measures how far
the two books' hourly bars diverge over the window where both exist.
"""
import io
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
RAW = os.path.join(CACHE, "raw")

VISION = "https://data.binance.vision/data/futures/um/monthly"
FAPI = "https://fapi.binance.com"
HL_MAINNET = "https://api.hyperliquid.xyz/info"

START = (2020, 1)

# strategy2.WATCHLIST (20) plus five long-listed majors, so the pre-2023 sample
# is not carried by a handful of coins. Deliberately NOT chosen by any backtest.
UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "SUI", "DOGE", "BNB", "AAVE", "ARB",
            "ADA", "WLD", "TIA", "INJ", "NEAR", "APT", "OP", "ATOM", "XLM",
            "FIL", "LDO", "LINK", "XRP", "LTC", "DOT", "BCH"]

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_volume", "trades", "taker_buy_volume",
              "taker_buy_quote", "ignore"]
KEEP = ["open", "high", "low", "close", "volume", "quote_volume",
        "taker_buy_volume"]

HOUR_MS = 3_600_000
TIMEOUT = (10, 60)


def _months():
    """(year, month) from START through the last COMPLETED month."""
    now = datetime.now(timezone.utc)
    y, m = START
    out = []
    while (y, m) < (now.year, now.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _fetch_zip(url, path):
    """Download once. Returns 'cached' | 'ok' | 'missing' | 'error:<why>'.

    A 404 is a real answer (the coin was not listed that month) and is
    remembered with an empty marker file so it is not re-requested forever;
    any other failure is NOT cached, so a transient outage cannot be mistaken
    for "no data existed".
    """
    if os.path.exists(path):
        return "cached"
    if os.path.exists(path + ".404"):
        return "missing"
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        return "error:" + type(e).__name__
    if r.status_code == 404:
        open(path + ".404", "w").close()
        return "missing"
    if r.status_code != 200:
        return "error:http%d" % r.status_code
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return "ok"


def _read_zip_csv(path, names):
    """First CSV in the archive. Newer Binance dumps carry a header row and
    older ones do not, so the header is detected, never assumed."""
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    has_header = not raw[:1].isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None,
                       names=names)


def fetch(coins=None, workers=12, verbose=True):
    """Download every missing monthly kline + funding archive. Idempotent."""
    coins = coins or UNIVERSE
    jobs = []
    for c in coins:
        sym = c + "USDT"
        for kind in ("klines", "fundingRate"):
            os.makedirs(os.path.join(RAW, kind, sym), exist_ok=True)
        for y, m in _months():
            tag = "%04d-%02d" % (y, m)
            jobs.append(("%s/klines/%s/1h/%s-1h-%s.zip" % (VISION, sym, sym, tag),
                         os.path.join(RAW, "klines", sym, tag + ".zip")))
            jobs.append(("%s/fundingRate/%s/%s-fundingRate-%s.zip" % (VISION, sym, sym, tag),
                         os.path.join(RAW, "fundingRate", sym, tag + ".zip")))
    counts = {}
    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(lambda j: _fetch_zip(*j), jobs):
            counts[res] = counts.get(res, 0) + 1
    if verbose:
        print("fetch:", ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    errors = sum(v for k, v in counts.items() if k.startswith("error"))
    if errors:
        print("  WARNING: %d downloads failed and were NOT cached -- re-run fetch" % errors)
    return counts


def _rest_tail(sym, start_ms):
    """Hourly klines from start_ms to now via REST, for the month in progress."""
    rows = []
    while True:
        r = requests.get(FAPI + "/fapi/v1/klines", timeout=TIMEOUT,
                         params={"symbol": sym, "interval": "1h",
                                 "startTime": start_ms, "limit": 1500})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows += batch
        start_ms = batch[-1][0] + HOUR_MS
        if len(batch) < 1500:
            break
    return pd.DataFrame(rows, columns=KLINE_COLS) if rows else None


def build(coin, tail=True):
    """Assemble one coin's full hourly history from the cached archives."""
    sym = coin + "USDT"
    d = os.path.join(RAW, "klines", sym)
    parts = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".zip"):
            parts.append(_read_zip_csv(os.path.join(d, name), KLINE_COLS))
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    if tail:
        nxt = int(df["open_time"].max()) + HOUR_MS
        try:
            t = _rest_tail(sym, nxt)
            if t is not None:
                df = pd.concat([df, t], ignore_index=True)
        except requests.RequestException as e:
            print("  %s: REST tail failed (%s) -- history ends at last full month"
                  % (coin, type(e).__name__))
    for col in KLINE_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_time", "open", "high", "low", "close"])
    df = df.drop_duplicates("open_time").sort_values("open_time")
    # Closed bars only: a bar is closed once its close_time is in the past.
    now_ms = int(time.time() * 1000)
    df = df[df["open_time"] + HOUR_MS <= now_ms]
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms")
    df.index.name = "time"
    return df[KEEP].astype(float)


def build_funding(coin):
    """Funding prints as a Series of rates indexed by settlement time."""
    sym = coin + "USDT"
    d = os.path.join(RAW, "fundingRate", sym)
    parts = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".zip"):
            parts.append(_read_zip_csv(os.path.join(d, name),
                                       ["calc_time", "interval_h", "rate"]))
    if not parts:
        return None
    f = pd.concat(parts, ignore_index=True)
    f["calc_time"] = pd.to_numeric(f["calc_time"], errors="coerce")
    f["rate"] = pd.to_numeric(f["rate"], errors="coerce")
    f = f.dropna(subset=["calc_time", "rate"]).drop_duplicates("calc_time")
    f = f.sort_values("calc_time")
    # Settlement stamps carry a few ms of jitter; snap to the hour.
    idx = pd.to_datetime(f["calc_time"].astype("int64"), unit="ms").dt.round("h")
    s = pd.Series(f["rate"].values, index=idx, name="funding")
    return s[~s.index.duplicated()]


def _pkl(coin, kind):
    # Pickle is safe HERE because these files are never downloaded: rebuild()
    # writes them locally from CSV it has already parsed into floats, and
    # load() only ever reads back what rebuild() wrote. The untrusted input is
    # the Binance zip, and that is read as CSV text, never unpickled. Do not
    # point load() at a pickle that came from anywhere else.
    return os.path.join(CACHE, "%s_%s.pkl" % (coin, kind))


def rebuild(coins=None, verbose=True):
    """Parse the raw archives into one pickle per coin. Run after fetch()."""
    os.makedirs(CACHE, exist_ok=True)
    for c in coins or UNIVERSE:
        df = build(c)
        if df is None:
            print("  %s: NO DATA" % c)
            continue
        df.to_pickle(_pkl(c, "1h"))
        fu = build_funding(c)
        if fu is not None:
            fu.to_pickle(_pkl(c, "funding"))
        if verbose:
            print("  %-5s %6d bars  %s -> %s  funding=%s" % (
                c, len(df), df.index[0].date(), df.index[-1],
                len(fu) if fu is not None else "none"))


RESAMPLE = {"open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "quote_volume": "sum", "taker_buy_volume": "sum"}


def resample(df, tf):
    """1h -> 4h / 1d, anchored at 00:00 UTC. A bucket is kept only when ALL of
    its hours are present, so a higher-timeframe bar is never built from a
    partial window (which would be both a wrong bar and, at the right edge, a
    look at a bar that has not closed)."""
    hours = {"1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12, "1d": 24}[tf]
    if hours == 1:
        return df
    g = df.resample("%dh" % hours, origin="epoch", label="left", closed="left")
    out = g.agg(RESAMPLE)
    return out[g["close"].count() == hours]


def load(coin, tf="1h", start=None, end=None):
    """Cached history for one coin. Index is the bar OPEN time, UTC, naive."""
    p = _pkl(coin, "1h")
    if not os.path.exists(p):
        raise FileNotFoundError(
            "%s not cached -- run: python -m lab.data fetch" % coin)
    df = resample(pd.read_pickle(p), tf)
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index < pd.Timestamp(end)]
    return df


def load_funding(coin):
    p = _pkl(coin, "funding")
    return pd.read_pickle(p) if os.path.exists(p) else None


def load_all(coins=None, tf="1h", start=None, end=None):
    out = {}
    for c in coins or UNIVERSE:
        try:
            df = load(c, tf, start, end)
        except FileNotFoundError:
            continue
        if len(df):
            out[c] = df
    return out


def quality(df):
    """Count what a backtest must know about before trusting the series."""
    n = len(df)
    span = int((df.index[-1] - df.index[0]) / pd.Timedelta(hours=1)) + 1
    step = df.index.to_series().diff().dropna()
    return {
        "bars": n,
        "first": df.index[0],
        "last": df.index[-1],
        "missing": span - n,
        "largest_gap_h": float(step.max() / pd.Timedelta(hours=1)) if len(step) else 0.0,
        "zero_vol": int((df["volume"] <= 0).sum()),
        "repeat_close": int((df["close"].diff() == 0).sum()),
        "bad_ohlc": int(((df["high"] < df[["open", "close"]].max(axis=1)) |
                         (df["low"] > df[["open", "close"]].min(axis=1))).sum()),
    }


def report(coins=None):
    print("%-5s %7s %-10s %-16s %7s %6s %7s %7s %4s" % (
        "coin", "bars", "first", "last", "missing", "gap_h", "zerovol",
        "repeat", "bad"))
    for c in coins or UNIVERSE:
        try:
            q = quality(load(c))
        except FileNotFoundError:
            print("%-5s NOT CACHED" % c)
            continue
        print("%-5s %7d %-10s %-16s %7d %6.0f %6.2f%% %6.2f%% %4d" % (
            c, q["bars"], q["first"].date(), q["last"].strftime("%Y-%m-%d %H:%M"),
            q["missing"], q["largest_gap_h"], 100 * q["zero_vol"] / q["bars"],
            100 * q["repeat_close"] / q["bars"], q["bad_ohlc"]))


def hl_candles(coin, bars=5000):
    """Hyperliquid MAINNET hourly bars (real prices), for venue comparison."""
    end = int(time.time() * 1000)
    r = requests.post(HL_MAINNET, timeout=TIMEOUT, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h",
                "startTime": end - bars * HOUR_MS, "endTime": end}})
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"].astype("int64"), unit="ms")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close",
                            "v": "volume"})[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)
    return df[df.index + pd.Timedelta(hours=1) <= pd.Timestamp.utcnow().tz_localize(None)]


def venue_gap(coins=None):
    """How different are Binance and Hyperliquid hourly bars?

    Signals will be researched on Binance history and executed against the
    Hyperliquid book, so the two must agree closely enough that a level
    touched on one is touched on the other. Reported per coin over the overlap:
    median and 99th-percentile |close gap| in basis points, hourly-return
    correlation, and how often an HL bar's range differs from Binance's by
    more than a quarter of the bar -- the case that changes a stop outcome.
    """
    print("%-5s %6s %9s %9s %8s %10s" % (
        "coin", "bars", "med_bps", "p99_bps", "ret_corr", "range_diff"))
    for c in coins or UNIVERSE:
        try:
            b = load(c)
            h = hl_candles(c)
        except Exception as e:
            print("%-5s unavailable (%s)" % (c, type(e).__name__))
            continue
        if h is None:
            print("%-5s no HL data" % c)
            continue
        j = b.join(h, how="inner", rsuffix="_hl")
        if len(j) < 100:
            print("%-5s overlap too short (%d)" % (c, len(j)))
            continue
        gap = (j["close_hl"] / j["close"] - 1).abs() * 1e4
        corr = np.corrcoef(j["close"].pct_change().dropna(),
                           j["close_hl"].pct_change().dropna())[0, 1]
        rb, rh = j["high"] - j["low"], j["high_hl"] - j["low_hl"]
        rdiff = ((rh - rb).abs() > 0.25 * rb.replace(0, np.nan)).mean()
        print("%-5s %6d %9.1f %9.1f %8.4f %9.1f%%" % (
            c, len(j), gap.median(), gap.quantile(0.99), corr, 100 * rdiff))
        time.sleep(0.2)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "fetch":
        fetch()
        rebuild()
    elif cmd == "rebuild":
        rebuild()
    elif cmd == "report":
        report()
    elif cmd == "venue":
        venue_gap()
    else:
        print("usage: python -m lab.data [fetch|rebuild|report|venue]")
