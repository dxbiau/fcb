"""
v13pro/lifecycle.py -- Per-Pair Lifecycle Classifier

Computes continuous lifecycle scores for each pair from rolling shadow data.
No binary labels — outputs probabilistic scores (0.0–1.0) for each lifecycle state.

Lifecycle states (not mutually exclusive):
  - expanding:    Pair is trending with increasing vol + WR momentum
  - compressing:  Vol contracting, moves are shrinking
  - improving:    Recent WR/ExpR trending upward vs historical
  - degrading:    Recent WR/ExpR trending downward vs historical
  - stable:       Low drift, consistent behavior

Used by bot.py to:
  - Modulate TP (expand TP for expanding pairs, contract for compressing)
  - Modulate risk (reduce for degrading, increase for improving)
  - Inform pair selection (lifecycle-aware filtering)

Design principles:
  - All probabilistic (continuous 0.0–1.0 scores)
  - EWMA smoothed (no jitter)
  - O(1) per-pair lookup after periodic refresh
  - Falls back to neutral scores (0.5) when insufficient data
  - Computationally scalable to 3000+ pairs
  - Reversible: set LIFECYCLE_ENABLED=False to bypass
"""

import glob
import json
import math
import os
import time
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
STATE_FILE = os.path.join(cfg.BASE_DIR, "lifecycle_state.json")

# ── Configuration ──
LIFECYCLE_ENABLED = True
REFRESH_INTERVAL = 1200      # 20 minutes (same cadence as adaptive.py)
EWMA_ALPHA = 0.25            # smoothing factor for score updates
MIN_TRADES = 10              # minimum trades for a pair to get scored
RECENT_WINDOW = 30           # "recent" = last N trades for drift computation
OLD_WINDOW = 60              # "old" = older N trades for comparison
VOL_LOOKBACK = 50            # trades for volatility trend

# Score thresholds for modulation
# Scores are 0.0 (absent) to 1.0 (strong)
NEUTRAL_SCORE = 0.5


class PairLifecycle:
    """Lifecycle scores for a single pair."""
    __slots__ = ["expanding", "compressing", "improving", "degrading",
                 "stable", "n_trades", "recent_wr", "recent_expr",
                 "vol_trend", "updated_at"]

    def __init__(self):
        self.expanding = NEUTRAL_SCORE
        self.compressing = NEUTRAL_SCORE
        self.improving = NEUTRAL_SCORE
        self.degrading = NEUTRAL_SCORE
        self.stable = NEUTRAL_SCORE
        self.n_trades = 0
        self.recent_wr = 0.5
        self.recent_expr = 0.0
        self.vol_trend = 0.0  # positive = expanding, negative = compressing
        self.updated_at = 0.0

    def tp_multiplier(self) -> float:
        """TP scaling factor based on lifecycle state.

        - Expanding pairs → larger TP (capture bigger moves)
        - Compressing pairs → smaller TP (take what's available)
        - Degrading pairs → smaller TP (lock profits faster)
        - Improving pairs → slightly larger TP
        - Returns 0.80–1.25 range
        """
        expansion_boost = (self.expanding - NEUTRAL_SCORE) * 0.5   # max +0.25
        compress_cut = (self.compressing - NEUTRAL_SCORE) * -0.4   # max -0.20
        improve_boost = (self.improving - NEUTRAL_SCORE) * 0.2     # max +0.10
        degrade_cut = (self.degrading - NEUTRAL_SCORE) * -0.3      # max -0.15

        mult = 1.0 + expansion_boost + compress_cut + improve_boost + degrade_cut
        return max(0.80, min(1.25, mult))

    def risk_multiplier(self) -> float:
        """Risk scaling factor based on lifecycle state.

        - Improving pairs → normal or slightly increased risk
        - Degrading pairs → reduced risk
        - Expanding + improving → full risk
        - Compressing + degrading → significantly reduced risk
        - Returns 0.50–1.15 range (never amplify risk more than 15%)
        """
        improve_boost = (self.improving - NEUTRAL_SCORE) * 0.3     # max +0.15
        degrade_cut = (self.degrading - NEUTRAL_SCORE) * -1.0      # max -0.50
        stable_boost = (self.stable - NEUTRAL_SCORE) * 0.1         # max +0.05

        mult = 1.0 + improve_boost + degrade_cut + stable_boost
        return max(0.50, min(1.15, mult))

    def to_dict(self) -> dict:
        return {
            "expanding": round(self.expanding, 3),
            "compressing": round(self.compressing, 3),
            "improving": round(self.improving, 3),
            "degrading": round(self.degrading, 3),
            "stable": round(self.stable, 3),
            "n_trades": self.n_trades,
            "recent_wr": round(self.recent_wr, 3),
            "recent_expr": round(self.recent_expr, 4),
            "vol_trend": round(self.vol_trend, 4),
            "tp_mult": round(self.tp_multiplier(), 3),
            "risk_mult": round(self.risk_multiplier(), 3),
        }


class LifecycleTracker:
    """
    Per-pair lifecycle scoring from shadow data.

    Refreshes periodically from shadow JSONL files.
    All getters are O(1) lookups into pre-computed cache.
    Falls back to neutral scores when insufficient data.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._pairs: Dict[str, PairLifecycle] = {}
        self._last_refresh = 0.0
        self._n_refreshes = 0
        self._smoothed: Dict[str, Dict[str, float]] = {}  # persistent EWMA state

        # Load persisted state
        self._load_state()

        # Initial computation
        self.refresh()

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API (O(1) lookups)
    # ═══════════════════════════════════════════════════════════

    def get_lifecycle(self, pair: str) -> PairLifecycle:
        """Get lifecycle scores for a pair. Returns neutral if unknown."""
        if not LIFECYCLE_ENABLED:
            return PairLifecycle()
        with self._lock:
            return self._pairs.get(pair, PairLifecycle())

    def tp_multiplier(self, pair: str) -> float:
        """Get lifecycle-adjusted TP multiplier for a pair."""
        return self.get_lifecycle(pair).tp_multiplier()

    def risk_multiplier(self, pair: str) -> float:
        """Get lifecycle-adjusted risk multiplier for a pair."""
        return self.get_lifecycle(pair).risk_multiplier()

    def maybe_refresh(self):
        """Refresh if stale — call from hot path."""
        if time.time() - self._last_refresh > REFRESH_INTERVAL:
            self.refresh()

    def summary(self) -> dict:
        """Dashboard summary."""
        with self._lock:
            n_scored = sum(1 for p in self._pairs.values() if p.n_trades >= MIN_TRADES)
            expanding = [sym for sym, p in self._pairs.items() if p.expanding > 0.65]
            degrading = [sym for sym, p in self._pairs.items() if p.degrading > 0.65]
            return {
                "pairs_scored": n_scored,
                "pairs_total": len(self._pairs),
                "expanding": [s.replace("/USDT:USDT", "") for s in expanding[:5]],
                "degrading": [s.replace("/USDT:USDT", "") for s in degrading[:5]],
                "refreshes": self._n_refreshes,
            }

    def log_status(self):
        """Log initial status."""
        s = self.summary()
        log.info(f"Lifecycle tracker: {s['pairs_scored']} pairs scored, "
                 f"{len(s['expanding'])} expanding, {len(s['degrading'])} degrading")

    # ═══════════════════════════════════════════════════════════
    #  REFRESH & COMPUTATION
    # ═══════════════════════════════════════════════════════════

    def refresh(self):
        """Reload shadow data and recompute all lifecycle scores."""
        try:
            outcomes = self._load_shadow_outcomes()
            if not outcomes:
                return

            # Group by pair
            by_pair: Dict[str, List[dict]] = defaultdict(list)
            for o in outcomes:
                sym = o.get("symbol", "")
                if sym:
                    by_pair[sym].append(o)

            # Sort each pair's trades by timestamp
            for sym in by_pair:
                by_pair[sym].sort(key=lambda x: x.get("ts_ms", 0))

            # Compute lifecycle for each pair
            with self._lock:
                for sym, trades in by_pair.items():
                    self._compute_pair(sym, trades)

                self._last_refresh = time.time()
                self._n_refreshes += 1
                self._save_state()

        except Exception as e:
            _log.warning(f"Lifecycle refresh error: {e}")

    def _compute_pair(self, sym: str, trades: List[dict]):
        """Compute lifecycle scores for one pair."""
        n = len(trades)
        if n < MIN_TRADES:
            return

        lc = self._pairs.get(sym, PairLifecycle())
        lc.n_trades = n

        # ── Split into old and recent windows ──
        recent_n = min(RECENT_WINDOW, n // 2)
        old_n = min(OLD_WINDOW, n - recent_n)

        recent = trades[-recent_n:]
        old = trades[-(recent_n + old_n):-recent_n] if old_n > 0 else []

        # ── Recent performance ──
        recent_wins = sum(1 for t in recent if t.get("pnl_r", 0) > 0)
        lc.recent_wr = recent_wins / len(recent) if recent else 0.5
        lc.recent_expr = sum(t.get("pnl_r", 0) for t in recent) / len(recent) if recent else 0.0

        old_wr = 0.5
        old_expr = 0.0
        if old:
            old_wr = sum(1 for t in old if t.get("pnl_r", 0) > 0) / len(old)
            old_expr = sum(t.get("pnl_r", 0) for t in old) / len(old)

        # ── WR drift → improving/degrading ──
        wr_drift = lc.recent_wr - old_wr  # positive = improving

        # Map drift to 0-1 score (sigmoid-like mapping)
        # +20% drift → improving=0.85, -20% drift → degrading=0.85
        drift_sensitivity = 3.0  # controls how fast scores saturate
        if wr_drift > 0:
            raw_improving = 0.5 + 0.5 * min(1.0, wr_drift * drift_sensitivity)
            raw_degrading = 1.0 - raw_improving
        else:
            raw_degrading = 0.5 + 0.5 * min(1.0, abs(wr_drift) * drift_sensitivity)
            raw_improving = 1.0 - raw_degrading

        # ── Volatility trend → expanding/compressing ──
        vol_window = min(VOL_LOOKBACK, n)
        vol_trades = trades[-vol_window:]

        # Use stop_dist as volatility proxy
        stop_dists = [t.get("stop_dist", 0) for t in vol_trades if t.get("stop_dist", 0) > 0]
        if len(stop_dists) >= 10:
            mid = len(stop_dists) // 2
            recent_vol = sum(stop_dists[mid:]) / len(stop_dists[mid:])
            old_vol = sum(stop_dists[:mid]) / len(stop_dists[:mid])
            # Normalize by old_vol to get percentage change
            if old_vol > 0:
                lc.vol_trend = (recent_vol - old_vol) / old_vol
            else:
                lc.vol_trend = 0.0

            # Map vol_trend to expanding/compressing scores
            # +50% vol increase → expanding=0.85
            # -50% vol decrease → compressing=0.85
            vol_sensitivity = 1.5
            if lc.vol_trend > 0:
                raw_expanding = 0.5 + 0.5 * min(1.0, lc.vol_trend * vol_sensitivity)
                raw_compressing = 1.0 - raw_expanding
            else:
                raw_compressing = 0.5 + 0.5 * min(1.0, abs(lc.vol_trend) * vol_sensitivity)
                raw_expanding = 1.0 - raw_compressing
        else:
            raw_expanding = NEUTRAL_SCORE
            raw_compressing = NEUTRAL_SCORE

        # ── Stability score ──
        # High when both WR drift and vol trend are near zero
        drift_magnitude = abs(wr_drift) + abs(lc.vol_trend)
        raw_stable = max(0.0, 1.0 - drift_magnitude * 2.0)  # high when drift is low

        # ── EWMA smoothing ──
        key = sym
        if key not in self._smoothed:
            self._smoothed[key] = {}
        sm = self._smoothed[key]

        lc.expanding = self._ewma(sm, "expanding", raw_expanding)
        lc.compressing = self._ewma(sm, "compressing", raw_compressing)
        lc.improving = self._ewma(sm, "improving", raw_improving)
        lc.degrading = self._ewma(sm, "degrading", raw_degrading)
        lc.stable = self._ewma(sm, "stable", raw_stable)
        lc.updated_at = time.time()

        self._pairs[sym] = lc

    @staticmethod
    def _ewma(store: dict, key: str, new_val: float) -> float:
        """EWMA smoothing: blend old and new values."""
        old = store.get(key, new_val)
        smoothed = EWMA_ALPHA * new_val + (1 - EWMA_ALPHA) * old
        store[key] = smoothed
        return smoothed

    # ═══════════════════════════════════════════════════════════
    #  DATA LOADING
    # ═══════════════════════════════════════════════════════════

    def _load_shadow_outcomes(self) -> List[dict]:
        """Load all shadow_outcome records from JSONL files."""
        rows = []
        pattern = os.path.join(SHADOW_DIR, "shadow_*.jsonl")
        for f in sorted(glob.glob(pattern)):
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                            if r.get("event") == "shadow_outcome":
                                rows.append(r)
                        except Exception:
                            pass
            except Exception:
                pass
        return rows

    # ═══════════════════════════════════════════════════════════
    #  PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _save_state(self):
        """Persist smoothed scores to disk."""
        try:
            state = {
                "smoothed": self._smoothed,
                "ts": time.time(),
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            if os.path.exists(STATE_FILE):
                os.replace(tmp, STATE_FILE)
            else:
                os.rename(tmp, STATE_FILE)
        except Exception as e:
            _log.debug(f"Lifecycle save: {e}")

    def _load_state(self):
        """Load persisted smoothed scores."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self._smoothed = state.get("smoothed", {})
            except Exception:
                self._smoothed = {}
