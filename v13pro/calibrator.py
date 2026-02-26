"""
v13pro/calibrator.py -- Self-Calibration Engine

Periodically computes drift metrics and adjustment factors that expose
when the system's assumptions diverge from market reality.

Key calibration targets (from SYSTEM_REVIEW.md findings):

1. GRADE CALIBRATION:
   - Shadow data shows C grade outperforms A+ (63.3% vs 57.5% WR)
   - Conviction scoring may be anti-predictive in certain regimes
   - Calibrator detects this and adjusts grade multipliers

2. TP LEAK DETECTION:
   - Trail exit leaks 0.24R vs fix2.0 leaking 0.16R
   - Monitors actual vs expected leak per exit mode
   - Flags when exit modes become misaligned

3. TEMPORAL STATIONARITY INDEX:
   - System WR oscillates 42%–79% in 100-trade windows
   - Computes rolling stationarity score
   - When non-stationarity is high, increases regime sensitivity

4. EDGE DECAY DETECTION:
   - Monitors if overall system edge is degrading over time
   - Early warning before expectancy goes negative

Design principles:
  - Read-only analysis engine — never directly modifies parameters
  - Outputs adjustment multipliers consumed by adaptive.py/bot.py
  - EWMA smoothed, periodic refresh, O(1) lookups
  - All adjustments are bounded and reversible
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
STATE_FILE = os.path.join(cfg.BASE_DIR, "calibrator_state.json")

# ── Configuration ──
CALIBRATOR_ENABLED = True
REFRESH_INTERVAL = 1800      # 30 minutes (slower than adaptive — strategic layer)
EWMA_ALPHA = 0.20            # slow smoothing for strategic metrics
MIN_GRADE_SAMPLES = 20       # minimum trades per grade for calibration
STATIONARITY_WINDOW = 100    # trades per window for stationarity check
EDGE_DECAY_WINDOWS = 5       # number of windows to check for decay trend


class CalibrationState:
    """Holds all calibration metrics."""

    def __init__(self):
        # Grade calibration: actual performance ratios per grade
        # Maps grade → observed multiplier relative to average
        self.grade_adjustments: Dict[str, float] = {
            "A+": 1.0, "A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0
        }

        # TP leak per exit mode: how much R is "leaked" (peak - actual win)
        self.tp_leak: Dict[str, float] = {}

        # Temporal stationarity index: 0.0 = perfectly stationary, 1.0 = chaotic
        self.stationarity_index = 0.0

        # Edge decay: trend in ExpR over recent windows
        # Negative = edge degrading, positive = edge strengthening
        self.edge_trend = 0.0

        # Overall system health score (0.0–1.0, 1.0 = healthy)
        self.health_score = 1.0

        # Conviction anti-predictiveness: correlation between conviction and outcome
        # Positive = working correctly, negative = anti-predictive
        self.conviction_correlation = 0.0

        self.updated_at = 0.0
        self.n_outcomes = 0


class SelfCalibrator:
    """
    Self-calibration engine.

    Periodically analyzes shadow data to detect drift between
    system assumptions and market reality. Outputs adjustment
    factors that other modules can consume.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = CalibrationState()
        self._last_refresh = 0.0
        self._n_refreshes = 0
        self._smoothed: Dict[str, float] = {}

        # Load persisted state
        self._load_state()

        # Initial computation
        self.refresh()

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API (O(1) lookups)
    # ═══════════════════════════════════════════════════════════

    def grade_adjustment(self, grade: str) -> float:
        """
        Get calibrated grade multiplier adjustment.

        Returns a factor (0.7–1.3) to multiply against the adaptive
        conviction multiplier. When C outperforms A+, this rebalances
        the sizing toward actual observed edge per grade.
        """
        if not CALIBRATOR_ENABLED:
            return 1.0
        with self._lock:
            return self._state.grade_adjustments.get(grade, 1.0)

    def stationarity_index(self) -> float:
        """
        How non-stationary is the system? 0.0=stable, 1.0=chaotic.

        When high (>0.5), the regime detector should be more aggressive
        about reducing exposure, and TP targets should contract.
        """
        if not CALIBRATOR_ENABLED:
            return 0.0
        with self._lock:
            return self._state.stationarity_index

    def edge_trend(self) -> float:
        """
        Trend in system edge. Negative = degrading, positive = strengthening.

        When negative, overall risk should be reduced.
        Returns roughly -1.0 to +1.0.
        """
        if not CALIBRATOR_ENABLED:
            return 0.0
        with self._lock:
            return self._state.edge_trend

    def health_score(self) -> float:
        """Overall system health 0.0–1.0."""
        if not CALIBRATOR_ENABLED:
            return 1.0
        with self._lock:
            return self._state.health_score

    def risk_multiplier(self) -> float:
        """
        Calibration-adjusted risk multiplier.

        Combines edge trend + stationarity into a single modulator.
        Returns 0.6–1.0 (never amplifies risk from calibration alone).
        """
        if not CALIBRATOR_ENABLED:
            return 1.0
        with self._lock:
            # Edge decay penalty: -0.5 edge trend → 0.85x risk
            edge_penalty = max(-0.4, min(0.0, self._state.edge_trend * 0.4))

            # Stationarity penalty: high chaos → reduced risk
            stat_penalty = -self._state.stationarity_index * 0.2  # max -0.2

            mult = 1.0 + edge_penalty + stat_penalty
            return max(0.60, min(1.0, mult))

    def maybe_refresh(self):
        """Refresh if stale."""
        if time.time() - self._last_refresh > REFRESH_INTERVAL:
            self.refresh()

    def summary(self) -> dict:
        """Dashboard summary."""
        with self._lock:
            return {
                "enabled": CALIBRATOR_ENABLED,
                "health": round(self._state.health_score, 3),
                "stationarity": round(self._state.stationarity_index, 3),
                "edge_trend": round(self._state.edge_trend, 4),
                "conviction_corr": round(self._state.conviction_correlation, 3),
                "risk_mult": round(self.risk_multiplier(), 3),
                "grade_adj": {g: round(v, 3) for g, v in self._state.grade_adjustments.items()},
                "tp_leak": {k: round(v, 3) for k, v in self._state.tp_leak.items()},
                "n_outcomes": self._state.n_outcomes,
                "refreshes": self._n_refreshes,
            }

    def log_status(self):
        """Log calibration status."""
        s = self.summary()
        log.info(f"Calibrator: health={s['health']:.2f} "
                 f"stationarity={s['stationarity']:.2f} "
                 f"edge_trend={s['edge_trend']:+.3f} "
                 f"risk_mult={s['risk_mult']:.2f}x "
                 f"conv_corr={s['conviction_corr']:+.2f}")
        if s['grade_adj']:
            adj_str = " ".join(f"{g}={v:.2f}" for g, v in s['grade_adj'].items())
            log.info(f"  Grade adjustments: {adj_str}")

    # ═══════════════════════════════════════════════════════════
    #  REFRESH & COMPUTATION
    # ═══════════════════════════════════════════════════════════

    def refresh(self):
        """Reload shadow data and recompute calibration metrics."""
        try:
            outcomes = self._load_shadow_outcomes()
            if not outcomes:
                return

            # Only use longs (LONG_ONLY_MODE)
            longs = [o for o in outcomes if o.get("side", "").lower() == "long"]
            # Only passed signals for calibration (these are what we'd actually trade)
            passed = [o for o in longs if o.get("passed")]

            with self._lock:
                self._state.n_outcomes = len(passed)
                if len(passed) >= MIN_GRADE_SAMPLES:
                    self._calibrate_grades(passed)
                    self._calibrate_tp_leak(passed)
                    self._calibrate_stationarity(passed)
                    self._calibrate_edge_trend(passed)
                    self._calibrate_conviction_correlation(passed)
                    self._compute_health_score()
                    self._state.updated_at = time.time()

                self._last_refresh = time.time()
                self._n_refreshes += 1
                self._save_state()

        except Exception as e:
            _log.warning(f"Calibrator refresh error: {e}")

    def _calibrate_grades(self, passed: List[dict]):
        """Compute grade-vs-actual-performance calibration adjustments."""
        by_grade: Dict[str, List[dict]] = defaultdict(list)
        for o in passed:
            g = o.get("grade", "")
            if g:
                by_grade[g].append(o)

        # Compute overall average ExpR as baseline
        overall_expr = sum(o.get("pnl_r", 0) for o in passed) / len(passed)

        for grade in ["A+", "A", "B", "C", "D"]:
            trades = by_grade.get(grade, [])
            if len(trades) < MIN_GRADE_SAMPLES:
                continue

            grade_expr = sum(o.get("pnl_r", 0) for o in trades) / len(trades)

            # Compute adjustment: ratio of grade ExpR to overall ExpR
            # If C outperforms average, its multiplier should increase
            if overall_expr > 0:
                raw_adj = grade_expr / overall_expr
            elif grade_expr > 0:
                raw_adj = 1.2  # slightly boost profitable grade when overall is flat
            else:
                raw_adj = 0.8  # slightly penalize negative grade

            # Bound to 0.7–1.3 (never extreme swings)
            bounded = max(0.70, min(1.30, raw_adj))

            # EWMA smooth
            key = f"grade_{grade}"
            smoothed = self._ewma(key, bounded)
            self._state.grade_adjustments[grade] = smoothed

    def _calibrate_tp_leak(self, passed: List[dict]):
        """Compute TP leak (peak_r - actual_win_r) per exit mode."""
        by_exit: Dict[str, List[dict]] = defaultdict(list)
        for o in passed:
            em = o.get("exit_mode", "")
            if em:
                by_exit[em].append(o)

        for exit_mode, trades in by_exit.items():
            wins = [t for t in trades if t.get("pnl_r", 0) > 0]
            if len(wins) < 10:
                continue

            avg_peak = sum(t.get("peak_r", 0) for t in wins) / len(wins)
            avg_win = sum(t.get("pnl_r", 0) for t in wins) / len(wins)
            leak = avg_peak - avg_win

            key = f"tp_leak_{exit_mode}"
            self._state.tp_leak[exit_mode] = self._ewma(key, leak)

    def _calibrate_stationarity(self, passed: List[dict]):
        """Compute temporal stationarity index from rolling windows."""
        # Sort by timestamp
        sorted_trades = sorted(passed, key=lambda x: x.get("ts_ms", 0))
        n = len(sorted_trades)
        if n < STATIONARITY_WINDOW * 2:
            return

        # Compute WR for each window
        window_wrs = []
        for i in range(0, n - STATIONARITY_WINDOW + 1, STATIONARITY_WINDOW // 2):
            window = sorted_trades[i:i + STATIONARITY_WINDOW]
            if len(window) < STATIONARITY_WINDOW // 2:
                continue
            wr = sum(1 for t in window if t.get("pnl_r", 0) > 0) / len(window)
            window_wrs.append(wr)

        if len(window_wrs) < 3:
            return

        # Stationarity = coefficient of variation of window WRs
        mean_wr = sum(window_wrs) / len(window_wrs)
        if mean_wr <= 0:
            return

        variance = sum((wr - mean_wr) ** 2 for wr in window_wrs) / len(window_wrs)
        std_wr = math.sqrt(variance)
        cv = std_wr / mean_wr

        # Normalize to 0–1 range (cv of 0.3 = moderate, 0.5+ = chaotic)
        raw_stationarity = min(1.0, cv / 0.5)

        self._state.stationarity_index = self._ewma("stationarity", raw_stationarity)

    def _calibrate_edge_trend(self, passed: List[dict]):
        """Detect if system edge is degrading or strengthening over time."""
        sorted_trades = sorted(passed, key=lambda x: x.get("ts_ms", 0))
        n = len(sorted_trades)
        if n < STATIONARITY_WINDOW * EDGE_DECAY_WINDOWS:
            return

        # Compute ExpR for each recent window
        window_exprs = []
        step = STATIONARITY_WINDOW
        # Take the last EDGE_DECAY_WINDOWS windows
        start = max(0, n - step * EDGE_DECAY_WINDOWS)
        for i in range(start, n - step + 1, step):
            window = sorted_trades[i:i + step]
            if len(window) < step // 2:
                continue
            expr = sum(t.get("pnl_r", 0) for t in window) / len(window)
            window_exprs.append(expr)

        if len(window_exprs) < 3:
            return

        # Compute trend: simple linear slope of ExpR over windows
        # Positive = edge strengthening, negative = edge degrading
        x_mean = (len(window_exprs) - 1) / 2
        y_mean = sum(window_exprs) / len(window_exprs)

        numerator = sum((i - x_mean) * (y - y_mean)
                        for i, y in enumerate(window_exprs))
        denominator = sum((i - x_mean) ** 2 for i in range(len(window_exprs)))

        if denominator > 0:
            slope = numerator / denominator
        else:
            slope = 0.0

        # Normalize slope to roughly -1 to +1 range
        # A slope of +0.1 ExpR per window is very strong improvement
        normalized = max(-1.0, min(1.0, slope * 5.0))

        self._state.edge_trend = self._ewma("edge_trend", normalized)

    def _calibrate_conviction_correlation(self, passed: List[dict]):
        """Check if conviction score actually correlates with outcomes."""
        # Extract (conviction, pnl_r) pairs
        pairs = [(o.get("conviction", 50), o.get("pnl_r", 0))
                 for o in passed if o.get("conviction") is not None]

        if len(pairs) < 30:
            return

        # Pearson-like correlation (simplified)
        convs = [p[0] for p in pairs]
        pnls = [p[1] for p in pairs]

        mean_c = sum(convs) / len(convs)
        mean_p = sum(pnls) / len(pnls)

        cov = sum((c - mean_c) * (p - mean_p) for c, p in zip(convs, pnls))
        var_c = sum((c - mean_c) ** 2 for c in convs)
        var_p = sum((p - mean_p) ** 2 for p in pnls)

        denom = math.sqrt(var_c * var_p)
        if denom > 0:
            corr = cov / denom
        else:
            corr = 0.0

        self._state.conviction_correlation = self._ewma("conv_corr", corr)

    def _compute_health_score(self):
        """Composite health score from all calibration metrics."""
        # Health penalized by:
        # - High stationarity (chaotic environment)
        # - Negative edge trend (edge decaying)
        # - Negative conviction correlation (conviction is anti-predictive)

        stat_penalty = self._state.stationarity_index * 0.3    # max -0.3
        edge_penalty = max(0, -self._state.edge_trend) * 0.3   # max -0.3
        conv_penalty = max(0, -self._state.conviction_correlation) * 0.2  # max -0.2

        health = 1.0 - stat_penalty - edge_penalty - conv_penalty
        self._state.health_score = max(0.0, min(1.0, health))

    def _ewma(self, key: str, new_val: float) -> float:
        """EWMA smoothing with persistence."""
        old = self._smoothed.get(key, new_val)
        smoothed = EWMA_ALPHA * new_val + (1 - EWMA_ALPHA) * old
        self._smoothed[key] = smoothed
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
        """Persist smoothed state to disk."""
        try:
            state = {
                "smoothed": self._smoothed,
                "grade_adjustments": self._state.grade_adjustments,
                "tp_leak": self._state.tp_leak,
                "stationarity_index": self._state.stationarity_index,
                "edge_trend": self._state.edge_trend,
                "health_score": self._state.health_score,
                "conviction_correlation": self._state.conviction_correlation,
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
            _log.debug(f"Calibrator save: {e}")

    def _load_state(self):
        """Load persisted state."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self._smoothed = state.get("smoothed", {})
                ga = state.get("grade_adjustments", {})
                if ga:
                    self._state.grade_adjustments.update(ga)
                self._state.tp_leak = state.get("tp_leak", {})
                self._state.stationarity_index = state.get("stationarity_index", 0.0)
                self._state.edge_trend = state.get("edge_trend", 0.0)
                self._state.health_score = state.get("health_score", 1.0)
                self._state.conviction_correlation = state.get("conviction_correlation", 0.0)
            except Exception:
                pass
