"""
v13pro/momentum.py  --  Momentum Alignment Detector

Detects when BTC, ETH, SOL are ALL in unanimous trend alignment — the
condition that produced the winning Feb 25 session (+17% equity swing).

When ALIGNED:
  - Risk ×1.40 (cut through DD/regime throttle)
  - Use CONFIG conviction values (A+=1.50, A=1.15) not adaptive-crushed
  - Promote conditional combos: EMA_RIB/15m, DONCHIAN/1h
  - Override DD throttle floor to 0.55
  - BB_BREAK/1h gets priority slot + wider trail

When CONFLICTED:
  - Risk ×0.65 (the throttle is CORRECT for chop)
  - Keep adaptive conviction (calibrated for mixed conditions)
  - No promotions

Integrates with MicroTF:
  - ALIGNED + micro_barometer HOT → super_boost 1.50x
  - ALIGNED + micro_barometer COLD → reduced to 1.15x (caution)
  - CONFLICTED + micro HOT → neutral 1.00x (micro-only can't override macro)

Data source: SentimentGauge (already computes per-coin structure, EMA
spread, bias, score). This module reads sentiment and synthesizes the
alignment state. Zero new data fetching needed.
"""

import time
import threading
import logging
from typing import Dict, Optional

log = logging.getLogger("v13pro")

# ── Config ──────────────────────────────────────────────────────────
# Alignment requires all 3 leaders to agree in BOTH structure AND bias
ALIGNMENT_COINS = ["BTC", "ETH", "SOL"]

# Scoring weights for alignment components
WEIGHT_STRUCTURE = 0.35    # hh_hl or ll_lh structure
WEIGHT_BIAS = 0.25         # per-coin bias (bull/bear)
WEIGHT_SPREAD = 0.25       # EMA spread direction
WEIGHT_TREND_POS = 0.15    # close vs slow EMA

# Alignment state thresholds
ALIGNED_THRESHOLD = 0.80       # score >= this = ALIGNED
PARTIAL_THRESHOLD = 0.50       # score >= this = PARTIAL
# Below PARTIAL = CONFLICTED

# Minimum sustained time before declaring alignment (prevents fake alignment)
ALIGNED_MIN_SUSTAIN_SEC = 300   # 5 min sustained (conservative)

# Risk multipliers per state
ALIGNED_RISK_MULT = 1.40       # boost risk 40% during alignment
PARTIAL_RISK_MULT = 1.00       # neutral
CONFLICTED_RISK_MULT = 0.65    # cut risk 35% in chop

# DD throttle floor during alignment (prevents recovery trap)
ALIGNED_DD_FLOOR = 0.55        # Even at 20% DD, never below 55% during alignment

# Conviction overrides during alignment
ALIGNED_USE_CONFIG_CONVICTION = True  # use config values, not adaptive-crushed

# Combos that get promoted during alignment (shadow-only → live)
ALIGNMENT_COMBOS = {
    ("EMA_RIB", "15m"),   # #1 frequency combo on Feb 25 London
    ("DONCHIAN", "1h"),   # 3 NY winners on alignment day
}

# BB_BREAK/1h priority: wider trail during alignment
BB_BREAK_1H_ALIGNED_TRAIL_ACTIVATION = 2.0  # R (wider, give room)
BB_BREAK_1H_ALIGNED_TRAIL_DISTANCE = 0.70   # R (wider trail)
BB_BREAK_1H_ALIGNED_RISK_MULT = 1.25        # extra risk on top of alignment

# Integration with micro-TF
ALIGNED_MICRO_HOT_MULT = 1.50     # alignment + micro HOT → super boost
ALIGNED_MICRO_COLD_MULT = 1.15    # alignment + micro COLD → cautious
CONFLICTED_MICRO_HOT_MULT = 1.00  # conflicted + micro HOT → neutral

# Refresh interval
REFRESH_INTERVAL = 30  # recompute every 30s (sentiment caches for 30s anyway)


class MomentumAlignment:
    """
    Detects BTC/ETH/SOL unanimous trend alignment.

    Reads from SentimentGauge's cached data — zero additional API calls.
    Updates every ~30s in the heartbeat cycle.
    """

    def __init__(self, sentiment_gauge=None, micro_tf=None):
        self._sentiment = sentiment_gauge
        self._micro_tf = micro_tf
        self._lock = threading.RLock()

        # State
        self._alignment_score: float = 0.5
        self._alignment_state: str = "PARTIAL"  # ALIGNED / PARTIAL / CONFLICTED
        self._alignment_direction: str = "neutral"  # bull / bear / neutral
        self._coin_details: Dict = {}

        # Sustained tracking
        self._aligned_since: float = 0.0   # timestamp when alignment first detected
        self._sustained: bool = False       # True once ALIGNED for ALIGNED_MIN_SUSTAIN_SEC

        # History for dashboard
        self._state_history: list = []      # last 10 state transitions
        self._last_refresh: float = 0.0

    def set_sentiment(self, sentiment_gauge):
        """Late-bind sentiment gauge (after init)."""
        self._sentiment = sentiment_gauge

    def set_micro_tf(self, micro_tf):
        """Late-bind micro-TF intelligence."""
        self._micro_tf = micro_tf

    def update(self, sentiment_data: Optional[Dict] = None):
        """
        Recompute alignment from latest sentiment data.

        Can be called with explicit data or will try to read from
        the sentiment gauge cache.
        """
        if sentiment_data is None:
            if self._sentiment:
                try:
                    sentiment_data = self._sentiment.get_cached()
                except Exception:
                    return
            if not sentiment_data:
                return

        coins = sentiment_data.get("coins", {})
        if not coins:
            return

        with self._lock:
            self._compute_alignment(coins)
            self._last_refresh = time.time()

    def _compute_alignment(self, coins: Dict):
        """Core alignment computation. Must hold _lock."""
        now = time.time()
        scores_bull = []
        scores_bear = []
        self._coin_details = {}

        for coin_name in ALIGNMENT_COINS:
            c = coins.get(coin_name)
            if not c:
                # Missing coin data — can't compute alignment
                self._alignment_score = 0.5
                self._alignment_state = "PARTIAL"
                return

            bull_score = 0.0
            bear_score = 0.0

            # Structure: hh_hl is bullish, ll_lh is bearish
            structure = c.get("structure", "mixed")
            if structure == "hh_hl":
                bull_score += WEIGHT_STRUCTURE
            elif structure == "ll_lh":
                bear_score += WEIGHT_STRUCTURE

            # Bias: bull/bear/neutral
            bias = c.get("bias", "neutral")
            if bias == "bull":
                bull_score += WEIGHT_BIAS
            elif bias == "bear":
                bear_score += WEIGHT_BIAS

            # EMA spread: positive = bullish, negative = bearish
            spread = c.get("spread_pct", 0.0)
            if spread > 0.2:
                bull_score += WEIGHT_SPREAD
            elif spread < -0.2:
                bear_score += WEIGHT_SPREAD
            else:
                # Small spread: half weight to whichever direction
                if spread > 0:
                    bull_score += WEIGHT_SPREAD * 0.5
                elif spread < 0:
                    bear_score += WEIGHT_SPREAD * 0.5

            # Trend position: above/below slow EMA
            trend_pos = c.get("trend_pos", "unknown")
            if trend_pos == "above":
                bull_score += WEIGHT_TREND_POS
            elif trend_pos == "below":
                bear_score += WEIGHT_TREND_POS

            scores_bull.append(bull_score)
            scores_bear.append(bear_score)

            self._coin_details[coin_name] = {
                "bull_score": round(bull_score, 3),
                "bear_score": round(bear_score, 3),
                "structure": structure,
                "bias": bias,
                "spread": round(spread, 4),
                "trend_pos": trend_pos,
            }

        # Average scores across all coins
        avg_bull = sum(scores_bull) / len(scores_bull) if scores_bull else 0
        avg_bear = sum(scores_bear) / len(scores_bear) if scores_bear else 0

        # Alignment score: how much do all coins agree?
        # High bull agreement = high score, high bear agreement = high score
        # (we care about alignment in EITHER direction)
        max_agreement = max(avg_bull, avg_bear)

        # Unanimity bonus: if ALL coins agree in same direction, bonus
        if avg_bull > 0.5:
            # All leaning bull
            min_bull = min(scores_bull)
            unanimity = min_bull / max(avg_bull, 0.01)  # 0-1 how unanimous
            alignment_score = max_agreement * (0.7 + 0.3 * unanimity)
        elif avg_bear > 0.5:
            # All leaning bear
            min_bear = min(scores_bear)
            unanimity = min_bear / max(avg_bear, 0.01)
            alignment_score = max_agreement * (0.7 + 0.3 * unanimity)
        else:
            alignment_score = max_agreement * 0.6  # mixed → low score

        alignment_score = min(1.0, max(0.0, alignment_score))

        # Determine direction
        if avg_bull > avg_bear + 0.15:
            direction = "bull"
        elif avg_bear > avg_bull + 0.15:
            direction = "bear"
        else:
            direction = "neutral"

        # State transitions
        old_state = self._alignment_state

        if alignment_score >= ALIGNED_THRESHOLD:
            new_state = "ALIGNED"
        elif alignment_score >= PARTIAL_THRESHOLD:
            new_state = "PARTIAL"
        else:
            new_state = "CONFLICTED"

        # Sustained tracking
        if new_state == "ALIGNED":
            if old_state != "ALIGNED":
                self._aligned_since = now
                self._sustained = False
            elif not self._sustained and (now - self._aligned_since >= ALIGNED_MIN_SUSTAIN_SEC):
                self._sustained = True
                log.info(f"[Alignment] SUSTAINED ALIGNMENT confirmed "
                         f"({ALIGNED_MIN_SUSTAIN_SEC}s) — direction={direction}")
        else:
            self._aligned_since = 0.0
            self._sustained = False

        # Log state changes
        if new_state != old_state:
            log.info(f"[Alignment] {old_state} → {new_state} "
                     f"(score={alignment_score:.2f}, dir={direction})")
            self._state_history.append({
                "ts": now,
                "from": old_state,
                "to": new_state,
                "score": alignment_score,
                "direction": direction,
            })
            # Keep last 10 transitions
            if len(self._state_history) > 10:
                self._state_history = self._state_history[-10:]

        self._alignment_score = alignment_score
        self._alignment_state = new_state
        self._alignment_direction = direction

    # ── Public API ──────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current alignment state: ALIGNED / PARTIAL / CONFLICTED."""
        return self._alignment_state

    @property
    def score(self) -> float:
        """Current alignment score (0.0-1.0)."""
        return self._alignment_score

    @property
    def direction(self) -> str:
        """Current alignment direction: bull / bear / neutral."""
        return self._alignment_direction

    @property
    def is_aligned(self) -> bool:
        """True when alignment is CONFIRMED AND SUSTAINED."""
        return self._alignment_state == "ALIGNED" and self._sustained

    @property
    def is_conflicted(self) -> bool:
        """True when market is in conflicted/choppy state."""
        return self._alignment_state == "CONFLICTED"

    def risk_multiplier(self) -> float:
        """
        Get alignment-based risk multiplier.

        Integrates with micro-TF for combined intelligence:
        - ALIGNED + micro HOT = 1.50x (super boost)
        - ALIGNED + micro COLD = 1.15x (cautious alignment)
        - CONFLICTED = 0.65x regardless of micro
        """
        state = self._alignment_state
        sustained = self._sustained

        # Base multiplier from alignment state
        if state == "ALIGNED" and sustained:
            base_mult = ALIGNED_RISK_MULT  # 1.40
        elif state == "PARTIAL":
            base_mult = PARTIAL_RISK_MULT  # 1.00
        else:
            return CONFLICTED_RISK_MULT    # 0.65 — no override

        # If we have micro-TF data, integrate it
        if self._micro_tf:
            baro = self._micro_tf.market_barometer()
            baro_label = baro.get("label", "NEUTRAL")

            if state == "ALIGNED" and sustained:
                if baro_label == "HOT":
                    return ALIGNED_MICRO_HOT_MULT       # 1.50 — super boost
                elif baro_label == "COLD":
                    return ALIGNED_MICRO_COLD_MULT       # 1.15 — cautious
                else:
                    return base_mult                     # 1.40 — normal aligned

        return base_mult

    def dd_floor(self) -> Optional[float]:
        """
        During ALIGNED, return DD throttle floor to prevent recovery trap.

        Returns None when not aligned (no override).
        """
        if self.is_aligned:
            return ALIGNED_DD_FLOOR  # 0.55
        return None

    def should_use_config_conviction(self) -> bool:
        """
        During ALIGNED, use CONFIG conviction multipliers instead of
        adaptive-crushed values.

        The adaptive engine has inverted A+ to 1.09x (config says 1.50x).
        During alignment, A+ conviction IS the proven edge.
        """
        return ALIGNED_USE_CONFIG_CONVICTION and self.is_aligned

    def is_combo_promoted(self, strategy: str, tf: str) -> bool:
        """
        Check if a shadow-only combo should be promoted to live
        during alignment.

        EMA_RIB/15m and DONCHIAN/1h are weak in chop but strong
        in alignment. They get conditionally promoted.
        """
        if not self.is_aligned:
            return False
        return (strategy, tf) in ALIGNMENT_COMBOS

    def bb_break_priority(self, strategy: str, tf: str) -> Optional[dict]:
        """
        During alignment, BB_BREAK/1h gets priority treatment:
        - Wider trail (activation 2.0R, distance 0.70R)
        - Extra risk multiplier (1.25x)

        Returns dict with overrides or None.
        """
        if not self.is_aligned:
            return None
        if strategy != "BB_BREAK" or tf != "1h":
            return None

        return {
            "trail_activation_r": BB_BREAK_1H_ALIGNED_TRAIL_ACTIVATION,
            "trail_distance_r": BB_BREAK_1H_ALIGNED_TRAIL_DISTANCE,
            "risk_mult": BB_BREAK_1H_ALIGNED_RISK_MULT,
            "reason": "BB_BREAK_1h_aligned_priority",
        }

    def side_filter(self, side: str) -> bool:
        """
        During alignment, only allow trades in the alignment direction.

        BULL alignment → longs only
        BEAR alignment → both (we're long-only anyway, but doesn't hurt)
        PARTIAL/CONFLICTED → no filter (let other gates handle it)
        """
        if not self.is_aligned:
            return True  # no filter

        if self._alignment_direction == "bull" and side == "short":
            return False
        if self._alignment_direction == "bear" and side == "long":
            return False

        return True

    # ── Dashboard / Summary ─────────────────────────────────────────

    def summary(self) -> dict:
        """Summary for dashboard display."""
        with self._lock:
            micro_label = "N/A"
            if self._micro_tf:
                baro = self._micro_tf.market_barometer()
                micro_label = baro.get("label", "N/A")

            return {
                "state": self._alignment_state,
                "score": round(self._alignment_score, 2),
                "direction": self._alignment_direction,
                "sustained": self._sustained,
                "risk_mult": round(self.risk_multiplier(), 2),
                "dd_floor": self.dd_floor(),
                "micro_synergy": micro_label,
                "coins": dict(self._coin_details),
                "use_config_conviction": self.should_use_config_conviction(),
                "promoted_combos": list(ALIGNMENT_COMBOS) if self.is_aligned else [],
            }

    def log_status(self):
        """Log current alignment state."""
        s = self.summary()
        log.info(f"[Alignment] {s['state']} (score={s['score']:.2f}, "
                 f"dir={s['direction']}, sustained={s['sustained']}) "
                 f"risk×{s['risk_mult']:.2f} micro={s['micro_synergy']}")
