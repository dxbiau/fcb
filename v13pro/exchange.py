"""
v13pro/exchange.py -- Async Bybit exchange wrapper using ccxt.pro.

All exchange calls are async. No blocking calls anywhere.
"""

import asyncio
import ccxt.pro as ccxtpro
import ccxt
from typing import Optional, Dict, List
from v13pro import config as cfg
from v13pro import logger as log


_exchange: Optional[ccxtpro.bybit] = None


async def create_exchange() -> ccxtpro.bybit:
    """Create authenticated async Bybit exchange instance."""
    global _exchange
    ex = ccxtpro.bybit({
        "apiKey": cfg.API_KEY,
        "secret": cfg.API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
            "recvWindow": 20_000,
        },
    })

    if not cfg.MAINNET:
        if cfg.DEMO_MODE:
            ex.enable_demo_trading(True)
            mode = "DEMO"
        else:
            ex.set_sandbox_mode(True)
            mode = "testnet"
    else:
        mode = "MAINNET"

    await ex.load_markets()
    log.info(f"Connected to Bybit ({mode}) — "
             f"{len(ex.markets)} markets loaded [async]")
    _exchange = ex
    return ex


async def close_exchange():
    global _exchange
    if _exchange:
        try:
            await _exchange.close()
        except Exception:
            pass
        _exchange = None


async def get_equity(ex) -> float:
    bal = await ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    return float(usdt.get("total", 0))


async def get_available_balance(ex) -> float:
    bal = await ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT", {})
    return float(usdt.get("free", 0))


async def set_leverage(ex, symbol: str, leverage: int):
    try:
        mkt = ex.market(symbol)
        max_lev = mkt.get("limits", {}).get("leverage", {}).get("max")
        actual = leverage
        if max_lev and leverage > max_lev:
            actual = int(max_lev)
        await ex.set_leverage(actual, symbol, {"category": "linear"})
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning(f"set_leverage {symbol}: {e}")


async def set_margin_mode(ex, symbol: str, mode: str = "cross"):
    try:
        await ex.set_margin_mode(mode, symbol, {"category": "linear"})
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg and "already" not in msg:
            log.warning(f"set_margin_mode {symbol}: {e}")


async def set_position_mode(ex, symbol: str, mode: str = "oneway"):
    """Set position mode to one-way (MergedSingle) or hedge (BothSide).

    Must be one-way to avoid 'position idx not match position mode' errors.
    """
    try:
        bybit_mode = 0 if mode == "oneway" else 3  # 0=MergedSingle, 3=BothSide
        await ex.private_post_v5_position_switch_mode({
            "category": "linear",
            "symbol": ex.market_id(symbol),
            "mode": bybit_mode,
        })
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg and "same" not in msg and "not need" not in msg:
            log.debug(f"set_position_mode {symbol}: {e}")


def get_market_info(ex, symbol: str) -> dict:
    """Sync — market info is already loaded."""
    return ex.market(symbol)


def round_qty(ex, symbol: str, qty: float) -> float:
    return float(ex.amount_to_precision(symbol, qty))


def round_price(ex, symbol: str, price: float) -> float:
    return float(ex.price_to_precision(symbol, price))


async def fetch_ohlcv(ex, symbol: str, timeframe: str = "15m",
                      limit: int = 220) -> List[list]:
    """Fetch OHLCV candles via REST as fallback."""
    return await ex.fetch_ohlcv(symbol, timeframe, limit=limit)


async def fetch_latest_candles(ex, symbol: str, n: int = 220,
                                timeframe: str = "15m") -> List[dict]:
    """Fetch last N closed candles, return list of dicts."""
    raw = await ex.fetch_ohlcv(symbol, timeframe, limit=n + 1)
    if not raw:
        return []
    # Drop last (forming) candle
    closed = raw[:-1] if len(raw) > n else raw
    return [{"ts": c[0], "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]),
             "volume": float(c[5])} for c in closed[-n:]]


def _map_side(side: str) -> str:
    """Map long/short to buy/sell for ccxt."""
    return {"long": "buy", "short": "sell"}.get(side, side)


async def place_market_order(ex, symbol, side, qty, sl, tp):
    """Place market order with SL/TP.

    Uses simple takeProfit/stopLoss params (Bybit defaults to Market type).
    """
    side = _map_side(side)
    params = {
        "category": "linear",
        "stopLoss": str(sl),
        "takeProfit": str(tp),
    }

    order = await ex.create_order(symbol, "market", side, qty, params=params)
    return order


async def place_limit_order(ex, symbol, side, qty, price, sl, tp):
    """Place limit entry order with SL/TP.

    Uses simple takeProfit/stopLoss params (Bybit defaults to Market type).
    """
    side = _map_side(side)
    params = {
        "category": "linear",
        "stopLoss": str(sl),
        "takeProfit": str(tp),
    }

    order = await ex.create_order(symbol, "limit", side, qty, price, params=params)
    return order


async def fetch_order(ex, symbol, order_id):
    return await ex.fetch_order(order_id, symbol)


async def cancel_order(ex, symbol, order_id):
    return await ex.cancel_order(order_id, symbol)


async def get_open_positions(ex, symbol=None):
    """Fetch all open positions."""
    params = {"category": "linear"}
    if symbol:
        params["symbol"] = ex.market_id(symbol)
    positions = await ex.fetch_positions(params=params)
    return [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]


async def close_position(ex, symbol):
    """Close a position by placing opposite market order."""
    positions = await get_open_positions(ex, symbol)
    for pos in positions:
        side = pos.get("side", "").lower()
        contracts = abs(float(pos.get("contracts", 0) or 0))
        if contracts > 0:
            close_side = "sell" if side == "long" else "buy"
            await ex.create_order(symbol, "market", close_side, contracts,
                                  params={"category": "linear", "reduceOnly": True})


async def partial_close_position(ex, symbol, fraction: float = 0.5):
    """Close a fraction of an open position (for partial TP).

    fraction: 0.0–1.0 portion of position to close.
    Returns the qty actually closed, or 0 if failed.
    """
    positions = await get_open_positions(ex, symbol)
    for pos in positions:
        side = pos.get("side", "").lower()
        contracts = abs(float(pos.get("contracts", 0) or 0))
        if contracts > 0:
            close_qty = round_qty(ex, symbol, contracts * fraction)
            if close_qty <= 0:
                return 0.0
            close_side = "sell" if side == "long" else "buy"
            await ex.create_order(symbol, "market", close_side, close_qty,
                                  params={"category": "linear", "reduceOnly": True})
            log.info(f"Partial close {symbol}: {fraction*100:.0f}% "
                     f"({close_qty}/{contracts} contracts)")
            return close_qty
    return 0.0


async def set_trading_stop(ex, symbol, direction, sl_price=None, tp_price=None):
    """Update SL/TP on an open position via Bybit set_trading_stop."""
    try:
        side_str = "Buy" if direction == "long" else "Sell"
        params = {"category": "linear", "symbol": ex.market_id(symbol),
                  "positionIdx": 0, "tpSlMode": "Full"}
        if sl_price is not None:
            params["stopLoss"] = str(round_price(ex, symbol, sl_price))
        if tp_price is not None:
            params["takeProfit"] = str(round_price(ex, symbol, tp_price))

        await ex.private_post_v5_position_trading_stop(params)
        return True
    except Exception as e:
        msg = str(e).lower()
        if "position is not" in msg or "not exist" in msg:
            return "CLOSED"
        if "not modified" in msg or "same value" in msg:
            return True  # already at this SL, not an error
        log.warning(f"set_trading_stop {symbol}: {e}")
        return False


async def fetch_closed_pnl(ex, symbol, limit=3):
    """Fetch recent closed PnL records."""
    try:
        resp = await ex.private_get_v5_position_closed_pnl({
            "category": "linear",
            "symbol": ex.market_id(symbol),
            "limit": str(limit),
        })
        return resp.get("result", {}).get("list", [])
    except Exception as e:
        log.warning(f"fetch_closed_pnl {symbol}: {e}")
        return []


async def fetch_tickers(ex, symbols=None):
    """Fetch tickers for all or specific symbols."""
    if symbols:
        return await ex.fetch_tickers(symbols[:500])
    return await ex.fetch_tickers()


async def fetch_funding_rate(ex, symbol: str) -> float:
    """Fetch current predicted funding rate for a symbol.

    Returns rate as a percentage (e.g., 0.01 = 0.01%).
    Positive = longs pay shorts; negative = shorts pay longs.
    """
    try:
        resp = await ex.fetch_funding_rate(symbol)
        # ccxt returns fundingRate as decimal (0.0001 = 0.01%)
        rate = float(resp.get("fundingRate", 0) or 0)
        return rate * 100  # convert to percentage
    except Exception as e:
        log.debug(f"fetch_funding_rate {symbol}: {e}")
        return 0.0


async def fetch_funding_rates_batch(ex, symbols: list) -> Dict[str, float]:
    """Fetch funding rates for multiple symbols. Returns {symbol: rate%}."""
    rates = {}
    for sym in symbols:
        try:
            r = await fetch_funding_rate(ex, sym)
            rates[sym] = r
        except Exception:
            rates[sym] = 0.0
        await asyncio.sleep(0.05)  # tiny delay to avoid rate limits
    return rates
