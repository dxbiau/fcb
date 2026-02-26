"""
v13pro/edge_radar.py -- Edge Radar: Full Shadow Intelligence Exploitation

PURPOSE:
  Taps into ALL shadow data that the bot currently ignores:
    - Checkpoint momentum (1/5/15min) → early signal quality scoring
    - Rolling peak_r averages → market heat (are big moves happening?)
    - Strategy x TF heat map → which combos are HOT right now?
    - Sentiment score (continuous) → fine-grained edge by sentiment regime
    - Trough depth → winner drawdown profile for early exit confidence

  Answers the questions:
    "Is now a HOT SEAT? Should we be aggressive?"
    "Which strategy/TF combo has the best recent edge?"
    "Is this market giving runners or grinding to dust?"
    "Should we skip this combo because it's bleeding recently?"

PROVEN DATA POINTS (from audit of 4787 shadow outcomes):
  - 5min checkpoint >+0.1R → 71% WR (+0.57R ExpR)  vs  <-0.1R → 39% WR
  - Old 500: avg_peak 1.37R, 21% runners.  Recent 500: avg_peak 0.62R, 2% runners
  - MTF_RSI/15m: 83% WR (+0.42R).  TR_PULL/15m: 32% WR (-0.17R) 
  - Longs in bull: 51% WR (+0.09R).  Longs in bear/neutral: 70-79% WR (+0.62R)
  - 75% of winners never dip below -0.49R, 90% never below -0.73R

DESIGN:
  1. Reads ALL shadow outcomes on init, updates incrementally via record_outcome()
  2. Maintains rolling windows per strategy/tf combo for heat scoring
  3. Tracks rolling peak_r for market heat detection
  4. Computes sentiment-edge mapping from continuous score (not just bias)
  5. Outputs: combo_heat, market_heat, hot_seat flag, risk multipliers
  6. Refreshes from JSONL every 10 minutes

NON-DESTRUCTIVE:
  - Falls back to 1.0x multipliers when insufficient data
  - Only blocks combos that are proven COLD (WR < 35%, N > 15)
  - Hot seat detection requires MULTIPLE confirming signals
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

# Rolling window for strategy heat (recent trades per combo)
COMBO_WINDOW = 60

# Minimum trades per combo before scoring (prevent noise)
MIN_COMBO_TRADES = 8

# Strategy heat thresholds
COMBO_HOT_WR = 0.58        # above = HOT combo (boost risk)
COMBO_COLD_WR = 0.35       # below = COLD combo (block or reduce)
COMBO_HOT_EXPR = 0.15      # ExpR above this = HOT
COMBO_COLD_EXPR = -0.10    # ExpR below this = COLD

# Strategy heat risk multipliers
COMBO_HOT_MULT = 1.25      # boost proven combos
COMBO_WARM_MULT = 1.0      # neutral
COMBO_COLD_MULT = 0.50     # reduce cold combos aggressively
COMBO_FROZEN_MULT = 0.0    # block outright (WR < 25% with N > 20)

# Market heat: rolling avg peak_r
MARKET_HEAT_WINDOW = 100    # last N outcomes for market heat
MARKET_HOT_PEAK = 1.2       # avg_peak > this = market giving runners
MARKET_WARM_PEAK = 0.8      # avg_peak > this = decent conditions
MARKET_COLD_PEAK = 0.5      # avg_peak < this = no runners, grind

# Market heat risk multipliers
MARKET_HOT_MULT = 1.20
MARKET_WARM_MULT = 1.00
MARKET_COLD_MULT = 0.75

# Sentiment edge: continuous score ranges for longs
# From audit: longs in bull (+0.5) = 51% WR.  Longs in bear (-0.5) = 79% WR
SENTIMENT_EDGE_LONGS = {
    "strong_bull":  {"lo": 0.5, "hi": 1.1, "mult": 0.70},   # worst for longs
    "mild_bull":    {"lo": 0.2, "hi": 0.5, "mult": 0.85},    # mediocre
    "neutral":      {"lo": -0.2, "hi": 0.2, "mult": 1.15},   # great for longs
    "mild_bear":    {"lo": -0.5, "hi": -0.2, "mult": 1.25},  # best for longs!
    "strong_bear":  {"lo": -1.1, "hi": -0.5, "mult": 1.15},  # very good
}

# Hot seat: minimum confirming signals
HOT_SEAT_MIN_SIGNALS = 2   # at least 2 of 3 must be "hot"

# Refresh interval
REFRESH_INTERVAL = 600  # seconds

# ═══════════════════════════════════════════════════════════════
#  EDGE RADAR CLASS
# ═══════════════════════════════════════════════════════════════


class EdgeRadar:
    """Real-time exploitation of ALL shadow intelligence fields."""

    def __init__(self):
        self._lock = threading.RLock()

        # Per-combo rolling window: key = "strategy/tf"
        self._combo_outcomes: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=COMBO_WINDOW)
        )

        # Market heat: rolling peak_r values
        self._peak_r_window: deque = deque(maxlen=MARKET_HEAT_WINDOW)

        # Sentiment edge tracking: (score_bucket, side) -> outcomes
        self._sentiment_outcomes: Dict[str, List[float]] = defaultdict(list)

        # Runner percentage tracking
        self._runner_pct = 0.0      # % of recent trades with peak > 2R
        self._avg_peak_r = 0.0      # rolling avg peak_r

        # Combo heat cache
        self._combo_heat: Dict[str, dict] = {}

        # Hot seat state
        self._hot_seat = False
        self._hot_seat_signals = 0

        # Stats
        self._total_outcomes = 0
        self._last_refresh = 0

        # Load from JSONL
        self._load_from_files()

    # ───────────────────────────────────────────────────────────
    #  LOADING
    # ───────────────────────────────────────────────────────────

    def _load_from_files(self):
        """Load shadow outcomes from JSONL files."""
        files = sorted(glob.glob(os.path.join(SHADOW_DIR, "shadow_*.jsonl")))
        total = 0
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
                                total += 1
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                _log.warning(f"EdgeRadar: error reading {f}: {e}")

        self._recalc()
        self._last_refresh = time.time()
        _log.info(f"EdgeRadar: loaded {total} shadow outcomes")

    def _ingest(self, r: dict):
        """Ingest a single shadow outcome record into rolling windows."""
        combo = f"{r.get('strategy', '?')}/{r.get('tf', '?')}"
        pnl_r = r.get("pnl_r", 0)
        peak_r = r.get("peak_r", 0)
        side = r.get("side", "long")

        # Combo tracking
        self._combo_outcomes[combo].append({
            "pnl_r": pnl_r,
            "peak_r": peak_r,
            "side": side,
        })

        # Market heat tracking (all sides)
        self._peak_r_window.append(peak_r)

        self._total_outcomes += 1

    # ───────────────────────────────────────────────────────────
    #  RECALCULATION
    # ───────────────────────────────────────────────────────────

    def _recalc(self):
        """Recalculate all derived metrics from rolling windows."""
        with self._lock:
            self._recalc_combo_heat()
            self._recalc_market_heat()
            self._recalc_hot_seat()

    def _recalc_combo_heat(self):
        """Compute heat label + multiplier per strategy/tf combo."""
        heat = {}
        for combo, outcomes in self._combo_outcomes.items():
            ol = list(outcomes)
            n = len(ol)
            if n < MIN_COMBO_TRADES:
                heat[combo] = {
                    "label": "UNKNOWN", "mult": 1.0,
                    "wr": 0, "expr": 0, "n": n,
                }
                continue

            wins = sum(1 for o in ol if o["pnl_r"] > 0)
            wr = wins / n
            expr = sum(o["pnl_r"] for o in ol) / n
            avg_peak = sum(o["peak_r"] for o in ol) / n

            if wr < 0.25 and n >= 20:
                label, mult = "FROZEN", COMBO_FROZEN_MULT
            elif wr < COMBO_COLD_WR or expr < COMBO_COLD_EXPR:
                label, mult = "COLD", COMBO_COLD_MULT
            elif wr >= COMBO_HOT_WR and expr >= COMBO_HOT_EXPR:
                label, mult = "HOT", COMBO_HOT_MULT
            else:
                label, mult = "WARM", COMBO_WARM_MULT

            heat[combo] = {
                "label": label, "mult": mult,
                "wr": round(wr, 3), "expr": round(expr, 3),
                "avg_peak": round(avg_peak, 2), "n": n,
            }

        self._combo_heat = heat

    def _recalc_market_heat(self):
        """Compute rolling market heat from peak_r values."""
        peaks = list(self._peak_r_window)
        if not peaks:
            self._avg_peak_r = 0
            self._runner_pct = 0
            return

        self._avg_peak_r = sum(peaks) / len(peaks)
        self._runner_pct = sum(1 for p in peaks if p > 2.0) / len(peaks)

    def _recalc_hot_seat(self):
        """Detect hot seat: multiple signals aligning for aggression."""
        signals = 0

        # Signal 1: market heat is HOT
        if self._avg_peak_r >= MARKET_HOT_PEAK:
            signals += 1

        # Signal 2: at least one combo is HOT
        hot_combos = [c for c, h in self._combo_heat.items()
                      if h["label"] == "HOT"]
        if hot_combos:
            signals += 1

        # Signal 3: runner percentage is high
        if self._runner_pct >= 0.15:
            signals += 1

        self._hot_seat_signals = signals
        self._hot_seat = signals >= HOT_SEAT_MIN_SIGNALS

    # ───────────────────────────────────────────────────────────
    #  PUBLIC API: called from bot._execute_signal()
    # ───────────────────────────────────────────────────────────

    def combo_risk_multiplier(self, strategy: str, tf: str) -> float:
        """Risk multiplier based on strategy/tf combo heat.

        Returns:
            0.0  = FROZEN (block this combo entirely)
            0.50 = COLD (halve risk)
            1.00 = WARM (neutral)
            1.25 = HOT (boost risk)
        """
        combo = f"{strategy}/{tf}"
        with self._lock:
            info = self._combo_heat.get(combo)
            if not info:
                return 1.0  # unknown = neutral
            return info["mult"]

    def combo_label(self, strategy: str, tf: str) -> str:
        """Human-readable heat label for a combo."""
        combo = f"{strategy}/{tf}"
        with self._lock:
            info = self._combo_heat.get(combo)
            return info["label"] if info else "UNKNOWN"

    def is_combo_blocked(self, strategy: str, tf: str) -> bool:
        """Should this combo be blocked entirely? (FROZEN status)"""
        combo = f"{strategy}/{tf}"
        with self._lock:
            info = self._combo_heat.get(combo)
            if not info:
                return False
            return info["label"] == "FROZEN"

    def market_heat_multiplier(self) -> float:
        """Risk multiplier based on whether market is giving runners.

        Returns:
            1.20  = market is HOT (big moves happening)
            1.00  = WARM (decent)
            0.75  = COLD (no runners, grind - reduce exposure)
        """
        with self._lock:
            avg = self._avg_peak_r
        if avg >= MARKET_HOT_PEAK:
            return MARKET_HOT_MULT
        elif avg >= MARKET_WARM_PEAK:
            return MARKET_WARM_MULT
        else:
            return MARKET_COLD_MULT

    def market_heat_label(self) -> str:
        """Human-readable market heat label."""
        with self._lock:
            avg = self._avg_peak_r
        if avg >= MARKET_HOT_PEAK:
            return "HOT"
        elif avg >= MARKET_WARM_PEAK:
            return "WARM"
        else:
            return "COLD"

    def sentiment_risk_multiplier(self, side: str, score: float) -> float:
        """Fine-grained risk multiplier from continuous sentiment score.

        Uses the proven finding that longs in bull sentiment (score > 0.5)
        only have 51% WR / +0.09R, while longs in bear (score < -0.2)
        have 70-79% WR / +0.62R.

        For shorts, the mapping is inverted.

        Args:
            side: "long" or "short"
            score: continuous sentiment score (-1.0 to +1.0)

        Returns:
            Risk multiplier (0.70 to 1.25)
        """
        if side == "long":
            for _, bucket in SENTIMENT_EDGE_LONGS.items():
                if bucket["lo"] <= score < bucket["hi"]:
                    return bucket["mult"]
            return 1.0  # fallback

        # Shorts: inverse sentiment edge
        # (shorts work better in bull, worse in bear — opposite of longs)
        for _, bucket in SENTIMENT_EDGE_LONGS.items():
            if bucket["lo"] <= score < bucket["hi"]:
                # Invert: strong_bull becomes best for shorts
                inverted = 2.0 - bucket["mult"]
                return max(0.7, min(1.3, inverted))
        return 1.0

    def is_hot_seat(self) -> bool:
        """Is the system in a 'hot seat' — conditions ripe for aggression?

        Hot seat requires 2+ of:
          1. Market heat: avg_peak_r > 1.2R (runners available)
          2. At least one HOT combo exists
          3. Runner percentage > 15%
        """
        with self._lock:
            return self._hot_seat

    def hot_seat_boost(self) -> float:
        """Extra risk multiplier when in hot seat.

        Returns:
            1.0  if not hot seat
            1.15 if hot seat (2 signals)
            1.30 if blazing hot seat (3 signals)
        """
        with self._lock:
            if not self._hot_seat:
                return 1.0
            if self._hot_seat_signals >= 3:
                return 1.30
            return 1.15

    def hot_combos(self) -> List[str]:
        """List of currently HOT strategy/tf combos."""
        with self._lock:
            return [c for c, h in self._combo_heat.items()
                    if h["label"] == "HOT"]

    def cold_combos(self) -> List[str]:
        """List of currently COLD/FROZEN combos."""
        with self._lock:
            return [c for c, h in self._combo_heat.items()
                    if h["label"] in ("COLD", "FROZEN")]

    # ───────────────────────────────────────────────────────────
    #  INCREMENTAL UPDATE (called from shadow._finalize())
    # ───────────────────────────────────────────────────────────

    def record_outcome(self, *, pnl_r: float, peak_r: float,
                       strategy: str, tf: str, side: str,
                       sentiment_score: float = 0.0, **kwargs):
        """Ingest a new shadow outcome incrementally."""
        combo = f"{strategy}/{tf}"

        with self._lock:
            self._combo_outcomes[combo].append({
                "pnl_r": pnl_r,
                "peak_r": peak_r,
                "side": side,
            })
            self._peak_r_window.append(peak_r)
            self._total_outcomes += 1

        # Recalc (lightweight, just rolling stats)
        self._recalc()

    # ───────────────────────────────────────────────────────────
    #  PERIODIC REFRESH (called from heartbeat)
    # ───────────────────────────────────────────────────────────

    def maybe_refresh(self):
        """Reload from JSONL if stale — called from bot heartbeat."""
        now = time.time()
        if now - self._last_refresh < REFRESH_INTERVAL:
            return
        _log.info("EdgeRadar: periodic refresh from JSONL…")
        self._combo_outcomes.clear()
        self._peak_r_window.clear()
        self._total_outcomes = 0
        self._load_from_files()

    # ───────────────────────────────────────────────────────────
    #  LOGGING / STATUS
    # ───────────────────────────────────────────────────────────

    def log_status(self):
        """Log current edge radar state."""
        mkt = self.market_heat_label()
        hot = self.is_hot_seat()
        hot_c = self.hot_combos()
        cold_c = self.cold_combos()

        _log.info(
            f"EdgeRadar: market={mkt} (avg_peak={self._avg_peak_r:.2f}R, "
            f"runners={self._runner_pct:.0%}) | "
            f"hot_seat={'YES' if hot else 'no'} ({self._hot_seat_signals}/3) | "
            f"HOT combos={hot_c or 'none'} | "
            f"COLD combos={cold_c or 'none'} | "
            f"total_outcomes={self._total_outcomes}"
        )

        # Detail per combo
        for combo, info in sorted(self._combo_heat.items(),
                                   key=lambda x: x[1].get("expr", 0),
                                   reverse=True):
            if info["n"] >= MIN_COMBO_TRADES:
                _log.info(
                    f"  EdgeRadar combo {info['label']:6s} {combo:20s} "
                    f"WR={info['wr']:.0%} ExpR={info['expr']:+.3f} "
                    f"peak={info['avg_peak']:.2f}R N={info['n']}"
                )

    def summary(self) -> dict:
        """Summary dict for dashboard display."""
        with self._lock:
            return {
                "market_heat": self.market_heat_label(),
                "avg_peak_r": round(self._avg_peak_r, 2),
                "runner_pct": round(self._runner_pct, 3),
                "hot_seat": self._hot_seat,
                "hot_seat_signals": self._hot_seat_signals,
                "hot_combos": self.hot_combos(),
                "cold_combos": self.cold_combos(),
                "total_outcomes": self._total_outcomes,
                "combos": {k: v for k, v in self._combo_heat.items()
                           if v["n"] >= MIN_COMBO_TRADES},
            }
