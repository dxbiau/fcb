"""Iteration 13: Data-driven edge estimation.

Replaces 16 heuristic multipliers with a single prediction:
    (combo, sentiment, features) → μ(R), σ²(R), quarter-Kelly

Uses shadow outcome data to build:
    1. Per-(combo, sent_bias) edge lookup table with Bayesian shrinkage
    2. Continuous feature adjustments (binned residuals)
    3. Live calibration via EWMA prediction error tracking

MI analysis (Iteration 12) ranked top features:
    sent_score(0.206) > combo_id(0.157) > hour_utc(0.123) >
    stop_pct/pair_id/of_spread_bps(~0.11) > session/conviction(~0.09)
"""

import glob
import json
import math
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

from v13pro import config as cfg
from v13pro import logger as log

# ── Constants ──────────────────────────────────────────────
SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
RELOAD_INTERVAL_S = 3600        # Reload shadow data every hour
SHRINKAGE_K = 20                # Bayesian shrinkage pseudocount
MIN_N_FOR_CELL = 5              # Minimum outcomes in a cell to trust it
MIN_N_FOR_COMBO = 3             # Minimum for combo-global fallback
EWMA_ALPHA = 0.05              # Live calibration speed
MAX_KELLY_F = 0.08             # Cap quarter-Kelly at 8%
CONFIDENCE_THRESHOLD = 5.0     # sqrt(N) divisor for confidence scaling
SENT_THRESHOLD = 0.15          # Score above/below = bull/bear


class EdgeEstimator:
    """Data-driven edge prediction from shadow outcomes.

    Replaces: quality_mult, regime_mult, conv_mult, edge_combo_mult,
    edge_market_mult, edge_sent_mult, edge_hot_mult, kl_risk_mult,
    of_risk_mult, shadow_live_mult, correlation_mult, directional_mult,
    cross_tf_mult, session_lc_mult, calibrator_mult, cross_sect_mult
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._edge_table = {}           # (combo, sent_bin) → {mu, sigma2, n, wr}
        self._combo_global = {}         # combo → {mu, sigma2, n}
        self._global_mu = 0.0
        self._global_sigma2 = 1.0
        self._feature_adj = {}          # feature_name → {bin → adj_mu}
        self._ewma_error = 0.0          # Live calibration bias correction
        self._last_load = 0.0
        self._n_outcomes = 0

        self._load_and_build()

    # ── Data Loading ──────────────────────────────────────────

    def _load_outcomes(self):
        """Load passed shadow outcomes from JSONL files."""
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

    @staticmethod
    def _classify_sent(r):
        """Classify sentiment from outcome record."""
        sent = r.get("sentiment", {})
        score = sent.get("score", 0.0) if isinstance(sent, dict) else 0.0
        if score > SENT_THRESHOLD:
            return "bull"
        elif score < -SENT_THRESHOLD:
            return "bear"
        return "neutral"

    @staticmethod
    def _classify_sent_score(score: float) -> str:
        """Classify sentiment from raw score."""
        if score > SENT_THRESHOLD:
            return "bull"
        elif score < -SENT_THRESHOLD:
            return "bear"
        return "neutral"

    def _load_and_build(self):
        """Full reload and rebuild of edge tables."""
        outcomes = self._load_outcomes()
        if not outcomes:
            log.warning("  ⚡ EdgeEstimator: no shadow outcomes found")
            self._last_load = time.time()
            return

        n_outcomes = len(outcomes)

        # ── Global stats ──
        pnls = [r.get("pnl_r", 0.0) for r in outcomes]
        global_mu = sum(pnls) / len(pnls) if pnls else 0.0
        global_sigma2 = max(0.01,
            sum((p - global_mu) ** 2 for p in pnls) / len(pnls))

        # ── Per-combo global (shrinkage target) ──
        combo_groups = defaultdict(list)
        for r in outcomes:
            combo = f"{r.get('strategy', '')}/{r.get('tf', '')}"
            combo_groups[combo].append(r.get("pnl_r", 0.0))

        combo_global = {}
        for combo, pnls_c in combo_groups.items():
            mu_c = sum(pnls_c) / len(pnls_c)
            sigma2_c = max(0.01,
                sum((p - mu_c) ** 2 for p in pnls_c) / len(pnls_c))
            combo_global[combo] = {
                "mu": mu_c, "sigma2": sigma2_c, "n": len(pnls_c)}

        # ── Per-(combo, sent_bias) edge table ──
        cell_groups = defaultdict(list)
        for r in outcomes:
            combo = f"{r.get('strategy', '')}/{r.get('tf', '')}"
            sent_bin = self._classify_sent(r)
            cell_groups[(combo, sent_bin)].append(r.get("pnl_r", 0.0))

        edge_table = {}
        for (combo, sent_bin), pnls_cell in cell_groups.items():
            n = len(pnls_cell)
            raw_mu = sum(pnls_cell) / n
            raw_sigma2 = max(0.01,
                sum((p - raw_mu) ** 2 for p in pnls_cell) / n)

            # Bayesian shrinkage toward combo global
            combo_g = combo_global.get(combo,
                {"mu": global_mu, "sigma2": global_sigma2})
            shrink = n / (n + SHRINKAGE_K)
            mu = shrink * raw_mu + (1 - shrink) * combo_g["mu"]
            # Use combo-level σ² (more stable with small N)
            sigma2 = combo_g["sigma2"]

            wins = sum(1 for p in pnls_cell if p > 0)
            wr = wins / n if n > 0 else 0

            edge_table[(combo, sent_bin)] = {
                "mu": mu, "sigma2": sigma2, "n": n,
                "wr": wr, "raw_mu": raw_mu,
            }

        # ── Feature adjustments (binned residuals) ──
        feature_adj = self._compute_feature_adjustments(outcomes, edge_table)

        with self._lock:
            self._edge_table = edge_table
            self._combo_global = combo_global
            self._feature_adj = feature_adj
            self._global_mu = global_mu
            self._global_sigma2 = global_sigma2
            self._n_outcomes = n_outcomes
            self._last_load = time.time()

        # ── Log summary ──
        n_cells = len(edge_table)
        pos_cells = sum(1 for v in edge_table.values() if v["mu"] > 0)
        neg_cells = n_cells - pos_cells
        log.info(f"  ⚡ EdgeEstimator: {self._n_outcomes} outcomes → "
                 f"{n_cells} cells ({pos_cells} positive, {neg_cells} negative)")

        sorted_cells = sorted(
            edge_table.items(), key=lambda x: x[1]["mu"], reverse=True)
        for (combo, sent), info in sorted_cells[:5]:
            kelly = (0.25 * info["mu"] / info["sigma2"]
                     if info["sigma2"] > 0 else 0)
            log.info(f"    ⚡ TOP: {combo}/{sent} "
                     f"μ={info['mu']:+.3f} σ²={info['sigma2']:.3f} "
                     f"N={info['n']} WR={info['wr']:.0%} ¼K={kelly:.4f}")
        for (combo, sent), info in sorted_cells[-3:]:
            kelly = (0.25 * info["mu"] / info["sigma2"]
                     if info["sigma2"] > 0 else 0)
            log.info(f"    ⚡ BOT: {combo}/{sent} "
                     f"μ={info['mu']:+.3f} σ²={info['sigma2']:.3f} "
                     f"N={info['n']} WR={info['wr']:.0%} ¼K={kelly:.4f}")

    def _compute_feature_adjustments(self, outcomes, edge_table):
        """Compute binned residual adjustments for continuous features."""
        feature_adj = {}

        # Helper: compute residual from cell mean
        def _residuals_with_feature(extractor):
            bins = defaultdict(list)
            for r in outcomes:
                combo = f"{r.get('strategy', '')}/{r.get('tf', '')}"
                sent_bin = self._classify_sent(r)
                cell = edge_table.get((combo, sent_bin))
                if not cell:
                    continue
                residual = r.get("pnl_r", 0.0) - cell["mu"]
                bin_label = extractor(r)
                if bin_label is not None:
                    bins[bin_label].append(residual)
            return {
                b: (sum(v) / len(v) if len(v) >= 20 else 0.0)
                for b, v in bins.items()
            }

        # Hour bins: 4 groups (0-5, 6-11, 12-17, 18-23)
        def _hour_bin(r):
            ts_ms = r.get("ts_ms", 0)
            if ts_ms > 0:
                hour = datetime.fromtimestamp(
                    ts_ms / 1000.0, tz=timezone.utc).hour
                return hour // 6
            return None
        feature_adj["hour_bin"] = _residuals_with_feature(_hour_bin)

        # Conviction bins: low (<40), mid (40-70), high (>70)
        def _conv_bin(r):
            conv = r.get("conviction", 50)
            if conv < 40:
                return "low"
            elif conv > 70:
                return "high"
            return "mid"
        feature_adj["conviction"] = _residuals_with_feature(_conv_bin)

        # OF spread bins: tight (<30bps), normal (30-100), wide (>100)
        def _spread_bin(r):
            of = r.get("orderflow", {})
            spread = of.get("spread_bps", 50) if isinstance(of, dict) else 50
            if spread < 30:
                return "tight"
            elif spread > 100:
                return "wide"
            return "normal"
        feature_adj["of_spread"] = _residuals_with_feature(_spread_bin)

        return feature_adj

    # ── Prediction ────────────────────────────────────────────

    def estimate(self, combo: str, sent_score: float,
                 features: dict = None) -> dict:
        """
        Predict expected edge for a signal.

        Args:
            combo: "STRATEGY/tf" (e.g. "BB_BREAK/15m")
            sent_score: sentiment score (-1 to +1)
            features: optional {conviction, hour_utc, of_spread_bps, ...}

        Returns:
            {mu, sigma2, kelly_f, n, confidence, blocked, reason}
        """
        self._maybe_reload()

        with self._lock:
            sent_bin = self._classify_sent_score(sent_score)

            # ── Lookup chain: cell → combo_global → global ──
            cell = self._edge_table.get((combo, sent_bin))
            if cell and cell["n"] >= MIN_N_FOR_CELL:
                mu = cell["mu"]
                sigma2 = cell["sigma2"]
                n = cell["n"]
                reason = f"{combo}/{sent_bin} N={n}"
            else:
                # Try alternate sentiment bins for this combo
                alt_cell = None
                for alt_bin in ["bull", "neutral", "bear"]:
                    ac = self._edge_table.get((combo, alt_bin))
                    if ac and ac["n"] >= MIN_N_FOR_CELL:
                        if alt_cell is None or ac["n"] > alt_cell["n"]:
                            alt_cell = ac

                combo_g = self._combo_global.get(combo)
                if alt_cell:
                    # Prefer alt sentiment cell (same combo, different sent bin)
                    mu = alt_cell["mu"]
                    sigma2 = alt_cell["sigma2"]
                    n = alt_cell["n"]
                    reason = f"{combo}/alt_sent N={n}"
                elif combo_g and combo_g["n"] >= MIN_N_FOR_COMBO:
                    mu = combo_g["mu"]
                    sigma2 = combo_g["sigma2"]
                    n = combo_g["n"]
                    reason = f"{combo}/global N={n}"
                else:
                    # Total fallback: global average
                    mu = self._global_mu
                    sigma2 = self._global_sigma2
                    n = self._n_outcomes
                    reason = f"global_fallback N={n}"

            # ── Feature adjustments (additive residuals) ──
            adj = 0.0
            if features:
                hour = features.get("hour_utc", 12)
                hour_bin = hour // 6
                adj += self._feature_adj.get(
                    "hour_bin", {}).get(hour_bin, 0.0)

                conv = features.get("conviction", 50)
                if conv < 40:
                    adj += self._feature_adj.get(
                        "conviction", {}).get("low", 0.0)
                elif conv > 70:
                    adj += self._feature_adj.get(
                        "conviction", {}).get("high", 0.0)
                else:
                    adj += self._feature_adj.get(
                        "conviction", {}).get("mid", 0.0)

                spread = features.get("of_spread_bps", 50)
                if spread < 30:
                    adj += self._feature_adj.get(
                        "of_spread", {}).get("tight", 0.0)
                elif spread > 100:
                    adj += self._feature_adj.get(
                        "of_spread", {}).get("wide", 0.0)
                else:
                    adj += self._feature_adj.get(
                        "of_spread", {}).get("normal", 0.0)

            # Apply adjustments + EWMA calibration
            mu_adj = mu + adj + self._ewma_error

            # ── Standard error of μ estimate ──
            se = math.sqrt(sigma2 / n) if n > 0 and sigma2 > 0 else 0.5

            # ── Quarter-Kelly ──
            # Block only when μ is statistically significantly negative
            # (more than 1 SE below zero).  Near-zero μ gets minimum
            # kelly_f so the trade proceeds at minimum sizing rather
            # than being silently dropped.
            MIN_KELLY_FLOOR = 0.015  # floor for near-zero edge (must survive HMM + KellySizer)
            if sigma2 > 0 and mu_adj > 0:
                kelly_f = 0.25 * mu_adj / sigma2
            elif mu_adj > -se:
                # Near-zero: not statistically negative — use floor
                kelly_f = MIN_KELLY_FLOOR
            else:
                kelly_f = 0.0

            kelly_f = max(0.0, min(kelly_f, MAX_KELLY_F))

            # Confidence: sqrt(N) / threshold, capped at 1.0
            confidence = (min(1.0, math.sqrt(n) / CONFIDENCE_THRESHOLD)
                          if n > 0 else 0.0)

            # Block only when significantly negative (beyond noise)
            blocked = kelly_f <= 0.0
            if blocked:
                reason += f" (negative edge μ={mu_adj:+.3f} SE={se:.3f})"
            elif mu_adj <= 0:
                reason += f" (near-zero μ={mu_adj:+.3f}, floor)"

            return {
                "mu": round(mu_adj, 4),
                "sigma2": round(sigma2, 4),
                "kelly_f": round(kelly_f, 6),
                "n": n,
                "confidence": round(confidence, 3),
                "blocked": blocked,
                "reason": reason,
            }

    # ── Live Calibration ──────────────────────────────────────

    def record_outcome(self, combo: str, sent_score: float,
                       predicted_mu: float, actual_pnl_r: float):
        """Update EWMA calibration after a real trade resolves."""
        error = actual_pnl_r - predicted_mu
        with self._lock:
            self._ewma_error = (EWMA_ALPHA * error
                                + (1 - EWMA_ALPHA) * self._ewma_error)
        log.debug(f"  ⚡ EdgeEstimator calibration: pred={predicted_mu:+.3f} "
                  f"actual={actual_pnl_r:+.3f} "
                  f"ewma_err={self._ewma_error:+.4f}")

    # ── Maintenance ───────────────────────────────────────────

    def _maybe_reload(self):
        """Reload from JSONL if stale."""
        if time.time() - self._last_load > RELOAD_INTERVAL_S:
            self._load_and_build()

    def summary(self) -> dict:
        """Status summary for logging."""
        with self._lock:
            n_cells = len(self._edge_table)
            pos = sum(1 for v in self._edge_table.values() if v["mu"] > 0)
            return {
                "outcomes": self._n_outcomes,
                "cells": n_cells,
                "positive": pos,
                "negative": n_cells - pos,
                "global_mu": round(self._global_mu, 4),
                "ewma_error": round(self._ewma_error, 4),
            }

    def log_status(self):
        """Log current status."""
        s = self.summary()
        log.info(f"  ⚡ EdgeEstimator: {s['outcomes']} outcomes, "
                 f"{s['cells']} cells ({s['positive']}+/{s['negative']}-), "
                 f"global μ={s['global_mu']:+.4f}, "
                 f"EWMA err={s['ewma_error']:+.4f}")
