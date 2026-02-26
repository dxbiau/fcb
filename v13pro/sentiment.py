"""
v13pro/sentiment.py -- Real-time BTC/ETH/SOL momentum sentiment gauge.

Computes aggregate market bias from the "big 3" crypto leaders using
1h candle data. This is DATA COLLECTION ONLY — the gauge is logged
with every trade entry/exit for post-analysis, NOT used for filtering.

Methodology:
  - For each of BTC, ETH, SOL on 1h candles:
      1. EMA-8 vs EMA-21 slope → short-term momentum direction
      2. Last N candles higher-highs / lower-lows → structure
      3. Close vs EMA-21 → trend position
  - Aggregate: majority vote with confidence weighting
  - Output: {bias: "bull"/"bear"/"neutral", confidence, per-coin details}

Uses WS data if available, REST fallback for missing buffers.
"""

import asyncio
import time
from typing import Dict, Optional, Tuple

import numpy as np

from v13pro import config as cfg
from v13pro import logger as log

# Sentinel symbols for market sentiment
_SENTIMENT_SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
]

# Short names for display
_SHORT = {
    "BTC/USDT:USDT": "BTC",
    "ETH/USDT:USDT": "ETH",
    "SOL/USDT:USDT": "SOL",
}

# How many 1h candles we need for EMA calculation
_MIN_CANDLES = 30
_EMA_FAST = 8
_EMA_SLOW = 21
_LOOKBACK = 5   # recent candles for structure check


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    out = np.empty_like(arr)
    alpha = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _coin_bias(closes: np.ndarray, highs: np.ndarray,
               lows: np.ndarray) -> Dict:
    """
    Compute momentum bias for a single coin.

    Returns dict with:
      bias: "bull" / "bear" / "neutral"
      score: float [-1.0, +1.0]  (negative=bearish, positive=bullish)
      ema_fast: last fast EMA value
      ema_slow: last slow EMA value
      trend_pos: "above" / "below" (close vs slow EMA)
      structure: "hh_hl" / "ll_lh" / "mixed"
    """
    n = len(closes)
    if n < _EMA_SLOW + 2:
        return {"bias": "neutral", "score": 0.0,
                "trend_pos": "unknown", "structure": "unknown"}

    ema_f = _ema(closes, _EMA_FAST)
    ema_s = _ema(closes, _EMA_SLOW)

    # ── Signal 1: EMA crossover direction ──
    # Slope of fast EMA over last 3 bars
    fast_slope = (ema_f[-1] - ema_f[-4]) / ema_f[-4] * 100 if ema_f[-4] > 0 else 0
    slow_slope = (ema_s[-1] - ema_s[-4]) / ema_s[-4] * 100 if ema_s[-4] > 0 else 0

    # EMA spread: fast above slow = bullish
    spread = (ema_f[-1] - ema_s[-1]) / ema_s[-1] * 100 if ema_s[-1] > 0 else 0

    # ── Signal 2: Close position vs slow EMA ──
    above_ema = closes[-1] > ema_s[-1]
    trend_pos = "above" if above_ema else "below"

    # ── Signal 3: Structure — higher highs/lows or lower ──
    lb = min(_LOOKBACK, n - 1)
    recent_h = highs[-lb:]
    recent_l = lows[-lb:]

    hh_count = sum(1 for i in range(1, len(recent_h))
                   if recent_h[i] > recent_h[i - 1])
    ll_count = sum(1 for i in range(1, len(recent_l))
                   if recent_l[i] < recent_l[i - 1])
    hl_count = sum(1 for i in range(1, len(recent_l))
                   if recent_l[i] > recent_l[i - 1])
    lh_count = sum(1 for i in range(1, len(recent_h))
                   if recent_h[i] < recent_h[i - 1])

    if hh_count >= 2 and hl_count >= 2:
        structure = "hh_hl"  # bullish structure
    elif ll_count >= 2 and lh_count >= 2:
        structure = "ll_lh"  # bearish structure
    else:
        structure = "mixed"

    # ── Composite score ──
    # Each component contributes to a -1 to +1 score
    score = 0.0

    # EMA spread (capped at ±1.5% = full weight)
    score += np.clip(spread / 1.5, -1.0, 1.0) * 0.35

    # Fast EMA slope (capped at ±0.5%)
    score += np.clip(fast_slope / 0.5, -1.0, 1.0) * 0.25

    # Close vs EMA
    score += (0.20 if above_ema else -0.20)

    # Structure
    if structure == "hh_hl":
        score += 0.20
    elif structure == "ll_lh":
        score -= 0.20

    # Clamp to [-1, 1]
    score = float(np.clip(score, -1.0, 1.0))

    # Classify
    if score >= 0.25:
        bias = "bull"
    elif score <= -0.25:
        bias = "bear"
    else:
        bias = "neutral"

    return {
        "bias": bias,
        "score": round(score, 3),
        "ema_fast": round(float(ema_f[-1]), 4),
        "ema_slow": round(float(ema_s[-1]), 4),
        "spread_pct": round(spread, 4),
        "fast_slope_pct": round(fast_slope, 4),
        "trend_pos": trend_pos,
        "structure": structure,
    }


class SentimentGauge:
    """Real-time BTC/ETH/SOL momentum sentiment gauge."""

    def __init__(self, ws_data=None, exchange=None):
        self._ws = ws_data
        self._ex = exchange
        self._cache: Dict = {}
        self._cache_ts: float = 0
        self._cache_ttl: float = 30.0  # cache for 30s (avoids spam)

    async def get_sentiment(self, force: bool = False) -> Dict:
        """
        Compute current market sentiment from big-3 momentum.

        Returns:
            {
                "bias": "bull" / "bear" / "neutral",
                "confidence": 0.0-1.0,
                "score": -1.0 to +1.0 (aggregate),
                "coins": {
                    "BTC": {bias, score, trend_pos, structure, ...},
                    "ETH": {...},
                    "SOL": {...},
                },
                "ts": timestamp,
                "arrows": "BTC↑ ETH↑ SOL↓"  (display string)
            }
        """
        now = time.time()
        if not force and self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        coins = {}
        scores = []

        for sym in _SENTIMENT_SYMBOLS:
            short = _SHORT[sym]
            try:
                data = await self._get_ohlcv(sym, "1h", _MIN_CANDLES)
                if data is not None and len(data["close"]) >= _EMA_SLOW + 2:
                    result = _coin_bias(
                        data["close"], data["high"], data["low"])
                    coins[short] = result
                    scores.append(result["score"])
                else:
                    coins[short] = {"bias": "neutral", "score": 0.0,
                                    "trend_pos": "unknown",
                                    "structure": "unknown"}
            except Exception as e:
                log.debug(f"Sentiment {short}: {e}")
                coins[short] = {"bias": "neutral", "score": 0.0,
                                "trend_pos": "unknown",
                                "structure": "unknown"}

        # Aggregate
        if scores:
            avg_score = float(np.mean(scores))
        else:
            avg_score = 0.0

        if avg_score >= 0.20:
            bias = "bull"
        elif avg_score <= -0.20:
            bias = "bear"
        else:
            bias = "neutral"

        # Confidence = how aligned the 3 coins are (0 = mixed, 1 = all same)
        if len(scores) >= 2:
            # If all same sign and similar magnitude → high confidence
            same_sign = all(s > 0 for s in scores) or all(s < 0 for s in scores)
            if same_sign:
                confidence = min(1.0, abs(avg_score) / 0.5)
            else:
                confidence = max(0.0, abs(avg_score) / 0.5 - 0.3)
        else:
            confidence = 0.0

        # Build arrow display string
        arrows = []
        for short in ["BTC", "ETH", "SOL"]:
            c = coins.get(short, {})
            b = c.get("bias", "neutral")
            if b == "bull":
                arrows.append(f"{short}↑")
            elif b == "bear":
                arrows.append(f"{short}↓")
            else:
                arrows.append(f"{short}→")

        result = {
            "bias": bias,
            "confidence": round(confidence, 3),
            "score": round(avg_score, 3),
            "coins": coins,
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "arrows": " ".join(arrows),
        }

        self._cache = result
        self._cache_ts = now
        return result

    def get_cached(self) -> Dict:
        """Return last cached sentiment (non-async, for dashboard)."""
        if self._cache:
            return self._cache
        return {
            "bias": "unknown",
            "confidence": 0.0,
            "score": 0.0,
            "coins": {},
            "ts": 0,
            "arrows": "BTC? ETH? SOL?",
        }

    async def _get_ohlcv(self, symbol: str, tf: str,
                          n: int) -> Optional[Dict]:
        """Get OHLCV arrays, prefer WS, fallback REST."""
        # Try WS data first
        if self._ws:
            try:
                o, h, l, c, v = await self._ws.get_arrays(symbol, tf)
                if c is not None and len(c) >= _EMA_SLOW + 2:
                    return {"open": o, "high": h, "low": l,
                            "close": c, "volume": v}
            except Exception:
                pass

        # REST fallback — fetch from exchange directly
        if self._ex:
            try:
                candles = await self._ex.fetch_ohlcv(
                    symbol, tf, limit=n)
                if candles and len(candles) >= _EMA_SLOW + 2:
                    arr = np.array(candles)
                    return {
                        "open": arr[:, 1].astype(float),
                        "high": arr[:, 2].astype(float),
                        "low": arr[:, 3].astype(float),
                        "close": arr[:, 4].astype(float),
                        "volume": arr[:, 5].astype(float),
                    }
            except Exception as e:
                log.debug(f"Sentiment REST {symbol}: {e}")

        return None
