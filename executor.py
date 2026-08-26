"""
Order execution layer — wraps Hyperliquid SDK for clean trade management.
"""
import math
import time
import logging
import requests
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account
from loguru import logger


from config import HYPERLIQUID_PRIVATE_KEY as PRIVATE_KEY, HYPERLIQUID_ACCOUNT as ACCOUNT_ADDRESS, USE_TESTNET

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"
BASE_URL    = TESTNET_URL if USE_TESTNET else MAINNET_URL

# (connect, read) seconds. The Hyperliquid SDK defaults timeout=None, which
# means requests blocks FOREVER on a half-open socket -- and the API sits
# behind an nginx that regularly 502s, so half-open sockets do happen. On
# 2026-07-28 22:05 UTC this froze the entire bot for 4 hours: no scan, no stop
# ratchet, process still "active (running)" to systemd, main thread parked in a
# socket read with no timer set. Never build an Info/Exchange without this.
HTTP_TIMEOUT = (5, 20)

# ── Stop-order slippage caps ──────────────────────────────────────────────────
# A Hyperliquid trigger order with isMarket:True fires an IOC market order capped
# at `limit_px`. The cap is the WORST price accepted; anything beyond it does not
# fill and the remainder is cancelled -- it does NOT rest. So the cap trades one
# risk against the other: too wide gives back profit to slippage, too tight risks
# not closing at all and leaving the position with nothing resting against it.
#
# These two values were local variables in two different functions until
# 2026-08-26, which is why nobody noticed they differ by 6.7x -- and that the
# LOOSER one guards the order protecting 100% of this strategy's realised edge.
#
#   BRACKET_SLIP_CAP  the original protective stop placed at entry. Must fill:
#                     failing to fill means riding an unbounded loss.
#   RATCHET_SLIP_CAP  the stop update_sl() moves into profit once the ratchet
#                     arms. Fires essentially AT market (the stop is placed at
#                     the price that just triggered arming), so it is far more
#                     exposed to an adverse tick than the bracket stop ever is.
#
# Measured cost at 2.0%: see the RATCHET SLIPPAGE section of analyze.full_report.
# ETH 2026-08-25 armed at +2.50R and filled 24s later at +2.17R -- a 1.05%
# adverse fill, comfortably inside this cap and therefore accepted in full.
# Values are UNCHANGED from the locals they replace; narrowing RATCHET_SLIP_CAP
# is a deployed exit-behaviour change and is Kamran's call, not the nightly
# session's.
BRACKET_SLIP_CAP = 0.003
RATCHET_SLIP_CAP = 0.02

# ── Singleton clients — one shared connection pool for the whole process ──────
_info_obj     = None
_exchange_obj = None

def _clients():
    global _info_obj, _exchange_obj
    if _info_obj is None:
        wallet        = Account.from_key(PRIVATE_KEY)
        _info_obj     = Info(BASE_URL, skip_ws=True, timeout=HTTP_TIMEOUT)
        _exchange_obj = Exchange(wallet, BASE_URL,
                                 account_address=ACCOUNT_ADDRESS,
                                 timeout=HTTP_TIMEOUT)
    return _info_obj, _exchange_obj


def _hl_call(fn, *args, retries=4, **kwargs):
    """Call a Hyperliquid API function with exponential backoff on 429/502/500."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            # With HTTP_TIMEOUT set, the failure that used to hang the
            # process forever now surfaces here instead. Same transient
            # class as a 502 -- retry rather than abort the cycle.
            if attempt < retries - 1:
                wait = 2 ** attempt
                name = getattr(fn, "__name__", "?")
                logger.warning(
                    f"HL API network error ({type(e).__name__}) — "
                    f"retry in {wait}s ({name})")
                time.sleep(wait)
            else:
                raise
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
    """Tradeable equity: perp account value plus spot USDC.

    Read the perp wallet too, and never silently return 0.0.

    This read spot USDC alone and defaulted to 0.0 on any failure. Perps are
    margined from the PERP wallet, so on an account funded the normal way the
    sizing input was a balance the bot cannot trade with. Verified 2026-08-05:
    mainnet spot $9.51 / perp $0.00, testnet spot $639.36 / perp $0.00.

    The 0.0 default was the more dangerous half. risk_usd = 0 makes notional 0,
    which lands in open_trade's below-minimum branch -- which, before it was
    fixed, ordered 0.1 of the coin. A failed HTTP call and an unfunded spot
    wallet both reached it. Raising means a transient failure skips a candle
    instead of sizing an order off a balance that was never read.
    """
    info, _ = _clients()
    perp = spot = 0.0

    state = _hl_call(info.user_state, ACCOUNT_ADDRESS)
    if state is None:
        raise RuntimeError("account value unavailable: perp user_state failed")
    perp = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)

    try:
        sp = _hl_call(info.spot_user_state, ACCOUNT_ADDRESS)
        if sp:
            spot = next((float(b["total"]) for b in sp.get("balances", [])
                         if b["coin"] == "USDC"), 0.0)
    except Exception as e:
        # Spot is the smaller half on a normally-funded account; a perp read
        # that succeeded is still a usable number.
        logger.warning(f"spot balance unavailable ({e}) — using perp only")

    total = perp + spot
    if total <= 0:
        raise RuntimeError(
            f"account value is zero (perp={perp:.2f} spot={spot:.2f}) — "
            f"refusing to size an order")
    return total


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


def get_stop_price(coin):
    """Return the trigger price of the resting reduce-only STOP for `coin`.

    state.json records where the bot *believes* its stop is; this is where the
    stop actually is, and only this one will execute. The two can drift apart --
    a ratchet move that failed after the cancel leg, a manual intervention, or a
    bad write into the state file -- and the bot has no other way to notice.

    Stops are told apart from take-profits by order type rather than by which
    side of entry they sit on: update_sl() uses the side rule, but that rule is
    only valid before the ratchet arms. Once a stop has ratcheted into profit it
    sits on the take-profit's side of entry and the side rule misreads it.

    Returns None when there is no resting stop or the API call fails, so callers
    must treat None as "unknown", never as "no stop".
    """
    info, _ = _clients()
    try:
        orders = _hl_call(info.frontend_open_orders, ACCOUNT_ADDRESS)
    except Exception as e:
        logger.warning(f"Could not read resting orders for {coin}: {e}")
        return None
    for o in orders or []:
        if o.get("coin") != coin or not o.get("reduceOnly"):
            continue
        if "stop" not in str(o.get("orderType", "")).lower():
            continue
        try:
            return float(o["triggerPx"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


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

    # Re-validate the stop distance at EXECUTION price, not just at signal price.
    #
    # strategy2.signal already rejects stops tighter than MIN_SL_PCT, but it
    # measures against the price at signal time. The stop is then frozen while
    # the market keeps moving, so by the time the order goes in the distance can
    # be a fraction of what was validated -- and size is 1/distance, so it
    # explodes in exactly that case.
    #
    # FIL, 2026-08-12 22:01: signalled at 0.67396 with the stop at 0.66609
    # (1.168% away, comfortably valid). By execution get_price() returned
    # ~0.66690 -- 0.12% from the stop -- so risk_usd $6.97 sized 8642 units
    # instead of ~2150. The fill then landed at 0.66933, further from the stop
    # than the price it sized on, and the stop-out cost $33.29 against a $6.97
    # budget. That single trade is 84% of the account's entire realised loss
    # ($25 of $29.68); the other 9 trades all sized within 13% of budget.
    #
    # Aborting, never resizing: a stop this close means the setup has already
    # played out against us, and the risk budget is the thing being protected.
    try:
        from strategy2 import MIN_SL_PCT
    except Exception:
        MIN_SL_PCT = 0.4
    if risk_pct_sl * 100 < MIN_SL_PCT:
        msg = (f"{coin}: stop is {risk_pct_sl*100:.3f}% from execution price "
               f"${price:.6g} (SL ${sl_price:.6g}), under the {MIN_SL_PCT}% "
               f"floor — price drifted onto the stop between signal and fill. "
               f"Sizing off it would risk ~{MIN_SL_PCT/max(risk_pct_sl*100, 1e-9):.1f}x "
               f"the ${risk_usd:.2f} budget — trade aborted")
        logger.error(msg)
        try:
            import tg
            tg.dm_owner(f"⚠️ <b>Order aborted</b>\n{msg}")
        except Exception:
            pass
        return None

    if risk_pct_sl > 0:
        notional = min(risk_usd / risk_pct_sl, account_val * leverage * 0.5)
    else:
        notional = account_val * leverage * 0.1
    sz = _round_sz(notional / price, coin=coin)

    # Below the exchange minimum, ABORT -- never resize up.
    #
    # This used to read `sz = _round_sz(10.0 / price + 0.1, coin=coin)`. The
    # `+ 0.1` adds a tenth of a COIN, not a tenth of a dollar: on BTC it turns a
    # $10 floor into a ~$6,400 order, roughly 3200x the intended risk budget.
    #
    # The branch fires when `sz * price < 10`, and its most likely cause is
    # risk_usd == 0, which happens whenever get_account_value() cannot read a
    # balance. Before the fix below it defaulted to 0.0 on failure, so an
    # unreadable wallet and a wrong-wallet lookup both landed here. A size that
    # misses the minimum means the risk budget is already wrong; inflating it is
    # never the right recovery.
    if sz * price < 10:
        msg = (f"{coin}: computed size ${sz * price:.2f} is below the $10 "
               f"exchange minimum (risk_usd=${risk_usd:.2f}, "
               f"account=${account_val:.2f}) — trade aborted")
        logger.error(msg)
        try:
            import tg
            tg.dm_owner(f"⚠️ <b>Order aborted</b>\n{msg}")
        except Exception:
            pass
        return None

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
    original_tp_price = tp_price
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
    min_tp = entry_price + direction * risk_dist * tp_ratio
    tp_price = max(original_tp_price, min_tp) if direction == 1 else min(original_tp_price, min_tp)
    logger.info(f"Adjusted SL=${sl_price:.5f} TP=${tp_price:.5f} (R:R 1:{tp_ratio})")

    # Place SL order
    # LONG SL = SELL stop: limit must be BELOW trigger (accept selling into the drop)
    # SHORT SL = BUY stop: limit must be ABOVE trigger (accept buying into the rise)
    slippage_buf = BRACKET_SLIP_CAP
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


def get_close_fill(coin, since_ms=0):
    """Size-weighted average price of the fills that CLOSED `coin`, or None.

    The bot notices a position is gone up to POLL seconds after the exchange
    filled it, so reading the current mid at that moment records wherever the
    market drifted to in the meantime -- not the price actually traded. On the
    first live strategy-2 trade (ARB, 2026-07-27) the stop filled at 0.07817 but
    the mid 22s later was 0.07767, and the journal, the owner DM and the public
    channel post all recorded -19.4% for a trade that really lost -13.1%.
    Losses are biased worst by this: price keeps running after a stop.

    Returns None when no closing fill can be attributed, so callers can fall
    back to the old mid-price behaviour rather than record a zero.
    """
    info, _ = _clients()
    fills = _hl_call(info.user_fills, ACCOUNT_ADDRESS) or []
    sz_sum = notional = 0.0
    for f in fills:
        if f.get("coin") != coin or f.get("time", 0) < since_ms:
            continue
        if not str(f.get("dir", "")).startswith("Close"):
            continue
        sz = abs(float(f.get("sz", 0) or 0))
        if sz <= 0:
            continue
        sz_sum   += sz
        notional += sz * float(f.get("px", 0) or 0)
    return notional / sz_sum if sz_sum else None


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
    buf    = RATCHET_SLIP_CAP

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
                    continue  # if query fails, preserve unknown order — redundant SL is harmless, lost TP is not
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
    # The cancel loop above has ALREADY removed the protective orders by this
    # point -- when called without `entry` (the strategy-2 ratchet path) that
    # includes the take-profit AND the old stop. So a placement that fails here
    # leaves the position with nothing resting against it. Every other order
    # site in this file checks both the outer status and the inner one (the
    # outer "ok" can mask an inner error, see place_bracket); this one only
    # logged it, so a rejected ratchet stop would have been recorded as a
    # success and never retried. Raise instead, and let the caller decide.
    ok = result.get("status") == "ok"
    inner = None
    if ok:
        try:
            inner = result["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            inner = None
        if isinstance(inner, dict) and "error" in inner:
            ok = False
    if not ok:
        raise RuntimeError(f"SL placement rejected for {coin}: {inner or result}")

    logger.info(f"New SL for {coin} @ ${new_sl:.4f}: {result.get('status')}")
    return result
