"""
v13pro/ws_data.py -- Async WebSocket multi-TF data engine.

Maintains live candle buffers for ALL portfolio pairs across ALL timeframes
via concurrent WebSocket streams. Zero REST polling for candle data.

Architecture:
  - One ccxt.pro exchange for WS connections
  - watch_ohlcv_for_symbols for batch subscriptions
  - Candle buffers: {(symbol, tf): deque of candle dicts}
  - Event-driven: asyncio.Event fires when a candle closes
  - Thread-safe reads via asyncio locks
"""

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import ccxt.pro as ccxtpro
import numpy as np

from v13pro import config as cfg
from v13pro import logger as log

# Candle close event payload
CandleClose = Tuple[str, str]  # (symbol, timeframe)


class WSDataEngine:
    """Async WebSocket candle data engine for multi-TF portfolio."""

    def __init__(self, max_candles: int = 220):
        self._max_candles = max_candles
        self._ex: Optional[ccxtpro.bybit] = None

        # Candle buffer: (symbol, tf) -> deque of closed candle dicts
        self._buffers: Dict[Tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=max_candles))
        self._lock = asyncio.Lock()

        # Subscriptions
        self._subs: Dict[str, Set[str]] = defaultdict(set)  # tf -> set of symbols
        self._running = False

        # Event: fires when ANY candle closes (listeners check which)
        self.candle_closed = asyncio.Event()
        self._recent_closes: List[CandleClose] = []

        # Last known timestamp per (symbol, tf) to detect new closes
        self._last_ts: Dict[Tuple[str, str], int] = {}

        # Stats
        self._updates = 0
        self._closes = 0
        self._errors = 0
        self._connected = False

    async def start(self, subscriptions: Dict[str, Set[str]]):
        """
        Start WS engine.

        Args:
            subscriptions: {timeframe: {symbol1, symbol2, ...}}
        """
        self._subs = subscriptions
        self._running = True

        # Create async exchange
        self._ex = ccxtpro.bybit({
            "apiKey": cfg.API_KEY,
            "secret": cfg.API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        if not cfg.MAINNET:
            if cfg.DEMO_MODE:
                self._ex.enable_demo_trading(True)
            else:
                self._ex.set_sandbox_mode(True)

        await self._ex.load_markets()
        self._connected = True

        total_subs = sum(len(s) for s in self._subs.values())
        log.info(f"  📡 WSData: starting {total_subs} subscriptions "
                 f"across {len(self._subs)} TFs")

        # Pre-fetch historical candles via REST before WS starts
        # (WS only provides forming candle — no history)
        await self._prefetch_history()

        # Launch one watcher task per TF
        tasks = []
        for tf, symbols in self._subs.items():
            if symbols:
                tasks.append(asyncio.create_task(
                    self._watch_tf(tf, list(symbols)),
                    name=f"ws-{tf}"))

        # Also watch 1m for rejection exit detection
        all_symbols = set()
        for s in self._subs.values():
            all_symbols.update(s)
        if all_symbols:
            tasks.append(asyncio.create_task(
                self._watch_tf("1m", list(all_symbols)),
                name="ws-1m"))

        self._tasks = tasks
        log.info(f"  📡 WSData: {len(tasks)} stream tasks launched")

    async def _prefetch_history(self):
        """Fetch historical candles via REST to populate buffers before WS starts.

        Bybit WS only sends the forming candle — no historical bars.
        Without this, strategies needing SMA(200) etc. would have no data.
        """
        total = 0
        errors = 0

        for tf, symbols in self._subs.items():
            for sym in symbols:
                try:
                    candles = await self._ex.fetch_ohlcv(
                        sym, tf, limit=self._max_candles + 1)
                    if not candles:
                        continue

                    key = (sym, tf)
                    # Separate closed from forming
                    closed = candles[:-1] if len(candles) > 1 else []
                    if not closed:
                        continue

                    async with self._lock:
                        buf = self._buffers[key]
                        buf.clear()
                        for c in closed[-self._max_candles:]:
                            buf.append({
                                "ts": c[0], "open": float(c[1]),
                                "high": float(c[2]), "low": float(c[3]),
                                "close": float(c[4]), "volume": float(c[5]),
                            })
                        # Set last_ts so close detection works correctly
                        self._last_ts[key] = closed[-1][0]

                    total += 1
                except Exception as e:
                    errors += 1
                    log.debug(f"  📡 Prefetch failed {sym}/{tf}: {e}")

        log.info(f"  📡 WSData: pre-fetched {total} buffers "
                 f"({self._max_candles} bars each)"
                 + (f", {errors} errors" if errors else ""))

    async def stop(self):
        self._running = False
        for t in getattr(self, '_tasks', []):
            t.cancel()
        if self._ex:
            try:
                await self._ex.close()
            except Exception:
                pass
        self._connected = False

    async def _watch_tf(self, tf: str, symbols: List[str]):
        """Watch OHLCV for all symbols on a single timeframe.

        IMPORTANT: WS only streams the forming candle. Pre-fetched history
        is already in self._buffers. We only APPEND new closed candles —
        never clear/rebuild the buffer (that would destroy history).
        """
        while self._running:
            try:
                # Build symbol-tf pairs for batch subscription
                sym_tf_pairs = [[s, tf] for s in symbols]

                # Batch into chunks to avoid overwhelming WS
                chunk_size = cfg.WS_MAX_SUBS_PER_BATCH
                for i in range(0, len(sym_tf_pairs), chunk_size):
                    chunk = sym_tf_pairs[i:i + chunk_size]

                    ohlcvs = await self._ex.watch_ohlcv_for_symbols(
                        chunk, limit=self._max_candles + 1)

                    # Process results
                    async with self._lock:
                        for sym, tf_data in ohlcvs.items():
                            candle_list = tf_data.get(tf, [])
                            if not candle_list:
                                continue

                            self._updates += 1

                            # Separate closed candles from forming
                            closed = candle_list[:-1] if len(candle_list) > 1 else []
                            if not closed:
                                continue

                            key = (sym, tf)
                            last_ts = closed[-1][0]
                            prev_ts = self._last_ts.get(key, 0)

                            if last_ts > prev_ts:
                                # Append only NEW candles (not already in buffer)
                                buf = self._buffers[key]
                                for c in closed:
                                    if c[0] > prev_ts:
                                        buf.append({
                                            "ts": c[0],
                                            "open": float(c[1]),
                                            "high": float(c[2]),
                                            "low": float(c[3]),
                                            "close": float(c[4]),
                                            "volume": float(c[5]),
                                        })
                                self._last_ts[key] = last_ts

                                if prev_ts > 0:
                                    self._closes += 1
                                    self._recent_closes.append((sym, tf))
                                    self.candle_closed.set()

                    # Trim ccxt internal caches to prevent memory leak
                    try:
                        if hasattr(self._ex, 'ohlcvs'):
                            for sym in list(self._ex.ohlcvs.keys()):
                                for _tf in list(self._ex.ohlcvs.get(sym, {}).keys()):
                                    cache = self._ex.ohlcvs[sym][_tf]
                                    if hasattr(cache, '__len__') and len(cache) > 250:
                                        # Only trim plain lists; ccxt ArrayCache
                                        # manages its own limit via getLimit()
                                        if not hasattr(cache, 'getLimit'):
                                            self._ex.ohlcvs[sym][_tf] = cache[-220:]
                        # Trim ticker cache (shadow adds many)
                        if hasattr(self._ex, 'tickers') and len(self._ex.tickers) > 80:
                            keep_syms = set()
                            for _tf2, syms in self._subs.items():
                                keep_syms.update(syms)
                            for k in list(self._ex.tickers.keys()):
                                if k not in keep_syms:
                                    del self._ex.tickers[k]
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._errors += 1
                err = str(e).lower()
                if "connection" in err or "closed" in err:
                    log.warning(f"  📡 WSData {tf}: reconnecting... ({e})")
                    await asyncio.sleep(cfg.WS_RECONNECT_DELAY)
                else:
                    log.debug(f"  📡 WSData {tf} error: {e}")
                    await asyncio.sleep(1)

    # ── Public API ────────────────────────────────────────────

    async def get_candles(self, symbol: str, tf: str,
                          n: int = 220) -> Optional[List[dict]]:
        """Get last N closed candles from buffer."""
        async with self._lock:
            buf = self._buffers.get((symbol, tf))
            if not buf or len(buf) < 20:
                return None
            return list(buf)[-n:]

    async def get_arrays(self, symbol: str, tf: str):
        """Get numpy arrays (o, h, l, c, v) from buffer."""
        candles = await self.get_candles(symbol, tf)
        if candles is None or len(candles) < 20:
            return None
        o = np.array([c["open"] for c in candles], dtype=float)
        h = np.array([c["high"] for c in candles], dtype=float)
        l = np.array([c["low"] for c in candles], dtype=float)
        c = np.array([c["close"] for c in candles], dtype=float)
        v = np.array([c["volume"] for c in candles], dtype=float)
        return o, h, l, c, v

    async def get_1m_candles(self, symbol: str, n: int = 2) -> Optional[List[dict]]:
        """Get 1m candles for rejection detection."""
        return await self.get_candles(symbol, "1m", n)

    async def drain_closes(self) -> List[CandleClose]:
        """Get and clear recent candle close events."""
        async with self._lock:
            closes = list(self._recent_closes)
            self._recent_closes.clear()
            self.candle_closed.clear()
            return closes

    async def wait_for_close(self, timeout: float = 120):
        """Wait for any candle to close, with timeout."""
        try:
            await asyncio.wait_for(self.candle_closed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    @property
    def stats(self):
        return {
            "connected": self._connected,
            "buffers": len(self._buffers),
            "updates": self._updates,
            "closes": self._closes,
            "errors": self._errors,
        }

    @property
    def is_ready(self):
        return self._connected and len(self._buffers) > 0
