"""
obr/exchange.py -- Bybit exchange wrapper for OBR bot.

  - ccxt.bybit with USDT perpetual (swap)
  - Native SL/TP on orders (exchange-managed)
  - set_trading_stop for updating positions
  - Position/order queries
  - Leverage and margin mode setup
"""


import time
import ccxt
from typing import Optional, Dict, List
from obr.config import (
    API_KEY, API_SECRET, MAINNET, DEMO_MODE, LEVERAGE, TIMEFRAME,
    LIMIT_ENTRY_ENABLED, LIMIT_ENTRY_TIMEOUT_SEC,
    MAKER_TP_ENABLED,
)
from obr import logger as log


def create_exchange() -> ccxt.bybit:
    """Create authenticated Bybit exchange instance."""
    ex = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
            "recvWindow": 20_000,
        },
    })

    if not MAINNET:
        if DEMO_MODE:
            ex.enable_demo_trading(True)
            mode_label = "DEMO"
        else:
            ex.set_sandbox_mode(True)
            mode_label = "testnet"
    else:
        mode_label = "MAINNET"

    ex.load_markets()
    log.info(f"Connected to Bybit ({mode_label}) -- "
             f"{len(ex.markets)} markets loaded")
    return ex


# ------------------------------------------------------------------
#  Balance
# ------------------------------------------------------------------

@log.timed_api
def get_equity(ex: ccxt.bybit) -> float:
    bal = ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    return float(usdt.get("total", 0))


@log.timed_api
def get_available_balance(ex: ccxt.bybit) -> float:
    """Return free (available) USDT balance — excludes margin locked by open positions."""
    bal = ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    return float(usdt.get("free", 0))


# ------------------------------------------------------------------
#  Setup
# ------------------------------------------------------------------

@log.timed_api
def set_leverage(ex: ccxt.bybit, symbol: str, leverage: int = LEVERAGE):
    try:
        mkt = ex.market(symbol)
        max_lev = mkt.get("limits", {}).get("leverage", {}).get("max")
        actual_lev = leverage
        if max_lev and leverage > max_lev:
            actual_lev = int(max_lev)
            log.info(f"  {symbol}: capped leverage {leverage}x -> {actual_lev}x (exchange max)")
        ex.set_leverage(actual_lev, symbol, {"category": "linear"})
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning(f"set_leverage {symbol}: {e}")


@log.timed_api
def set_margin_mode(ex: ccxt.bybit, symbol: str, mode: str = "isolated"):
    try:
        ex.set_margin_mode(mode, symbol, {"category": "linear"})
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg and "already" not in msg:
            log.warning(f"set_margin_mode {symbol}: {e}")


# ------------------------------------------------------------------
#  Market data
# ------------------------------------------------------------------

@log.timed_api
def fetch_latest_candles(
    ex: ccxt.bybit,
    symbol: str,
    n: int = 5,
    timeframe: str = TIMEFRAME,
) -> List[Dict]:
    """Fetch last N CLOSED candles (drops the forming candle).

    Validates that the most recent closed candle's timestamp matches
    the expected last-closed boundary so we never act on mid-formation data.
    """
    import time as _time
    from datetime import datetime, timezone

    # Determine interval in ms for timestamp validation
    if timeframe.endswith("m"):
        interval_ms = int(timeframe[:-1]) * 60_000
    elif timeframe.endswith("h"):
        interval_ms = int(timeframe[:-1]) * 3_600_000
    else:
        interval_ms = 300_000  # default 5m

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Expected open-timestamp of the CURRENT (forming) candle
    expected_forming_ts = (now_ms // interval_ms) * interval_ms
    # Expected open-timestamp of the LAST closed candle
    expected_closed_ts = expected_forming_ts - interval_ms

    for attempt in range(3):
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=n + 1)
        if not raw:
            return []

        # Drop the forming candle (last one)
        closed = raw[:-1]
        if not closed:
            return []

        # Validate: the last closed candle should match expected_closed_ts
        last_ts = int(closed[-1][0])
        if last_ts == expected_closed_ts:
            break  # candle is fully closed and current

        # If the last closed candle is OLDER, the exchange hasn't published
        # the new forming candle yet — wait and retry
        if last_ts < expected_closed_ts and attempt < 2:
            log.debug(f"  {symbol}: candle not finalised yet "
                      f"(got {last_ts}, want {expected_closed_ts}), "
                      f"retry {attempt+1}/3...")
            _time.sleep(2)
            continue
        break  # close enough or final attempt

    result = []
    for c in closed[-n:]:
        result.append({
            "ts": c[0],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return result


@log.timed_api
def get_ticker(ex: ccxt.bybit, symbol: str) -> Dict:
    return ex.fetch_ticker(symbol)


@log.timed_api
def get_funding_rate(ex: ccxt.bybit, symbol: str) -> float:
    try:
        info = ex.fetch_funding_rate(symbol)
        return float(info.get("fundingRate", 0) or 0)
    except Exception:
        return 0.0


# ------------------------------------------------------------------
#  Positions & orders
# ------------------------------------------------------------------

@log.timed_api
def get_open_positions(ex: ccxt.bybit, symbol: str = None) -> List[Dict]:
    try:
        positions = ex.fetch_positions([symbol] if symbol else None,
                                        params={"category": "linear"})
        return [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]
    except Exception as e:
        log.warning(f"get_open_positions: {e}")
        return []


@log.timed_api
def get_open_orders(ex: ccxt.bybit, symbol: str = None) -> List[Dict]:
    try:
        return ex.fetch_open_orders(symbol, params={"category": "linear"})
    except Exception as e:
        log.warning(f"get_open_orders: {e}")
        return []


# ------------------------------------------------------------------
#  Order placement
# ------------------------------------------------------------------

def _clamp_qty_to_max(ex, symbol, qty):
    mkt = ex.market(symbol)
    max_qty = mkt.get("limits", {}).get("amount", {}).get("max")
    if max_qty and qty > max_qty:
        log.warning(f"Clamping qty {qty} -> {max_qty} for {symbol}")
        return max_qty
    return qty


@log.timed_api
def place_market_order(
    ex: ccxt.bybit,
    symbol: str,
    side: str,
    qty: float,
    sl_price: float,
    tp_price: float,
) -> Dict:
    """Place market order with native SL/TP."""
    qty = _clamp_qty_to_max(ex, symbol, qty)

    # SL always uses market (safety: limit SL can be skipped in gaps)
    sl_params = {"triggerPrice": str(sl_price), "type": "market"}

    # TP can use limit (maker fees) if enabled
    if MAKER_TP_ENABLED:
        tp_params = {
            "triggerPrice": str(tp_price),
            "type": "limit",
            "limitPrice": str(round_price(ex, symbol, tp_price)),
        }
    else:
        tp_params = {"triggerPrice": str(tp_price), "type": "market"}

    params = {
        "category": "linear",
        "stopLoss": sl_params,
        "takeProfit": tp_params,
    }

    log.info(f"ORDER: {side.upper()} {qty} {symbol} | SL={sl_price} TP={tp_price}")
    log.order_placed(symbol, side, "market", qty, sl=sl_price, tp=tp_price)

    order = ex.create_order(
        symbol=symbol, type="market", side=side,
        amount=qty, params=params,
    )

    order_id = order.get("id", "unknown")
    avg_price = order.get("average") or order.get("price") or 0
    log.info(f"  -> Order ID: {order_id} | AvgPrice: {avg_price}")
    return order


@log.timed_api
def place_limit_order(
    ex: ccxt.bybit,
    symbol: str,
    side: str,
    qty: float,
    limit_price: float,
    sl_price: float,
    tp_price: float,
) -> Dict:
    """Place limit order with native SL/TP (maker fees)."""
    qty = _clamp_qty_to_max(ex, symbol, qty)
    limit_price = round_price(ex, symbol, limit_price)

    # SL always market (safety)
    sl_params = {"triggerPrice": str(sl_price), "type": "market"}

    # TP can use limit (maker fees) if enabled
    if MAKER_TP_ENABLED:
        tp_params = {
            "triggerPrice": str(tp_price),
            "type": "limit",
            "limitPrice": str(round_price(ex, symbol, tp_price)),
        }
    else:
        tp_params = {"triggerPrice": str(tp_price), "type": "market"}

    params = {
        "category": "linear",
        "stopLoss": sl_params,
        "takeProfit": tp_params,
    }

    log.info(f"LIMIT ORDER: {side.upper()} {qty} {symbol} @ {limit_price} | "
             f"SL={sl_price} TP={tp_price}")
    log.order_placed(symbol, side, "limit", qty, sl=sl_price, tp=tp_price)

    order = ex.create_order(
        symbol=symbol, type="limit", side=side,
        amount=qty, price=limit_price, params=params,
    )

    order_id = order.get("id", "unknown")
    log.info(f"  -> Limit Order ID: {order_id} @ {limit_price}")
    return order


@log.timed_api
def cancel_order(ex: ccxt.bybit, symbol: str, order_id: str) -> bool:
    """Cancel a specific order by ID."""
    try:
        ex.cancel_order(order_id, symbol, params={"category": "linear"})
        log.info(f"Cancelled order {order_id} for {symbol}")
        return True
    except Exception as e:
        msg = str(e)
        # Order already filled or cancelled
        if "110001" in msg or "110002" in msg or "110003" in msg:
            return True
        log.warning(f"cancel_order {symbol} {order_id}: {e}")
        return False


@log.timed_api
def fetch_order(ex: ccxt.bybit, symbol: str, order_id: str) -> Optional[Dict]:
    """Fetch order status by ID."""
    try:
        return ex.fetch_order(order_id, symbol, params={"category": "linear"})
    except Exception as e:
        log.debug(f"fetch_order {symbol} {order_id}: {e}")
        return None


# ------------------------------------------------------------------
#  Position management
# ------------------------------------------------------------------

@log.timed_api
def cancel_all_orders(ex: ccxt.bybit, symbol: str):
    try:
        ex.cancel_all_orders(symbol, params={"category": "linear"})
        log.info(f"Cancelled all orders for {symbol}")
    except Exception as e:
        log.warning(f"cancel_all_orders {symbol}: {e}")


@log.timed_api
def close_position(ex: ccxt.bybit, symbol: str):
    """Close any open position on a symbol via market order."""
    positions = get_open_positions(ex, symbol)
    for pos in positions:
        side = pos.get("side", "").lower()
        contracts = abs(float(pos.get("contracts", 0) or 0))
        if contracts <= 0:
            continue
        close_side = "sell" if side == "long" else "buy"
        log.info(f"CLOSING {symbol}: {close_side} {contracts}")
        ex.create_order(
            symbol=symbol, type="market", side=close_side,
            amount=contracts,
            params={"category": "linear", "reduceOnly": True},
        )


@log.timed_api
def set_trading_stop(
    ex: ccxt.bybit,
    symbol: str,
    side: str,
    sl_price: float = None,
    tp_price: float = None,
) -> bool:
    """Update SL/TP on an existing position."""
    try:
        params = {
            "category": "linear",
            "symbol": ex.market_id(symbol),
            "positionIdx": 0,
        }
        if sl_price is not None:
            params["stopLoss"] = str(round_price(ex, symbol, sl_price))
            params["slOrderType"] = "Market"  # SL always market
        if tp_price is not None:
            params["takeProfit"] = str(round_price(ex, symbol, tp_price))
            params["tpOrderType"] = "Limit" if MAKER_TP_ENABLED else "Market"

        ex.private_post_v5_position_trading_stop(params)
        log.info(f"Updated SL/TP for {symbol}: SL={sl_price} TP={tp_price}")
        return True
    except Exception as e:
        msg = str(e)
        # 34040 = "not modified" — SL/TP already at that value, ignore silently
        if "34040" in msg:
            return True
        log.error(f"set_trading_stop {symbol}: {e}")
        if "10001" in msg:
            return "CLOSED"
        return False


# ------------------------------------------------------------------
#  Precision helpers
# ------------------------------------------------------------------

def get_market_info(ex: ccxt.bybit, symbol: str) -> Dict:
    mkt = ex.market(symbol)
    return {
        "symbol": symbol,
        "price_precision": mkt.get("precision", {}).get("price"),
        "amount_precision": mkt.get("precision", {}).get("amount"),
        "min_qty": mkt.get("limits", {}).get("amount", {}).get("min"),
        "min_notional": mkt.get("limits", {}).get("cost", {}).get("min"),
    }


def round_price(ex: ccxt.bybit, symbol: str, price: float) -> float:
    return float(ex.price_to_precision(symbol, price))


def round_qty(ex: ccxt.bybit, symbol: str, qty: float) -> float:
    return float(ex.amount_to_precision(symbol, qty))


# ------------------------------------------------------------------
#  Closed PnL (actual trade results from Bybit)
# ------------------------------------------------------------------

@log.timed_api
def fetch_closed_pnl(ex: ccxt.bybit, symbol: str, limit: int = 5) -> List[Dict]:
    """
    Fetch recent closed PnL records for a symbol from Bybit v5 API.
    Returns list of dicts with: closedPnl, avgEntryPrice, avgExitPrice,
    side, qty, createdTime, orderId, etc.
    """
    try:
        market_id = ex.market_id(symbol)
        result = ex.private_get_v5_position_closed_pnl({
            "category": "linear",
            "symbol": market_id,
            "limit": str(limit),
        })
        records = result.get("result", {}).get("list", [])
        return records
    except Exception as e:
        log.warning(f"fetch_closed_pnl {symbol}: {e}")
        return []
