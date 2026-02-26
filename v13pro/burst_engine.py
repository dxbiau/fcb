"""
v13pro/burst_engine.py -- Burst Detection & Exploitation Engine

Evolutionary overlay that detects edge strengthening and exploits burst
windows aggressively after Shadow Trader validation. Contracts rapidly
under degradation.

Core formulas:
  ECS = Edge Confidence Score per combo (Bayesian, decay-weighted)
  ΔECS = ECS momentum (rate of change between refreshes)
  BCS = w1·ECS + w2·σ(ΔECS) + w3·P_lifecycle + w4·V_norm + w5·R_cross
  
  Risk:     risk_mult   = f(BCS, drawdown)  ∈ [DECAY_FLOOR, BURST_CEIL]
  Leverage: lev_mult    = f(BCS, drawdown)  ∈ [DECAY_FLOOR, BURST_CEIL]
  TP:       tp_mult     = f(BCS, ECS)       ∈ [DECAY_FLOOR, BURST_CEIL]

Burst activation gating:
  - Shadow Trader must confirm persistent edge over rolling window
  - BCS must exceed BURST_THRESHOLD for MIN_BURST_SUSTAIN consecutive refreshes
  - Drawdown override: burst disabled when DD > BURST_DD_CUTOFF

Decay activation:
  - Immediate when BCS < DECAY_THRESHOLD (no confirmation needed — safety first)

Design principles:
  - All probabilistic (continuous 0.0–1.0 scores)
  - EWMA smoothed, periodic refresh, O(1) lookups
  - Shadow-validated — never scale aggressively without statistical proof
  - Asymmetric: slow to scale up (safety), fast to scale down (protection)
  - Computationally scalable to 3000+ pairs
  - Reversible: set BURST_ENABLED=False to bypass entirely
  - Non-destructive overlay — never alters core entry logic
"""

import glob
import json
import math
import os
import time
import threading
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
STATE_FILE = os.path.join(cfg.BASE_DIR, "burst_state.json")

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════

BURST_ENABLED = True

# Refresh cadence
REFRESH_INTERVAL = 600          # 10 minutes — faster than lifecycle (20m)
                                # Burst detection needs higher temporal resolution

# ── ECS (Edge Confidence Score) ──
ECS_DECAY_HALFLIFE = 40         # half-life in trades — recent trades weighted 2x
ECS_MIN_TRADES = 15             # minimum trades for valid ECS
ECS_RECENT_WINDOW = 50          # trades for rolling ECS computation
ECS_EWMA_ALPHA = 0.30           # smoothing for ECS updates (faster than lifecycle)

# ── BCS (Burst Confidence Score) ──
# BCS = w1·ECS + w2·σ(ΔECS) + w3·P_lifecycle + w4·V_norm + w5·R_cross
BCS_W1 = 0.35                  # ECS weight — edge confidence is primary signal
BCS_W2 = 0.25                  # ΔECS weight — momentum matters for burst detection
BCS_W3 = 0.20                  # lifecycle weight — pair-level expansion/improvement
BCS_W4 = 0.10                  # volatility normalized
BCS_W5 = 0.10                  # cross-sectional inverse risk

# ── State thresholds ──
BURST_THRESHOLD = 0.68          # BCS > this → candidate for burst mode
DECAY_THRESHOLD = 0.35          # BCS < this → immediate decay mode
MIN_BURST_SUSTAIN = 2           # consecutive refreshes above threshold to activate
BURST_DD_CUTOFF_PCT = 12.0      # disable burst when drawdown > 12%

# ── Shadow validation ──
SHADOW_VALIDATION_WINDOW = 30   # last N passed-long outcomes for validation
SHADOW_MIN_WR = 0.52            # min WR for shadow validation
SHADOW_MIN_EXPR = 0.00          # min ExpR for shadow validation (must be positive)

# ── Multiplier ranges ──
# Burst (BCS > BURST_THRESHOLD + validated)
BURST_RISK_MAX = 1.25           # up to +25% risk during confirmed burst
BURST_LEVERAGE_MAX = 1.40       # up to +40% leverage during confirmed burst
BURST_TP_MAX = 1.35             # up to +35% TP during confirmed burst

# Decay (BCS < DECAY_THRESHOLD)
DECAY_RISK_MIN = 0.50           # down to 50% risk during decay
DECAY_LEVERAGE_MIN = 0.70       # down to 70% leverage during decay
DECAY_TP_MIN = 0.80             # down to 80% TP during decay

# ── Dynamic leverage formula ──
# L_mult = 1 + (BURST_LEVERAGE_MAX - 1) · BCS^γ · f_drawdown
GAMMA = 1.5                     # convex — only aggressive at high BCS
DD_MAX_FOR_BURST = 15.0         # drawdown % at which f_drawdown → 0

# ── Live outcome tracking ──
MAX_LIVE_OUTCOMES = 200         # ring buffer of recent live outcomes
LIVE_OUTCOME_WEIGHT = 1.5       # live outcomes weighted 1.5x vs shadow


# ══════════════════════════════════════════════════════════════
#  EDGE CONFIDENCE SCORE (per combo)
# ══════════════════════════════════════════════════════════════

class EdgeConfidence:
    """Bayesian decay-weighted edge confidence for a (strategy, tf) combo."""
    __slots__ = ["ecs", "prev_ecs", "delta_ecs", "n_trades",
                 "recent_wr", "recent_expr", "updated_at"]

    def __init__(self):
        self.ecs = 0.5           # neutral
        self.prev_ecs = 0.5      # previous refresh value (for ΔECS)
        self.delta_ecs = 0.0     # rate of change
        self.n_trades = 0
        self.recent_wr = 0.5
        self.recent_expr = 0.0
        self.updated_at = 0.0

    def to_dict(self) -> dict:
        return {
            "ecs": round(self.ecs, 4),
            "delta_ecs": round(self.delta_ecs, 4),
            "n_trades": self.n_trades,
            "recent_wr": round(self.recent_wr, 3),
            "recent_expr": round(self.recent_expr, 4),
        }


# ══════════════════════════════════════════════════════════════
#  BURST ENGINE
# ══════════════════════════════════════════════════════════════

class BurstEngine:
    """
    Burst detection and exploitation engine.

    Detects edge strengthening via rolling ECS computation, aggregates
    into system-level BCS, and provides dynamic risk/leverage/TP multipliers.

    All getters are O(1). Refresh is periodic from shadow data.
    """

    def __init__(self, lifecycle=None, cross_sect=None):
        self._lock = threading.RLock()

        # External references (optional — enriches BCS when available)
        self._lifecycle = lifecycle
        self._cross_sect = cross_sect

        # ECS cache: (strategy, tf) → EdgeConfidence
        self._ecs_cache: Dict[Tuple[str, str], EdgeConfidence] = {}

        # Per-pair ECS: symbol → EdgeConfidence
        self._pair_ecs: Dict[str, EdgeConfidence] = {}

        # System-level BCS
        self._bcs = 0.50          # current BCS
        self._bcs_smoothed = 0.50 # EWMA smoothed
        self._burst_state = "NORMAL"  # BURST / NORMAL / DECAY
        self._burst_sustain_count = 0  # consecutive refreshes above threshold
        self._shadow_validated = False # shadow confirms edge

        # Drawdown tracking (fed from bot.py)
        self._current_dd_pct = 0.0
        self._current_equity = cfg.START_EQUITY
        self._peak_equity = cfg.START_EQUITY

        # Live outcome ring buffer
        self._live_outcomes: deque = deque(maxlen=MAX_LIVE_OUTCOMES)

        # EWMA state for ECS smoothing
        self._ecs_smoothed: Dict[str, float] = {}

        # Timing
        self._last_refresh = 0.0
        self._n_refreshes = 0
        self._start_time = time.time()

        # Load persisted state
        self._load_state()

        # Initial computation
        self.refresh()

    # ═══════════════════════════════════════════════════════════
    #  EXTERNAL WIRING (set lifecycle/cross_sect references)
    # ═══════════════════════════════════════════════════════════

    def set_lifecycle(self, lifecycle):
        """Wire lifecycle tracker for P_lifecycle component."""
        self._lifecycle = lifecycle

    def set_cross_sectional(self, cross_sect):
        """Wire cross-sectional awareness for R_cross component."""
        self._cross_sect = cross_sect

    def update_equity(self, equity: float, peak_equity: float):
        """Update equity/DD state from bot heartbeat."""
        with self._lock:
            self._current_equity = equity
            self._peak_equity = peak_equity
            if peak_equity > 0:
                self._current_dd_pct = (peak_equity - equity) / peak_equity * 100
            else:
                self._current_dd_pct = 0.0

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API (O(1) lookups)
    # ═══════════════════════════════════════════════════════════

    def risk_multiplier(self, symbol: str = "",
                        strategy: str = "", tf: str = "") -> float:
        """
        Risk scaling factor based on burst state.

        BURST:  1.0 to BURST_RISK_MAX (scale up proportional to BCS)
        NORMAL: 1.0 (neutral)
        DECAY:  DECAY_RISK_MIN to 1.0 (scale down proportional to BCS)

        Returns float ∈ [DECAY_RISK_MIN, BURST_RISK_MAX].
        """
        if not BURST_ENABLED:
            return 1.0
        with self._lock:
            return self._compute_risk_mult(symbol, strategy, tf)

    def leverage_multiplier(self, symbol: str = "") -> float:
        """
        Leverage scaling factor based on burst state + drawdown.

        Formula: L_mult = 1 + (MAX-1) · BCS^γ · f_drawdown
        Only > 1.0 when in validated BURST mode.

        Returns float ∈ [DECAY_LEVERAGE_MIN, BURST_LEVERAGE_MAX].
        """
        if not BURST_ENABLED:
            return 1.0
        with self._lock:
            return self._compute_leverage_mult(symbol)

    def tp_multiplier(self, symbol: str = "",
                      strategy: str = "", tf: str = "") -> float:
        """
        TP scaling factor based on burst state + ECS.

        BURST:  1.0 to BURST_TP_MAX (wider TP to capture bigger runs)
        NORMAL: 1.0
        DECAY:  DECAY_TP_MIN to 1.0 (tighter TP to lock profits faster)

        Returns float ∈ [DECAY_TP_MIN, BURST_TP_MAX].
        """
        if not BURST_ENABLED:
            return 1.0
        with self._lock:
            return self._compute_tp_mult(symbol, strategy, tf)

    @property
    def bcs(self) -> float:
        """Current system-level Burst Confidence Score [0, 1]."""
        return self._bcs_smoothed

    @property
    def burst_state(self) -> str:
        """Current burst state: BURST / NORMAL / DECAY."""
        return self._burst_state

    @property
    def shadow_validated(self) -> bool:
        """Whether shadow data confirms the current edge."""
        return self._shadow_validated

    def get_combo_ecs(self, strategy: str, tf: str) -> float:
        """Get ECS for a specific combo. Returns 0.5 if unknown."""
        with self._lock:
            ec = self._ecs_cache.get((strategy, tf))
            return ec.ecs if ec else 0.5

    def get_pair_ecs(self, symbol: str) -> float:
        """Get ECS for a specific pair. Returns 0.5 if unknown."""
        with self._lock:
            ec = self._pair_ecs.get(symbol)
            return ec.ecs if ec else 0.5

    def record_outcome(self, symbol: str, strategy: str, tf: str,
                       pnl_r: float, passed: bool = True):
        """Feed live trade outcomes for real-time burst tracking."""
        with self._lock:
            self._live_outcomes.append({
                "symbol": symbol, "strategy": strategy, "tf": tf,
                "pnl_r": pnl_r, "passed": passed,
                "ts": time.time(),
            })

    def maybe_refresh(self):
        """Refresh if stale — call from heartbeat."""
        if time.time() - self._last_refresh > REFRESH_INTERVAL:
            self.refresh()

    def summary(self) -> dict:
        """Dashboard summary."""
        with self._lock:
            top_combos = sorted(
                self._ecs_cache.items(),
                key=lambda x: x[1].ecs, reverse=True
            )[:5]

            top_pairs = sorted(
                self._pair_ecs.items(),
                key=lambda x: x[1].ecs, reverse=True
            )[:5]

            return {
                "bcs": round(self._bcs_smoothed, 3),
                "state": self._burst_state,
                "shadow_valid": self._shadow_validated,
                "dd_pct": round(self._current_dd_pct, 1),
                "sustain": self._burst_sustain_count,
                "top_combos": [
                    (f"{s}/{t}", round(ec.ecs, 3), round(ec.delta_ecs, 3))
                    for (s, t), ec in top_combos
                ],
                "top_pairs": [
                    (sym.replace("/USDT:USDT", ""), round(ec.ecs, 3))
                    for sym, ec in top_pairs
                ],
                "refreshes": self._n_refreshes,
                "risk_mult": round(self._compute_risk_mult(), 3),
                "lev_mult": round(self._compute_leverage_mult(), 3),
                "tp_mult": round(self._compute_tp_mult(), 3),
            }

    def log_status(self):
        """Log initial status."""
        s = self.summary()
        state_str = s["state"]
        valid_str = "✓" if s["shadow_valid"] else "✗"
        log.info(f"Burst engine: BCS={s['bcs']:.3f} [{state_str}] "
                 f"shadow={valid_str} "
                 f"risk={s['risk_mult']:.2f}x lev={s['lev_mult']:.2f}x "
                 f"tp={s['tp_mult']:.2f}x")
        if s["top_combos"]:
            top = ", ".join(f"{c[0]}({c[1]:.2f})" for c in s["top_combos"][:3])
            log.info(f"  Top ECS: {top}")

    # ═══════════════════════════════════════════════════════════
    #  REFRESH & COMPUTATION
    # ═══════════════════════════════════════════════════════════

    def refresh(self):
        """Reload shadow data and recompute all burst metrics."""
        try:
            outcomes = self._load_shadow_outcomes()
            if not outcomes:
                return

            # Filter to longs only (LONG_ONLY_MODE)
            if cfg.LONG_ONLY_MODE:
                outcomes = [o for o in outcomes if o.get("side", "").lower() == "long"]

            # Separate passed vs all
            passed = [o for o in outcomes if o.get("passed")]

            # ── Step 1: Compute per-combo ECS ──
            self._refresh_combo_ecs(passed)

            # ── Step 2: Compute per-pair ECS ──
            self._refresh_pair_ecs(outcomes)

            # ── Step 3: Shadow validation ──
            self._validate_shadow(passed)

            # ── Step 4: Compute system BCS ──
            self._compute_bcs()

            # ── Step 5: Update burst state ──
            self._update_burst_state()

            # ── Step 6: Persist ──
            with self._lock:
                self._last_refresh = time.time()
                self._n_refreshes += 1
            self._save_state()

        except Exception as e:
            _log.warning(f"Burst engine refresh error: {e}")

    def _refresh_combo_ecs(self, passed_outcomes: List[dict]):
        """Compute decay-weighted ECS per (strategy, tf) combo."""
        by_combo: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for o in passed_outcomes:
            key = (o.get("strategy", ""), o.get("tf", ""))
            by_combo[key].append(o)

        # Include live outcomes with higher weight
        with self._lock:
            for lo in self._live_outcomes:
                if lo.get("passed"):
                    key = (lo.get("strategy", ""), lo.get("tf", ""))
                    # Create a synthetic shadow-like record with weight marker
                    by_combo[key].append({
                        "pnl_r": lo["pnl_r"],
                        "ts_ms": lo["ts"] * 1000,
                        "_live_weight": LIVE_OUTCOME_WEIGHT,
                    })

        with self._lock:
            for (strat, tf), trades in by_combo.items():
                if len(trades) < ECS_MIN_TRADES:
                    continue

                # Sort by timestamp
                trades.sort(key=lambda x: x.get("ts_ms", 0))

                # Take recent window
                recent = trades[-ECS_RECENT_WINDOW:]

                # Compute decay-weighted metrics
                ecs_val, wr, expr = self._decay_weighted_ecs(recent)

                # Get or create EdgeConfidence
                ec = self._ecs_cache.get((strat, tf), EdgeConfidence())

                # Save previous for ΔECS computation
                ec.prev_ecs = ec.ecs

                # EWMA smooth the new ECS
                sm_key = f"combo_{strat}_{tf}"
                ec.ecs = self._ewma_update(sm_key, ecs_val)

                # Compute ΔECS (momentum)
                ec.delta_ecs = ec.ecs - ec.prev_ecs

                ec.n_trades = len(trades)
                ec.recent_wr = wr
                ec.recent_expr = expr
                ec.updated_at = time.time()

                self._ecs_cache[(strat, tf)] = ec

    def _refresh_pair_ecs(self, all_outcomes: List[dict]):
        """Compute decay-weighted ECS per pair (across all combos)."""
        by_pair: Dict[str, List[dict]] = defaultdict(list)
        for o in all_outcomes:
            sym = o.get("symbol", "")
            if sym:
                by_pair[sym].append(o)

        with self._lock:
            for sym, trades in by_pair.items():
                if len(trades) < ECS_MIN_TRADES:
                    continue

                trades.sort(key=lambda x: x.get("ts_ms", 0))
                recent = trades[-ECS_RECENT_WINDOW:]

                ecs_val, wr, expr = self._decay_weighted_ecs(recent)

                ec = self._pair_ecs.get(sym, EdgeConfidence())
                ec.prev_ecs = ec.ecs

                sm_key = f"pair_{sym}"
                ec.ecs = self._ewma_update(sm_key, ecs_val)
                ec.delta_ecs = ec.ecs - ec.prev_ecs
                ec.n_trades = len(trades)
                ec.recent_wr = wr
                ec.recent_expr = expr
                ec.updated_at = time.time()

                self._pair_ecs[sym] = ec

    def _decay_weighted_ecs(self, trades: List[dict]) -> Tuple[float, float, float]:
        """
        Compute decay-weighted Edge Confidence Score from trade list.

        Uses exponential decay weighting: more recent trades count more.
        Maps resulting metrics into [0, 1] via sigmoid.

        Returns: (ecs, win_rate, expected_r)
        """
        if not trades:
            return 0.5, 0.5, 0.0

        n = len(trades)
        # Compute decay weights: w_i = 2^(-i/halflife) where i=0 is newest
        weights = []
        for i in range(n):
            age = n - 1 - i  # 0 for oldest, n-1 for newest → flip so newest=0
            w = 2.0 ** (-age / ECS_DECAY_HALFLIFE)
            # Apply live outcome boost
            if trades[i].get("_live_weight"):
                w *= trades[i]["_live_weight"]
            weights.append(w)

        total_w = sum(weights)
        if total_w == 0:
            return 0.5, 0.5, 0.0

        # Weighted win rate
        weighted_wins = sum(
            w for w, t in zip(weights, trades) if t.get("pnl_r", 0) > 0
        )
        wr = weighted_wins / total_w

        # Weighted expected R
        expr = sum(
            w * t.get("pnl_r", 0) for w, t in zip(weights, trades)
        ) / total_w

        # Map to [0, 1] via sigmoid
        # Raw score combines WR and ExpR
        # WR=50% ExpR=0 → 0.5, WR=70% ExpR=+0.5 → ~0.85
        raw = (wr - 0.5) * 3.0 + expr * 1.5  # scale factors
        ecs = 1.0 / (1.0 + math.exp(-raw))    # sigmoid → [0, 1]

        return ecs, wr, expr

    def _validate_shadow(self, passed_outcomes: List[dict]):
        """
        Validate current edge using Shadow Trader data.

        Checks recent N passed-long outcomes for positive expectancy.
        Asymmetric: quick to invalidate (safety), slower to validate.
        """
        if not passed_outcomes:
            with self._lock:
                self._shadow_validated = False
            return

        # Sort and take recent window
        sorted_outcomes = sorted(passed_outcomes, key=lambda x: x.get("ts_ms", 0))
        recent = sorted_outcomes[-SHADOW_VALIDATION_WINDOW:]

        if len(recent) < 10:  # need minimum sample
            with self._lock:
                self._shadow_validated = False
            return

        wins = sum(1 for t in recent if t.get("pnl_r", 0) > 0)
        wr = wins / len(recent)
        expr = sum(t.get("pnl_r", 0) for t in recent) / len(recent)

        with self._lock:
            self._shadow_validated = (wr >= SHADOW_MIN_WR and expr >= SHADOW_MIN_EXPR)

    def _compute_bcs(self):
        """
        Compute system-level Burst Confidence Score.

        BCS = w1·ECS_system + w2·σ(ΔECS_system) + w3·P_lifecycle + w4·V_norm + w5·R_cross
        """
        with self._lock:
            # ── Component 1: System ECS (weighted average across active combos) ──
            if self._ecs_cache:
                # Weight by trade count (more data = more reliable)
                total_n = sum(ec.n_trades for ec in self._ecs_cache.values())
                if total_n > 0:
                    system_ecs = sum(
                        ec.ecs * ec.n_trades for ec in self._ecs_cache.values()
                    ) / total_n
                else:
                    system_ecs = 0.5
            else:
                system_ecs = 0.5

            # ── Component 2: ECS momentum (system-level ΔECS via sigmoid) ──
            if self._ecs_cache:
                total_n = sum(ec.n_trades for ec in self._ecs_cache.values())
                if total_n > 0:
                    system_delta = sum(
                        ec.delta_ecs * ec.n_trades for ec in self._ecs_cache.values()
                    ) / total_n
                else:
                    system_delta = 0.0
            else:
                system_delta = 0.0
            # Sigmoid mapping: ΔECS → [0, 1]
            # Positive delta (improving) → > 0.5, negative → < 0.5
            delta_sensitivity = 20.0  # controls how fast momentum saturates
            delta_norm = 1.0 / (1.0 + math.exp(-system_delta * delta_sensitivity))

            # ── Component 3: Lifecycle burst component ──
            # Average of (improving - degrading) across scored pairs
            p_lifecycle = 0.5
            if self._lifecycle:
                try:
                    lc_sum = self._lifecycle.summary()
                    # Pull from expanding + improving as burst indicators
                    n_expanding = len(lc_sum.get("expanding", []))
                    n_degrading = len(lc_sum.get("degrading", []))
                    n_total = lc_sum.get("pairs_scored", 1)
                    if n_total > 0:
                        # Ratio of expanding to total as lifecycle burst probability
                        p_lifecycle = 0.5 + 0.5 * (n_expanding - n_degrading) / max(n_total, 1)
                        p_lifecycle = max(0.0, min(1.0, p_lifecycle))
                except Exception:
                    p_lifecycle = 0.5

            # ── Component 4: Normalized volatility ──
            # Use average pair vol_trend from pair ECS data
            # Positive vol_trend = expanding vol = more opportunity
            v_norm = 0.5
            if self._pair_ecs:
                # Use recent_expr as a proxy for opportunity quality
                exprs = [ec.recent_expr for ec in self._pair_ecs.values()
                         if ec.n_trades >= ECS_MIN_TRADES]
                if exprs:
                    avg_expr = sum(exprs) / len(exprs)
                    # Map ExpR to [0,1]: ExpR=0 → 0.5, ExpR=+0.5 → ~0.8
                    v_norm = 1.0 / (1.0 + math.exp(-avg_expr * 3.0))

            # ── Component 5: Cross-sectional inverse risk ──
            # Low cluster risk = more room to be aggressive
            r_cross = 0.5
            if self._cross_sect:
                try:
                    cs_mult = self._cross_sect.risk_multiplier()
                    # cs_mult is 0.5–1.0 (low = high cluster risk)
                    # Invert: high cs_mult → high r_cross (room to be aggressive)
                    r_cross = cs_mult  # already in [0.5, 1.0] → normalize to [0, 1]
                    r_cross = (r_cross - 0.5) * 2.0  # map [0.5, 1.0] → [0.0, 1.0]
                except Exception:
                    r_cross = 0.5

            # ── Aggregate BCS ──
            raw_bcs = (BCS_W1 * system_ecs +
                       BCS_W2 * delta_norm +
                       BCS_W3 * p_lifecycle +
                       BCS_W4 * v_norm +
                       BCS_W5 * r_cross)

            # Clamp to [0, 1]
            raw_bcs = max(0.0, min(1.0, raw_bcs))

            # EWMA smooth
            self._bcs_smoothed = self._ewma_update("system_bcs", raw_bcs)
            self._bcs = raw_bcs

    def _update_burst_state(self):
        """
        Determine burst state based on BCS + shadow validation + drawdown.

        State transitions:
          NORMAL → BURST:  BCS > BURST_THRESHOLD for MIN_BURST_SUSTAIN + shadow validated + DD < cutoff
          BURST  → NORMAL: BCS drops below BURST_THRESHOLD or shadow invalidated or DD > cutoff
          ANY    → DECAY:  BCS < DECAY_THRESHOLD (immediate — safety first)
          DECAY  → NORMAL: BCS rises above DECAY_THRESHOLD
        """
        with self._lock:
            bcs = self._bcs_smoothed
            prev_state = self._burst_state

            # ── Decay check (always immediate — safety first) ──
            if bcs < DECAY_THRESHOLD:
                self._burst_state = "DECAY"
                self._burst_sustain_count = 0
                if prev_state != "DECAY":
                    _log.info(f"Burst engine: → DECAY (BCS={bcs:.3f})")
                return

            # ── Drawdown override ──
            if self._current_dd_pct > BURST_DD_CUTOFF_PCT:
                if prev_state == "BURST":
                    self._burst_state = "NORMAL"
                    self._burst_sustain_count = 0
                    _log.info(f"Burst engine: BURST → NORMAL "
                              f"(DD={self._current_dd_pct:.1f}% > cutoff)")
                return

            # ── Burst candidate check ──
            if bcs >= BURST_THRESHOLD:
                self._burst_sustain_count += 1

                if (self._burst_sustain_count >= MIN_BURST_SUSTAIN
                        and self._shadow_validated):
                    if prev_state != "BURST":
                        self._burst_state = "BURST"
                        _log.info(f"Burst engine: → BURST "
                                  f"(BCS={bcs:.3f} sustained={self._burst_sustain_count} "
                                  f"shadow=✓)")
                else:
                    if prev_state == "BURST":
                        # Lost validation while BCS still high
                        if not self._shadow_validated:
                            self._burst_state = "NORMAL"
                            _log.info(f"Burst engine: BURST → NORMAL (shadow invalidated)")
                    # Stay in current state otherwise
            else:
                # Between DECAY_THRESHOLD and BURST_THRESHOLD → NORMAL
                self._burst_sustain_count = 0
                if prev_state != "NORMAL":
                    self._burst_state = "NORMAL"
                    if prev_state == "BURST":
                        _log.info(f"Burst engine: BURST → NORMAL (BCS={bcs:.3f})")
                    elif prev_state == "DECAY":
                        _log.info(f"Burst engine: DECAY → NORMAL (BCS={bcs:.3f})")

    # ═══════════════════════════════════════════════════════════
    #  MULTIPLIER COMPUTATION
    # ═══════════════════════════════════════════════════════════

    def _compute_risk_mult(self, symbol: str = "",
                           strategy: str = "", tf: str = "") -> float:
        """Compute risk multiplier based on burst state + pair-level ECS."""
        state = self._burst_state
        bcs = self._bcs_smoothed

        if state == "BURST":
            # Scale from 1.0 to BURST_RISK_MAX proportional to BCS
            # BCS at threshold → 1.0, BCS at 1.0 → BURST_RISK_MAX
            burst_range = BURST_RISK_MAX - 1.0
            fraction = (bcs - BURST_THRESHOLD) / (1.0 - BURST_THRESHOLD + 1e-9)
            fraction = max(0.0, min(1.0, fraction))

            # Combo-level boost for hot combos
            combo_boost = 0.0
            if strategy and tf:
                ec = self._ecs_cache.get((strategy, tf))
                if ec and ec.ecs > 0.7:
                    combo_boost = (ec.ecs - 0.7) * 0.3  # up to +0.09 extra

            mult = 1.0 + burst_range * fraction + combo_boost

            # Apply drawdown damping
            mult *= self._f_drawdown()

            return max(1.0, min(BURST_RISK_MAX, mult))

        elif state == "DECAY":
            # Scale from DECAY_RISK_MIN to 1.0 proportional to BCS
            decay_range = 1.0 - DECAY_RISK_MIN
            fraction = bcs / (DECAY_THRESHOLD + 1e-9)
            fraction = max(0.0, min(1.0, fraction))
            return DECAY_RISK_MIN + decay_range * fraction

        else:  # NORMAL
            return 1.0

    def _compute_leverage_mult(self, symbol: str = "") -> float:
        """
        Compute leverage multiplier using the BCS^γ formula.

        L_mult = 1 + (BURST_LEVERAGE_MAX - 1) · BCS^γ · f_drawdown
        Only exceeds 1.0 during validated BURST mode.
        """
        state = self._burst_state
        bcs = self._bcs_smoothed

        if state == "BURST":
            # Dynamic leverage formula: L_mult = 1 + (MAX-1) · BCS^γ · f_dd
            burst_range = BURST_LEVERAGE_MAX - 1.0
            bcs_power = bcs ** GAMMA  # convex — aggressive only at high BCS
            f_dd = self._f_drawdown()
            mult = 1.0 + burst_range * bcs_power * f_dd
            return max(1.0, min(BURST_LEVERAGE_MAX, mult))

        elif state == "DECAY":
            # Scale down leverage during decay
            decay_range = 1.0 - DECAY_LEVERAGE_MIN
            fraction = bcs / (DECAY_THRESHOLD + 1e-9)
            fraction = max(0.0, min(1.0, fraction))
            return DECAY_LEVERAGE_MIN + decay_range * fraction

        else:  # NORMAL
            return 1.0

    def _compute_tp_mult(self, symbol: str = "",
                         strategy: str = "", tf: str = "") -> float:
        """
        Compute TP multiplier.

        During burst: widen TP to capture larger moves.
        Formula: TP_mult = 1 + k1·V_norm + k2·ECS
        During decay: tighten TP to lock profits faster.
        """
        state = self._burst_state
        bcs = self._bcs_smoothed

        if state == "BURST":
            # k1, k2 coefficients for TP expansion
            # Use optimizer-tuned values if available, else defaults
            k1 = getattr(self, "_optim_k1", 0.15)
            k2 = getattr(self, "_optim_k2", 0.20)

            # Get combo ECS for TP modulation
            combo_ecs = 0.5
            if strategy and tf:
                ec = self._ecs_cache.get((strategy, tf))
                if ec:
                    combo_ecs = ec.ecs

            # V_norm from BCS computation (approximated from pair ECS avg)
            v_norm = 0.5
            if self._pair_ecs:
                exprs = [ec.recent_expr for ec in self._pair_ecs.values()
                         if ec.n_trades >= ECS_MIN_TRADES]
                if exprs:
                    avg_expr = sum(exprs) / len(exprs)
                    v_norm = 1.0 / (1.0 + math.exp(-avg_expr * 3.0))

            tp_boost = k1 * (v_norm - 0.5) * 2.0 + k2 * (combo_ecs - 0.5) * 2.0
            mult = 1.0 + max(0.0, tp_boost)

            # Apply drawdown damping (don't widen TP in drawdown)
            mult = 1.0 + (mult - 1.0) * self._f_drawdown()

            return max(1.0, min(BURST_TP_MAX, mult))

        elif state == "DECAY":
            # Tighten TP during decay to take profits faster
            decay_range = 1.0 - DECAY_TP_MIN
            fraction = bcs / (DECAY_THRESHOLD + 1e-9)
            fraction = max(0.0, min(1.0, fraction))
            return DECAY_TP_MIN + decay_range * fraction

        else:  # NORMAL
            return 1.0

    def _f_drawdown(self) -> float:
        """
        Drawdown dampening factor: f_dd = max(0, 1 - DD/DD_MAX)^2.

        Convex (quadratic) — more aggressive reduction as DD grows.
        Returns 1.0 when no drawdown, approaches 0 rapidly as DD → DD_MAX.
        Prevents burst exploitation during significant drawdowns.
        """
        if self._current_dd_pct <= 0:
            return 1.0
        linear = max(0.0, 1.0 - self._current_dd_pct / DD_MAX_FOR_BURST)
        return linear * linear  # convex: 5% DD → 0.44, 10% DD → 0.11

    def max_positions_multiplier(self) -> float:
        """
        Position slot reduction during DECAY state.

        BURST:  1.0 (full slots)
        NORMAL: 1.0 (full slots)
        DECAY:  0.50 to 0.75 depending on BCS depth
          → e.g. max_concurrent 6 → 3 during deep decay

        Returns float ∈ [0.5, 1.0].
        """
        if not BURST_ENABLED:
            return 1.0
        with self._lock:
            if self._burst_state == "DECAY":
                # Lower BCS → fewer slots
                # At BCS=0.35 (just entered): 0.75x
                # At BCS=0.0 (deep decay): 0.50x
                fraction = max(0.0, self._bcs_smoothed / DECAY_THRESHOLD)
                return 0.50 + 0.25 * fraction
            return 1.0

    # ═══════════════════════════════════════════════════════════
    #  EWMA HELPER
    # ═══════════════════════════════════════════════════════════

    def _ewma_update(self, key: str, new_val: float) -> float:
        """EWMA smoothing: blend old and new values."""
        old = self._ecs_smoothed.get(key, new_val)
        smoothed = ECS_EWMA_ALPHA * new_val + (1 - ECS_EWMA_ALPHA) * old
        self._ecs_smoothed[key] = smoothed
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
        """Persist burst state to disk for fast recovery."""
        try:
            with self._lock:
                state = {
                    "bcs_smoothed": self._bcs_smoothed,
                    "burst_state": self._burst_state,
                    "burst_sustain_count": self._burst_sustain_count,
                    "shadow_validated": self._shadow_validated,
                    "ecs_smoothed": self._ecs_smoothed,
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
            _log.debug(f"Burst state save: {e}")

    def _load_state(self):
        """Load persisted burst state."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self._ecs_smoothed = state.get("ecs_smoothed", {})
                self._bcs_smoothed = state.get("bcs_smoothed", 0.5)
                self._burst_state = state.get("burst_state", "NORMAL")
                self._burst_sustain_count = state.get("burst_sustain_count", 0)
                self._shadow_validated = state.get("shadow_validated", False)
            except Exception:
                self._ecs_smoothed = {}
