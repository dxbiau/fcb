"""
v13pro/shadow_live.py -- ShadowLive: Real-Time Shadow-Powered Intelligence

Turns shadow data into a live, actionable intelligence feed with two
novel layers that COMPLEMENT (not duplicate) EdgeRadar:

  EdgeRadar  = (strategy, tf) heat from ALL outcomes (passed+rejected)
  ShadowLive = PAIR-level momentum + passed-only combo focus with recency

Layer 1 — Pair Momentum
  Detects which specific pairs are in hot/cold streaks across all combos.
  Markets develop "pocket-of-edge" regimes where certain pairs move well
  for days, then go dead.  This captures that.

Layer 2 — Live Combo Focus (passed-only)
  EdgeRadar uses all 10k+ outcomes (passed + rejected).  This layer uses
  only PASSED outcomes with exponential time-decay, giving a sharper,
  recency-weighted view of what we actually would have traded.

Layer 3 — Dynamic Focus Multiplier
  Combines pair momentum × combo focus into a single risk-chain multiplier.
  Uses continuous sigmoid mapping — no discrete HOT/WARM/COLD buckets.

OUTPUT:
  shadow_live_mult(symbol, strategy, tf) → float  [0.50 … 1.50]
  pair_momentum_mult(symbol)             → float  [0.60 … 1.35]
  combo_focus_mult(strategy, tf)         → float  [0.70 … 1.40]
  hot_pairs()                            → List[str]

DATA SOURCE: Same shadow JSONL files + incremental feed from shadow._finalize()
UPDATE: Incremental on each outcome + full JSONL reload every 30 minutes
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

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# ── Pair Momentum ──
PAIR_WINDOW = 30               # last N passed outcomes per pair
PAIR_MIN_TRADES = 6            # minimum passed outcomes to score a pair
PAIR_BASELINE_WR = 0.50        # baseline for "average" pair performance
PAIR_HOT_WR = 0.65             # WR above this = hot pair
PAIR_COLD_WR = 0.30            # WR below this = cold pair
PAIR_HOT_MULT_MAX = 1.35       # max pair boost (sigmoid ceiling)
PAIR_COLD_MULT_MIN = 0.60      # min pair penalty (sigmoid floor)

# ── Live Combo Focus (passed-only) ──
COMBO_FOCUS_WINDOW = 40        # last N passed outcomes per combo
COMBO_FOCUS_MIN = 8            # minimum passed outcomes for scoring
COMBO_FOCUS_BASELINE_WR = 0.50 # baseline win rate
COMBO_FOCUS_HOT_MULT = 1.40    # max combo focus boost
COMBO_FOCUS_COLD_MULT = 0.70   # min combo focus penalty
COMBO_FOCUS_DECAY_HALFLIFE_H = 48  # hours halflife for time-decay weighting

# ── Combined (capped) ──
COMBINED_MAX = 1.50            # hard ceiling for shadow_live_mult
COMBINED_MIN = 0.50            # hard floor

# ── Refresh ──
REFRESH_INTERVAL = 1800        # full JSONL reload every 30 minutes

# ═══════════════════════════════════════════════════════════════
#  SHADOW LIVE CLASS
# ═══════════════════════════════════════════════════════════════


class ShadowLive:
    """Real-time shadow-powered pair momentum + combo focus intelligence."""

    def __init__(self):
        self._lock = threading.RLock()

        # Per-pair rolling window: key = symbol (e.g. "BTC/USDT:USDT")
        self._pair_outcomes: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=PAIR_WINDOW)
        )

        # Per-combo rolling window (passed-only): key = "strategy/tf"
        self._combo_outcomes: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=COMBO_FOCUS_WINDOW)
        )

        # Computed caches
        self._pair_heat: Dict[str, dict] = {}     # symbol → {wr, expr, mult, n}
        self._combo_focus: Dict[str, dict] = {}   # combo → {wr, expr, mult, n}

        # Stats
        self._total_outcomes = 0
        self._last_refresh = 0

        # Load historical data
        self._load_from_files()

    # ───────────────────────────────────────────────────────────
    #  LOADING
    # ───────────────────────────────────────────────────────────

    def _load_from_files(self):
        """Load passed shadow outcomes from JSONL files."""
        files = sorted(glob.glob(os.path.join(SHADOW_DIR, "shadow_*.jsonl")))
        loaded = 0
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                            if r.get("event") == "shadow_outcome":
                                self._ingest(r)
                                loaded += 1
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                _log.warning(f"ShadowLive: error reading {f}: {e}")

        self._recalc()
        self._last_refresh = time.time()
        _log.info(f"ShadowLive: loaded {loaded} shadow outcomes, "
                  f"{len(self._pair_heat)} pairs, "
                  f"{len(self._combo_focus)} combos")

    def _ingest(self, r: dict):
        """Ingest a single shadow outcome — PASSED ONLY for pair/combo focus."""
        # Only use passed outcomes for this module
        if not r.get("passed", False):
            return

        symbol = r.get("symbol", "")
        combo = f"{r.get('strategy', '?')}/{r.get('tf', '?')}"
        pnl_r = r.get("pnl_r", 0)
        peak_r = r.get("peak_r", 0)
        ts_ms = r.get("ts_ms", 0)
        side = r.get("side", "long")

        rec = {
            "pnl_r": pnl_r,
            "peak_r": peak_r,
            "ts_ms": ts_ms,
            "side": side,
            "combo": combo,
            "symbol": symbol,
        }

        # Pair-level tracking (all combos for this pair)
        self._pair_outcomes[symbol].append(rec)

        # Combo-level tracking (passed-only — complement to EdgeRadar)
        self._combo_outcomes[combo].append(rec)

        self._total_outcomes += 1

    # ───────────────────────────────────────────────────────────
    #  RECALCULATION
    # ───────────────────────────────────────────────────────────

    def _recalc(self):
        """Recalculate all pair + combo scores."""
        with self._lock:
            self._recalc_pair_heat()
            self._recalc_combo_focus()

    def _recalc_pair_heat(self):
        """Compute pair momentum score with recency-weighted WR + ExpR."""
        heat = {}
        now_ms = int(time.time() * 1000)
        hl_ms = COMBO_FOCUS_DECAY_HALFLIFE_H * 3600 * 1000

        for symbol, outcomes in self._pair_outcomes.items():
            ol = list(outcomes)
            n = len(ol)
            if n < PAIR_MIN_TRADES:
                heat[symbol] = {"wr": 0, "expr": 0, "mult": 1.0,
                                "n": n, "label": "UNKNOWN"}
                continue

            # Time-decay weighted stats
            w_total = 0.0
            w_wins = 0.0
            w_pnl = 0.0
            for o in ol:
                age_ms = max(1, now_ms - o.get("ts_ms", now_ms))
                weight = 2 ** (-age_ms / hl_ms)  # exponential decay
                w_total += weight
                if o["pnl_r"] > 0:
                    w_wins += weight
                w_pnl += o["pnl_r"] * weight

            if w_total < 0.01:
                heat[symbol] = {"wr": 0, "expr": 0, "mult": 1.0,
                                "n": n, "label": "UNKNOWN"}
                continue

            wr = w_wins / w_total
            expr = w_pnl / w_total

            # Sigmoid multiplier: smooth transition from cold→hot
            # Centers on baseline WR, maps to [COLD_MIN, HOT_MAX]
            z = 8.0 * (wr - PAIR_BASELINE_WR)  # steepness factor
            sigmoid = 1.0 / (1.0 + math.exp(-z))
            mult = PAIR_COLD_MULT_MIN + (PAIR_HOT_MULT_MAX - PAIR_COLD_MULT_MIN) * sigmoid

            # Label
            if wr >= PAIR_HOT_WR:
                label = "HOT"
            elif wr <= PAIR_COLD_WR:
                label = "COLD"
            else:
                label = "WARM"

            heat[symbol] = {
                "wr": round(wr, 3), "expr": round(expr, 3),
                "mult": round(mult, 3), "n": n, "label": label,
            }

        self._pair_heat = heat

    def _recalc_combo_focus(self):
        """Compute passed-only combo focus with time-decay weighting."""
        focus = {}
        now_ms = int(time.time() * 1000)
        hl_ms = COMBO_FOCUS_DECAY_HALFLIFE_H * 3600 * 1000

        for combo, outcomes in self._combo_outcomes.items():
            ol = list(outcomes)
            n = len(ol)
            if n < COMBO_FOCUS_MIN:
                focus[combo] = {"wr": 0, "expr": 0, "mult": 1.0,
                                "n": n, "label": "UNKNOWN"}
                continue

            # Time-decay weighted stats
            w_total = 0.0
            w_wins = 0.0
            w_pnl = 0.0
            for o in ol:
                age_ms = max(1, now_ms - o.get("ts_ms", now_ms))
                weight = 2 ** (-age_ms / hl_ms)
                w_total += weight
                if o["pnl_r"] > 0:
                    w_wins += weight
                w_pnl += o["pnl_r"] * weight

            if w_total < 0.01:
                focus[combo] = {"wr": 0, "expr": 0, "mult": 1.0,
                                "n": n, "label": "UNKNOWN"}
                continue

            wr = w_wins / w_total
            expr = w_pnl / w_total

            # Sigmoid multiplier
            z = 8.0 * (wr - COMBO_FOCUS_BASELINE_WR)
            sigmoid = 1.0 / (1.0 + math.exp(-z))
            mult = COMBO_FOCUS_COLD_MULT + (COMBO_FOCUS_HOT_MULT - COMBO_FOCUS_COLD_MULT) * sigmoid

            if wr >= 0.65:
                label = "HOT"
            elif wr <= 0.30:
                label = "COLD"
            else:
                label = "WARM"

            focus[combo] = {
                "wr": round(wr, 3), "expr": round(expr, 3),
                "mult": round(mult, 3), "n": n, "label": label,
            }

        self._combo_focus = focus

    # ───────────────────────────────────────────────────────────
    #  PUBLIC API
    # ───────────────────────────────────────────────────────────

    def pair_momentum_mult(self, symbol: str) -> float:
        """Risk multiplier based on pair-level momentum.

        Returns 0.60 – 1.35 (sigmoid, continuous).
        Pairs with < PAIR_MIN_TRADES passed outcomes return 1.0.
        """
        with self._lock:
            info = self._pair_heat.get(symbol)
            if not info:
                return 1.0
            return info["mult"]

    def pair_label(self, symbol: str) -> str:
        """HOT / WARM / COLD / UNKNOWN for a pair."""
        with self._lock:
            info = self._pair_heat.get(symbol)
            return info["label"] if info else "UNKNOWN"

    def combo_focus_mult(self, strategy: str, tf: str) -> float:
        """Risk multiplier from passed-only combo focus.

        Returns 0.70 – 1.40 (sigmoid, continuous).
        Combos with < COMBO_FOCUS_MIN passed outcomes return 1.0.
        """
        combo = f"{strategy}/{tf}"
        with self._lock:
            info = self._combo_focus.get(combo)
            if not info:
                return 1.0
            return info["mult"]

    def combo_focus_label(self, strategy: str, tf: str) -> str:
        """HOT / WARM / COLD / UNKNOWN for a combo (passed-only)."""
        combo = f"{strategy}/{tf}"
        with self._lock:
            info = self._combo_focus.get(combo)
            return info["label"] if info else "UNKNOWN"

    def shadow_live_mult(self, symbol: str, strategy: str, tf: str) -> float:
        """Combined pair momentum × combo focus multiplier.

        Capped to [COMBINED_MIN, COMBINED_MAX] for safety.
        This is the single number that enters the risk chain.
        """
        p = self.pair_momentum_mult(symbol)
        c = self.combo_focus_mult(strategy, tf)
        combined = p * c
        return max(COMBINED_MIN, min(COMBINED_MAX, combined))

    def hot_pairs(self) -> List[str]:
        """List of currently HOT pairs (momentum > threshold)."""
        with self._lock:
            return [s for s, h in self._pair_heat.items()
                    if h["label"] == "HOT"]

    def cold_pairs(self) -> List[str]:
        """List of currently COLD pairs."""
        with self._lock:
            return [s for s, h in self._pair_heat.items()
                    if h["label"] == "COLD"]

    # ───────────────────────────────────────────────────────────
    #  INCREMENTAL UPDATE (called from shadow._finalize())
    # ───────────────────────────────────────────────────────────

    def record_outcome(self, *, pnl_r: float, peak_r: float,
                       strategy: str, tf: str, side: str,
                       symbol: str, passed: bool = True,
                       ts_ms: int = 0, **kwargs):
        """Ingest a new shadow outcome incrementally.

        Only processes passed outcomes (the module's raison d'être).
        """
        if not passed:
            return

        if ts_ms <= 0:
            ts_ms = int(time.time() * 1000)

        combo = f"{strategy}/{tf}"
        rec = {
            "pnl_r": pnl_r,
            "peak_r": peak_r,
            "ts_ms": ts_ms,
            "side": side,
            "combo": combo,
            "symbol": symbol,
        }

        with self._lock:
            self._pair_outcomes[symbol].append(rec)
            self._combo_outcomes[combo].append(rec)
            self._total_outcomes += 1

        # Lightweight recalc
        self._recalc()

    # ───────────────────────────────────────────────────────────
    #  PERIODIC REFRESH
    # ───────────────────────────────────────────────────────────

    def maybe_refresh(self):
        """Reload from JSONL if stale — called from bot heartbeat."""
        now = time.time()
        if now - self._last_refresh < REFRESH_INTERVAL:
            return
        _log.info("ShadowLive: periodic refresh from JSONL…")
        self._pair_outcomes.clear()
        self._combo_outcomes.clear()
        self._total_outcomes = 0
        self._load_from_files()

    # ───────────────────────────────────────────────────────────
    #  LOGGING / STATUS
    # ───────────────────────────────────────────────────────────

    def log_status(self):
        """Log current shadow live intelligence state."""
        hot_p = self.hot_pairs()
        cold_p = self.cold_pairs()

        # Count scoreable pairs/combos
        scored_pairs = sum(1 for h in self._pair_heat.values()
                          if h["label"] != "UNKNOWN")
        scored_combos = sum(1 for h in self._combo_focus.values()
                           if h["label"] != "UNKNOWN")

        _log.info(
            f"ShadowLive: {self._total_outcomes} passed outcomes, "
            f"{scored_pairs} scored pairs, "
            f"{scored_combos} scored combos"
        )

        # Top 5 hot pairs
        ranked = sorted(
            [(s, h) for s, h in self._pair_heat.items()
             if h["label"] != "UNKNOWN"],
            key=lambda x: x[1]["mult"], reverse=True)[:5]
        for sym, h in ranked:
            short_sym = sym.split("/")[0] if "/" in sym else sym
            _log.info(
                f"  ShadowLive pair {h['label']:6s} {short_sym:10s} "
                f"WR={h['wr']:.0%} ExpR={h['expr']:+.3f} "
                f"x{h['mult']:.2f} N={h['n']}"
            )

        # Top 5 hot combos (passed-only focus)
        ranked_c = sorted(
            [(c, h) for c, h in self._combo_focus.items()
             if h["label"] != "UNKNOWN"],
            key=lambda x: x[1]["mult"], reverse=True)[:5]
        for combo, h in ranked_c:
            _log.info(
                f"  ShadowLive focus {h['label']:6s} {combo:20s} "
                f"WR={h['wr']:.0%} ExpR={h['expr']:+.3f} "
                f"x{h['mult']:.2f} N={h['n']}"
            )

    def summary(self) -> dict:
        """Summary dict for dashboard display."""
        with self._lock:
            return {
                "total_outcomes": self._total_outcomes,
                "hot_pairs": self.hot_pairs(),
                "cold_pairs": self.cold_pairs(),
                "scored_pairs": sum(1 for h in self._pair_heat.values()
                                    if h["label"] != "UNKNOWN"),
                "scored_combos": sum(1 for h in self._combo_focus.values()
                                     if h["label"] != "UNKNOWN"),
                "pair_heat": {k: v for k, v in self._pair_heat.items()
                              if v["label"] != "UNKNOWN"},
                "combo_focus": {k: v for k, v in self._combo_focus.items()
                                if v["label"] != "UNKNOWN"},
            }
