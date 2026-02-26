"""
v13pro/session_lifecycle.py  --  Session Lifecycle Manager

Tracks per-session performance in real-time and modulates risk
across the session lifecycle. Based on the Feb 25 masterplan finding:

  - First 4 NY trades: ALL WINNERS (+$17.14)
  - Last 3 NY trades: ALL LOSERS (-$13.92 giveback)

The session has a natural lifecycle:
  EARLY (first 3h) — Momentum builds, front-loaded winners
  PEAK  (middle 3h) — Full momentum, edge is live
  LATE  (last 2h)   — Exhaustion, chop, edge decays

Risk modulation:
  EARLY + front-loaded wins → 1.15x momentum boost
  PEAK + session hot (>+3R) → 1.20x hot session boost
  LATE → 0.50x fade (prevent giveback)
  LATE + session negative → 0.30x (hard fade)
  Giveback from peak > 2R → session FATIGUED → 0.35x or STOP

Smart TP:
  EARLY momentum → allow wider TP (trail for bigger moves)
  LATE fade → tighter TP (lock what you have, don't reach)

Integrates with:
  - MomentumAlignment: ALIGNED + EARLY = maximum aggression (1.15 × 1.40 = 1.61x)
  - MicroTF: session hot + micro HOT = confirm market is clean
"""

import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from v13pro import config as cfg

log = logging.getLogger("v13pro")

# ── Session Phase Definitions ───────────────────────────────────────
# Each 8-hour session is split into 3 phases
PHASE_EARLY_HOURS = 3    # first 3 hours
PHASE_PEAK_HOURS = 3     # next 3 hours
PHASE_LATE_HOURS = 2     # last 2 hours

# ── Risk Multipliers ───────────────────────────────────────────────
# Base phase multipliers
EARLY_MULT = 1.00          # neutral start (boost comes from momentum)
PEAK_MULT = 1.00           # full risk
LATE_MULT = 0.50           # fade to prevent giveback

# Conditional boosts
MOMENTUM_EARLY_MULT = 1.15  # first 3 trades are winners → momentum boost
SESSION_HOT_MULT = 1.20     # session PnL > +3.0R → hot session boost
SESSION_NEGATIVE_LATE_MULT = 0.30  # late + session in red → hard fade
FATIGUED_MULT = 0.35        # giveback from peak > 2R → fatigued

# Thresholds
MOMENTUM_MIN_WINS = 3       # first N trades must be wins for momentum
MOMENTUM_MIN_WR = 0.75      # minimum WR for first N trades
SESSION_HOT_R = 3.0          # session PnL R threshold for hot
FATIGUE_GIVEBACK_R = 2.0     # giveback from peak to trigger fatigue
STOP_SIGNAL_GIVEBACK_R = 3.5 # stop new trades entirely

# Smart TP modifiers
EARLY_TP_MULT = 1.15         # wider TP during EARLY momentum
LATE_TP_MULT = 0.85          # tighter TP during LATE
HOT_SESSION_TP_MULT = 1.20   # wider TP when session is hot


class SessionTracker:
    """
    Tracks real-time performance within the current trading session.

    Resets on session boundaries (asia→london→ny→asia).
    Provides risk multipliers and TP modifiers based on:
    - Session phase (EARLY/PEAK/LATE)
    - Intra-session performance (momentum, hot, fatigued)
    """

    def __init__(self):
        self._lock = threading.RLock()

        # Current session identity
        self._current_session: str = ""
        self._session_start_ts: float = 0.0

        # Per-session tracking
        self._trades: list = []     # list of {pnl_r, ts, strategy, symbol}
        self._wins: int = 0
        self._losses: int = 0
        self._pnl_r: float = 0.0
        self._peak_pnl_r: float = 0.0

        # State flags
        self._phase: str = "EARLY"   # EARLY / PEAK / LATE
        self._momentum_early: bool = False
        self._session_hot: bool = False
        self._fatigued: bool = False
        self._stopped: bool = False

    # ── Trade Recording ─────────────────────────────────────────────

    def record_trade(self, pnl_r: float, strategy: str = "", symbol: str = ""):
        """Record a closed trade for this session."""
        with self._lock:
            self._check_session_reset()

            self._trades.append({
                "pnl_r": pnl_r,
                "ts": time.time(),
                "strategy": strategy,
                "symbol": symbol,
            })

            if pnl_r > 0:
                self._wins += 1
            else:
                self._losses += 1

            self._pnl_r += pnl_r
            if self._pnl_r > self._peak_pnl_r:
                self._peak_pnl_r = self._pnl_r

            # Evaluate conditions
            self._evaluate_conditions()

    # ── Risk Multipliers ────────────────────────────────────────────

    def risk_multiplier(self) -> float:
        """
        Get session-lifecycle risk multiplier.

        Returns combined multiplier from phase + conditions.
        """
        with self._lock:
            self._check_session_reset()
            self._update_phase()

            if self._stopped:
                return 0.10  # effectively stop (let existing positions run)

            if self._fatigued:
                return FATIGUED_MULT  # 0.35

            # Base phase multiplier
            if self._phase == "EARLY":
                base = EARLY_MULT
                if self._momentum_early:
                    base *= MOMENTUM_EARLY_MULT  # 1.15x momentum
            elif self._phase == "PEAK":
                base = PEAK_MULT
                if self._session_hot:
                    base *= SESSION_HOT_MULT  # 1.20x hot session
            else:  # LATE
                if self._pnl_r < 0:
                    base = SESSION_NEGATIVE_LATE_MULT  # 0.30
                else:
                    base = LATE_MULT  # 0.50
                    # If session is hot, don't fade as hard
                    if self._session_hot:
                        base = 0.65  # hot session + late → still some caution

            return round(max(0.10, min(1.50, base)), 2)

    def tp_multiplier(self) -> float:
        """
        Get TP modifier based on session state.

        EARLY momentum → wider TP (trail for bigger moves)
        LATE → tighter TP (lock gains, don't overreach)
        HOT → wider TP (let winners run)
        """
        with self._lock:
            self._check_session_reset()

            if self._phase == "EARLY" and self._momentum_early:
                return EARLY_TP_MULT  # 1.15
            elif self._phase == "LATE":
                return LATE_TP_MULT  # 0.85
            elif self._session_hot:
                return HOT_SESSION_TP_MULT  # 1.20
            return 1.0

    def should_stop_trading(self) -> bool:
        """Check if we should stop opening new positions this session."""
        with self._lock:
            return self._stopped

    @property
    def phase(self) -> str:
        """Current session phase: EARLY / PEAK / LATE."""
        with self._lock:
            self._update_phase()
            return self._phase

    @property
    def session_name(self) -> str:
        """Current session name: asia / london / ny."""
        return self._current_session or cfg.current_session_name(
            datetime.now(timezone.utc).hour)

    # ── Internal ────────────────────────────────────────────────────

    def _check_session_reset(self):
        """Reset tracking if session changed. Must hold _lock."""
        now_session = cfg.current_session_name(datetime.now(timezone.utc).hour)
        if now_session != self._current_session:
            if self._current_session:
                # Log session summary before reset
                log.info(
                    f"[SessionLC] Session {self._current_session.upper()} ended: "
                    f"{self._wins}W/{self._losses}L pnl={self._pnl_r:+.2f}R "
                    f"peak={self._peak_pnl_r:+.2f}R "
                    f"{'MOMENTUM' if self._momentum_early else ''} "
                    f"{'HOT' if self._session_hot else ''} "
                    f"{'FATIGUED' if self._fatigued else ''}")

            self._current_session = now_session
            self._session_start_ts = time.time()
            self._trades.clear()
            self._wins = 0
            self._losses = 0
            self._pnl_r = 0.0
            self._peak_pnl_r = 0.0
            self._momentum_early = False
            self._session_hot = False
            self._fatigued = False
            self._stopped = False
            self._phase = "EARLY"

    def _update_phase(self):
        """Update session phase based on current time. Must hold _lock."""
        hour = datetime.now(timezone.utc).hour
        session = cfg.current_session_name(hour)

        # Get session start hour
        session_start, session_end = cfg.SESSIONS.get(session, (0, 8))
        hours_into_session = (hour - session_start) % 24

        if hours_into_session < PHASE_EARLY_HOURS:
            self._phase = "EARLY"
        elif hours_into_session < PHASE_EARLY_HOURS + PHASE_PEAK_HOURS:
            self._phase = "PEAK"
        else:
            self._phase = "LATE"

    def _evaluate_conditions(self):
        """Evaluate momentum, hot, fatigue conditions. Must hold _lock."""
        n_trades = len(self._trades)

        # Momentum early: first N trades mostly winners
        if n_trades >= MOMENTUM_MIN_WINS and not self._momentum_early:
            early_trades = self._trades[:MOMENTUM_MIN_WINS]
            early_wins = sum(1 for t in early_trades if t["pnl_r"] > 0)
            if early_wins / MOMENTUM_MIN_WINS >= MOMENTUM_MIN_WR:
                self._momentum_early = True
                log.info(f"[SessionLC] MOMENTUM detected — "
                         f"first {MOMENTUM_MIN_WINS} trades: "
                         f"{early_wins}W WR={early_wins/MOMENTUM_MIN_WINS:.0%}")

        # Session hot: PnL exceeds threshold
        if self._pnl_r >= SESSION_HOT_R and not self._session_hot:
            self._session_hot = True
            log.info(f"[SessionLC] SESSION HOT — pnl={self._pnl_r:+.2f}R "
                     f"(>= {SESSION_HOT_R}R)")

        # Fatigue: giveback from peak exceeds threshold
        giveback = self._peak_pnl_r - self._pnl_r
        if giveback >= FATIGUE_GIVEBACK_R and not self._fatigued:
            self._fatigued = True
            log.info(f"[SessionLC] FATIGUED — giveback {giveback:.2f}R "
                     f"from peak {self._peak_pnl_r:+.2f}R")

        # Stop signal: massive giveback
        if giveback >= STOP_SIGNAL_GIVEBACK_R and not self._stopped:
            self._stopped = True
            log.info(f"[SessionLC] STOP SIGNAL — giveback {giveback:.2f}R "
                     f"exceeds {STOP_SIGNAL_GIVEBACK_R}R. "
                     f"No new trades this session.")

    # ── Dashboard / Summary ─────────────────────────────────────────

    def summary(self) -> dict:
        """Summary for dashboard display."""
        with self._lock:
            self._check_session_reset()
            self._update_phase()

            giveback = self._peak_pnl_r - self._pnl_r

            return {
                "session": self._current_session,
                "phase": self._phase,
                "trades": len(self._trades),
                "wins": self._wins,
                "losses": self._losses,
                "pnl_r": round(self._pnl_r, 2),
                "peak_pnl_r": round(self._peak_pnl_r, 2),
                "giveback_r": round(giveback, 2),
                "momentum": self._momentum_early,
                "hot": self._session_hot,
                "fatigued": self._fatigued,
                "stopped": self._stopped,
                "risk_mult": self.risk_multiplier(),
                "tp_mult": self.tp_multiplier(),
            }

    def log_status(self):
        """Log current session lifecycle state."""
        s = self.summary()
        log.info(
            f"[SessionLC] {s['session'].upper()} {s['phase']} | "
            f"{s['wins']}W/{s['losses']}L "
            f"pnl={s['pnl_r']:+.2f}R peak={s['peak_pnl_r']:+.2f}R | "
            f"risk×{s['risk_mult']:.2f} tp×{s['tp_mult']:.2f}"
            f"{' MOMENTUM' if s['momentum'] else ''}"
            f"{' HOT!' if s['hot'] else ''}"
            f"{' FATIGUED' if s['fatigued'] else ''}"
            f"{' STOPPED' if s['stopped'] else ''}")
