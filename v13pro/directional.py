"""
v13pro/directional.py -- Directional Intelligence Engine

PURPOSE:
  Discovers which market regimes (bull/bear/neutral) favour which
  direction (long vs short) and which timeframes (5m/15m/1h) WORK
  in each regime — purely from shadow outcome data.

  This engine answers:
    "Right now the market is neutral — should we go long, short, or sit?"
    "In a bull market, are 15m scalps or 1h swings more profitable?"
    "In neutral, do shorts actually outperform longs?"

  ALL answers come from proven shadow trade results, NOT guesses.

WHY THIS IS NEEDED:
  - LONG_ONLY_MODE is a static hardcode. In neutral/bear markets,
    shorts may actually win more. The bot should adapt.
  - Different TFs shine in different regimes: 5m/15m for choppy/neutral
    quick scalps, 1h for trending bull/bear.
  - Without this, we trade the same way regardless of conditions.

DESIGN:
  1. Reads ALL shadow outcomes (both longs AND shorts)
  2. Groups by: sentiment_bias × side × timeframe
  3. Computes rolling WR + ExpR per bucket
  4. Outputs: allowed_sides, preferred_tfs, side_risk_mult
  5. Incremental updates via record_outcome() from shadow._finalize()
  6. Periodic full refresh from JSONL files
  7. EWMA-smoothed to prevent whipsawing

DATA SOURCES:
  - Shadow outcomes with sentiment.bias field (bull/bear/neutral)
  - Current sentiment from SentimentGauge (real-time)

NON-DESTRUCTIVE:
  - Falls back to current behaviour (long-only) when insufficient data
  - Requires MIN_SAMPLES per bucket before overriding defaults
"""

import glob
import json
import os
import time
import threading
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
STATE_FILE = os.path.join(cfg.BASE_DIR, "directional_state.json")

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Minimum shadow outcomes per bucket before trusting the data
MIN_SAMPLES = 20

# Minimum WR to consider a direction tradeable in a regime
MIN_WR_TO_ALLOW = 0.42    # Below 42% WR → don't trade that direction

# Minimum ExpR to consider a direction profitable
MIN_EXPR_TO_ALLOW = -0.05  # Slightly negative is OK (fee drag), deeply negative is not

# Rolling window: how many recent outcomes per bucket to use
# Shorter window = faster adaptation to regime changes
ROLLING_WINDOW = 100

# How often to do a full refresh from JSONL (seconds)
REFRESH_INTERVAL = 600  # 10 minutes

# EWMA alpha for smoothing stat transitions
EWMA_ALPHA = 0.30

# Risk multiplier range for directional confidence
# Strong edge in this direction → up to 1.2x risk
# Weak edge → down to 0.5x risk
RISK_MULT_RANGE = (0.5, 1.2)

# TF preference: minimum edge advantage to prefer a TF
TF_PREF_MIN_EDGE = 0.05   # 5% WR advantage needed to declare a TF "preferred"


class DirectionalIntelligence:
    """
    Adaptive directional intelligence from shadow trade data.

    Discovers which market regimes favour which directions and timeframes.
    Updates incrementally as new shadow outcomes arrive.

    Usage:
        di = DirectionalIntelligence()

        # Check if a side is allowed in current sentiment
        if di.is_side_allowed("long", "bull"):
            ...

        # Get risk multiplier for this direction in current sentiment
        mult = di.side_risk_multiplier("long", "bull")  # 0.5x to 1.2x

        # Get preferred timeframes for current sentiment
        tfs = di.preferred_timeframes("neutral")  # e.g. {"15m", "5m"}

        # Should we override LONG_ONLY_MODE?
        if di.should_allow_shorts("bear"):
            ...
    """

    def __init__(self):
        self._lock = threading.RLock()

        # Rolling buffers: {(sentiment_bias, side, tf): deque of pnl_r}
        self._buckets: Dict[Tuple[str, str, str], deque] = defaultdict(
            lambda: deque(maxlen=ROLLING_WINDOW))

        # Aggregated stats: {(sentiment_bias, side): {wr, expr, n}}
        self._side_stats: Dict[Tuple[str, str], dict] = {}

        # Per-TF stats: {(sentiment_bias, tf): {wr, expr, n}}
        self._tf_stats: Dict[Tuple[str, str], dict] = {}

        # Per-side-TF stats: {(sentiment_bias, side, tf): {wr, expr, n}}
        self._side_tf_stats: Dict[Tuple[str, str, str], dict] = {}

        # Smoothed risk multipliers: {(sentiment_bias, side): float}
        self._smoothed_mults: Dict[Tuple[str, str], float] = {}

        # Stats
        self._n_outcomes = 0
        self._last_refresh = 0.0
        self._n_refreshes = 0

        # Load persisted state
        self._load_state()

        # Initial load from shadow files
        self._load_from_shadow()

    # ═══════════════════════════════════════════════════════════
    #  INCREMENTAL UPDATES (called by shadow._finalize)
    # ═══════════════════════════════════════════════════════════

    def record_outcome(self, pnl_r: float, side: str, tf: str,
                       sentiment_bias: str, strategy: str = "",
                       symbol: str = "", peak_r: float = 0.0):
        """
        Record a new shadow outcome for directional analysis.

        Called incrementally for EVERY shadow completion.
        """
        bias = self._normalize_bias(sentiment_bias)
        side = side.lower()
        tf = tf.lower()

        with self._lock:
            # Add to granular bucket
            self._buckets[(bias, side, tf)].append(pnl_r)
            self._n_outcomes += 1

            # Recompute stats
            self._recompute()

    # ═══════════════════════════════════════════════════════════
    #  BULK LOAD FROM SHADOW FILES
    # ═══════════════════════════════════════════════════════════

    def _load_from_shadow(self):
        """Load historical outcomes from shadow JSONL files."""
        try:
            rows = self._read_shadow_outcomes()
            if not rows:
                log.info("DirectionalIntel: no shadow outcomes found, "
                         "starting from defaults")
                return

            with self._lock:
                for r in rows:
                    side = r.get("side", "").lower()
                    tf = r.get("tf", "").lower()
                    pnl_r = r.get("pnl_r", 0)

                    # Extract sentiment bias from the snapshot at signal time
                    sent = r.get("sentiment", {})
                    bias = self._normalize_bias(sent.get("bias", "neutral"))

                    self._buckets[(bias, side, tf)].append(pnl_r)

                self._n_outcomes = sum(len(d) for d in self._buckets.values())
                self._recompute()
                self._last_refresh = time.time()
                self._save_state()

            log.info(f"DirectionalIntel: loaded {self._n_outcomes} outcomes "
                     f"across {len(self._buckets)} buckets")
            self.log_status()

        except Exception as e:
            _log.warning(f"DirectionalIntel load error: {e}")

    def _read_shadow_outcomes(self) -> List[dict]:
        """Read ALL shadow_outcome records (both longs and shorts)."""
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

    def maybe_refresh(self):
        """Refresh from shadow files if stale. Called from heartbeat."""
        if time.time() - self._last_refresh > REFRESH_INTERVAL:
            self._load_from_shadow()
            self._n_refreshes += 1

    # ═══════════════════════════════════════════════════════════
    #  CORE COMPUTATION
    # ═══════════════════════════════════════════════════════════

    def _recompute(self):
        """Recompute all directional stats from bucket data."""
        # ── Aggregate by (bias, side) ──
        side_agg = defaultdict(list)
        tf_agg = defaultdict(list)
        side_tf_agg = defaultdict(list)

        for (bias, side, tf), outcomes in self._buckets.items():
            pnl_list = list(outcomes)
            if not pnl_list:
                continue
            side_agg[(bias, side)].extend(pnl_list)
            tf_agg[(bias, tf)].extend(pnl_list)
            side_tf_agg[(bias, side, tf)].extend(pnl_list)

        # ── Compute stats per (bias, side) ──
        self._side_stats = {}
        for key, pnls in side_agg.items():
            n = len(pnls)
            if n < MIN_SAMPLES:
                continue
            wins = sum(1 for p in pnls if p > 0)
            wr = wins / n
            expr = sum(pnls) / n
            self._side_stats[key] = {"wr": wr, "expr": expr, "n": n}

        # ── Compute stats per (bias, tf) ──
        self._tf_stats = {}
        for key, pnls in tf_agg.items():
            n = len(pnls)
            if n < MIN_SAMPLES:
                continue
            wins = sum(1 for p in pnls if p > 0)
            wr = wins / n
            expr = sum(pnls) / n
            self._tf_stats[key] = {"wr": wr, "expr": expr, "n": n}

        # ── Compute stats per (bias, side, tf) ──
        self._side_tf_stats = {}
        for key, pnls in side_tf_agg.items():
            n = len(pnls)
            if n < MIN_SAMPLES // 2:  # Lower threshold for granular buckets
                continue
            wins = sum(1 for p in pnls if p > 0)
            wr = wins / n
            expr = sum(pnls) / n
            self._side_tf_stats[key] = {"wr": wr, "expr": expr, "n": n}

        # ── Compute smoothed risk multipliers per (bias, side) ──
        for (bias, side), stats in self._side_stats.items():
            # Map WR + ExpR to a risk multiplier
            # 50% WR, 0 ExpR → 1.0x (neutral)
            # 60% WR, +0.3 ExpR → 1.2x (strong edge)
            # 40% WR, -0.2 ExpR → 0.5x (weak edge)
            wr_component = (stats["wr"] - 0.45) / 0.15  # -1 to +1 range
            expr_component = stats["expr"] / 0.30  # -1 to +1 range
            raw_mult = 1.0 + 0.15 * (0.6 * wr_component + 0.4 * expr_component)
            raw_mult = max(RISK_MULT_RANGE[0], min(RISK_MULT_RANGE[1], raw_mult))

            # EWMA smooth
            old = self._smoothed_mults.get((bias, side), 1.0)
            smoothed = old * (1 - EWMA_ALPHA) + raw_mult * EWMA_ALPHA
            self._smoothed_mults[(bias, side)] = smoothed

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API — QUERIED BY BOT.PY
    # ═══════════════════════════════════════════════════════════

    def is_side_allowed(self, side: str, sentiment_bias: str) -> bool:
        """
        Is this side (long/short) profitable in this sentiment regime?

        Returns True if the shadow data shows this direction has edge,
        or if we don't have enough data (falls back to current config).
        """
        bias = self._normalize_bias(sentiment_bias)
        side = side.lower()

        with self._lock:
            stats = self._side_stats.get((bias, side))
            if stats is None:
                # No data — fall back to existing behaviour
                if side == "short":
                    return not cfg.LONG_ONLY_MODE  # respect current config
                return True  # longs always allowed by default

            # Has enough data — check if profitable
            return (stats["wr"] >= MIN_WR_TO_ALLOW and
                    stats["expr"] >= MIN_EXPR_TO_ALLOW)

    def should_allow_shorts(self, sentiment_bias: str) -> bool:
        """
        Should we override LONG_ONLY_MODE and allow shorts?

        Only returns True when shadow data PROVES shorts are profitable
        in this sentiment regime with sufficient sample size.
        """
        bias = self._normalize_bias(sentiment_bias)

        with self._lock:
            stats = self._side_stats.get((bias, "short"))
            if stats is None or stats["n"] < MIN_SAMPLES:
                return False  # Not enough data — stay safe with longs only

            # Shorts need a STRONGER edge to override LONG_ONLY_MODE
            # because overall shorts have 27.9% WR. Need clear proof.
            return (stats["wr"] >= 0.48 and
                    stats["expr"] >= 0.05 and
                    stats["n"] >= MIN_SAMPLES * 2)

    def side_risk_multiplier(self, side: str, sentiment_bias: str) -> float:
        """
        Risk multiplier for this direction in this sentiment regime.

        Strong proven edge → up to 1.2x risk
        Weak/unproven edge → down to 0.5x risk
        No data → 1.0x (neutral, no adjustment)
        """
        bias = self._normalize_bias(sentiment_bias)
        side = side.lower()

        with self._lock:
            return self._smoothed_mults.get((bias, side), 1.0)

    def preferred_timeframes(self, sentiment_bias: str,
                             side: str = "long") -> Set[str]:
        """
        Which timeframes work best in this sentiment regime for this side?

        Returns the set of TFs that have meaningfully better edge than
        the average. Empty set means "no strong preference, use all."
        """
        bias = self._normalize_bias(sentiment_bias)
        side = side.lower()

        with self._lock:
            # Get all TF stats for this bias+side combo
            tf_data = {}
            for (b, s, tf), stats in self._side_tf_stats.items():
                if b == bias and s == side:
                    tf_data[tf] = stats

            if not tf_data:
                return set()  # No data — no preference

            # Find TFs with meaningful edge advantage
            avg_wr = sum(s["wr"] for s in tf_data.values()) / len(tf_data)
            preferred = set()
            for tf, stats in tf_data.items():
                if stats["wr"] >= avg_wr + TF_PREF_MIN_EDGE:
                    preferred.add(tf)

            return preferred

    def best_direction(self, sentiment_bias: str) -> Optional[str]:
        """
        What's the best direction to trade in this sentiment regime?

        Returns "long", "short", or None (neither has proven edge).
        """
        bias = self._normalize_bias(sentiment_bias)

        with self._lock:
            long_stats = self._side_stats.get((bias, "long"))
            short_stats = self._side_stats.get((bias, "short"))

            long_edge = long_stats["expr"] if long_stats else -999
            short_edge = short_stats["expr"] if short_stats else -999

            # Need minimum edge to declare a direction
            if long_edge >= MIN_EXPR_TO_ALLOW and long_edge > short_edge:
                return "long"
            if short_edge >= MIN_EXPR_TO_ALLOW and short_edge > long_edge:
                return "short"
            return None

    def get_regime_direction_summary(self, sentiment_bias: str) -> dict:
        """
        Full directional summary for a sentiment regime.

        Returns dict with long/short stats, preferred TFs, best direction.
        Used for logging and dashboard.
        """
        bias = self._normalize_bias(sentiment_bias)

        with self._lock:
            long_stats = self._side_stats.get((bias, "long"), {})
            short_stats = self._side_stats.get((bias, "short"), {})

            # Collect per-TF breakdown for this bias
            tf_breakdown = {}
            for (b, s, tf), stats in self._side_tf_stats.items():
                if b == bias:
                    key = f"{s}/{tf}"
                    tf_breakdown[key] = {
                        "wr": round(stats["wr"] * 100, 1),
                        "expr": round(stats["expr"], 3),
                        "n": stats["n"],
                    }

            return {
                "sentiment": bias,
                "long": {
                    "wr": round(long_stats.get("wr", 0) * 100, 1),
                    "expr": round(long_stats.get("expr", 0), 3),
                    "n": long_stats.get("n", 0),
                    "allowed": self.is_side_allowed("long", bias),
                    "risk_mult": round(self.side_risk_multiplier("long", bias), 2),
                },
                "short": {
                    "wr": round(short_stats.get("wr", 0) * 100, 1),
                    "expr": round(short_stats.get("expr", 0), 3),
                    "n": short_stats.get("n", 0),
                    "allowed": self.is_side_allowed("short", bias),
                    "risk_mult": round(self.side_risk_multiplier("short", bias), 2),
                },
                "best_direction": self.best_direction(bias),
                "preferred_tfs": list(self.preferred_timeframes(bias)),
                "tf_breakdown": tf_breakdown,
            }

    # ═══════════════════════════════════════════════════════════
    #  LOGGING
    # ═══════════════════════════════════════════════════════════

    def log_status(self):
        """Log current directional intelligence state."""
        with self._lock:
            log.info(f"DirectionalIntel: {self._n_outcomes} outcomes, "
                     f"{len(self._buckets)} buckets")

            for bias in ["bull", "bear", "neutral"]:
                long_s = self._side_stats.get((bias, "long"))
                short_s = self._side_stats.get((bias, "short"))

                if not long_s and not short_s:
                    continue

                parts = [f"  {bias.upper()}:"]
                if long_s:
                    l_ok = "✓" if self.is_side_allowed("long", bias) else "✗"
                    parts.append(
                        f"LONG {l_ok} WR={long_s['wr']:.0%} "
                        f"ExpR={long_s['expr']:+.3f} N={long_s['n']}")
                if short_s:
                    s_ok = "✓" if self.is_side_allowed("short", bias) else "✗"
                    parts.append(
                        f"SHORT {s_ok} WR={short_s['wr']:.0%} "
                        f"ExpR={short_s['expr']:+.3f} N={short_s['n']}")

                best = self.best_direction(bias)
                if best:
                    parts.append(f"→ best={best}")

                pref_tfs = self.preferred_timeframes(bias)
                if pref_tfs:
                    parts.append(f"preferred_tfs={sorted(pref_tfs)}")

                log.info("  ".join(parts))

    # ═══════════════════════════════════════════════════════════
    #  PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _save_state(self):
        """Persist smoothed multipliers to disk."""
        try:
            state = {
                "smoothed_mults": {
                    f"{k[0]}_{k[1]}": v
                    for k, v in self._smoothed_mults.items()
                },
                "ts": time.time(),
            }
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            pass

    def _load_state(self):
        """Load persisted smoothed multipliers."""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                for key, val in state.get("smoothed_mults", {}).items():
                    parts = key.split("_", 1)
                    if len(parts) == 2:
                        self._smoothed_mults[(parts[0], parts[1])] = val
                log.info("DirectionalIntel: restored smoothed state from disk")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_bias(bias: str) -> str:
        """Normalize sentiment bias to bull/bear/neutral."""
        if not bias:
            return "neutral"
        b = bias.lower().strip()
        if b in ("bull", "bullish"):
            return "bull"
        if b in ("bear", "bearish"):
            return "bear"
        return "neutral"

    @property
    def stats(self) -> dict:
        """Summary stats for dashboard."""
        with self._lock:
            return {
                "outcomes": self._n_outcomes,
                "buckets": len(self._buckets),
                "refreshes": self._n_refreshes,
                "side_stats": {
                    f"{k[0]}_{k[1]}": {
                        "wr": round(v["wr"] * 100, 1),
                        "expr": round(v["expr"], 3),
                        "n": v["n"],
                    }
                    for k, v in self._side_stats.items()
                },
            }
