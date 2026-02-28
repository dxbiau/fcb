"""Iteration 14: Learned exit thresholds per combo × sentiment.

Uses shadow checkpoint data to compute optimal trail parameters
for each (strategy/tf, sentiment_bias) combination.

Guardian already supports per-trade override of trail_activation_r
and trail_distance_r via exit_params dict.  ExitOracle provides
the data-driven values.

Key analysis per cell:
  1. Peak-R distribution → trail activation level
  2. Give-back analysis (peak_r - pnl_r) → trail distance
  3. Checkpoint velocity → early exit detection
"""

import glob
import json
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

from v13pro import config as cfg
from v13pro import logger as log

# ── Constants ──────────────────────────────────────────────
SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
RELOAD_INTERVAL_S = 3600
SENT_THRESHOLD = 0.15
MIN_N_FOR_PARAMS = 8            # Need ≥8 outcomes to compute exit params
DEFAULT_ACTIVATION_R = 1.5      # cfg.TRAIL_ACTIVATION_R
DEFAULT_DISTANCE_R = 0.50       # cfg.TRAIL_DISTANCE_R
DEFAULT_TP_CAP_R = 2.75         # cfg.TP_R

# Trail distance bounds
MIN_TRAIL_DIST = 0.20
MAX_TRAIL_DIST = 1.00
# Trail activation bounds
MIN_TRAIL_ACT = 0.50
MAX_TRAIL_ACT = 3.00


def _percentile(data, pct):
    """Compute percentile without numpy."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = pct / 100.0 * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _median(data):
    return _percentile(data, 50)


class ExitOracle:
    """Data-driven exit parameter oracle.

    For each (combo, sent_bias), computes:
        trail_activation_r — when to start trailing
        trail_distance_r   — how tight to trail
        tp_cap_r           — suggested TP ceiling
        early_exit_1m_r    — 1-minute fade threshold for rejection exits
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._exit_table = {}       # (combo, sent_bin) → {activation, distance, tp_cap}
        self._combo_global = {}     # combo → {activation, distance, tp_cap}
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
        if score > SENT_THRESHOLD:
            return "bull"
        elif score < -SENT_THRESHOLD:
            return "bear"
        return "neutral"

    def _compute_exit_params(self, outcomes):
        """Compute exit params for a group of outcomes."""
        if len(outcomes) < MIN_N_FOR_PARAMS:
            return None

        # ── Peak-R analysis ──
        peak_rs = [r.get("peak_r", 0) for r in outcomes]
        pnl_rs = [r.get("pnl_r", 0) for r in outcomes]
        med_peak = _median(peak_rs)
        p75_peak = _percentile(peak_rs, 75)
        p90_peak = _percentile(peak_rs, 90)

        # ── Give-back analysis (how much do winning trades give back?) ──
        winners = [r for r in outcomes if r.get("pnl_r", 0) > 0]
        if winners:
            give_backs = [
                r.get("peak_r", 0) - r.get("pnl_r", 0) for r in winners
            ]
            med_give_back = _median(give_backs)
            p25_give_back = _percentile(give_backs, 25)
        else:
            med_give_back = DEFAULT_DISTANCE_R
            p25_give_back = DEFAULT_DISTANCE_R

        # ── Trail activation: based on where peak_r typically reaches ──
        # Activate trail when price reaches 50% of typical peak
        # (catches the mid-run while letting it develop)
        if med_peak > 2.0:
            activation = min(MAX_TRAIL_ACT, med_peak * 0.50)
        elif med_peak > 1.0:
            activation = min(MAX_TRAIL_ACT, med_peak * 0.60)
        elif med_peak > 0.5:
            activation = max(MIN_TRAIL_ACT, med_peak * 0.70)
        else:
            activation = MIN_TRAIL_ACT
        activation = max(MIN_TRAIL_ACT, min(MAX_TRAIL_ACT, activation))

        # ── Trail distance: based on give-back behavior ──
        # Use 75th percentile of give-back (generous enough to not get
        # stopped out prematurely, but tight enough to capture most profit)
        if winners:
            # Tighter of: median give-back or p25 give-back * 1.5
            distance = min(med_give_back, p25_give_back * 1.5)
        else:
            distance = DEFAULT_DISTANCE_R
        distance = max(MIN_TRAIL_DIST, min(MAX_TRAIL_DIST, distance))

        # ── TP cap: based on peak_r tail ──
        # Use p75 as target (captures most of the distribution)
        tp_cap = max(1.5, min(6.0, p75_peak))

        # ── Checkpoint velocity: detect fast-fading setups ──
        # If 1m checkpoint typically already losing → setup fades fast
        chk_1m_rs = []
        for r in outcomes:
            chks = r.get("checkpoints", [])
            if chks and isinstance(chks, list):
                for c in chks:
                    if c.get("minutes") == 1:
                        chk_1m_rs.append(c.get("move_r", 0))
                        break

        early_fade_r = None
        if len(chk_1m_rs) >= MIN_N_FOR_PARAMS:
            med_1m = _median(chk_1m_rs)
            # If median 1m move is negative → this combo fades fast
            if med_1m < -0.10:
                # Early exit threshold: if 1m already at -0.3R, bail
                early_fade_r = -0.30

        return {
            "activation": round(activation, 2),
            "distance": round(distance, 2),
            "tp_cap": round(tp_cap, 2),
            "early_fade_r": early_fade_r,
            "n": len(outcomes),
            "med_peak": round(med_peak, 3),
            "p75_peak": round(p75_peak, 3),
            "med_give_back": round(med_give_back, 3),
        }

    def _load_and_build(self):
        """Full reload and rebuild."""
        outcomes = self._load_outcomes()
        if not outcomes:
            log.warning("  🎯 ExitOracle: no shadow outcomes found")
            self._last_load = time.time()
            return

        self._n_outcomes = len(outcomes)

        # ── Per-combo global exit params ──
        combo_groups = defaultdict(list)
        for r in outcomes:
            combo = f"{r.get('strategy', '')}/{r.get('tf', '')}"
            combo_groups[combo].append(r)

        combo_global = {}
        for combo, outs in combo_groups.items():
            params = self._compute_exit_params(outs)
            if params:
                combo_global[combo] = params

        # ── Per-(combo, sent_bias) exit table ──
        cell_groups = defaultdict(list)
        for r in outcomes:
            combo = f"{r.get('strategy', '')}/{r.get('tf', '')}"
            sent_bin = self._classify_sent(r)
            cell_groups[(combo, sent_bin)].append(r)

        exit_table = {}
        for (combo, sent_bin), outs in cell_groups.items():
            params = self._compute_exit_params(outs)
            if params:
                exit_table[(combo, sent_bin)] = params

        with self._lock:
            self._exit_table = exit_table
            self._combo_global = combo_global

        self._last_load = time.time()

        # ── Log summary ──
        n_cells = len(exit_table)
        n_combos = len(combo_global)
        log.info(f"  🎯 ExitOracle: {self._n_outcomes} outcomes → "
                 f"{n_combos} combos, {n_cells} cells")

        # Log top combos by peak potential
        sorted_combos = sorted(
            combo_global.items(),
            key=lambda x: x[1]["med_peak"], reverse=True)
        for combo, p in sorted_combos[:5]:
            log.info(f"    🎯 {combo}: act={p['activation']:.2f}R "
                     f"dist={p['distance']:.2f}R "
                     f"tp_cap={p['tp_cap']:.2f}R "
                     f"peak_med={p['med_peak']:.2f} "
                     f"give_back={p['med_give_back']:.2f} "
                     f"N={p['n']}")

    # ── Query ─────────────────────────────────────────────────

    def get_exit_params(self, combo: str, sent_score: float = 0.0
                        ) -> dict:
        """
        Get optimal exit parameters for a position.

        Args:
            combo: "STRATEGY/tf"
            sent_score: sentiment score at entry

        Returns:
            {
                "trail_activation_r": float,
                "trail_distance_r": float,
                "tp_r": float,
                "early_fade_r": float or None,
                "source": str,
            }
        """
        self._maybe_reload()
        sent_bin = self._classify_sent_score(sent_score)

        with self._lock:
            # Cell lookup
            cell = self._exit_table.get((combo, sent_bin))
            if cell:
                return {
                    "trail_activation_r": cell["activation"],
                    "trail_distance_r": cell["distance"],
                    "tp_r": cell["tp_cap"],
                    "early_fade_r": cell.get("early_fade_r"),
                    "source": f"{combo}/{sent_bin} N={cell['n']}",
                }

            # Combo-global fallback
            combo_g = self._combo_global.get(combo)
            if combo_g:
                return {
                    "trail_activation_r": combo_g["activation"],
                    "trail_distance_r": combo_g["distance"],
                    "tp_r": combo_g["tp_cap"],
                    "early_fade_r": combo_g.get("early_fade_r"),
                    "source": f"{combo}/global N={combo_g['n']}",
                }

            # Static fallback
            return {
                "trail_activation_r": DEFAULT_ACTIVATION_R,
                "trail_distance_r": DEFAULT_DISTANCE_R,
                "tp_r": DEFAULT_TP_CAP_R,
                "early_fade_r": None,
                "source": "static_default",
            }

    # ── Maintenance ───────────────────────────────────────────

    def _maybe_reload(self):
        if time.time() - self._last_load > RELOAD_INTERVAL_S:
            self._load_and_build()

    def summary(self) -> dict:
        with self._lock:
            return {
                "outcomes": self._n_outcomes,
                "combos": len(self._combo_global),
                "cells": len(self._exit_table),
            }

    def log_status(self):
        s = self.summary()
        log.info(f"  🎯 ExitOracle: {s['outcomes']} outcomes, "
                 f"{s['combos']} combos, {s['cells']} cells")
