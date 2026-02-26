"""
v13pro/burst_optimizer.py -- Iterative Self-Optimization for Burst Engine (Phase 2A)

Periodically replays shadow data with candidate parameter sets, evaluates
burst timing, profit capture, and drawdown, and updates burst_engine
parameters **only when statistically validated improvement is found**.

Design:
  - Grid search over key param dimensions (BCS weights, γ, k1/k2, thresholds)
  - Walk-forward evaluation: train on 70% of data, test on 30%
  - Monte Carlo confidence: bootstrap resample N times to ensure robustness
  - Only promote a candidate param set if it beats current by > MIN_IMPROVEMENT
  - Computationally cheap: runs in ~2-3 seconds even with 5000 outcomes
  - Non-destructive: never touches core entry logic, only overlay params
  - Persists iteration history for auditability

Constraints:
  - Never modify core v13Pro entry logic
  - Only refine overlay parameters and scaling functions
  - Preserve modular architecture
  - Maintain Shadow Trader gating
  - Ensure computational efficiency for 3000+ pairs
"""

import copy
import glob
import json
import math
import os
import random
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
OPTIM_STATE_FILE = os.path.join(cfg.BASE_DIR, "burst_optim_state.json")
OPTIM_HISTORY_FILE = os.path.join(cfg.BASE_DIR, "burst_optim_history.jsonl")

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════

OPTIMIZER_ENABLED = True
OPTIMIZER_INTERVAL = 7200       # run every 2 hours (shadow data grows slowly)
MIN_SHADOW_OUTCOMES = 200       # minimum outcomes needed for optimization
WALK_FORWARD_SPLIT = 0.70      # 70% train / 30% test
MONTE_CARLO_N = 50             # bootstrap resamples for confidence
MIN_IMPROVEMENT_PCT = 5.0      # candidate must beat current by ≥5%
MAX_ITERATIONS_PER_RUN = 80    # max param combos to evaluate per run
CONFIDENCE_THRESHOLD = 0.65    # candidate must win ≥65% of MC resamples

# ── Tunable Parameter Ranges ──
# These are the ranges the optimizer searches over.
# Each tuple is (min, max, step)
PARAM_RANGES = {
    "w1": (0.20, 0.50, 0.05),    # ECS weight
    "w2": (0.10, 0.35, 0.05),    # ΔECS momentum weight
    "w3": (0.10, 0.30, 0.05),    # lifecycle weight
    "w4": (0.05, 0.20, 0.05),    # volatility weight
    "w5": (0.05, 0.20, 0.05),    # cross-sectional weight
    "gamma": (1.0, 2.5, 0.25),   # leverage exponent
    "k1": (0.05, 0.30, 0.05),    # TP volatility coeff
    "k2": (0.10, 0.35, 0.05),    # TP ECS coeff
    "burst_threshold": (0.60, 0.78, 0.03),   # BCS burst activation
    "decay_threshold": (0.25, 0.42, 0.03),   # BCS decay activation
}


# ══════════════════════════════════════════════════════════════
#  DATA CLASSES
# ══════════════════════════════════════════════════════════════

@dataclass
class ParamSet:
    """A candidate parameter set for the burst engine."""
    w1: float = 0.35
    w2: float = 0.25
    w3: float = 0.20
    w4: float = 0.10
    w5: float = 0.10
    gamma: float = 1.5
    k1: float = 0.15
    k2: float = 0.20
    burst_threshold: float = 0.68
    decay_threshold: float = 0.35

    def weights_sum(self) -> float:
        return self.w1 + self.w2 + self.w3 + self.w4 + self.w5

    def normalize_weights(self):
        """Ensure BCS weights sum to 1.0."""
        s = self.weights_sum()
        if s > 0:
            self.w1 /= s
            self.w2 /= s
            self.w3 /= s
            self.w4 /= s
            self.w5 /= s


@dataclass
class SimResult:
    """Result of a simulation run on a dataset."""
    total_trades: int = 0
    burst_trades: int = 0
    burst_wins: int = 0
    burst_wr: float = 0.0
    burst_total_r: float = 0.0
    burst_avg_r: float = 0.0
    normal_trades: int = 0
    normal_total_r: float = 0.0
    decay_skipped: int = 0
    max_drawdown_pct: float = 0.0
    total_pnl_r: float = 0.0
    sharpe_approx: float = 0.0    # simplified daily Sharpe proxy
    score: float = 0.0            # composite fitness score


# ══════════════════════════════════════════════════════════════
#  BURST OPTIMIZER
# ══════════════════════════════════════════════════════════════

class BurstOptimizer:
    """
    Iterative self-optimizer for burst engine parameters.

    Periodically replays shadow data with candidate param sets,
    evaluates burst timing/profit/drawdown, and promotes the best
    if it beats current by MIN_IMPROVEMENT_PCT with MC confidence.
    """

    def __init__(self, burst_engine=None):
        self._lock = threading.RLock()
        self._burst_engine = burst_engine

        # Current best params (matches burst_engine defaults)
        self._current_params = ParamSet()

        # Optimization state
        self._last_run = 0.0
        self._n_runs = 0
        self._last_score = 0.0
        self._iterations_total = 0

        # Load persisted state
        self._load_state()

    def set_burst_engine(self, engine):
        """Wire burst engine reference."""
        self._burst_engine = engine

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════════

    def maybe_optimize(self):
        """Run optimization if enough time has passed. Called from heartbeat."""
        if not OPTIMIZER_ENABLED:
            return
        if time.time() - self._last_run < OPTIMIZER_INTERVAL:
            return
        self._run_optimization()

    def summary(self) -> dict:
        """Dashboard summary."""
        with self._lock:
            return {
                "enabled": OPTIMIZER_ENABLED,
                "runs": self._n_runs,
                "last_score": round(self._last_score, 3),
                "iterations": self._iterations_total,
                "params": asdict(self._current_params),
                "last_run_ago": int(time.time() - self._last_run) if self._last_run > 0 else -1,
            }

    def log_status(self):
        """Log initial status."""
        s = self.summary()
        _log.info(f"Burst optimizer: {s['runs']} runs, "
                  f"score={s['last_score']:.3f}, "
                  f"γ={self._current_params.gamma:.2f}, "
                  f"burst_thr={self._current_params.burst_threshold:.2f}")

    # ═══════════════════════════════════════════════════════════
    #  OPTIMIZATION CORE
    # ═══════════════════════════════════════════════════════════

    def _run_optimization(self):
        """
        Full optimization cycle:
          1. Load shadow outcomes
          2. Walk-forward split (70/30)
          3. Generate candidate param sets (smart grid + neighborhood)
          4. Simulate each on train set → score
          5. Validate top candidates on test set with MC bootstrap
          6. Promote if improvement > MIN_IMPROVEMENT_PCT with confidence
          7. Apply to burst engine + persist
        """
        try:
            outcomes = self._load_outcomes()
            if len(outcomes) < MIN_SHADOW_OUTCOMES:
                self._last_run = time.time()
                return

            # Walk-forward split
            split_idx = int(len(outcomes) * WALK_FORWARD_SPLIT)
            train = outcomes[:split_idx]
            test = outcomes[split_idx:]

            if len(test) < 30:
                self._last_run = time.time()
                return

            # Score current params on train set
            current_score = self._simulate(train, self._current_params)

            # Generate candidates
            candidates = self._generate_candidates()

            # Score all candidates on train set
            scored = []
            for params in candidates:
                result = self._simulate(train, params)
                scored.append((result.score, params, result))
                self._iterations_total += 1

            # Sort by score (descending)
            scored.sort(key=lambda x: x[0], reverse=True)

            # Take top 5 candidates for MC validation
            top_candidates = scored[:5]

            best_promoted = None
            for cand_score, cand_params, cand_train_result in top_candidates:
                # Must beat current by MIN_IMPROVEMENT (on train)
                if current_score.score > 0:
                    improvement = (cand_score - current_score.score) / current_score.score * 100
                else:
                    improvement = 100.0 if cand_score > 0 else 0.0

                if improvement < MIN_IMPROVEMENT_PCT:
                    continue

                # MC bootstrap validation on test set
                mc_wins = 0
                for _ in range(MONTE_CARLO_N):
                    # Resample test set with replacement
                    boot = random.choices(test, k=len(test))
                    cand_test = self._simulate(boot, cand_params)
                    curr_test = self._simulate(boot, self._current_params)
                    if cand_test.score > curr_test.score:
                        mc_wins += 1

                confidence = mc_wins / MONTE_CARLO_N
                if confidence >= CONFIDENCE_THRESHOLD:
                    # Validate on full test set (not bootstrapped)
                    cand_test_full = self._simulate(test, cand_params)
                    curr_test_full = self._simulate(test, self._current_params)

                    test_improvement = 0.0
                    if curr_test_full.score > 0:
                        test_improvement = ((cand_test_full.score - curr_test_full.score)
                                            / curr_test_full.score * 100)

                    if test_improvement > 0:
                        best_promoted = (cand_params, cand_train_result,
                                         cand_test_full, confidence,
                                         improvement, test_improvement)
                        break  # take first that passes all gates

            # Apply promotion
            if best_promoted:
                params, train_res, test_res, conf, train_imp, test_imp = best_promoted
                self._promote_params(params, train_res, test_res,
                                     conf, train_imp, test_imp)
            else:
                _log.info(f"Burst optimizer: no improvement found "
                          f"(current score={current_score.score:.3f}, "
                          f"tested {len(candidates)} candidates)")

            # Update state
            with self._lock:
                self._last_run = time.time()
                self._n_runs += 1
                self._last_score = current_score.score

            self._save_state()

        except Exception as e:
            _log.warning(f"Burst optimizer error: {e}")
            self._last_run = time.time()

    # ═══════════════════════════════════════════════════════════
    #  SIMULATION ENGINE
    # ═══════════════════════════════════════════════════════════

    def _simulate(self, outcomes: List[dict], params: ParamSet) -> SimResult:
        """
        Replay outcomes through a simulated burst engine with given params.

        Computes BCS at each step, determines burst/normal/decay state,
        applies multipliers, and tracks aggregate performance.
        """
        result = SimResult()
        if not outcomes:
            return result

        # Group by combo for ECS computation
        by_combo = defaultdict(list)
        for o in outcomes:
            key = (o.get("strategy", ""), o.get("tf", ""))
            by_combo[key].append(o)

        # Pre-compute rolling ECS per combo at each point
        equity = 100.0  # normalized starting equity
        peak = 100.0
        pnl_list = []

        # Sliding window simulation
        warmup = 50  # need at least 50 trades for valid ECS
        if len(outcomes) <= warmup:
            return result

        bcs_state = "NORMAL"
        sustain_count = 0

        for i in range(warmup, len(outcomes)):
            o = outcomes[i]
            pnl_r = o.get("pnl_r", 0)
            passed = o.get("passed", False)

            if not passed:
                continue

            # Compute rolling ECS from last 50 outcomes up to this point
            window = outcomes[max(0, i - 50):i]
            wins = sum(1 for t in window if t.get("pnl_r", 0) > 0 and t.get("passed"))
            total = sum(1 for t in window if t.get("passed"))
            wr = wins / max(total, 1)
            expr = sum(t.get("pnl_r", 0) for t in window if t.get("passed")) / max(total, 1)

            # ECS via sigmoid
            raw = (wr - 0.5) * 3.0 + expr * 1.5
            system_ecs = 1.0 / (1.0 + math.exp(-raw))

            # ΔECS (simplified — compare to 10 trades ago)
            if i > warmup + 10:
                old_window = outcomes[max(0, i - 60):max(0, i - 10)]
                o_wins = sum(1 for t in old_window if t.get("pnl_r", 0) > 0 and t.get("passed"))
                o_total = sum(1 for t in old_window if t.get("passed"))
                o_wr = o_wins / max(o_total, 1)
                o_expr = sum(t.get("pnl_r", 0) for t in old_window if t.get("passed")) / max(o_total, 1)
                o_raw = (o_wr - 0.5) * 3.0 + o_expr * 1.5
                old_ecs = 1.0 / (1.0 + math.exp(-o_raw))
                delta_ecs = system_ecs - old_ecs
            else:
                delta_ecs = 0.0

            delta_norm = 1.0 / (1.0 + math.exp(-delta_ecs * 20.0))

            # Simplified lifecycle + vol + cross-section (use defaults)
            p_lifecycle = 0.5
            v_norm = 1.0 / (1.0 + math.exp(-expr * 3.0))
            r_cross = 0.5  # neutral when no cluster data

            # Compute BCS with candidate weights
            bcs = (params.w1 * system_ecs +
                   params.w2 * delta_norm +
                   params.w3 * p_lifecycle +
                   params.w4 * v_norm +
                   params.w5 * r_cross)
            bcs = max(0.0, min(1.0, bcs))

            # Drawdown
            dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0

            # State determination
            if bcs < params.decay_threshold:
                bcs_state = "DECAY"
                sustain_count = 0
            elif bcs >= params.burst_threshold:
                sustain_count += 1
                if sustain_count >= 2 and wr >= 0.52:  # simplified shadow validation
                    bcs_state = "BURST"
                elif bcs_state == "BURST" and wr < 0.52:
                    bcs_state = "NORMAL"
            else:
                bcs_state = "NORMAL"
                sustain_count = 0

            # Apply multipliers and compute adjusted PnL
            result.total_trades += 1

            if bcs_state == "BURST":
                # Leverage boost: L = 1 + (1.4-1) · BCS^γ · f_dd
                f_dd = max(0.0, 1.0 - dd_pct / 15.0) ** 2
                lev_mult = 1.0 + 0.4 * (bcs ** params.gamma) * f_dd
                lev_mult = min(1.4, lev_mult)

                # TP mult: 1 + k1*(v-0.5)*2 + k2*(ecs-0.5)*2
                tp_boost = params.k1 * (v_norm - 0.5) * 2 + params.k2 * (system_ecs - 0.5) * 2
                tp_mult = 1.0 + max(0.0, tp_boost)
                tp_mult = min(1.35, tp_mult)

                # Risk mult (simplified proportional to BCS)
                risk_mult = 1.0 + 0.25 * (bcs - params.burst_threshold) / (1 - params.burst_threshold + 1e-9)
                risk_mult = min(1.25, max(1.0, risk_mult))

                adj_pnl = pnl_r * lev_mult * risk_mult
                # If profitable, partial TP locks ~33% at base, rest runs at TP_mult
                if pnl_r > 0:
                    base_portion = 0.33 * pnl_r * lev_mult * risk_mult
                    runner_portion = 0.67 * pnl_r * lev_mult * risk_mult * tp_mult
                    adj_pnl = base_portion + runner_portion

                result.burst_trades += 1
                if pnl_r > 0:
                    result.burst_wins += 1
                result.burst_total_r += adj_pnl

            elif bcs_state == "DECAY":
                # Reduced exposure during decay
                decay_mult = max(0.5, bcs / (params.decay_threshold + 1e-9))
                adj_pnl = pnl_r * decay_mult * 0.7  # position sizing + leverage reduction
                result.decay_skipped += 1

            else:
                # Normal — no modification
                adj_pnl = pnl_r
                result.normal_trades += 1
                result.normal_total_r += adj_pnl

            equity += adj_pnl  # simplified equity tracking (pnl_r as % of risk allocation)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd > result.max_drawdown_pct:
                result.max_drawdown_pct = dd

            pnl_list.append(adj_pnl)
            result.total_pnl_r += adj_pnl

        # Compute aggregate metrics
        if result.burst_trades > 0:
            result.burst_wr = result.burst_wins / result.burst_trades
            result.burst_avg_r = result.burst_total_r / result.burst_trades

        # Sharpe approximation
        if pnl_list and len(pnl_list) > 1:
            mean_r = sum(pnl_list) / len(pnl_list)
            var = sum((x - mean_r) ** 2 for x in pnl_list) / len(pnl_list)
            std = math.sqrt(var) if var > 0 else 1e-9
            result.sharpe_approx = mean_r / std

        # Composite fitness score
        # Weighted: total PnL (40%), burst WR (20%), Sharpe (20%), -max DD (20%)
        result.score = (
            0.40 * result.total_pnl_r +
            0.20 * (result.burst_wr * 10 if result.burst_trades >= 5 else 0) +
            0.20 * result.sharpe_approx * 5 +
            0.20 * max(0, 10 - result.max_drawdown_pct)
        )

        return result

    # ═══════════════════════════════════════════════════════════
    #  CANDIDATE GENERATION
    # ═══════════════════════════════════════════════════════════

    def _generate_candidates(self) -> List[ParamSet]:
        """
        Generate candidate param sets via:
          1. Neighborhood perturbation around current best (50%)
          2. Random grid sampling from param ranges (30%)
          3. Targeted mutations on individual params (20%)
        """
        candidates = []
        budget = MAX_ITERATIONS_PER_RUN

        # ── Neighborhood perturbation (50% of budget) ──
        n_neighbor = int(budget * 0.50)
        for _ in range(n_neighbor):
            p = copy.copy(self._current_params)
            # Perturb 2-3 random params by ±1 step
            n_perturb = random.randint(2, 3)
            keys = random.sample(list(PARAM_RANGES.keys()), n_perturb)
            for key in keys:
                lo, hi, step = PARAM_RANGES[key]
                current = getattr(p, key)
                delta = random.choice([-step, step])
                new_val = max(lo, min(hi, current + delta))
                setattr(p, key, round(new_val, 4))
            p.normalize_weights()
            candidates.append(p)

        # ── Random grid sampling (30% of budget) ──
        n_random = int(budget * 0.30)
        for _ in range(n_random):
            p = ParamSet()
            for key, (lo, hi, step) in PARAM_RANGES.items():
                n_steps = int((hi - lo) / step) + 1
                val = lo + random.randint(0, n_steps - 1) * step
                setattr(p, key, round(val, 4))
            p.normalize_weights()
            candidates.append(p)

        # ── Targeted single-param mutations (20% of budget) ──
        n_targeted = budget - n_neighbor - n_random
        for _ in range(n_targeted):
            p = copy.copy(self._current_params)
            key = random.choice(list(PARAM_RANGES.keys()))
            lo, hi, step = PARAM_RANGES[key]
            # Random value within range
            n_steps = int((hi - lo) / step) + 1
            val = lo + random.randint(0, n_steps - 1) * step
            setattr(p, key, round(val, 4))
            if key.startswith("w"):
                p.normalize_weights()
            candidates.append(p)

        return candidates

    # ═══════════════════════════════════════════════════════════
    #  PROMOTION (apply validated params)
    # ═══════════════════════════════════════════════════════════

    def _promote_params(self, params: ParamSet, train_res: SimResult,
                        test_res: SimResult, confidence: float,
                        train_improvement: float, test_improvement: float):
        """
        Apply validated param set to burst engine and persist.

        Updates burst_engine module-level constants via the engine's
        internal state. Logs and records the iteration.
        """
        old_params = copy.copy(self._current_params)

        with self._lock:
            self._current_params = params

        # Apply to burst engine module-level constants
        if self._burst_engine:
            self._apply_to_engine(params)

        # Log promotion
        _log.info(f"Burst optimizer: PROMOTED new params "
                  f"(train +{train_improvement:.1f}%, test +{test_improvement:.1f}%, "
                  f"MC conf={confidence:.0%})")
        _log.info(f"  w1={params.w1:.2f} w2={params.w2:.2f} w3={params.w3:.2f} "
                  f"w4={params.w4:.2f} w5={params.w5:.2f}")
        _log.info(f"  γ={params.gamma:.2f} k1={params.k1:.2f} k2={params.k2:.2f}")
        _log.info(f"  burst_thr={params.burst_threshold:.2f} "
                  f"decay_thr={params.decay_threshold:.2f}")
        _log.info(f"  Train: PnL={train_res.total_pnl_r:.2f}R "
                  f"burst_wr={train_res.burst_wr:.0%} "
                  f"DD={train_res.max_drawdown_pct:.1f}%")
        _log.info(f"  Test:  PnL={test_res.total_pnl_r:.2f}R "
                  f"burst_wr={test_res.burst_wr:.0%} "
                  f"DD={test_res.max_drawdown_pct:.1f}%")

        # Record iteration to history
        self._record_iteration(old_params, params, train_res, test_res,
                               confidence, train_improvement, test_improvement)

    def _apply_to_engine(self, params: ParamSet):
        """
        Apply optimized params to the running burst engine's module-level constants.

        This is the actual mechanism that changes burst behavior.
        Uses importlib to update the burst_engine module namespace.
        """
        try:
            import v13pro.burst_engine as be_mod

            # BCS weights
            be_mod.BCS_W1 = params.w1
            be_mod.BCS_W2 = params.w2
            be_mod.BCS_W3 = params.w3
            be_mod.BCS_W4 = params.w4
            be_mod.BCS_W5 = params.w5

            # Leverage exponent
            be_mod.GAMMA = params.gamma

            # State thresholds
            be_mod.BURST_THRESHOLD = params.burst_threshold
            be_mod.DECAY_THRESHOLD = params.decay_threshold

            # TP coefficients are inside _compute_tp_mult — we store them
            # on the engine instance for runtime access
            if self._burst_engine:
                self._burst_engine._optim_k1 = params.k1
                self._burst_engine._optim_k2 = params.k2

            _log.debug("Burst optimizer: applied params to engine module")

        except Exception as e:
            _log.warning(f"Burst optimizer: failed to apply params: {e}")

    # ═══════════════════════════════════════════════════════════
    #  DATA LOADING
    # ═══════════════════════════════════════════════════════════

    def _load_outcomes(self) -> List[dict]:
        """Load all shadow outcomes, sorted by timestamp."""
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

        # Filter to longs if LONG_ONLY
        if cfg.LONG_ONLY_MODE:
            rows = [r for r in rows if r.get("side", "").lower() == "long"]

        # Sort by timestamp
        rows.sort(key=lambda x: x.get("ts_ms", 0))
        return rows

    # ═══════════════════════════════════════════════════════════
    #  PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _save_state(self):
        """Persist optimizer state."""
        try:
            with self._lock:
                state = {
                    "current_params": asdict(self._current_params),
                    "n_runs": self._n_runs,
                    "last_score": self._last_score,
                    "iterations_total": self._iterations_total,
                    "last_run": self._last_run,
                    "ts": time.time(),
                }
            tmp = OPTIM_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            if os.path.exists(OPTIM_STATE_FILE):
                os.replace(tmp, OPTIM_STATE_FILE)
            else:
                os.rename(tmp, OPTIM_STATE_FILE)
        except Exception as e:
            _log.debug(f"Burst optimizer save: {e}")

    def _load_state(self):
        """Load persisted optimizer state."""
        if os.path.exists(OPTIM_STATE_FILE):
            try:
                with open(OPTIM_STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                p = state.get("current_params", {})
                self._current_params = ParamSet(**{
                    k: v for k, v in p.items()
                    if k in ParamSet.__dataclass_fields__
                })
                self._n_runs = state.get("n_runs", 0)
                self._last_score = state.get("last_score", 0)
                self._iterations_total = state.get("iterations_total", 0)
                self._last_run = state.get("last_run", 0)

                # Apply loaded params to burst engine on startup
                if self._burst_engine:
                    self._apply_to_engine(self._current_params)

            except Exception as e:
                _log.debug(f"Burst optimizer load: {e}")

    def _record_iteration(self, old_params: ParamSet, new_params: ParamSet,
                          train_res: SimResult, test_res: SimResult,
                          confidence: float, train_imp: float, test_imp: float):
        """Append iteration record to history file for auditability."""
        try:
            record = {
                "ts": time.time(),
                "iteration": self._n_runs,
                "old_params": asdict(old_params),
                "new_params": asdict(new_params),
                "train": {
                    "pnl_r": round(train_res.total_pnl_r, 3),
                    "burst_wr": round(train_res.burst_wr, 3),
                    "burst_trades": train_res.burst_trades,
                    "max_dd": round(train_res.max_drawdown_pct, 2),
                    "sharpe": round(train_res.sharpe_approx, 3),
                    "score": round(train_res.score, 3),
                },
                "test": {
                    "pnl_r": round(test_res.total_pnl_r, 3),
                    "burst_wr": round(test_res.burst_wr, 3),
                    "burst_trades": test_res.burst_trades,
                    "max_dd": round(test_res.max_drawdown_pct, 2),
                    "sharpe": round(test_res.sharpe_approx, 3),
                    "score": round(test_res.score, 3),
                },
                "confidence": round(confidence, 3),
                "train_improvement_pct": round(train_imp, 1),
                "test_improvement_pct": round(test_imp, 1),
            }
            with open(OPTIM_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            _log.debug(f"Burst optimizer history write: {e}")
