"""
obr/regime.py -- Market regime detection for x1000 compounding (Mod 5).

Classifies market conditions as: trending, ranging, volatile, unknown.
Uses pure price action from existing candle data (no new API calls).
Thread-safe cache with configurable TTL.

No external dependencies -- pure Python stdlib.
"""

import time
import threading
from typing import Dict, List

from obr import logger as log

# Cache TTL in seconds (refresh every 5 minutes)
REGIME_CACHE_TTL = 300


# ═══════════════════════════════════════════════════════════════
#  REGIME CLASSIFIER
# ═══════════════════════════════════════════════════════════════

def classify_regime(candles: list) -> str:
    """
    Classify market regime from candle data.

    Uses:
    - Price efficiency ratio (trending vs ranging)
    - ATR-based volatility (volatile vs normal)

    Args:
        candles: list of dicts with 'open', 'high', 'low', 'close' keys.
                 Minimum 10 candles recommended, 20+ ideal.

    Returns:
        "trending" | "ranging" | "volatile" | "unknown"
    """
    if not candles or len(candles) < 10:
        return "unknown"

    try:
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        n = len(candles)

        # ── Average True Range (ATR) proxy ──
        trs = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        if not trs:
            return "unknown"

        window = min(14, len(trs))
        atr = sum(trs[-window:]) / window
        avg_price = sum(closes[-window:]) / window

        if avg_price <= 0:
            return "unknown"

        atr_pct = atr / avg_price * 100

        # ── Price efficiency ratio (simplified ADX) ──
        # net directional change vs total path length
        net_change = abs(closes[-1] - closes[0])
        total_range = sum(abs(closes[i] - closes[i - 1]) for i in range(1, n))

        if total_range <= 0:
            return "unknown"

        efficiency = net_change / total_range  # 0=choppy, 1=perfectly trending

        # ── Classification ──
        if atr_pct > 3.0:
            return "volatile"
        elif efficiency > 0.40:
            return "trending"
        elif efficiency < 0.15:
            return "ranging"
        else:
            return "ranging"  # default to ranging (safer TP)

    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════
#  REGIME CACHE
# ═══════════════════════════════════════════════════════════════

class RegimeCache:
    """Thread-safe regime cache for multiple symbols + global market."""

    def __init__(self, ttl: int = REGIME_CACHE_TTL):
        self._cache: Dict[str, tuple] = {}  # symbol -> (regime, timestamp)
        self._global: tuple = ("unknown", 0.0)
        self._lock = threading.Lock()
        self._ttl = ttl

    def get(self, symbol: str) -> str:
        """Get cached regime for symbol (or 'unknown' if stale/missing)."""
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and (time.time() - entry[1]) < self._ttl:
                return entry[0]
            return "unknown"

    def update(self, symbol: str, candles: list):
        """Classify and cache regime for a symbol."""
        regime = classify_regime(candles)
        with self._lock:
            self._cache[symbol] = (regime, time.time())

    def get_global(self) -> str:
        """Get global market regime (typically based on BTC)."""
        with self._lock:
            if (time.time() - self._global[1]) < self._ttl:
                return self._global[0]
            return "unknown"

    def update_global(self, candles: list):
        """Update global regime (e.g. from BTC candles)."""
        regime = classify_regime(candles)
        with self._lock:
            self._global = (regime, time.time())
        return regime
