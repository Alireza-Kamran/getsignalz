"""
Order execution layer — wraps Hyperliquid SDK for clean trade management.
"""
import math
import time
import logging
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account
from loguru import logger


from config import HYPERLIQUID_PRIVATE_KEY as PRIVATE_KEY, HYPERLIQUID_ACCOUNT as ACCOUNT_ADDRESS, USE_TESTNET

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"
BASE_URL    = TESTNET_URL if USE_TESTNET else MAINNET_URL

# ── Singleton clients — one shared connection pool for the whole process ──────
_info_obj     = None
_exchange_obj = None

def _clients():
    global _info_obj, _exchange_obj
    if _info_obj is None:
        wallet        = Account.from_key(PRIVATE_KEY)
        _info_obj     = Info(BASE_URL, skip_ws=True)
        _exchange_obj = Exchange(wallet, BASE_URL, account_address=ACCOUNT_ADDRESS)
    return _info_obj, _exchange_obj


def _hl_call(fn, *args, retries=4, **kwargs):
    """Call a Hyperliquid API function with exponential backoff on 429/502/500."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            code = e.args[0] if e.args else 0
            if code in (429, 502, 503, 500) and attempt < retries - 1:
                wait = 2 ** attempt   # 1, 2, 4, 8 seconds
                logger.warning(f"HL API {code} — retry in {wait}s ({fn.__name__ if hasattr(fn,'__name__') else '?'})")
                time.sleep(wait)
            else:
                raise
    return None


def get_account_value():
    """Spot USDC balance — the actual funds in the account."""
    info, _ = _clients()
    spot = _hl_call(info.spot_user_state, ACCOUNT_ADDRESS)
    return next((float(b["total"]) for b in spot["balances"] if b["coin"] == "USDC"), 0.0)


def get_positions():
    info, _ = _clients()
    state = _hl_call(info.user_state, ACCOUNT_ADDRESS)
    positions = {}
    for p in state["assetPositions"]:
        pos = p["position"]
        sz  = float(pos["szi"])
        if sz != 0:
            lev_info = pos.get("leverage", {})
            positions[pos["coin"]] = {
                "size":            sz,
                "entry":           float(pos["entryPx"]),
                "direction":       1 if sz > 0 else -1,
                "unrealized_pnl":  float(pos.get("unrealizedPnl", 0)),
                "roe":             float(pos.get("returnOnEquity", 0)),
                "leverage":        int(lev_info.get("value", 1)) if isinstance(lev_info, dict) else 1,
                "margin_used":     float(pos.get("marginUsed", 0)),
            }
    return positions


def get_mids():
    """Return all mid prices as a dict {coin: float}."""
    info, _ = _clients()
    return {k: float(v) for k, v in _hl_call(info.all_mids).items()}


def get_price(coin):
    return get_mids().get(coin, 0.0)


def _px(x):
    """Round price to Hyperliquid precision: 5 sig figs, max 4 decimal places."""
    if x == 0:
        return 0.0
    d = math.floor(math.log10(abs(x)))
    return round(x, min(-d + 4, 4))


_sz_decimals_cache = {}

def _sz_decimals(coin):
    """Return the number of decimal places allowed for this coin's size."""
    if coin not in _sz_decimals_cache:
        try:
            info, _ = _clients()
            meta = info.meta()
            for a in meta["universe"]:
                _sz_decimals_cache[a["name"]] = int(a.get("szDecimals", 1))
        except Exception:
            return 1
    return _sz_decimals_cache.get(coin, 1)


def _round_sz(sz, coin=None):
    """Round size down to the coin's szDecimals (no floating point noise)."""
    decimals = _sz_decimals(coin) if coin else 1
    factor = 10 ** decimals
    return math.floor(sz * factor) / factor


def open_trade(coin, direction, risk_usd, sl_price, tp_price, leverage=10, tp_ratio=2.0):
    """
    Open a position with automatic SL and TP orders.
    direction: 1=long, -1=short
    risk_usd: dollar amount to risk on this trade
    Returns order result or None on failure.
    """
    info, exchange = _clients()
    price = get_price(coin)
    if price <= 0:
        logger.error(f"Could not get price for {coin}")
        return None

    risk_pct = abs(price - sl_price) / price
    if risk_pct <= 0:
        logger.error(f"Invalid SL for {coin}: price={price}, sl={sl_price}")
        return None

    # Risk-based sizing: notional = risk_usd / sl_pct ensures each SL hit costs
    # exactly RISK_PCT (2%) of account. Old formula (account*leverage*0.5) lost
    # ~12.5% per SL regardless of stop width — root cause of -81.5% drawdown.
    # Cap at account*leverage*0.5 preserves the old value as a hard maximum.
    account_val = get_account_value()
    risk_pct_sl = abs(price - sl_price) / price
    if risk_pct_sl > 0:
        notional = min(risk_usd / risk_pct_sl, account_val * leverage * 0.5)
    else:
        notional = account_val * leverage * 0.1
    sz = _round_sz(notional / price, coin=coin)

    # Minimum $10 notional
    if sz * price < 10:
        sz = _round_sz(10.0 / price + 0.1, coin=coin)

    is_buy = direction == 1
    logger.info(f"Opening {'LONG' if is_buy else 'SHORT'} {sz} {coin} @ ~${price:.4f} | SL=${sl_price:.4f} TP=${tp_price:.4f}")

    # Market entry
    result = exchange.market_open(coin, is_buy=is_buy, sz=sz, px=None, slippage=0.01)
    if result.get("status") != "ok":
        logger.error(f"Entry failed: {result}")
        return None

    filled = result["response"]["data"]["statuses"][0]
    if "error" in filled:
        logger.error(f"Entry error: {filled['error']}")
        return None

    entry_price = float(filled["filled"]["avgPx"])
    actual_sz   = float(filled["filled"]["totalSz"])
    logger.info(f"Filled {actual_sz} {coin} @ ${entry_price:.4f}")

    # Recalculate SL/TP from actual fill price — price may have moved since signal
    MAX_LEV_LOSS = 0.25   # max 25% leveraged loss at SL regardless of trade leverage
    max_sl_pct   = MAX_LEV_LOSS / leverage   # e.g. 1.25% for 20x, 2.5% for 10x
    risk_dist = abs(entry_price - sl_price)
    if direction == 1 and sl_price >= entry_price:
        sl_price = entry_price * (1 - max_sl_pct)
        risk_dist = abs(entry_price - sl_price)
    elif direction == -1 and sl_price <= entry_price:
        sl_price = entry_price * (1 + max_sl_pct)
        risk_dist = abs(entry_price - sl_price)
    # Hard cap: SL must be within max_sl_pct of fill (leverage-aware)
    max_risk = entry_price * max_sl_pct
    if risk_dist > max_risk:
        risk_dist = max_risk
        sl_price = entry_price + risk_dist if direction == -1 else entry_price - risk_dist
        logger.info(f"SL clamped to {max_sl_pct*100:.2f}% from fill ({leverage}x → max {MAX_LEV_LOSS*100:.0f}% risk): ${sl_price:.5f}")
    tp_price = entry_price + direction * risk_dist * tp_ratio
    logger.info(f"Adjusted SL=${sl_price:.5f} TP=${tp_price:.5f} (R:R 1:{tp_ratio})")

    # Place SL order
    # LONG SL = SELL stop: limit must be BELOW trigger (accept selling into the drop)
    # SHORT SL = BUY stop: limit must be ABOVE trigger (accept buying into the rise)
    slippage_buf = 0.003
    sl_trigger  = _px(sl_price)
    sl_limit_px = _px(sl_price * (1 - slippage_buf) if is_buy else sl_price * (1 + slippage_buf))
    sl_result = exchange.order(
        coin,
        is_buy=not is_buy,
        sz=actual_sz,
        limit_px=sl_limit_px,
        order_type={"trigger": {"triggerPx": sl_trigger, "isMarket": True, "tpsl": "sl"}},
        reduce_only=True,
    )
    try:
        inner_sl = sl_result["response"]["data"]["statuses"][0]
        if "error" in inner_sl:
            logger.warning(f"SL inner error for {coin}: {inner_sl['error']}")
        else:
            logger.info(f"SL set at ${sl_price:.5f}")
    except Exception:
        if sl_result.get("status") == "ok":
            logger.info(f"SL set at ${sl_price:.5f}")
        else:
            logger.warning(f"SL placement failed: {sl_result}")

    # TP order — use isMarket:False (more reliable across coins)
    tp_trigger  = _px(tp_price)
    # For sell TP (long): limit slightly below trigger (accept some slippage down)
    # For buy TP (short): limit slightly above trigger (accept some slippage up)
    tp_limit_px = _px(tp_price * (1 - slippage_buf) if is_buy else tp_price * (1 + slippage_buf))

    tp_result = exchange.order(
        coin,
        is_buy=not is_buy,
        sz=actual_sz,
        limit_px=tp_limit_px,
        order_type={"trigger": {"triggerPx": tp_trigger, "isMarket": False, "tpsl": "tp"}},
        reduce_only=True,
    )
    # Check inner status (outer "ok" can mask inner errors)
    try:
        inner = tp_result["response"]["data"]["statuses"][0]
        if "error" in inner:
            logger.warning(f"TP inner error for {coin}: {inner['error']}")
        elif "resting" in inner or "filled" in inner:
            logger.info(f"TP set at ${tp_price:.5f}")
        else:
            logger.warning(f"TP unexpected status: {inner}")
    except Exception:
        if tp_result.get("status") == "ok":
            logger.info(f"TP set at ${tp_price:.5f}")
        else:
            logger.warning(f"TP placement failed: {tp_result}")

    return {
        "coin":   coin,
        "dir":    direction,
        "size":   actual_sz,
        "entry":  entry_price,
        "sl":     sl_price,
        "tp":     tp_price,
        "risk_usd": round(actual_sz * price * risk_pct, 2),
    }


def close_trade(coin):
    _, exchange = _clients()
    result = exchange.market_close(coin)
    if result.get("status") == "ok":
        logger.info(f"Closed {coin}")
    else:
        logger.error(f"Close failed: {result}")
    return result


def update_sl(coin, direction, sz, new_sl, entry=None):
    """Cancel existing SL and place new one (for trailing stop).
    Pass entry so TP orders on the profit side are preserved.
    """
    info, exchange = _clients()
    is_buy = direction == -1  # short position → buy to close
    buf    = 0.02

    # Cancel old SL only — skip TP orders
    # open_orders does not include triggerPx; query each order individually to get it
    try:
        orders = _hl_call(info.open_orders, ACCOUNT_ADDRESS)
        for o in orders:
            if o["coin"] != coin or not o.get("reduceOnly"):
                continue
            if entry is not None:
                try:
                    detail = _hl_call(info.query_order_by_oid, ACCOUNT_ADDRESS, o["oid"])
                    trig = float(detail["order"]["order"]["triggerPx"])
                    if direction == -1 and trig < entry:
                        continue  # SHORT TP is below entry — preserve
                    if direction == 1 and trig > entry:
                        continue  # LONG TP is above entry — preserve
                except Exception:
                    pass  # if query fails, cancel it (safe fallback)
            exchange.cancel(coin, o["oid"])
    except Exception as e:
        logger.warning(f"Could not cancel old orders: {e}")

    # Place new SL
    sl_trigger = _px(new_sl)
    sl_limit   = _px(new_sl * (1 + buf) if is_buy else new_sl * (1 - buf))
    result = exchange.order(
        coin, is_buy=is_buy, sz=sz,
        limit_px=sl_limit,
        order_type={"trigger": {"triggerPx": sl_trigger, "isMarket": True, "tpsl": "sl"}},
        reduce_only=True,
    )
    logger.info(f"New SL for {coin} @ ${new_sl:.4f}: {result.get('status')}")
    return result
