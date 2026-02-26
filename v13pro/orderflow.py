"""
v13pro/orderflow.py -- Order Flow Intelligence module.

Captures orderbook microstructure data at entry time for research:
  - Spread (bid-ask) in bps
  - Orderbook imbalance (bid vs ask volume at top N levels)
  - Spread stability (tight vs noisy)
  - Depth ratio (buy wall vs sell wall pressure)

This is DATA COLLECTION ONLY — logged with every trade for analysis.
Once enough data is collected, insights will drive entry scoring.

Toggle: cfg.ORDERFLOW_ENABLED (default True)
"""

import asyncio
import time
from typing import Dict, Optional

from v13pro import config as cfg
from v13pro import logger as log

# How many orderbook levels to analyze
OB_DEPTH = 20


class OrderFlowIntel:
    """Captures orderbook microstructure snapshots at trade entry."""

    def __init__(self, exchange=None):
        self._ex = exchange
        self._cache: Dict[str, dict] = {}  # symbol -> last snapshot
        self._cache_ts: Dict[str, float] = {}
        self._cache_ttl: float = 5.0  # 5s cache per symbol
        self._snapshots_taken = 0
        self._errors = 0

    @property
    def stats(self) -> dict:
        return {
            "snapshots": self._snapshots_taken,
            "errors": self._errors,
            "cached": len(self._cache),
        }

    async def snapshot(self, symbol: str, side: str,
                       entry_price: float = 0) -> Dict:
        """
        Capture orderbook microstructure for a symbol.

        Args:
            symbol: e.g. "BTC/USDT:USDT"
            side: "long" or "short"
            entry_price: planned entry price (for context)

        Returns dict with:
            spread_bps: bid-ask spread in basis points
            spread_pct: spread as percentage
            bid_price: best bid
            ask_price: best ask
            mid_price: midpoint
            imbalance: -1.0 (all asks) to +1.0 (all bids)
            bid_depth: total bid volume (top N levels)
            ask_depth: total ask volume (top N levels)
            depth_ratio: bid_depth / ask_depth
            pressure: "buy" / "sell" / "balanced"
            side_aligned: True if pressure matches trade direction
            quality: "tight" / "normal" / "wide" / "dangerous"
            ts: timestamp
        """
        now = time.time()

        # Check cache
        cached_ts = self._cache_ts.get(symbol, 0)
        if (now - cached_ts) < self._cache_ttl and symbol in self._cache:
            return self._cache[symbol]

        try:
            ob = await self._ex.fetch_order_book(symbol, limit=OB_DEPTH)
        except Exception as e:
            self._errors += 1
            log.debug(f"OrderFlow {symbol}: {e}")
            return self._empty(symbol, side)

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        if not bids or not asks:
            self._errors += 1
            return self._empty(symbol, side)

        # ── Spread ──
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / mid * 100) if mid > 0 else 0
        spread_bps = spread_pct * 100  # 1% = 100 bps

        # ── Depth volumes ──
        bid_depth = sum(float(b[1]) for b in bids[:OB_DEPTH])
        ask_depth = sum(float(a[1]) for a in asks[:OB_DEPTH])
        total_depth = bid_depth + ask_depth

        # ── Imbalance: -1 (all asks) to +1 (all bids) ──
        if total_depth > 0:
            imbalance = (bid_depth - ask_depth) / total_depth
        else:
            imbalance = 0.0

        # ── Depth ratio ──
        depth_ratio = (bid_depth / ask_depth) if ask_depth > 0 else 999.0

        # ── Pressure classification ──
        if imbalance > 0.15:
            pressure = "buy"
        elif imbalance < -0.15:
            pressure = "sell"
        else:
            pressure = "balanced"

        # ── Side alignment ──
        side_aligned = (
            (side == "long" and pressure == "buy") or
            (side == "short" and pressure == "sell")
        )

        # ── Spread quality ──
        # Classify based on bps thresholds
        if spread_bps <= 3:
            quality = "tight"
        elif spread_bps <= 8:
            quality = "normal"
        elif spread_bps <= 20:
            quality = "wide"
        else:
            quality = "dangerous"

        # ── Top-of-book wall detection ──
        # Check if there's a large order at L1 (2x average of next 4 levels)
        bid_wall = False
        ask_wall = False
        if len(bids) >= 5:
            l1_bid = float(bids[0][1])
            avg_bid = sum(float(b[1]) for b in bids[1:5]) / 4
            if avg_bid > 0 and l1_bid > avg_bid * 2:
                bid_wall = True
        if len(asks) >= 5:
            l1_ask = float(asks[0][1])
            avg_ask = sum(float(a[1]) for a in asks[1:5]) / 4
            if avg_ask > 0 and l1_ask > avg_ask * 2:
                ask_wall = True

        result = {
            "symbol": symbol,
            "side": side,
            "spread_bps": round(spread_bps, 2),
            "spread_pct": round(spread_pct, 6),
            "bid_price": best_bid,
            "ask_price": best_ask,
            "mid_price": round(mid, 8),
            "imbalance": round(imbalance, 4),
            "bid_depth": round(bid_depth, 4),
            "ask_depth": round(ask_depth, 4),
            "depth_ratio": round(depth_ratio, 3),
            "pressure": pressure,
            "side_aligned": side_aligned,
            "quality": quality,
            "bid_wall": bid_wall,
            "ask_wall": ask_wall,
            "ts": now,
        }

        self._cache[symbol] = result
        self._cache_ts[symbol] = now
        self._snapshots_taken += 1
        return result

    def _empty(self, symbol: str, side: str) -> Dict:
        """Return empty snapshot when orderbook unavailable."""
        return {
            "symbol": symbol,
            "side": side,
            "spread_bps": 0,
            "spread_pct": 0,
            "bid_price": 0,
            "ask_price": 0,
            "mid_price": 0,
            "imbalance": 0,
            "bid_depth": 0,
            "ask_depth": 0,
            "depth_ratio": 0,
            "pressure": "unknown",
            "side_aligned": False,
            "quality": "unknown",
            "bid_wall": False,
            "ask_wall": False,
            "ts": time.time(),
        }

    def format_dashboard(self) -> str:
        """Build summary string for dashboard from recent snapshots."""
        if not self._cache:
            return "no data"
        # Show stats of most recent snapshots
        spreads = [v["spread_bps"] for v in self._cache.values()
                   if v.get("spread_bps", 0) > 0]
        aligned = sum(1 for v in self._cache.values()
                      if v.get("side_aligned"))
        total = len(self._cache)
        avg_spread = sum(spreads) / len(spreads) if spreads else 0
        return (f"snaps={self._snapshots_taken}  "
                f"avg_spread={avg_spread:.1f}bps  "
                f"aligned={aligned}/{total}")
