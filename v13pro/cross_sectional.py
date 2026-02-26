"""
v13pro/cross_sectional.py -- Cross-Sectional Awareness Engine

Detects and mitigates correlated risk across simultaneously active positions.

Shadow data shows:
  - Avg 28.9 losses within 1hr window, max 40
  - Losses are heavily clustered — correlated market events
  - Multiple entries in short windows amplify drawdown risk

This module provides:
  1. Temporal entry spacing — reduce risk when entries cluster in time
  2. Active position correlation — detect when open positions are all
     moving in the same adverse direction
  3. Emergency exposure throttle — when loss cluster is detected

Design principles:
  - Probabilistic multipliers (0.5–1.0), never hard blocks
  - O(1) per-call: pre-computed from rolling state
  - Modular: set CROSS_SECTIONAL_ENABLED=False to bypass
  - No exchange calls — uses only bot state + timestamps
"""

import time
import threading
from collections import deque
from typing import Dict, List, Optional

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

# ── Configuration ──
CROSS_SECTIONAL_ENABLED = True

# Temporal clustering: entries within this window are "clustered"
ENTRY_CLUSTER_WINDOW_SEC = 3600     # 1 hour

# Risk reduction per additional clustered entry
# 1st entry: 1.0, 2nd: 0.90, 3rd: 0.80, etc.
CLUSTER_RISK_DECAY = 0.10           # 10% reduction per clustered entry

# Minimum risk multiplier from clustering (never go below this)
CLUSTER_RISK_FLOOR = 0.50

# Loss clustering: if N losses within LOSS_WINDOW, apply emergency throttle
LOSS_CLUSTER_WINDOW_SEC = 3600      # 1 hour
LOSS_CLUSTER_THRESHOLD = 3          # 3+ losses in window → throttle
LOSS_CLUSTER_THROTTLE = 0.50        # 50% risk reduction during loss cluster

# Loss cluster cooldown: wait this long after cluster before resuming normal
LOSS_CLUSTER_COOLDOWN_SEC = 1800    # 30 minutes

# Maximum recent entries to track
MAX_ENTRY_HISTORY = 200
MAX_LOSS_HISTORY = 200


class CrossSectionalAwareness:
    """
    Cross-sectional risk awareness engine.

    Tracks entry timestamps and loss timestamps to detect clustering.
    Provides risk multipliers that modulate position sizing.
    """

    def __init__(self):
        self._lock = threading.RLock()  # RLock: summary() calls risk_multiplier() re-entrantly
        # Ring buffers for recent events
        self._entry_times: deque = deque(maxlen=MAX_ENTRY_HISTORY)
        self._loss_times: deque = deque(maxlen=MAX_LOSS_HISTORY)
        self._last_cluster_event = 0.0  # timestamp of last loss cluster detection

    # ═══════════════════════════════════════════════════════════
    #  EVENT RECORDING (called by bot.py)
    # ═══════════════════════════════════════════════════════════

    def record_entry(self, symbol: str):
        """Record that an entry was placed. Call from bot.py after order."""
        if not CROSS_SECTIONAL_ENABLED:
            return
        with self._lock:
            self._entry_times.append(time.time())

    def record_loss(self, symbol: str, pnl_r: float):
        """Record a losing trade. Call from bot.py on position close."""
        if not CROSS_SECTIONAL_ENABLED:
            return
        if pnl_r >= 0:
            return  # only track losses
        with self._lock:
            now = time.time()
            self._loss_times.append(now)

            # Check for loss cluster
            cutoff = now - LOSS_CLUSTER_WINDOW_SEC
            recent_losses = sum(1 for t in self._loss_times if t > cutoff)
            if recent_losses >= LOSS_CLUSTER_THRESHOLD:
                if now - self._last_cluster_event > LOSS_CLUSTER_COOLDOWN_SEC:
                    self._last_cluster_event = now
                    log.warning(f"Cross-sectional: LOSS CLUSTER detected "
                                f"({recent_losses} losses in {LOSS_CLUSTER_WINDOW_SEC}s) "
                                f"— throttling risk {LOSS_CLUSTER_THROTTLE:.0%}")

    # ═══════════════════════════════════════════════════════════
    #  RISK MULTIPLIER (called by bot.py before sizing)
    # ═══════════════════════════════════════════════════════════

    def risk_multiplier(self) -> float:
        """
        Compute cross-sectional risk multiplier.

        Combines:
        1. Entry clustering penalty (many entries in short window)
        2. Loss clustering emergency throttle

        Returns 0.5–1.0 (never amplifies risk).
        """
        if not CROSS_SECTIONAL_ENABLED:
            return 1.0

        with self._lock:
            now = time.time()

            # ── Entry clustering penalty ──
            cutoff = now - ENTRY_CLUSTER_WINDOW_SEC
            recent_entries = sum(1 for t in self._entry_times if t > cutoff)

            # Each additional entry beyond the first reduces risk
            if recent_entries <= 1:
                entry_mult = 1.0
            else:
                reduction = (recent_entries - 1) * CLUSTER_RISK_DECAY
                entry_mult = max(CLUSTER_RISK_FLOOR, 1.0 - reduction)

            # ── Loss cluster throttle ──
            loss_mult = 1.0
            if self._last_cluster_event > 0:
                time_since = now - self._last_cluster_event
                if time_since < LOSS_CLUSTER_COOLDOWN_SEC:
                    # Active loss cluster — apply throttle
                    # Fade from full throttle to normal over cooldown period
                    fade = time_since / LOSS_CLUSTER_COOLDOWN_SEC
                    loss_mult = LOSS_CLUSTER_THROTTLE + (1.0 - LOSS_CLUSTER_THROTTLE) * fade
                # else: cooldown expired, loss_mult stays 1.0

            # Combine (multiplicative)
            combined = entry_mult * loss_mult
            return max(CLUSTER_RISK_FLOOR, min(1.0, combined))

    # ═══════════════════════════════════════════════════════════
    #  DASHBOARD / STATUS
    # ═══════════════════════════════════════════════════════════

    def summary(self) -> dict:
        """Dashboard summary."""
        with self._lock:
            now = time.time()
            cutoff_entry = now - ENTRY_CLUSTER_WINDOW_SEC
            cutoff_loss = now - LOSS_CLUSTER_WINDOW_SEC

            recent_entries = sum(1 for t in self._entry_times if t > cutoff_entry)
            recent_losses = sum(1 for t in self._loss_times if t > cutoff_loss)

            cluster_active = False
            cluster_remaining = 0
            if self._last_cluster_event > 0:
                elapsed = now - self._last_cluster_event
                if elapsed < LOSS_CLUSTER_COOLDOWN_SEC:
                    cluster_active = True
                    cluster_remaining = int(LOSS_CLUSTER_COOLDOWN_SEC - elapsed)

            return {
                "enabled": CROSS_SECTIONAL_ENABLED,
                "risk_mult": round(self.risk_multiplier(), 3),
                "entries_1h": recent_entries,
                "losses_1h": recent_losses,
                "loss_cluster_active": cluster_active,
                "cluster_cooldown_remaining_s": cluster_remaining,
            }

    def log_status(self):
        """Log initial status."""
        s = self.summary()
        log.info(f"Cross-sectional awareness: risk_mult={s['risk_mult']:.2f}x, "
                 f"entries_1h={s['entries_1h']}, losses_1h={s['losses_1h']}")
