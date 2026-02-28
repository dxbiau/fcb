"""
v13pro/flow_throttle.py -- Smart Strategy-Level Adaptive Throttle.

Replaces the blunt 'circuit breaker' that blocks ALL entries when
daily PnL drops.  Instead, this module:

1. Tracks per-combo (strategy, tf) rolling performance in real-time.
2. Penalises LOSING combos with risk multipliers + temporary cooldowns.
3. Lets WINNING combos continue unrestricted, even during bad stretches.
4. Provides a portfolio-level 'health score' that only triggers full
   pause when MULTIPLE combos fail simultaneously AND equity DD is severe.
5. Implements priority scoring so high-conviction signals get concurrent
   slots ahead of low-conviction ones.

Design philosophy:  "Punish the guilty, reward the innocent."
Goal: maximise throughput of profitable signals while throttling losers.
"""

import math
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple
from v13pro import logger as log

# ══════════════════════════════════════════════════════════════════
#  TUNABLE CONSTANTS (conservative starting values)
# ══════════════════════════════════════════════════════════════════

# Rolling window per combo
COMBO_WINDOW = 20              # last N outcomes per combo for throttle decisions
COMBO_MIN_TRADES = 3           # need at least this many before penalising

# Combo penalty thresholds
COMBO_COLD_WR = 0.30           # WR below 30% → combo cooling off
COMBO_FREEZE_WR = 0.20         # WR below 20% → combo frozen
COMBO_FREEZE_MAX_MIN = 30      # max freeze duration (minutes)
COMBO_COLD_RISK_MULT = 0.40    # risk multiplier when combo is COLD
COMBO_STREAK_FREEZE = 4        # consecutive losses to freeze combo

# Combo reward
COMBO_HOT_WR = 0.65            # WR above 65% → combo is HOT → boost
COMBO_HOT_RISK_MULT = 1.25     # risk multiplier when combo is HOT

# Portfolio-level full pause (extreme safety net — very hard to trigger)
PORTFOLIO_CRITICAL_DD_PCT = 25  # only consider full pause if DD > 25%
PORTFOLIO_MIN_FAILING = 4       # AND at least 4 active combos are failing
PORTFOLIO_PAUSE_MIN = 15        # pause duration (minutes)

# Priority queue: conviction scoring for concurrent slot allocation
PRIORITY_GRADE_SCORE = {"A+": 100, "A": 80, "B": 60, "C": 40, "D": 20}
PRIORITY_COMBO_HOT_BONUS = 30   # extra points for HOT combo
PRIORITY_PAIR_HOT_BONUS = 20    # extra points for pair with recent wins


# ══════════════════════════════════════════════════════════════════
#  COMBO STATE
# ══════════════════════════════════════════════════════════════════

class _ComboState:
    """Track rolling performance for a single (strategy, tf) combo."""

    __slots__ = ("outcomes", "consec_losses", "freeze_until", "last_ts")

    def __init__(self):
        self.outcomes: List[float] = []      # pnl_r values (rolling window)
        self.consec_losses: int = 0
        self.freeze_until: float = 0.0       # time.time() until frozen
        self.last_ts: float = 0.0

    def record(self, pnl_r: float, ts: float = 0.0):
        self.outcomes.append(pnl_r)
        if len(self.outcomes) > COMBO_WINDOW:
            self.outcomes = self.outcomes[-COMBO_WINDOW:]
        self.last_ts = ts or time.time()

        if pnl_r > 0:
            self.consec_losses = 0
        else:
            self.consec_losses += 1
            # Auto-freeze on consecutive loss streak
            if self.consec_losses >= COMBO_STREAK_FREEZE:
                cool_min = min(
                    10 * (self.consec_losses - COMBO_STREAK_FREEZE + 1),
                    COMBO_FREEZE_MAX_MIN)
                self.freeze_until = time.time() + cool_min * 60

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def wr(self) -> float:
        if not self.outcomes:
            return 0.5  # neutral
        return sum(1 for r in self.outcomes if r > 0) / len(self.outcomes)

    @property
    def total_r(self) -> float:
        return sum(self.outcomes)

    @property
    def is_frozen(self) -> bool:
        return time.time() < self.freeze_until

    @property
    def status(self) -> str:
        """HOT / WARM / COLD / FROZEN."""
        if self.is_frozen:
            return "FROZEN"
        if self.n < COMBO_MIN_TRADES:
            return "WARM"  # not enough data to judge
        if self.wr >= COMBO_HOT_WR:
            return "HOT"
        if self.wr <= COMBO_FREEZE_WR:
            return "FROZEN"
        if self.wr <= COMBO_COLD_WR:
            return "COLD"
        return "WARM"


# ══════════════════════════════════════════════════════════════════
#  PAIR STATE (lightweight — for priority scoring)
# ══════════════════════════════════════════════════════════════════

class _PairState:
    __slots__ = ("outcomes",)

    def __init__(self):
        self.outcomes: List[float] = []

    def record(self, pnl_r: float):
        self.outcomes.append(pnl_r)
        if len(self.outcomes) > COMBO_WINDOW:
            self.outcomes = self.outcomes[-COMBO_WINDOW:]

    @property
    def wr(self) -> float:
        if not self.outcomes:
            return 0.5
        return sum(1 for r in self.outcomes if r > 0) / len(self.outcomes)


# ══════════════════════════════════════════════════════════════════
#  FLOW THROTTLE  (main class)
# ══════════════════════════════════════════════════════════════════

class FlowThrottle:
    """
    Strategy-level adaptive throttle + priority queue for slot allocation.

    API:
      .record_outcome(strat, tf, symbol, pnl_r)
          Feed every live trade outcome.

      .combo_risk_mult(strat, tf) -> float
          Risk multiplier for this combo: 0.0 (frozen) to 1.25 (hot).

      .is_combo_blocked(strat, tf) -> (bool, str)
          Whether this combo is currently frozen + reason.

      .is_portfolio_paused(dd_pct) -> bool
          Extreme safety net — rarely triggers.

      .priority_score(strat, tf, symbol, grade, conviction) -> float
          Priority score for slot allocation.  Higher = should trade first.

      .summary() -> dict
          Heartbeat telemetry.
    """

    def __init__(self):
        self._combos: Dict[Tuple[str, str], _ComboState] = defaultdict(_ComboState)
        self._pairs: Dict[str, _PairState] = defaultdict(_PairState)
        self._portfolio_pause_until: float = 0.0
        self._total_outcomes: int = 0

    # ── Feed ──────────────────────────────────────────────────

    def record_outcome(self, strat: str, tf: str, symbol: str,
                       pnl_r: float):
        """Feed a live trade outcome. Call from _on_position_closed."""
        self._combos[(strat, tf)].record(pnl_r)
        self._pairs[symbol].record(pnl_r)
        self._total_outcomes += 1

    # ── Combo Throttle ────────────────────────────────────────

    def combo_risk_mult(self, strat: str, tf: str) -> float:
        """
        Risk multiplier for this combo.
          HOT  (WR≥65%)  → 1.25x (reward winners)
          WARM (default)  → 1.00x
          COLD (WR≤30%)  → 0.40x (reduce risk, don't block)
          FROZEN          → 0.00x (temporary block)
        """
        cs = self._combos.get((strat, tf))
        if cs is None:
            return 1.0  # no data → neutral

        status = cs.status
        if status == "FROZEN":
            return 0.0
        if status == "COLD":
            # Sigmoid scaling between COLD and FREEZE thresholds
            # WR 0.30 → 0.40x,  WR 0.20 → 0.0x
            wr = cs.wr
            if wr <= COMBO_FREEZE_WR:
                return 0.0
            # Linear ramp from FREEZE_WR to COLD_WR
            frac = (wr - COMBO_FREEZE_WR) / max(COMBO_COLD_WR - COMBO_FREEZE_WR, 0.01)
            return COMBO_COLD_RISK_MULT * frac
        if status == "HOT":
            return COMBO_HOT_RISK_MULT
        return 1.0  # WARM

    def is_combo_blocked(self, strat: str, tf: str) -> Tuple[bool, str]:
        """Check if combo is temporarily frozen. Returns (blocked, reason)."""
        cs = self._combos.get((strat, tf))
        if cs is None:
            return False, ""

        status = cs.status
        if status == "FROZEN":
            if cs.is_frozen:
                remain = max(0, (cs.freeze_until - time.time()) / 60)
                return True, (f"combo_freeze({strat}/{tf} "
                              f"WR={cs.wr:.0%} streak={cs.consec_losses} "
                              f"thaw={remain:.0f}m)")
            elif cs.n >= COMBO_MIN_TRADES and cs.wr <= COMBO_FREEZE_WR:
                return True, (f"combo_cold_freeze({strat}/{tf} "
                              f"WR={cs.wr:.0%} n={cs.n})")
        return False, ""

    # ── Portfolio Pause (extreme safety net) ──────────────────

    def is_portfolio_paused(self, dd_pct: float) -> bool:
        """
        Full portfolio pause — ONLY when multiple combos are failing
        AND drawdown is severe.  Should almost never trigger.
        """
        # Existing pause active?
        if time.time() < self._portfolio_pause_until:
            return True

        # Need severe drawdown
        if dd_pct < PORTFOLIO_CRITICAL_DD_PCT:
            return False

        # Count failing combos (COLD or FROZEN with enough trades)
        failing = 0
        active = 0
        for key, cs in self._combos.items():
            if cs.n < COMBO_MIN_TRADES:
                continue
            active += 1
            if cs.status in ("COLD", "FROZEN"):
                failing += 1

        if active > 0 and failing >= PORTFOLIO_MIN_FAILING:
            self._portfolio_pause_until = time.time() + PORTFOLIO_PAUSE_MIN * 60
            log.info(f"  ⚠️ FlowThrottle PORTFOLIO PAUSE: "
                     f"{failing}/{active} combos failing, DD={dd_pct:.1f}%"
                     f" — pausing {PORTFOLIO_PAUSE_MIN}m")
            return True

        return False

    # ── Priority Queue ────────────────────────────────────────

    def priority_score(self, strat: str, tf: str, symbol: str,
                       grade: str = "B", conviction: float = 50) -> float:
        """
        Priority score for concurrent slot allocation.
        Higher score = should get the slot.

        Factors:
        1. Grade base score (A+=100, A=80, B=60, C=40, D=20)
        2. Conviction (raw value / 2, capped at 50)
        3. Combo performance bonus/penalty
        4. Pair recent performance bonus
        """
        # Base from grade
        score = PRIORITY_GRADE_SCORE.get(grade, 50)

        # Conviction component (0-50)
        score += min(50, max(0, conviction) / 2)

        # Combo performance
        cs = self._combos.get((strat, tf))
        if cs and cs.n >= COMBO_MIN_TRADES:
            if cs.status == "HOT":
                score += PRIORITY_COMBO_HOT_BONUS
            elif cs.status == "COLD":
                score -= 30
            elif cs.status == "FROZEN":
                score -= 100  # should not be reaching here, but safety

        # Pair performance
        ps = self._pairs.get(symbol)
        if ps and len(ps.outcomes) >= 3:
            if ps.wr >= 0.60:
                score += PRIORITY_PAIR_HOT_BONUS
            elif ps.wr <= 0.25:
                score -= 20

        return max(0, score)

    # ── Telemetry ─────────────────────────────────────────────

    def summary(self) -> dict:
        """Heartbeat telemetry for dashboard."""
        hot = cold = frozen = warm = 0
        for key, cs in self._combos.items():
            s = cs.status
            if s == "HOT": hot += 1
            elif s == "COLD": cold += 1
            elif s == "FROZEN": frozen += 1
            else: warm += 1

        paused = time.time() < self._portfolio_pause_until
        remain = max(0, (self._portfolio_pause_until - time.time()) / 60) if paused else 0

        return {
            "total_outcomes": self._total_outcomes,
            "combos_tracked": len(self._combos),
            "pairs_tracked": len(self._pairs),
            "hot": hot,
            "warm": warm,
            "cold": cold,
            "frozen": frozen,
            "portfolio_paused": paused,
            "pause_remain_min": round(remain, 1),
        }

    def combo_detail(self) -> List[dict]:
        """Detailed per-combo breakdown for diagnostics."""
        out = []
        for (strat, tf), cs in sorted(self._combos.items()):
            out.append({
                "combo": f"{strat}/{tf}",
                "n": cs.n,
                "wr": round(cs.wr * 100, 1),
                "total_r": round(cs.total_r, 2),
                "status": cs.status,
                "consec_loss": cs.consec_losses,
                "risk_mult": round(self.combo_risk_mult(strat, tf), 2),
            })
        return out
