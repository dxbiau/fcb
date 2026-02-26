"""
v13pro/regime.py -- Self-Calibrating Regime Detector & Exposure Modulator

PURPOSE:
  Detects whether the system is in a HOT, WARM, NORMAL, COOL, or COLD
  regime based on rolling shadow outcomes. Outputs a continuous exposure
  multiplier (0.40x to 1.40x) that modulates position sizing.

WHY THIS IS NEEDED:
  Shadow data shows WR oscillating wildly across 200-trade windows:
    Best:  78.5% WR, +0.701 ExpR
    Worst: 14.6% WR, -0.648 ExpR
    Last window: 14.6% — system is CURRENTLY cold
  Without regime detection, the bot trades at full risk during cold
  streaks, amplifying drawdowns unnecessarily.

DESIGN PRINCIPLES:
  1. MODULAR — standalone module, no modification of existing logic
  2. REVERSIBLE — remove import + 1 multiply to disable completely
  3. PROBABILISTIC — Bayesian confidence, not binary switches
  4. ANTI-OVERREACTION — minimum confirming trades before regime shift
  5. MULTI-WINDOW — short (20 trades), medium (50), long (150) blended
  6. SESSION-AWARE — each session adapts independently from RECENT data only
     (no historical bias — all sessions start neutral at 1.0x)
  7. SMOOTH — EWMA prevents whipsawing between regimes

DATA SOURCES:
  - Shadow outcomes (ALL signals, not just live trades)
  - Loaded from JSONL on init, updated incrementally via record_outcome()

NON-DESTRUCTIVE:
  - Does NOT modify signals, strategies, or exit logic
  - Only outputs a float multiplier for position sizing
  - Falls back to 1.0x (neutral) when insufficient data
  - Never blocks trades — only scales risk up/down
"""

import json
import glob
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
STATE_FILE = os.path.join(cfg.BASE_DIR, "regime_state.json")

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — all derived from shadow data analysis
# ═══════════════════════════════════════════════════════════════

# Rolling windows (number of trades)
# Multiple windows prevent overreaction to noise while catching trends
WINDOW_SHORT = 20       # fast-reacting (noise-prone, low weight)
WINDOW_MEDIUM = 50      # balanced signal (primary)
WINDOW_LONG = 150       # trend confirmation (high inertia)

# Window weights for blended regime score
# Medium window gets most weight — best noise/signal tradeoff
WINDOW_WEIGHTS = {
    "short": 0.20,
    "medium": 0.50,
    "long": 0.30,
}

# Regime thresholds — derived from shadow temporal WR analysis:
#   P25 of 200-trade windows: ~45% WR  (COLD territory)
#   P50 of 200-trade windows: ~52% WR  (NORMAL)
#   P75 of 200-trade windows: ~72% WR  (HOT territory)
# ExpR thresholds similarly calibrated
REGIMES = {
    "HOT":    {"wr_min": 0.65, "expr_min": +0.30, "mult": 1.30},
    "WARM":   {"wr_min": 0.55, "expr_min": +0.10, "mult": 1.10},
    "NORMAL": {"wr_min": 0.45, "expr_min": -0.05, "mult": 1.00},
    "COOL":   {"wr_min": 0.35, "expr_min": -0.20, "mult": 0.70},
    "COLD":   {"wr_min": 0.00, "expr_min": -9.00, "mult": 0.45},
}

# Minimum trades before regime assessment (prevents noise-driven regime)
MIN_TRADES_FOR_REGIME = 15

# Anti-overreaction: minimum confirming trades before regime transition
# Prevents one bad/good trade from flipping regime state
CONFIRM_TRADES = 3

# Regime refresh interval (seconds) — how often to recalc from JSONL
REFRESH_INTERVAL = 600  # 10 minutes

# Session-level analysis enabled
SESSION_REGIME_ENABLED = True

# Session exposure multipliers — ALL start neutral (1.0x)
# NO historical bias. Crypto doesn't repeat history reliably.
# Big moves happen in any session — let rolling data decide.
SESSION_BASE_MULT = {
    "london": 1.0,
    "asia": 1.0,
    "ny": 1.0,
}

# Session-specific rolling window — shorter than global so sessions
# react to CURRENT conditions, not months-old patterns
SESSION_WINDOW = 30  # last 30 trades per session

# EWMA alpha for smoothing regime transitions
EWMA_ALPHA = 0.25        # conservative for global regime shifts
SESSION_EWMA_ALPHA = 0.35  # faster for session adaptation


class RegimeDetector:
    """
    Self-calibrating regime detector with exposure modulation.

    Usage:
        regime = RegimeDetector()
        mult = regime.exposure_multiplier()       # 0.4x to 1.4x
        mult = regime.exposure_multiplier("ny")   # session-specific

        # On new shadow outcome:
        regime.record_outcome(pnl_r, session)

    The multiplier is applied to position sizing in bot.py:
        effective_risk *= regime.exposure_multiplier(session)
    """

    def __init__(self):
        self._lock = threading.RLock()
        # Rolling outcome buffers (deques auto-trim to max length)
        self._global_buffer: deque = deque(maxlen=WINDOW_LONG * 2)
        self._session_buffers: Dict[str, deque] = {
            "asia": deque(maxlen=WINDOW_LONG),
            "london": deque(maxlen=WINDOW_LONG),
            "ny": deque(maxlen=WINDOW_LONG),
        }
        # Current regime state
        self._regime = "NORMAL"
        self._regime_mult = 1.0
        self._session_mults: Dict[str, float] = dict(SESSION_BASE_MULT)

        # Smoothed values
        self._smoothed_global_mult = 1.0
        self._smoothed_session_mults: Dict[str, float] = dict(SESSION_BASE_MULT)

        # Regime transition tracking
        self._pending_regime = "NORMAL"
        self._pending_confirm_count = 0

        # Stats
        self._last_refresh = 0.0
        self._n_outcomes = 0
        self._n_refreshes = 0

        # Load persisted state
        self._load_state()

        # Initial load from shadow files
        self._load_from_shadow()

    # ═══════════════════════════════════════════════════════════
    #  INCREMENTAL UPDATES (called by shadow._finalize)
    # ═══════════════════════════════════════════════════════════

    def record_outcome(self, pnl_r: float, session: str = "",
                       strategy: str = "", tf: str = "",
                       symbol: str = ""):
        """
        Record a new outcome for regime tracking.

        Called incrementally as shadow trades complete —
        no need to re-parse all JSONL files.
        """
        with self._lock:
            outcome = {
                "pnl_r": pnl_r,
                "win": pnl_r > 0,
                "session": session,
                "strategy": strategy,
                "tf": tf,
                "symbol": symbol,
                "ts": time.time(),
            }
            self._global_buffer.append(outcome)
            self._n_outcomes += 1

            # Session buffer
            sess_key = session.lower() if session else ""
            if sess_key in self._session_buffers:
                self._session_buffers[sess_key].append(outcome)

            # Recompute regime
            self._recompute()

    # ═══════════════════════════════════════════════════════════
    #  BULK LOAD FROM SHADOW FILES
    # ═══════════════════════════════════════════════════════════

    def _load_from_shadow(self):
        """Load historical outcomes from shadow JSONL files."""
        try:
            rows = self._read_shadow_outcomes()
            if not rows:
                return

            # Only use longs (LONG_ONLY_MODE)
            longs = [r for r in rows if r.get("side", "").lower() == "long"]

            with self._lock:
                # Load into buffers (most recent first for deque trimming)
                for o in longs:
                    outcome = {
                        "pnl_r": o.get("pnl_r", 0),
                        "win": o.get("pnl_r", 0) > 0,
                        "session": o.get("session", ""),
                        "strategy": o.get("strategy", ""),
                        "tf": o.get("tf", ""),
                        "symbol": o.get("symbol", ""),
                        "ts": o.get("ts_ms", 0) / 1000 if o.get("ts_ms") else 0,
                    }
                    self._global_buffer.append(outcome)
                    sess = outcome["session"].lower()
                    if sess in self._session_buffers:
                        self._session_buffers[sess].append(outcome)

                self._n_outcomes = len(self._global_buffer)
                self._last_refresh = time.time()
                # On full load, apply regime directly (skip anti-overreaction)
                self._recompute(force=True)
                self._save_state()

            log.info(f"Regime: loaded {len(longs)} long outcomes, "
                     f"regime={self._regime} mult={self._regime_mult:.2f}x")

        except Exception as e:
            _log.warning(f"Regime load error: {e}")

    def _read_shadow_outcomes(self) -> List[dict]:
        """Read shadow outcome records from JSONL files."""
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

    def _recompute(self, force: bool = False):
        """Recompute regime state from buffered outcomes.
        
        Args:
            force: If True, skip anti-overreaction and apply regime directly.
                   Used on initial load when we have hundreds of data points.
        """
        buf = list(self._global_buffer)
        if len(buf) < MIN_TRADES_FOR_REGIME:
            return

        # ── Multi-window blended score ──
        # Each window produces a WR and ExpR. We blend them
        # using weights to get a single regime assessment.
        windows = {
            "short": buf[-WINDOW_SHORT:] if len(buf) >= WINDOW_SHORT else buf,
            "medium": buf[-WINDOW_MEDIUM:] if len(buf) >= WINDOW_MEDIUM else buf,
            "long": buf[-WINDOW_LONG:] if len(buf) >= WINDOW_LONG else buf,
        }

        blended_wr = 0.0
        blended_expr = 0.0
        total_weight = 0.0

        for window_name, data in windows.items():
            if len(data) < 5:
                continue
            wr = sum(1 for o in data if o["win"]) / len(data)
            expr = sum(o["pnl_r"] for o in data) / len(data)
            weight = WINDOW_WEIGHTS[window_name]

            # Confidence scaling: shorter windows with few trades get
            # reduced weight (Bayesian shrinkage toward prior)
            conf = min(1.0, len(data) / WINDOW_MEDIUM)
            effective_weight = weight * conf

            blended_wr += wr * effective_weight
            blended_expr += expr * effective_weight
            total_weight += effective_weight

        if total_weight > 0:
            blended_wr /= total_weight
            blended_expr /= total_weight

        # ── Classify regime ──
        new_regime = "NORMAL"
        for regime_name in ["HOT", "WARM", "NORMAL", "COOL", "COLD"]:
            thresholds = REGIMES[regime_name]
            if (blended_wr >= thresholds["wr_min"] and
                    blended_expr >= thresholds["expr_min"]):
                new_regime = regime_name
                break

        # ── Anti-overreaction: require confirmation ──
        if force:
            # On bulk load, apply directly with no confirmation needed
            if new_regime != self._regime:
                old = self._regime
                self._regime = new_regime
                raw_mult = REGIMES[new_regime]["mult"]
                # On force, set directly instead of EWMA from stale value
                self._smoothed_global_mult = raw_mult
                self._regime_mult = raw_mult
                self._pending_confirm_count = 0
                log.info(f"  Regime (force): {old} → {new_regime} "
                         f"(WR={blended_wr:.1%} ExpR={blended_expr:+.3f} "
                         f"mult={self._regime_mult:.2f}x)")
            else:
                raw_mult = REGIMES[new_regime]["mult"]
                self._smoothed_global_mult = raw_mult
                self._regime_mult = raw_mult
        elif new_regime != self._regime:
            if new_regime == self._pending_regime:
                self._pending_confirm_count += 1
            else:
                self._pending_regime = new_regime
                self._pending_confirm_count = 1

            if self._pending_confirm_count >= CONFIRM_TRADES:
                old = self._regime
                self._regime = new_regime
                self._pending_confirm_count = 0
                raw_mult = REGIMES[new_regime]["mult"]
                self._smoothed_global_mult = self._ewma(
                    self._smoothed_global_mult, raw_mult)
                self._regime_mult = self._smoothed_global_mult
                log.info(f"  Regime shift: {old} → {new_regime} "
                         f"(WR={blended_wr:.1%} ExpR={blended_expr:+.3f} "
                         f"mult={self._regime_mult:.2f}x)")
        else:
            # Same regime — still smooth the multiplier toward target
            raw_mult = REGIMES[new_regime]["mult"]
            self._smoothed_global_mult = self._ewma(
                self._smoothed_global_mult, raw_mult)
            self._regime_mult = self._smoothed_global_mult

        # ── Session-level regime ──
        if SESSION_REGIME_ENABLED:
            self._recompute_sessions()

    def _recompute_sessions(self):
        """Compute per-session exposure multipliers from recent rolling data.

        No historical bias — every session starts at 1.0x and adapts
        purely from the last SESSION_WINDOW trades in that session.
        Uses faster EWMA so it reacts to current conditions.
        """
        for sess_name, buf in self._session_buffers.items():
            data = list(buf)
            if len(data) < MIN_TRADES_FOR_REGIME:
                # Insufficient data — stay neutral (1.0x)
                self._session_mults[sess_name] = 1.0
                self._smoothed_session_mults[sess_name] = 1.0
                continue

            # Use short recent window — react to CURRENT conditions
            window = data[-SESSION_WINDOW:] if len(data) >= SESSION_WINDOW else data
            wr = sum(1 for o in window if o["win"]) / len(window)
            expr = sum(o["pnl_r"] for o in window) / len(window)

            # Scale multiplier proportional to recent edge
            # Baseline: WR=50%, ExpR=0 → 1.0x
            # Every +5% WR above 50% → +0.08x
            # Every -5% WR below 50% → -0.08x
            wr_edge = (wr - 0.50) / 0.05 * 0.08
            expr_edge = expr * 0.5  # ExpR of +0.2 → +0.1x boost
            raw_mult = max(0.60, min(1.40, 1.0 + wr_edge + expr_edge))

            # Smooth toward new value with faster session EWMA
            old = self._smoothed_session_mults.get(sess_name, 1.0)
            smoothed = self._session_ewma(old, raw_mult)
            self._smoothed_session_mults[sess_name] = smoothed
            self._session_mults[sess_name] = smoothed

    @staticmethod
    def _ewma(old: float, new: float) -> float:
        """Exponentially weighted moving average (global regime)."""
        return old * (1 - EWMA_ALPHA) + new * EWMA_ALPHA

    @staticmethod
    def _session_ewma(old: float, new: float) -> float:
        """Faster EWMA for session multipliers — react to current conditions."""
        return old * (1 - SESSION_EWMA_ALPHA) + new * SESSION_EWMA_ALPHA

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC API — call these from bot.py
    # ═══════════════════════════════════════════════════════════

    def exposure_multiplier(self, session: str = "") -> float:
        """
        Get the current exposure multiplier.

        Combines global regime mult with session-specific mult.
        Result range: ~0.20x to ~1.80x (product of two multipliers).

        Args:
            session: current session name (asia/london/ny). If empty,
                     uses global regime mult only.

        Returns:
            float multiplier for position sizing.
        """
        with self._lock:
            global_mult = self._regime_mult

            if session and SESSION_REGIME_ENABLED:
                sess_mult = self._session_mults.get(
                    session.lower(), 1.0)
                # Blend: 60% global regime, 40% session-specific
                # This prevents session mult from completely overriding
                # a global COLD regime
                combined = global_mult * 0.60 + sess_mult * 0.40
            else:
                combined = global_mult

            # Clamp to safe range — never more than 1.40x, never less than 0.40x
            return max(0.40, min(1.40, combined))

    @property
    def regime(self) -> str:
        """Current regime classification."""
        with self._lock:
            return self._regime

    @property
    def regime_mult(self) -> float:
        """Current raw global regime multiplier."""
        with self._lock:
            return round(self._regime_mult, 3)

    def session_mult(self, session: str) -> float:
        """Session-specific multiplier."""
        with self._lock:
            return round(self._session_mults.get(session.lower(), 1.0), 3)

    @property
    def stats(self) -> dict:
        """Get regime stats for dashboard / logging."""
        with self._lock:
            buf = list(self._global_buffer)
            short = buf[-WINDOW_SHORT:] if len(buf) >= WINDOW_SHORT else buf
            med = buf[-WINDOW_MEDIUM:] if len(buf) >= WINDOW_MEDIUM else buf

            short_wr = (sum(1 for o in short if o["win"]) / len(short)
                        if short else 0)
            med_wr = (sum(1 for o in med if o["win"]) / len(med)
                      if med else 0)

            return {
                "regime": self._regime,
                "regime_mult": round(self._regime_mult, 3),
                "n_outcomes": self._n_outcomes,
                "short_wr": round(short_wr, 3),
                "medium_wr": round(med_wr, 3),
                "session_mults": {
                    k: round(v, 3) for k, v in self._session_mults.items()
                },
                "refreshes": self._n_refreshes,
            }

    def summary(self) -> dict:
        """Dashboard-friendly summary dict."""
        s = self.stats
        return {
            "regime": s["regime"],
            "global_mult": s["regime_mult"],
            "rolling_wr": round(s["short_wr"] * 100, 1),
            "window_n": s["n_outcomes"],
            "confidence": s["medium_wr"],
            "session_mults": s["session_mults"],
        }

    def log_status(self):
        """Log current regime state."""
        s = self.stats
        log.info(f"Regime: {s['regime']} "
                 f"(global={s['regime_mult']:.2f}x, "
                 f"short_wr={s['short_wr']:.1%}, "
                 f"med_wr={s['medium_wr']:.1%}, "
                 f"N={s['n_outcomes']})")
        for sess, mult in s["session_mults"].items():
            marker = " *** " if abs(mult - 1.0) > 0.10 else ""
            log.info(f"  Session {sess}: {mult:.2f}x{marker}")

    # ═══════════════════════════════════════════════════════════
    #  PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _load_state(self):
        """Load persisted regime state."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._regime = data.get("regime", "NORMAL")
                self._regime_mult = float(data.get("regime_mult", 1.0))
                self._smoothed_global_mult = self._regime_mult
                sm = data.get("session_mults", {})
                for sess in ["asia", "london", "ny"]:
                    if sess in sm:
                        self._session_mults[sess] = float(sm[sess])
                        self._smoothed_session_mults[sess] = float(sm[sess])
                log.info(f"Regime: restored state from disk "
                         f"({self._regime} {self._regime_mult:.2f}x)")
            except Exception as e:
                _log.warning(f"Regime: load state error: {e}")

    def _save_state(self):
        """Persist regime state for crash recovery."""
        try:
            data = {
                "regime": self._regime,
                "regime_mult": round(self._regime_mult, 4),
                "session_mults": {k: round(v, 4)
                                  for k, v in self._session_mults.items()},
                "n_outcomes": self._n_outcomes,
                "refreshes": self._n_refreshes,
                "ts": time.time(),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  CLI — python -m v13pro.regime
# ═══════════════════════════════════════════════════════════════

def print_report():
    """Print regime analysis report."""
    det = RegimeDetector()
    s = det.stats

    print("\n" + "=" * 60)
    print("  REGIME DETECTOR REPORT")
    print("=" * 60)
    print(f"\n  Outcomes loaded: {s['n_outcomes']}")
    print(f"  Current regime:  {s['regime']}")
    print(f"  Global mult:     {s['regime_mult']:.3f}x")
    print(f"\n  Rolling WR:")
    print(f"    Short ({WINDOW_SHORT} trades):  {s['short_wr']:.1%}")
    print(f"    Medium ({WINDOW_MEDIUM} trades): {s['medium_wr']:.1%}")
    print(f"\n  Session multipliers:")
    for sess, mult in s["session_mults"].items():
        src = "data-driven" if s['n_outcomes'] > MIN_TRADES_FOR_REGIME else "default"
        print(f"    {sess:>8}: {mult:.3f}x ({src})")

    # Show exposure for each session
    print(f"\n  Final exposure multiplier (regime * session):")
    for sess in ["asia", "london", "ny"]:
        em = det.exposure_multiplier(sess)
        print(f"    {sess:>8}: {em:.3f}x")
    print()


if __name__ == "__main__":
    print_report()
