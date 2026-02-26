"""
v13pro/hunter.py -- Async pair hunter.

Periodically scans ALL liquid Bybit USDT-perp pairs for fresh opportunities
using the v13pro 12-strategy engine. Discovers new tradeable pairs that
aren't in the static portfolio but show strong signals.

Reuses obr/pair_hunter.py pattern:
  - Bulk fetch_tickers (1 API call for universe)
  - Filter by 24h volume + spread
  - Scan candles with v13pro strategies
  - Return qualified signals to the bot
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from v13pro import config as cfg
from v13pro import logger as log
from v13pro.strategies import scan_last_bar, ensemble_signals, STRATEGIES
from v13pro.registry import ComboRegistry, EXIT_PARAMS


class PairHunter:
    """Async pair universe scanner."""

    def __init__(self, exchange, registry: ComboRegistry, on_signals=None):
        self._ex = exchange
        self._reg = registry
        self._on_signals = on_signals  # async callback: list[dict] -> None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Cache
        self._universe: List[str] = []
        self._last_scan: float = 0
        self._signals_found: int = 0
        self._scans_done: int = 0

    async def start(self):
        if not cfg.HUNTER_ENABLED:
            log.info("Hunter: disabled in config")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="hunter")
        log.info("Hunter started (async)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # Initial delay to let WS warm up
        await asyncio.sleep(30)

        while self._running:
            try:
                await self._scan_universe()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.log_exception("Hunter scan", e)
            await asyncio.sleep(cfg.HUNTER_SCAN_INTERVAL)

    async def _scan_universe(self):
        """Refresh universe, scan for signals on non-portfolio pairs."""
        self._scans_done += 1

        # Step 1: Bulk fetch all tickers (1 API call)
        try:
            from v13pro.exchange import fetch_tickers
            tickers = await fetch_tickers(self._ex)
        except Exception as e:
            log.warning(f"Hunter: fetch_tickers failed: {e}")
            return

        # Step 2: Filter liquid USDT perps
        portfolio_pairs = set(self._reg.all_pairs)
        candidates = []

        for sym, tick in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            # Skip already-in-portfolio pairs
            if sym in portfolio_pairs:
                continue

            vol_24h = float(tick.get("quoteVolume", 0) or 0)
            bid = float(tick.get("bid", 0) or 0)
            ask = float(tick.get("ask", 0) or 0)

            if vol_24h < cfg.HUNTER_MIN_VOL_24H:
                continue
            if bid <= 0 or ask <= 0:
                continue

            spread_pct = (ask - bid) / bid * 100
            if spread_pct > 0.15:
                continue

            candidates.append(sym)

        self._universe = candidates
        log.debug(f"Hunter: {len(candidates)} liquid candidates "
                  f"(ex-portfolio)")

        # Step 3: Scan top N by volume for signals
        # Sort by volume descending, take top 20
        vol_map = {}
        for sym in candidates:
            vol_map[sym] = float(tickers[sym].get("quoteVolume", 0) or 0)
        top_syms = sorted(candidates, key=lambda s: vol_map.get(s, 0),
                          reverse=True)[:20]

        signals = []
        for sym in top_syms:
            for tf in ["15m", "1h"]:
                try:
                    sig = await self._scan_pair(sym, tf)
                    if sig:
                        signals.extend(sig)
                except Exception:
                    continue
            # Rate limit
            await asyncio.sleep(0.3)

        if signals:
            self._signals_found += len(signals)
            log.info(f"Hunter: found {len(signals)} signals on "
                     f"non-portfolio pairs")
            # Pass signals to bot for possible execution
            if self._on_signals:
                try:
                    await self._on_signals(signals)
                except Exception as e:
                    log.warning(f"Hunter on_signals callback error: {e}")

        self._last_scan = asyncio.get_event_loop().time()
        return signals

    async def _scan_pair(self, symbol: str, tf: str) -> List[dict]:
        """Fetch candles for a pair and scan with all strategies."""
        import numpy as np
        from v13pro.exchange import fetch_ohlcv

        candles = await fetch_ohlcv(self._ex, symbol, tf, limit=220)
        if not candles or len(candles) < 50:
            return []

        o = np.array([c[1] for c in candles], dtype=float)
        h = np.array([c[2] for c in candles], dtype=float)
        l = np.array([c[3] for c in candles], dtype=float)
        c = np.array([c[4] for c in candles], dtype=float)
        v = np.array([c[5] for c in candles], dtype=float)

        hits = scan_last_bar(o, h, l, c, v, symbol, tf,
                             list(STRATEGIES.keys()))

        results = []
        for sig in hits:
            results.append({
                "pair": symbol,
                "tf": tf,
                "strategy": sig.strategy,
                "side": sig.side,
                "entry": sig.entry,
                "stop_dist": sig.stop_dist,
                "source": "hunter",
            })

        return results

    @property
    def stats(self):
        return {
            "enabled": cfg.HUNTER_ENABLED,
            "universe_size": len(self._universe),
            "scans_done": self._scans_done,
            "signals_found": self._signals_found,
        }
