"""
v13pro/correlation_engine.py -- Correlated Pair Intelligence

Computes rolling price correlations across all tracked pairs and provides:
  1. Correlation clusters (groups of highly correlated pairs)
  2. Signal confirmation — did a correlated pair recently signal same direction?
  3. Correlation multiplier for the risk chain

DATA SOURCE: WS 1h candle data (already buffered at 220 bars per pair).
             Shadow signal entries (recent shadow_entry events) for confirmation.

UPDATE: Full correlation matrix rebuilt every 4 hours.
        Signal log updated incrementally on each shadow entry.

OUTPUT:
  correlation_mult(symbol, side) → float  [0.80 … 1.30]
    1.15-1.30 : correlated pair confirmed same direction recently
    1.00      : no cluster membership or no recent signals
    0.80-0.90 : correlated pair signaled OPPOSITE direction (contradiction)

DESIGN NOTES:
  - Uses Pearson correlation of 1h log-returns over last 48 bars (2 days)
  - Cluster threshold: |ρ| ≥ 0.65 (strong enough to be meaningful)
  - Signal confirmation window: 15 minutes (recent shadow entries)
  - Inverse correlations (ρ ≤ -0.65) also form clusters — a signal on one
    pair in a given direction CONFIRMS the inverse direction on its mirror
  - Thread-safe, async-compatible (correlation computed synchronously,
    WS data accessed via stored cache from heartbeat)
"""

import asyncio
import math
import os
import time
import threading
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Correlation computation
CORR_BARS = 48                 # 48 x 1h = 2 days of data for correlation
CORR_MIN_BARS = 24             # minimum bars required (1 day)
CORR_THRESHOLD = 0.65          # |ρ| ≥ this → cluster membership
CORR_INVERSE_THRESHOLD = -0.65 # ρ ≤ this → inverse cluster (opposite direction)

# Update frequency
CORR_REFRESH_INTERVAL = 14400  # 4 hours between full matrix rebuilds

# Signal confirmation
SIGNAL_CONFIRM_WINDOW_S = 900  # 15 minutes — recent shadow entries
SIGNAL_LOG_MAX = 200           # max recent shadow entries to remember

# Multiplier ranges
CONFIRMED_MULT_MAX = 1.30      # confirmed by 2+ cluster mates
CONFIRMED_MULT_MIN = 1.15      # confirmed by 1 cluster mate
NEUTRAL_MULT = 1.00            # no cluster data
CONTRADICTION_MULT = 0.85      # contradicted by cluster mate
INVERSE_CONFIRM_MULT = 1.20    # inverse cluster mate confirms (opposite sig)

# ═══════════════════════════════════════════════════════════════
#  CORRELATION ENGINE CLASS
# ═══════════════════════════════════════════════════════════════


class CorrelationEngine:
    """Real-time correlated pair intelligence for signal confirmation."""

    def __init__(self):
        self._lock = threading.RLock()

        # Correlation matrix (pair_a, pair_b) → ρ
        self._corr_matrix: Dict[Tuple[str, str], float] = {}

        # Cluster membership: symbol → set of correlated symbols
        self._clusters: Dict[str, Set[str]] = defaultdict(set)

        # Inverse clusters: symbol → set of inversely correlated symbols
        self._inverse_clusters: Dict[str, Set[str]] = defaultdict(set)

        # Recent signal log for confirmation
        # deque of {symbol, side, ts, strategy, tf}
        self._recent_signals: deque = deque(maxlen=SIGNAL_LOG_MAX)

        # Cached 1h close arrays: symbol → np.array of closes
        self._price_cache: Dict[str, np.ndarray] = {}

        # Stats
        self._n_pairs = 0
        self._n_clusters = 0
        self._n_inverse = 0
        self._last_refresh = 0

    # ───────────────────────────────────────────────────────────
    #  INITIALIZATION (async — needs WS data)
    # ───────────────────────────────────────────────────────────

    async def initialize(self, ws_data, pairs: set):
        """Fetch 1h candle data and build initial correlation matrix.

        Called once during bot startup after WS warmup.

        Args:
            ws_data: WSDataEngine instance
            pairs: set of symbol strings to track
        """
        await self._fetch_prices(ws_data, pairs)
        self._build_matrix()
        self._last_refresh = time.time()

    async def _fetch_prices(self, ws_data, pairs: set):
        """Fetch 1h close prices from WS buffers for all pairs."""
        cache = {}
        for symbol in pairs:
            try:
                candles = await ws_data.get_candles(symbol, "1h", n=CORR_BARS)
                if candles and len(candles) >= CORR_MIN_BARS:
                    closes = np.array([c["close"] for c in candles], dtype=float)
                    cache[symbol] = closes
            except Exception:
                continue

        with self._lock:
            self._price_cache = cache
            self._n_pairs = len(cache)

        _log.info(f"CorrelationEngine: fetched 1h closes for "
                  f"{len(cache)}/{len(pairs)} pairs")

    def _build_matrix(self):
        """Compute pairwise Pearson correlation from log-returns."""
        with self._lock:
            cache = dict(self._price_cache)

        if len(cache) < 2:
            return

        # Compute log-returns for each pair
        returns = {}
        for sym, closes in cache.items():
            if len(closes) < CORR_MIN_BARS:
                continue
            # Use last CORR_BARS closes
            c = closes[-CORR_BARS:] if len(closes) >= CORR_BARS else closes
            # Log-returns: ln(p_t / p_{t-1})
            lr = np.diff(np.log(c + 1e-12))
            if len(lr) >= CORR_MIN_BARS - 1 and np.std(lr) > 1e-10:
                returns[sym] = lr

        # Pairwise Pearson correlation
        symbols = list(returns.keys())
        n = len(symbols)
        matrix = {}
        clusters = defaultdict(set)
        inverse_clusters = defaultdict(set)

        for i in range(n):
            for j in range(i + 1, n):
                sym_a = symbols[i]
                sym_b = symbols[j]
                ra = returns[sym_a]
                rb = returns[sym_b]

                # Align lengths
                min_len = min(len(ra), len(rb))
                ra_aligned = ra[-min_len:]
                rb_aligned = rb[-min_len:]

                if min_len < CORR_MIN_BARS - 1:
                    continue

                # Pearson correlation
                try:
                    rho = np.corrcoef(ra_aligned, rb_aligned)[0, 1]
                except Exception:
                    continue

                if np.isnan(rho):
                    continue

                matrix[(sym_a, sym_b)] = round(float(rho), 3)

                # Build clusters
                if rho >= CORR_THRESHOLD:
                    clusters[sym_a].add(sym_b)
                    clusters[sym_b].add(sym_a)
                elif rho <= CORR_INVERSE_THRESHOLD:
                    inverse_clusters[sym_a].add(sym_b)
                    inverse_clusters[sym_b].add(sym_a)

        with self._lock:
            self._corr_matrix = matrix
            self._clusters = clusters
            self._inverse_clusters = inverse_clusters
            self._n_clusters = sum(1 for v in clusters.values() if v)
            self._n_inverse = sum(1 for v in inverse_clusters.values() if v)

    # ───────────────────────────────────────────────────────────
    #  SIGNAL LOGGING (incremental, from shadow)
    # ───────────────────────────────────────────────────────────

    def record_signal(self, *, symbol: str, side: str,
                      strategy: str = "", tf: str = "",
                      ts: float = 0, **kwargs):
        """Record a shadow signal entry for confirmation tracking.

        Called for every shadow entry (both passed and rejected).
        We want ALL of them for confirmation — a rejected signal
        still indicates the market setup existed.
        """
        if ts <= 0:
            ts = time.time()

        with self._lock:
            self._recent_signals.append({
                "symbol": symbol,
                "side": side.lower(),
                "ts": ts,
                "strategy": strategy,
                "tf": tf,
            })

    # ───────────────────────────────────────────────────────────
    #  PUBLIC API
    # ───────────────────────────────────────────────────────────

    def correlation_mult(self, symbol: str, side: str) -> float:
        """Risk multiplier based on correlated pair signal confirmation.

        Logic:
          1. Find cluster mates for this symbol
          2. Check if any cluster mate had a recent shadow signal
             in the SAME direction → confirmation (boost)
          3. Check if any cluster mate had a recent signal in the
             OPPOSITE direction → contradiction (reduce)
          4. Check inverse cluster mates for opposite-direction signals
             (inversely correlated + opposite direction = confirmation)

        Returns:
            0.80 – 1.30 float multiplier
        """
        side = side.lower()
        opp_side = "short" if side == "long" else "long"
        now = time.time()
        cutoff = now - SIGNAL_CONFIRM_WINDOW_S

        with self._lock:
            mates = self._clusters.get(symbol, set())
            inv_mates = self._inverse_clusters.get(symbol, set())

            if not mates and not inv_mates:
                return NEUTRAL_MULT

            # Collect recent signals from cluster mates
            confirms = 0
            contradicts = 0

            for sig in self._recent_signals:
                if sig["ts"] < cutoff:
                    continue

                sig_sym = sig["symbol"]
                sig_side = sig["side"]

                # Direct cluster: same direction = confirm
                if sig_sym in mates:
                    if sig_side == side:
                        confirms += 1
                    else:
                        contradicts += 1

                # Inverse cluster: opposite direction = confirm
                if sig_sym in inv_mates:
                    if sig_side == opp_side:
                        confirms += 1
                    elif sig_side == side:
                        contradicts += 1

        # Score → multiplier
        if confirms >= 2:
            return CONFIRMED_MULT_MAX
        elif confirms == 1 and contradicts == 0:
            return CONFIRMED_MULT_MIN
        elif confirms == 1 and contradicts >= 1:
            # Mixed signals — net neutral
            return NEUTRAL_MULT
        elif contradicts >= 1 and confirms == 0:
            return CONTRADICTION_MULT
        else:
            return NEUTRAL_MULT

    def get_cluster(self, symbol: str) -> List[str]:
        """Get correlated cluster mates for a symbol."""
        with self._lock:
            return list(self._clusters.get(symbol, []))

    def get_inverse_cluster(self, symbol: str) -> List[str]:
        """Get inversely correlated cluster mates."""
        with self._lock:
            return list(self._inverse_clusters.get(symbol, []))

    def get_correlation(self, sym_a: str, sym_b: str) -> Optional[float]:
        """Get pairwise correlation between two symbols."""
        with self._lock:
            rho = self._corr_matrix.get((sym_a, sym_b))
            if rho is None:
                rho = self._corr_matrix.get((sym_b, sym_a))
            return rho

    # ───────────────────────────────────────────────────────────
    #  PERIODIC REFRESH (async — needs WS data)
    # ───────────────────────────────────────────────────────────

    async def maybe_refresh(self, ws_data, pairs: set):
        """Rebuild correlation matrix if stale.

        Called from bot heartbeat.  Only runs every CORR_REFRESH_INTERVAL.
        """
        now = time.time()
        if now - self._last_refresh < CORR_REFRESH_INTERVAL:
            return
        _log.info("CorrelationEngine: refreshing correlation matrix…")
        await self._fetch_prices(ws_data, pairs)
        self._build_matrix()
        self._last_refresh = now
        self.log_status()

    # ───────────────────────────────────────────────────────────
    #  LOGGING / STATUS
    # ───────────────────────────────────────────────────────────

    def log_status(self):
        """Log current correlation engine state."""
        with self._lock:
            n_clustered = sum(1 for v in self._clusters.values() if v)
            n_inv = sum(1 for v in self._inverse_clusters.values() if v)
            n_pairs_total = self._n_pairs

        _log.info(
            f"CorrelationEngine: {n_pairs_total} pairs tracked, "
            f"{n_clustered} in clusters (ρ≥{CORR_THRESHOLD}), "
            f"{n_inv} inverse clusters (ρ≤{CORR_INVERSE_THRESHOLD})"
        )

        # Show biggest clusters
        with self._lock:
            sorted_clusters = sorted(
                self._clusters.items(),
                key=lambda x: len(x[1]), reverse=True)[:5]

        for sym, mates in sorted_clusters:
            if not mates:
                continue
            short_sym = sym.split("/")[0] if "/" in sym else sym
            mate_names = [m.split("/")[0] if "/" in m else m for m in mates]
            # Get correlation values
            rhos = []
            for m in mates:
                r = self.get_correlation(sym, m)
                if r is not None:
                    rhos.append(f"{r:+.2f}")
            _log.info(
                f"  Cluster: {short_sym} ↔ "
                f"{', '.join(mate_names[:4])} "
                f"(ρ={', '.join(rhos[:4])})"
            )

    def summary(self) -> dict:
        """Summary dict for dashboard display."""
        with self._lock:
            clusters_info = {}
            for sym, mates in self._clusters.items():
                if mates:
                    clusters_info[sym] = list(mates)

            return {
                "n_pairs": self._n_pairs,
                "n_clustered": sum(1 for v in self._clusters.values() if v),
                "n_inverse": sum(1 for v in self._inverse_clusters.values() if v),
                "clusters": clusters_info,
                "recent_signals": len(self._recent_signals),
            }
