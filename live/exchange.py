"""
live/exchange.py — Bybit exchange wrapper using ccxt.

Handles:
  - Connection & authentication (mainnet / demo / testnet)
  - Market data (candles, ticker)
  - Order placement (market + SL/TP)
  - Position queries
  - Balance queries
  - Leverage setting
"""

import time
import ccxt
from typing import Optional, Dict, List
from live.config import API_KEY, API_SECRET, MAINNET, DEMO_MODE, LEVERAGE, TIMEFRAME
from live import logger as log


def create_exchange() -> ccxt.bybit:
    """Create authenticated Bybit exchange instance.

    Modes (controlled by config flags):
      MAINNET=True, DEMO_MODE=False  → real mainnet trading
      MAINNET=False, DEMO_MODE=True  → Bybit Demo Trading (api-demo.bybit.com)
      MAINNET=False, DEMO_MODE=False → Bybit Testnet (api-testnet.bybit.com)
    """
    ex = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",       # USDT perpetual
            "adjustForTimeDifference": True,
            "recvWindow": 20_000,        # 20s — handles Windows clock drift
        },
    })

    if not MAINNET:
        if DEMO_MODE:
            # Bybit Demo Trading — uses api-demo.bybit.com
            ex.enable_demo_trading(True)
            mode_label = "DEMO"
        else:
            # Bybit Testnet — uses api-testnet.bybit.com
            ex.set_sandbox_mode(True)
            mode_label = "testnet"
    else:
        mode_label = "MAINNET"

    ex.load_markets()
    log.info(f"Connected to Bybit ({mode_label}) — "
             f"{len(ex.markets)} markets loaded")
    return ex


@log.timed_api
def get_balance(ex: ccxt.bybit) -> float:
    """Get available USDT balance."""
    bal = ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    total = float(usdt.get("total", 0))
    return total


@log.timed_api
def get_equity(ex: ccxt.bybit) -> float:
    """Get total equity (balance + unrealised PnL)."""
    bal = ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    # ccxt returns 'total' as equity for derivatives
    return float(usdt.get("total", 0))


@log.timed_api
def set_leverage(ex: ccxt.bybit, symbol: str, leverage: int = LEVERAGE):
    """Set leverage for a symbol. SKIP pairs whose max leverage < required."""
    try:
        # Check market's max leverage — skip if below required
        mkt = ex.market(symbol)
        max_lev = mkt.get("limits", {}).get("leverage", {}).get("max")
        if max_lev and leverage > max_lev:
            raise ValueError(
                f"max leverage {int(max_lev)}x < required {leverage}x — EXCLUDED"
            )
        ex.set_leverage(leverage, symbol, {"category": "linear"})
    except ValueError:
        raise  # re-raise exclusion errors so caller can skip this pair
    except Exception as e:
        # "leverage not modified" is fine
        if "not modified" not in str(e).lower():
            log.warning(f"set_leverage {symbol}: {e}")


@log.timed_api
def set_margin_mode(ex: ccxt.bybit, symbol: str, mode: str = "isolated"):
    """Set margin mode (isolated/cross). Silently handles if already set."""
    try:
        ex.set_margin_mode(mode, symbol, {"category": "linear"})
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning(f"set_margin_mode {symbol}: {e}")


@log.timed_api
def fetch_latest_candles(ex: ccxt.bybit, symbol: str, n: int = 5,
                         timeframe: str = None) -> List[Dict]:
    """
    Fetch the last N closed candles.
    Returns list of dicts: {ts, open, high, low, close, volume}
    Sorted oldest → newest.

    timeframe: override (e.g. "15m"). Defaults to config TIMEFRAME ("5m").
    """
    tf = timeframe or TIMEFRAME
    # Determine candle duration in ms for forming-candle detection
    tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                  "1h": 60, "4h": 240}.get(tf, 5)
    tf_ms = tf_minutes * 60 * 1000

    # Fetch n+1 to ensure we get n *closed* candles (last may be forming)
    raw = ex.fetch_ohlcv(symbol, tf, limit=n + 1)
    if not raw:
        return []

    candles = []
    for r in raw:
        candles.append({
            "ts": r[0],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        })

    # Drop the last candle if it's still forming (not yet closed)
    now_ms = int(time.time() * 1000)
    if candles and (now_ms - candles[-1]["ts"]) < tf_ms:
        candles = candles[:-1]

    return candles[-n:]  # return last n closed


@log.timed_api
def get_ticker(ex: ccxt.bybit, symbol: str) -> Dict:
    """Get current ticker (last price, bid, ask)."""
    return ex.fetch_ticker(symbol)


def get_funding_rate(ex: ccxt.bybit, symbol: str) -> float:
    """Get current funding rate for a symbol.
    Returns the rate as a decimal (e.g. 0.0001 = 0.01%).
    Positive = longs pay shorts, Negative = shorts pay longs.
    Returns 0.0 on any error (fail-open: never blocks a trade on API failure).
    """
    try:
        resp = ex.fetch_funding_rate(symbol)
        return float(resp.get("fundingRate", 0) or 0)
    except Exception:
        return 0.0


@log.timed_api
def get_open_positions(ex: ccxt.bybit, symbol: Optional[str] = None) -> List[Dict]:
    """Get open positions. Filter by symbol if given."""
    positions = ex.fetch_positions([symbol] if symbol else None,
                                   params={"category": "linear"})
    # Filter to only those with non-zero size
    return [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]


@log.timed_api
def get_open_orders(ex: ccxt.bybit, symbol: Optional[str] = None) -> List[Dict]:
    """Get open orders for a symbol."""
    return ex.fetch_open_orders(symbol, params={"category": "linear"})


def _clamp_qty_to_max(ex: ccxt.bybit, symbol: str, qty: float) -> float:
    """Clamp qty to market's maxAmount to avoid 'exceeds maximum limit' rejections."""
    try:
        mkt = ex.market(symbol)
        max_qty = mkt.get("limits", {}).get("amount", {}).get("max")
        if max_qty and qty > max_qty:
            log.warning(f"  {symbol}: qty {qty} exceeds max {max_qty} — clamped")
            return max_qty
    except Exception:
        pass
    return qty


@log.timed_api
def place_market_order(
    ex: ccxt.bybit,
    symbol: str,
    side: str,           # "buy" or "sell"
    qty: float,          # in base currency units
    sl_price: float,     # stop-loss trigger price
    tp_price: float,     # take-profit trigger price
) -> Dict:
    """
    Place a market order with attached stop-loss and take-profit.

    Bybit supports setting SL/TP directly on the order via params.
    This creates a position with native exchange-managed SL/TP —
    no need for separate conditional orders.
    """
    # Clamp qty to exchange max
    qty = _clamp_qty_to_max(ex, symbol, qty)

    params = {
        "category": "linear",
        "stopLoss": {
            "triggerPrice": str(sl_price),
            "type": "market",
        },
        "takeProfit": {
            "triggerPrice": str(tp_price),
            "type": "market",
        },
    }

    log.info(f"ORDER: {side.upper()} {qty} {symbol} | SL={sl_price} TP={tp_price}")
    log.order_placed(symbol, side, "market", qty, sl=sl_price, tp=tp_price)

    order = ex.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=qty,
        params=params,
    )

    order_id = order.get("id", "unknown")
    status = order.get("status", "unknown")
    avg_price = order.get("average") or order.get("price") or 0
    log.info(f"  → Order ID: {order_id} | Status: {status} | AvgPrice: {avg_price}")
    log.order_placed(symbol, side, "market", qty, sl=sl_price, tp=tp_price,
                     order_id=order_id, notes=f"status={status} avg={avg_price}")
    return order


@log.timed_api
def place_limit_order(
    ex: ccxt.bybit,
    symbol: str,
    side: str,           # "buy" or "sell"
    qty: float,          # in base currency units
    limit_price: float,  # limit price (FC boundary)
    sl_price: float,     # stop-loss trigger price
    tp_price: float,     # take-profit trigger price
) -> Dict:
    """
    Place a limit order for split-entry scale-in at FC boundary.

    The limit sits at the FC high (long) or FC low (short), giving a
    better entry price if price retraces after the initial breakout.
    SL/TP are attached so the position is fully exchange-managed.
    """
    # Clamp qty to exchange max
    qty = _clamp_qty_to_max(ex, symbol, qty)

    params = {
        "category": "linear",
        "stopLoss": {
            "triggerPrice": str(sl_price),
            "type": "market",
        },
        "takeProfit": {
            "triggerPrice": str(tp_price),
            "type": "market",
        },
        "timeInForce": "GTC",
    }

    log.info(f"LIMIT ORDER: {side.upper()} {qty} {symbol} @ {limit_price} | "
             f"SL={sl_price} TP={tp_price}")
    log.order_placed(symbol, side, "limit", qty, price=limit_price,
                     sl=sl_price, tp=tp_price, notes="scale-in")

    order = ex.create_order(
        symbol=symbol,
        type="limit",
        side=side,
        amount=qty,
        price=limit_price,
        params=params,
    )

    order_id = order.get("id", "unknown")
    log.info(f"  → Limit Order ID: {order_id} | Status: {order.get('status')}")
    return order


@log.timed_api
def place_reduce_only_stop(
    ex: ccxt.bybit,
    symbol: str,
    side: str,           # "buy" or "sell" — the CLOSING side
    qty: float,          # quantity to close
    trigger_price: float,  # price at which the order activates
    direction: str,      # "long" or "short" — the POSITION direction
) -> Dict:
    """
    Place a conditional (trigger) reduce-only market order.

    Used for scale-out: close 50% of position when price retraces
    to FC boundary.  Unlike a limit order (which would fill immediately
    when placed at a price the market has already passed), a conditional
    order only activates when price reaches the trigger level.

    For LONG positions: triggers when price FALLS to trigger_price.
    For SHORT positions: triggers when price RISES to trigger_price.
    """
    qty = _clamp_qty_to_max(ex, symbol, qty)

    # triggerDirection: 1 = triggered when price rises to/above trigger
    #                   2 = triggered when price falls to/below trigger
    trigger_dir = 2 if direction == "long" else 1

    params = {
        "category": "linear",
        "reduceOnly": True,
        "triggerPrice": trigger_price,
        "triggerDirection": trigger_dir,
    }

    log.info(f"SCALE-OUT STOP: {side.upper()} {qty} {symbol} "
             f"trigger @ {trigger_price} (reduce-only, dir={trigger_dir})")
    log.order_placed(symbol, side, "conditional-market", qty, price=trigger_price,
                     notes="scale-out reduce-only stop")

    order = ex.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=qty,
        price=None,
        params=params,
    )

    order_id = order.get("id", "unknown")
    log.info(f"  → Scale-out Order ID: {order_id} | Status: {order.get('status')}")
    return order


@log.timed_api
def cancel_order(ex: ccxt.bybit, order_id: str, symbol: str) -> bool:
    """Cancel a specific order by ID (regular or conditional/stop).
    Returns True if cancelled successfully."""
    try:
        ex.cancel_order(order_id, symbol, params={"category": "linear"})
        log.info(f"Cancelled order {order_id} for {symbol}")
        log.order_cancelled(symbol, order_id, reason="requested")
        return True
    except Exception as e:
        msg = str(e).lower()
        # Order already filled or cancelled — not an error
        if "not found" in msg or "already" in msg or "filled" in msg:
            # Might be a conditional order — try with orderFilter
            try:
                ex.cancel_order(order_id, symbol,
                                params={"category": "linear",
                                        "orderFilter": "StopOrder"})
                log.info(f"Cancelled conditional order {order_id} for {symbol}")
                log.order_cancelled(symbol, order_id, reason="stop-order-cancelled")
                return True
            except Exception:
                pass
            log.info(f"Order {order_id} for {symbol} already resolved: {e}")
            log.order_cancelled(symbol, order_id, reason=f"already_resolved: {e}")
            return True
        log.warning(f"cancel_order {order_id} {symbol}: {e}")
        return False


@log.timed_api
def get_order_status(ex: ccxt.bybit, order_id: str, symbol: str) -> Dict:
    """Fetch the current status of a regular or conditional (stop) order.

    Checks regular open orders first, then conditional/stop orders.
    Falls back to fetch_order and finally position-size heuristic.
    """
    try:
        # 1. Check regular open orders
        open_orders = ex.fetch_open_orders(symbol, params={"category": "linear"})
        for o in open_orders:
            if o.get("id") == order_id:
                return {"id": order_id, "status": "open", "symbol": symbol}

        # 2. Check conditional/stop open orders (scale-out uses these)
        try:
            stop_orders = ex.fetch_open_orders(
                symbol, params={"category": "linear", "orderFilter": "StopOrder"}
            )
            for o in stop_orders:
                if o.get("id") == order_id:
                    return {"id": order_id, "status": "open", "symbol": symbol}
        except Exception:
            pass  # exchange may not support this query

        # 3. Not in any open orders → try fetch_order
        try:
            info = ex.fetch_order(order_id, symbol,
                                  params={"category": "linear",
                                          "acknowledged": True})
            return info
        except Exception:
            pass

        # 4. Fallback: order is gone from open orders, assume closed/cancelled
        # Caller must check position to determine if it was a fill
        return {"id": order_id, "status": "gone", "symbol": symbol,
                "_note": "not in open orders; check position size"}

    except Exception as e:
        log.warning(f"get_order_status {order_id} {symbol}: {e}")
        return {}


@log.timed_api
def cancel_all_orders(ex: ccxt.bybit, symbol: str):
    """Cancel all open orders for a symbol."""
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
            symbol=symbol,
            type="market",
            side=close_side,
            amount=contracts,
            params={"category": "linear", "reduceOnly": True},
        )


def get_market_info(ex: ccxt.bybit, symbol: str) -> Dict:
    """Get market precision and limits for a symbol."""
    mkt = ex.market(symbol)
    return {
        "symbol": symbol,
        "base": mkt.get("base"),
        "quote": mkt.get("quote"),
        "price_precision": mkt.get("precision", {}).get("price"),
        "amount_precision": mkt.get("precision", {}).get("amount"),
        "min_qty": mkt.get("limits", {}).get("amount", {}).get("min"),
        "min_notional": mkt.get("limits", {}).get("cost", {}).get("min"),
        "contract_size": mkt.get("contractSize", 1),
    }


def round_price(ex: ccxt.bybit, symbol: str, price: float) -> float:
    """Round price to exchange precision."""
    return float(ex.price_to_precision(symbol, price))


def round_qty(ex: ccxt.bybit, symbol: str, qty: float) -> float:
    """Round quantity to exchange precision."""
    return float(ex.amount_to_precision(symbol, qty))


@log.timed_api
def set_trading_stop(
    ex: ccxt.bybit,
    symbol: str,
    side: str,           # "long" or "short" (position side)
    sl_price: float = None,
    tp_price: float = None,
) -> bool:
    """
    Update SL/TP on an existing position via Bybit's set-trading-stop.

    CRITICAL: After scale-in fill on Bybit, the limit order's SL/TP
    overwrites the base position's SL/TP (because positions merge).
    This function corrects the TP back to the base position's wider target.

    Returns True if successful.
    """
    try:
        params = {
            "category": "linear",
            "symbol": ex.market_id(symbol),
            "positionIdx": 0,  # 0 = one-way mode (we don't use hedge mode)
        }
        if sl_price is not None:
            params["stopLoss"] = str(round_price(ex, symbol, sl_price))
        if tp_price is not None:
            params["takeProfit"] = str(round_price(ex, symbol, tp_price))

        ex.private_post_v5_position_trading_stop(params)
        log.info(f"Updated trading stop for {symbol}: "
                 f"SL={sl_price} TP={tp_price}")
        log.audit("TRADING_STOP_SET", symbol=symbol, side=side,
                  sl=str(sl_price), tp=str(tp_price))
        return True
    except Exception as e:
        msg = str(e)
        log.error(f"set_trading_stop {symbol}: {e}")
        # retCode 10001 = "can not set tp/sl/ts for zero position"
        # Position already closed — caller should stop retrying
        if "10001" in msg:
            return "CLOSED"
        return False
