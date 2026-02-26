"""
live/dynamic_engine.py — Real-Time Dynamic Hybrid Intelligence Engine

PURPOSE:
  Static pre-session intel tells us IF a pair is tradeable.
  The oracle tells us WHAT structural quality a single setup has.
  This engine tells us HOW MUCH to risk RIGHT NOW based on everything
  happening in real time across the entire session.

  It synthesizes: session P&L momentum, market regime (BTC), breakout
  hit rate across all pairs this session, equity trajectory, heat
  management, and volatility regime into ONE adaptive decision layer.

ARCHITECTURE:
  SessionPulse   — tracks intra-session wins/losses/R in real time
  MarketRegime   — classifies current market state from BTC + alt behavior
  HeatManager    — consecutive loss tracking → cooldown periods
  AdaptiveRisk   — dynamic Kelly fraction based on running session data
  DynamicEngine  — synthesizes all above into evaluate_entry() decision

INTEGRATION:
  Called by bot.py at two points:
    1. session_start()   — reset pulse, snapshot BTC
    2. evaluate_entry()  — before every entry, returns (take, risk_mult, flags)
    3. record_outcome()  — after every trade resolves (win/loss/R)

NO EXTERNAL DEPENDENCIES — stdlib only.
"""

from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — imported from config.py at runtime
# ═══════════════════════════════════════════════════════════════

# Defaults — overridden by config if available
_DEFAULT = {
    "heat_max_consec_loss": 3,         # consecutive losses before cooldown
    "heat_cooldown_trades": 2,         # trades at reduced size after cooldown
    "heat_cooldown_mult": 0.50,        # risk multiplier during cooldown
    "momentum_boost_threshold": 3,     # session wins before momentum boost
    "momentum_boost_mult": 1.15,       # risk multiplier when on a streak
    "session_loss_cap_r": -3.0,        # stop trading session if cumulative R < this
    "session_loss_cap_trades": 2,      # minimum trades before loss cap activates
    "bankroll_phase_x2": 0.15,         # equity growth fraction to enter phase 2
    "bankroll_phase_x5": 0.50,         # equity growth fraction to enter phase 3
    "regime_btc_dump": -3.0,           # BTC 24h change threshold for "dump"
    "regime_btc_crash": -5.0,          # BTC 24h change threshold for "crash"
    "regime_btc_pump": 3.0,            # BTC 24h change threshold for "pump"
    "regime_btc_rally": 5.0,           # BTC 24h change threshold for "rally"
    "market_wide_fail_pct": 0.75,      # if >=75% of session breakouts fail → hostile
    "market_wide_min_sample": 3,       # need >=3 resolved trades for market-wide signal
}


def _cfg(key: str):
    """Load from live.config if available, else use default."""
    try:
        from live import config as _c
        return getattr(_c, f"DYN_{key.upper()}", _DEFAULT.get(key, 0))
    except (ImportError, AttributeError):
        return _DEFAULT.get(key, 0)


# ═══════════════════════════════════════════════════════════════
#  SESSION PULSE — real-time intra-session tracking
# ═══════════════════════════════════════════════════════════════

@dataclass
class SessionPulse:
    """Tracks session performance in real time."""
    session: str = ""
    start_equity: float = 0.0
    start_time: float = 0.0

    # Running counters
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    entries: int = 0

    # Streak tracking
    consec_wins: int = 0
    consec_losses: int = 0
    last_result: str = ""           # "W" or "L"

    # Per-pair tracking: {symbol: {"w": N, "l": N, "r": float}}
    pair_results: Dict[str, Dict] = field(default_factory=dict)

    # Market-wide breakout quality tracking
    breakouts_taken: int = 0
    breakouts_won: int = 0

    # Heat state
    in_cooldown: bool = False
    cooldown_trades_remaining: int = 0

    @property
    def wr(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5

    @property
    def avg_r(self) -> float:
        total = self.wins + self.losses
        return self.total_r / total if total > 0 else 0.0

    @property
    def is_profitable(self) -> bool:
        return self.total_r > 0

    @property
    def elapsed_mins(self) -> float:
        return (time.time() - self.start_time) / 60 if self.start_time > 0 else 0

    def record(self, symbol: str, r_value: float, won: bool):
        """Record a trade outcome."""
        self.entries += 1

        if won:
            self.wins += 1
            self.consec_wins += 1
            self.consec_losses = 0
            self.last_result = "W"
            self.breakouts_won += 1
        else:
            self.losses += 1
            self.consec_losses += 1
            self.consec_wins = 0
            self.last_result = "L"

        self.total_r += r_value
        self.breakouts_taken += 1

        # Per-pair
        if symbol not in self.pair_results:
            self.pair_results[symbol] = {"w": 0, "l": 0, "r": 0.0}
        pr = self.pair_results[symbol]
        pr["w" if won else "l"] += 1
        pr["r"] += r_value


# ═══════════════════════════════════════════════════════════════
#  MARKET REGIME — macro environment classification
# ═══════════════════════════════════════════════════════════════

@dataclass
class MarketRegime:
    """Current market environment assessment."""
    btc_change_24h: float = 0.0
    btc_price: float = 0.0
    regime: str = "neutral"           # crash | dump | calm | pump | rally
    alt_breakout_quality: float = 0.5  # 0-1, running avg of breakout success this session
    confidence: float = 1.0           # 0-1, how confident we are in the regime call

    def classify(self, btc_chg: float, btc_price: float = 0):
        """Classify the current BTC-driven market regime."""
        self.btc_change_24h = btc_chg
        self.btc_price = btc_price

        crash_th = _cfg("regime_btc_crash")
        dump_th = _cfg("regime_btc_dump")
        pump_th = _cfg("regime_btc_pump")
        rally_th = _cfg("regime_btc_rally")

        if btc_chg <= crash_th:
            self.regime = "crash"
            self.confidence = 0.95
        elif btc_chg <= dump_th:
            self.regime = "dump"
            self.confidence = 0.80
        elif btc_chg >= rally_th:
            self.regime = "rally"
            self.confidence = 0.90
        elif btc_chg >= pump_th:
            self.regime = "pump"
            self.confidence = 0.80
        elif abs(btc_chg) <= 1.5:
            self.regime = "calm"
            self.confidence = 0.70
        else:
            self.regime = "neutral"
            self.confidence = 0.50

    def update_alt_quality(self, session_pulse: SessionPulse):
        """Update alt breakout quality from session results."""
        taken = session_pulse.breakouts_taken
        if taken >= 2:
            self.alt_breakout_quality = session_pulse.breakouts_won / taken
        else:
            self.alt_breakout_quality = 0.5  # insufficient data


# ═══════════════════════════════════════════════════════════════
#  BANKROLL PHASE — adapts aggression to equity growth stage
# ═══════════════════════════════════════════════════════════════

def _bankroll_phase(equity: float, start_equity: float) -> Tuple[int, str, float]:
    """
    Determine which growth phase we're in.

    Phase 1: SURVIVAL ($150 → ~$175)  — conservative, protect capital
    Phase 2: GROWTH   ($175 → ~$225)  — standard risk
    Phase 3: COMPOUND ($225 → $1500)  — maximize compounding
    Phase 4: CRUISE   ($1500+)        — mission accomplished, reduce risk

    Returns: (phase_number, phase_name, risk_modifier)
    """
    if start_equity <= 0:
        return 1, "SURVIVAL", 0.85

    growth = (equity - start_equity) / start_equity

    if growth < _cfg("bankroll_phase_x2"):
        # Phase 1: haven't grown 15% yet — protect capital
        return 1, "SURVIVAL", 0.85
    elif growth < _cfg("bankroll_phase_x5"):
        # Phase 2: growing, be more aggressive
        return 2, "GROWTH", 1.0
    elif equity < start_equity * 10:
        # Phase 3: deep compounding territory
        return 3, "COMPOUND", 1.10
    else:
        # Phase 4: mission complete (x10 achieved)
        return 4, "CRUISE", 0.80


# ═══════════════════════════════════════════════════════════════
#  DYNAMIC ENGINE — the real-time brain
# ═══════════════════════════════════════════════════════════════

class DynamicEngine:
    """
    Real-time adaptive decision engine.

    Synthesizes:
      - Session pulse (intra-session wins/losses)
      - Market regime (BTC + alt breakout quality)
      - Heat management (consecutive loss cooldown)
      - Bankroll phase (adapt to equity trajectory)
      - Pair-level intelligence (S/R, intel, oracle)
      - Volatility regime (from pair_intel)

    Into a single evaluate_entry() call that returns:
      (should_take: bool, risk_mult: float, flags: List[str])
    """

    def __init__(self, start_equity: float = 150.0):
        self.start_equity = start_equity
        self.pulse = SessionPulse()
        self.regime = MarketRegime()
        self._session_halted = False
        self._halt_reason = ""

    # ──────────────────────────────────────────────────
    #  LIFECYCLE
    # ──────────────────────────────────────────────────

    def session_start(self, session: str, equity: float,
                      btc_chg: float = 0.0, btc_price: float = 0.0):
        """Reset for new session. Called at session open."""
        self.pulse = SessionPulse(
            session=session,
            start_equity=equity,
            start_time=time.time(),
        )
        self.regime.classify(btc_chg, btc_price)
        self._session_halted = False
        self._halt_reason = ""

    def record_outcome(self, symbol: str, r_value: float, won: bool):
        """Record a resolved trade. Called when position closes."""
        self.pulse.record(symbol, r_value, won)
        self.regime.update_alt_quality(self.pulse)
        self._check_heat()
        self._check_session_halt()

    # ──────────────────────────────────────────────────
    #  CORE EVALUATION
    # ──────────────────────────────────────────────────

    def evaluate_entry(self, pair: str, pair_class: str, direction: str,
                       ctx_score: int, ctx_grade: str,
                       edge_tier: str, edge_risk_mult: float,
                       equity: float) -> Tuple[bool, float, List[str]]:
        """
        The master decision: should we take this trade, and at what size?

        Called AFTER ctx_score and edge_score are computed, BEFORE final
        signal computation. This is the last gate.

        Returns:
            (should_take, risk_multiplier, flags)
            - should_take: False = hard block
            - risk_multiplier: 0.0-1.3 applied to base risk_pct
            - flags: diagnostic strings for audit trail
        """
        flags: List[str] = []
        mult = 1.0

        # ── 1. SESSION HALT CHECK ──
        if self._session_halted:
            flags.append(f"SESSION_HALTED({self._halt_reason})")
            return False, 0.0, flags

        # ── 2. BANKROLL PHASE ──
        phase_num, phase_name, phase_mult = _bankroll_phase(
            equity, self.start_equity
        )
        mult *= phase_mult
        flags.append(f"PHASE_{phase_num}({phase_name})")

        # ── 3. MARKET REGIME ──
        regime_mult = self._regime_multiplier(direction)
        mult *= regime_mult
        if regime_mult != 1.0:
            flags.append(f"REGIME_{self.regime.regime.upper()}(×{regime_mult:.2f})")

        # ── 4. HEAT MANAGEMENT ──
        if self.pulse.in_cooldown:
            cooldown_mult = _cfg("heat_cooldown_mult")
            mult *= cooldown_mult
            flags.append(
                f"HEAT_COOLDOWN({self.pulse.cooldown_trades_remaining} "
                f"left, ×{cooldown_mult:.0%})"
            )
        elif self.pulse.consec_losses >= 2:
            # Not in cooldown yet but losing streak — slight caution
            mult *= 0.85
            flags.append(f"LOSING_STREAK({self.pulse.consec_losses})")

        # ── 5. SESSION MOMENTUM ──
        momentum_mult = self._momentum_multiplier()
        if momentum_mult != 1.0:
            mult *= momentum_mult
            flags.append(f"MOMENTUM(×{momentum_mult:.2f})")

        # ── 6. MARKET-WIDE BREAKOUT QUALITY ──
        market_mult = self._market_quality_multiplier()
        if market_mult != 1.0:
            mult *= market_mult
            flags.append(f"MARKET_QUALITY(×{market_mult:.2f})")

        # ── 7. PAIR TRACK RECORD (this session) ──
        pair_mult = self._pair_session_multiplier(pair)
        if pair_mult != 1.0:
            mult *= pair_mult
            flags.append(f"PAIR_HISTORY(×{pair_mult:.2f})")

        # ── 8. HARD BLOCKS ──
        # BTC crash + long = suicide
        if self.regime.regime == "crash" and direction == "long":
            flags.append("BLOCK_CRASH_LONG")
            return False, 0.0, flags

        # Session hemorrhage — stop the bleeding
        if self._session_halted:
            flags.append(f"BLOCK_SESSION_HALT({self._halt_reason})")
            return False, 0.0, flags

        # ── 9. CLAMP ──
        # Never go above 1.3x (don't over-lever) or below 0.35x (too small to matter)
        mult = max(0.35, min(1.30, mult))

        # If combined mult is very low AND context is already weak, just skip
        if mult < 0.45 and ctx_score < -5:
            flags.append(f"SKIP_LOW_COMBINED(mult={mult:.2f},ctx={ctx_score})")
            return False, 0.0, flags

        flags.append(f"DYNAMIC_MULT(×{mult:.2f})")
        return True, mult, flags

    # ──────────────────────────────────────────────────
    #  INTERNAL — multiplier calculators
    # ──────────────────────────────────────────────────

    def _regime_multiplier(self, direction: str) -> float:
        """Market regime risk modifier."""
        r = self.regime.regime

        if r == "crash":
            return 0.40 if direction == "short" else 0.0  # shorts only, small
        elif r == "dump":
            if direction == "long":
                return 0.60   # longs are risky
            return 1.0        # shorts are fine
        elif r == "rally":
            if direction == "short":
                return 0.50   # shorts are risky
            return 1.10       # longs love BTC rallies
        elif r == "pump":
            if direction == "short":
                return 0.70
            return 1.05
        elif r == "calm":
            return 1.0        # neutral
        else:
            return 1.0        # neutral

    def _momentum_multiplier(self) -> float:
        """Session win-streak momentum boost."""
        threshold = int(_cfg("momentum_boost_threshold"))
        boost = float(_cfg("momentum_boost_mult"))

        if self.pulse.consec_wins >= threshold and self.pulse.total_r > 0:
            return boost  # riding the wave
        elif self.pulse.consec_wins >= 2 and self.pulse.wr >= 0.6:
            return 1.05   # mild positive momentum
        return 1.0

    def _market_quality_multiplier(self) -> float:
        """
        Market-wide breakout quality this session.

        If most breakouts are failing → the market is hostile to FCB.
        """
        min_sample = int(_cfg("market_wide_min_sample"))
        fail_pct = float(_cfg("market_wide_fail_pct"))

        taken = self.pulse.breakouts_taken
        if taken < min_sample:
            return 1.0  # insufficient data

        fail_rate = 1.0 - (self.pulse.breakouts_won / taken)
        if fail_rate >= fail_pct:
            return 0.65  # market is hostile to breakouts right now
        elif fail_rate >= 0.60:
            return 0.80  # elevated failure rate
        elif fail_rate <= 0.30:
            return 1.10  # breakouts are working well
        return 1.0

    def _pair_session_multiplier(self, pair: str) -> float:
        """
        Adjust based on this specific pair's performance within the session.

        Pairs that already lost 2+ times this session = reduce.
        Pairs that won = slight confidence boost.
        """
        pr = self.pulse.pair_results.get(pair)
        if not pr:
            return 1.0

        if pr["l"] >= 2 and pr["w"] == 0:
            return 0.50  # this pair is not working today
        elif pr["l"] >= 1 and pr["w"] == 0:
            return 0.80  # already lost once — cautious
        elif pr["w"] >= 2 and pr["l"] == 0:
            return 1.15  # pair is hot, reward it
        elif pr["w"] >= 1 and pr["l"] == 0:
            return 1.05
        return 1.0

    # ──────────────────────────────────────────────────
    #  INTERNAL — state checks
    # ──────────────────────────────────────────────────

    def _check_heat(self):
        """Activate cooldown after consecutive losses."""
        max_consec = int(_cfg("heat_max_consec_loss"))
        cooldown_trades = int(_cfg("heat_cooldown_trades"))

        if self.pulse.in_cooldown:
            self.pulse.cooldown_trades_remaining -= 1
            if self.pulse.cooldown_trades_remaining <= 0:
                self.pulse.in_cooldown = False
                self.pulse.cooldown_trades_remaining = 0
        elif self.pulse.consec_losses >= max_consec:
            self.pulse.in_cooldown = True
            self.pulse.cooldown_trades_remaining = cooldown_trades

    def _check_session_halt(self):
        """Check if session should be halted (capital preservation)."""
        loss_cap_r = float(_cfg("session_loss_cap_r"))
        min_trades = int(_cfg("session_loss_cap_trades"))

        total = self.pulse.wins + self.pulse.losses
        if total >= min_trades and self.pulse.total_r <= loss_cap_r:
            self._session_halted = True
            self._halt_reason = (
                f"R={self.pulse.total_r:+.2f} in {total} trades"
            )

    # ──────────────────────────────────────────────────
    #  STATUS / DIAGNOSTICS
    # ──────────────────────────────────────────────────

    def status_line(self, equity: float) -> str:
        """One-line status for logging."""
        p = self.pulse
        phase_num, phase_name, _ = _bankroll_phase(equity, self.start_equity)
        heat = "COOL" if p.in_cooldown else (
            f"STREAK-{p.consec_losses}L" if p.consec_losses >= 2 else
            f"STREAK+{p.consec_wins}W" if p.consec_wins >= 2 else "NORMAL"
        )
        return (
            f"DYN [{phase_name}] {p.session}: "
            f"{p.wins}W/{p.losses}L R={p.total_r:+.2f} | "
            f"regime={self.regime.regime} heat={heat} | "
            f"{'HALTED' if self._session_halted else 'ACTIVE'}"
        )

    def to_dict(self) -> Dict:
        """Snapshot for audit logging."""
        p = self.pulse
        phase_num, phase_name, phase_mult = _bankroll_phase(
            p.start_equity, self.start_equity
        )
        return {
            "session": p.session,
            "session_wins": p.wins,
            "session_losses": p.losses,
            "session_r": round(p.total_r, 3),
            "session_wr": round(p.wr, 3),
            "consec_wins": p.consec_wins,
            "consec_losses": p.consec_losses,
            "in_cooldown": p.in_cooldown,
            "cooldown_remaining": p.cooldown_trades_remaining,
            "regime": self.regime.regime,
            "regime_confidence": round(self.regime.confidence, 2),
            "btc_24h_chg": round(self.regime.btc_change_24h, 2),
            "alt_breakout_quality": round(self.regime.alt_breakout_quality, 2),
            "bankroll_phase": phase_num,
            "bankroll_phase_name": phase_name,
            "session_halted": self._session_halted,
            "halt_reason": self._halt_reason,
        }
