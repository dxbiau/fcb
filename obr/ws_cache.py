"""
obr/ws_cache.py -- WebSocket candle cache for OBR bot.

Uses ccxt.pro (async WebSocket) to subscribe to kline streams.
Runs in a background thread with its own event loop.
Provides a synchronous get_candles(symbol) API for the bot.

Benefits:
  - Zero REST API calls for subscribed pairs
  - Real-time candle updates pushed from exchange
  - Thread-safe cache readable from the main bot thread
"""

import asyncio
import threading
import time
from typing import Dict, List, Optional, Set
from collections import defaultdict

from obr import logger as log
from obr import config as cfg


class WSCandleCache:
    """WebSocket-based candle cache using ccxt.pro."""

    def __init__(self, api_key: str, api_secret: str,
                 timeframe: str = "5m", max_candles: int = 10):
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeframe = timeframe
        self._max_candles = max_candles

        # Thread-safe cache: symbol -> list of candle dicts (oldest first)
        self._cache: Dict[str, List[dict]] = defaultdict(list)
        self._lock = threading.Lock()

        # Subscription management
        self._symbols: Set[str] = set()
        self._pending_subs: Set[str] = set()  # symbols waiting to be subscribed
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ready = threading.Event()  # set once first data arrives
        self._ws_exchange = None

    def start(self, symbols: List[str]):
        """Start WebSocket cache in background thread."""
        self._symbols = set(symbols)
        self._pending_subs = set(symbols)
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                         daemon=True, name="ws-cache")
        self._thread.start()
        # Wait up to 15s for first data
        self._ready.wait(timeout=15)

    def stop(self):
        """Stop WebSocket cache."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._ws_exchange:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ws_exchange.close(), self._loop
                ).result(timeout=5)
            except Exception:
                pass

    def add_symbol(self, symbol: str):
        """Dynamically add a symbol to the WebSocket subscriptions."""
        if symbol not in self._symbols:
            self._symbols.add(symbol)
            self._pending_subs.add(symbol)

    def get_candles(self, symbol: str, n: int = 5) -> Optional[List[dict]]:
        """
        Get last N closed candles for a symbol from cache.
        Returns None if symbol not cached, insufficient data, or stale.
        Staleness check: the latest closed candle must be from the
        current or immediately previous candle boundary.
        """
        from datetime import datetime, timezone

        # Determine interval in ms
        tf = self._timeframe
        if tf.endswith("m"):
            interval_ms = int(tf[:-1]) * 60_000
        elif tf.endswith("h"):
            interval_ms = int(tf[:-1]) * 3_600_000
        else:
            interval_ms = 300_000

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expected_closed_ts = ((now_ms // interval_ms) * interval_ms) - interval_ms

        with self._lock:
            candles = self._cache.get(symbol, [])
            if not candles or len(candles) < n:
                return None

            # Staleness guard: last closed candle must match expected boundary
            last_ts = int(candles[-1]["ts"])
            if last_ts < expected_closed_ts:
                return None  # stale — force REST fallback with validation

            return list(candles[-n:])

    def has_symbol(self, symbol: str) -> bool:
        """Check if a symbol is being tracked by WebSocket."""
        return symbol in self._symbols

    @property
    def cached_symbols(self) -> Set[str]:
        with self._lock:
            return set(self._cache.keys())

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "subscribed": len(self._symbols),
                "cached": len(self._cache),
                "candles": {s: len(c) for s, c in self._cache.items()},
            }

    # ─── Internal ────────────────────────────────────────────

    def _run_loop(self):
        """Background thread: create event loop and run WebSocket."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_main())
        except Exception as e:
            log.warning(f"WSCache loop exited: {e}")
        finally:
            self._loop.close()

    async def _ws_main(self):
        """Main async loop: connect and watch candles."""
        from ccxt.pro import bybit

        self._ws_exchange = bybit({
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
            },
        })

        if cfg.MAINNET:
            pass  # production by default
        elif cfg.DEMO_MODE:
            self._ws_exchange.enable_demo_trading(True)
        else:
            self._ws_exchange.set_sandbox_mode(True)

        # Load markets so exchange knows valid symbols
        await self._ws_exchange.load_markets()

        log.info(f"  📡 WSCache: connected to Bybit WebSocket "
                 f"({len(self._symbols)} pairs, {self._timeframe})")

        first_data = False

        try:
            while self._running:
                # Process any pending subscriptions
                symbols_to_watch = list(self._symbols)
                if not symbols_to_watch:
                    await asyncio.sleep(1)
                    continue

                try:
                    # watch_ohlcv_for_symbols expects [[symbol, timeframe], ...]
                    symbol_tf_pairs = [[s, self._timeframe] for s in symbols_to_watch]
                    ohlcvs = await self._ws_exchange.watch_ohlcv_for_symbols(
                        symbol_tf_pairs,
                        limit=self._max_candles + 1,
                    )

                    # ohlcvs is a dict: {symbol: {timeframe: [[ts, o, h, l, c, v], ...] }}
                    for sym, tf_data in ohlcvs.items():
                        candle_list = tf_data.get(self._timeframe, [])
                        if not candle_list:
                            continue

                        # Convert to our candle format, drop forming candle (last)
                        closed = candle_list[:-1] if len(candle_list) > 1 else []
                        formatted = []
                        for c in closed:
                            formatted.append({
                                "ts": c[0],
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                                "volume": float(c[5]),
                            })

                        if formatted:
                            with self._lock:
                                self._cache[sym] = formatted[-self._max_candles:]

                            if not first_data:
                                first_data = True
                                self._ready.set()
                                log.info(f"  📡 WSCache: first data received "
                                         f"({len(self._cache)} pairs cached)")

                except Exception as e:
                    err_str = str(e).lower()
                    if "connection" in err_str or "closed" in err_str:
                        log.warning(f"  📡 WSCache: reconnecting... ({e})")
                        await asyncio.sleep(2)
                    else:
                        log.debug(f"  📡 WSCache watch error: {e}")
                        await asyncio.sleep(0.5)

        finally:
            try:
                await self._ws_exchange.close()
            except Exception:
                pass
