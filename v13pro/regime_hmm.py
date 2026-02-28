"""Iteration 15: Continuous Bayesian regime detection.

Replaces the 5-state discrete regime detector (HOT/WARM/NORMAL/COOL/COLD)
with a 2-state online Bayesian filter:

    Hidden states: FAVORABLE, UNFAVORABLE
    Emissions: Gaussian pnl_r per state
    Filtering: recursive Bayesian (forward algorithm)
    Output: P(favorable | data_so_far) ∈ [0, 1]

Why this beats the old regime detector:
    1. No COLD trap (continuous probabilities, no confirmation gates)
    2. No hard WR thresholds (Gaussian emissions capture full shape)
    3. Smooth transitions (Bayes update, not step functions)
    4. Naturally handles uncertainty (wide priors → conservative)

Integration: EdgeEstimator uses P(favorable) as a confidence modifier.
No separate regime_mult in the risk chain.
"""

import glob
import json
import math
import os
import time
import threading
from collections import deque

from v13pro import config as cfg
from v13pro import logger as log

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")

# ── HMM Parameters ────────────────────────────────────────
# Estimated from shadow data split into winning/losing regimes
# These are initialized from data on startup, then updated online

DEFAULT_MU_FAV = 0.25           # Expected R in favorable regime
DEFAULT_MU_UNFAV = -0.30        # Expected R in unfavorable regime
DEFAULT_SIGMA_FAV = 0.80        # Volatility in favorable
DEFAULT_SIGMA_UNFAV = 0.90      # Volatility in unfavorable
DEFAULT_P_STAY = 0.92           # Probability of staying in same regime
PRIOR_FAV = 0.50                # Uninformative prior

# For EdgeEstimator integration
MIN_CONFIDENCE_MULT = 0.40      # Floor: even in worst regime, allow 40%
MAX_CONFIDENCE_MULT = 1.25      # Ceiling: favorable regime boosts up to 125%


def _gaussian_pdf(x, mu, sigma):
    """Standard Gaussian PDF."""
    if sigma <= 0:
        return 1e-10
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))


class RegimeHMM:
    """Two-state online Bayesian regime filter."""

    def __init__(self):
        self._lock = threading.Lock()

        # HMM parameters (fitted from shadow data)
        self._mu_fav = DEFAULT_MU_FAV
        self._mu_unfav = DEFAULT_MU_UNFAV
        self._sigma_fav = DEFAULT_SIGMA_FAV
        self._sigma_unfav = DEFAULT_SIGMA_UNFAV
        self._p_stay = DEFAULT_P_STAY

        # State: P(favorable | observations so far)
        self._p_fav = PRIOR_FAV
        self._n_updates = 0

        # Recent observations for logging
        self._recent = deque(maxlen=50)

        # Fit parameters from shadow data
        self._fit_from_shadow()

    # ── Fitting from shadow data ──────────────────────────────

    def _fit_from_shadow(self):
        """Estimate HMM parameters from shadow outcomes."""
        outcomes = self._load_outcomes()
        if len(outcomes) < 20:
            log.info("  🔮 RegimeHMM: insufficient data, using defaults")
            return

        pnls = [r.get("pnl_r", 0.0) for r in outcomes]

        # Split into favorable/unfavorable by median
        med = sorted(pnls)[len(pnls) // 2]
        fav = [p for p in pnls if p >= med]
        unfav = [p for p in pnls if p < med]

        if fav:
            self._mu_fav = sum(fav) / len(fav)
            self._sigma_fav = max(0.40, (
                sum((p - self._mu_fav) ** 2 for p in fav) / len(fav)
            ) ** 0.5)
        if unfav:
            self._mu_unfav = sum(unfav) / len(unfav)
            # Enforce minimum sigma to prevent overconfident state assignment
            self._sigma_unfav = max(0.40, (
                sum((p - self._mu_unfav) ** 2 for p in unfav) / len(unfav)
            ) ** 0.5)

        # Regime persistence: use high P(stay) — real market regimes
        # last days/weeks, not individual trades.  Autocorrelation from
        # shadow data is unreliable (shadow outcomes aren't a single
        # live trading stream).  Default to 0.93 → average 14-trade regime.
        self._p_stay = 0.93

        # Forward-filter through RECENT data only (last 200) to warm up
        # Using all 1500 leads to extreme P(fav) dominated by historical tails
        self._p_fav = PRIOR_FAV
        warmup = pnls[-200:] if len(pnls) > 200 else pnls
        for pnl in warmup:
            self._bayesian_update(pnl)

        log.info(f"  🔮 RegimeHMM: fitted from {len(pnls)} outcomes")
        log.info(f"    μ_fav={self._mu_fav:+.3f} σ_fav={self._sigma_fav:.3f}")
        log.info(f"    μ_unf={self._mu_unfav:+.3f} σ_unf={self._sigma_unfav:.3f}")
        log.info(f"    P(stay)={self._p_stay:.3f}")
        log.info(f"    P(favorable)={self._p_fav:.3f} after warm-up")

    def _load_outcomes(self):
        """Load passed shadow outcomes chronologically."""
        outcomes = []
        pattern = os.path.join(SHADOW_DIR, "shadow_*.jsonl")
        for fpath in sorted(glob.glob(pattern)):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if r.get("event") != "shadow_outcome":
                            continue
                        if not r.get("passed"):
                            continue
                        outcomes.append(r)
            except Exception:
                continue
        return outcomes

    # ── Bayesian Update ───────────────────────────────────────

    def _bayesian_update(self, pnl_r: float):
        """Update P(favorable) given a new observation."""
        p_f = self._p_fav
        p_u = 1.0 - p_f

        # Transition: predict next state probabilities
        p_f_pred = p_f * self._p_stay + p_u * (1 - self._p_stay)
        p_u_pred = 1.0 - p_f_pred

        # Emission: P(observation | state)
        lik_f = _gaussian_pdf(pnl_r, self._mu_fav, self._sigma_fav)
        lik_u = _gaussian_pdf(pnl_r, self._mu_unfav, self._sigma_unfav)

        # Posterior: Bayes rule
        numerator = p_f_pred * lik_f
        denominator = p_f_pred * lik_f + p_u_pred * lik_u

        if denominator > 0:
            self._p_fav = numerator / denominator
        else:
            self._p_fav = 0.5  # numerical safety

        # Clamp to prevent extreme lock-in (always allow recovery)
        self._p_fav = max(0.10, min(0.90, self._p_fav))
        self._n_updates += 1

    # ── Public API ────────────────────────────────────────────

    def update(self, pnl_r: float):
        """Feed a new trade outcome (called from bot on position close)."""
        with self._lock:
            self._bayesian_update(pnl_r)
            self._recent.append(pnl_r)

    @property
    def p_favorable(self) -> float:
        """Current probability of being in favorable regime."""
        with self._lock:
            return self._p_fav

    @property
    def regime_label(self) -> str:
        """Human-readable regime label."""
        p = self.p_favorable
        if p > 0.75:
            return "FAVORABLE"
        elif p > 0.55:
            return "WARM"
        elif p > 0.35:
            return "UNCERTAIN"
        else:
            return "UNFAVORABLE"

    def confidence_multiplier(self) -> float:
        """
        Multiplier for EdgeEstimator confidence.

        Maps P(favorable) to [MIN_CONFIDENCE_MULT, MAX_CONFIDENCE_MULT]:
            P=0.0 → 0.40x (floor)
            P=0.5 → 1.00x (neutral)
            P=1.0 → 1.25x (boost)
        """
        p = self.p_favorable
        if p >= 0.5:
            # Linear from 1.0 to MAX
            mult = 1.0 + (p - 0.5) * 2.0 * (MAX_CONFIDENCE_MULT - 1.0)
        else:
            # Linear from MIN to 1.0
            mult = MIN_CONFIDENCE_MULT + p * 2.0 * (1.0 - MIN_CONFIDENCE_MULT)
        return round(max(MIN_CONFIDENCE_MULT, min(MAX_CONFIDENCE_MULT, mult)), 4)

    def summary(self) -> dict:
        with self._lock:
            return {
                "p_favorable": round(self._p_fav, 4),
                "label": self.regime_label,
                "confidence_mult": self.confidence_multiplier(),
                "n_updates": self._n_updates,
                "mu_fav": round(self._mu_fav, 4),
                "mu_unfav": round(self._mu_unfav, 4),
                "p_stay": round(self._p_stay, 4),
            }

    def log_status(self):
        s = self.summary()
        log.info(f"  🔮 RegimeHMM: P(fav)={s['p_favorable']:.3f} "
                 f"[{s['label']}] conf_mult={s['confidence_mult']:.3f} "
                 f"updates={s['n_updates']}")
