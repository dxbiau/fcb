"""
v13pro/micro_tf.py  --  Micro-TF Intelligence Engine

Tracks 3m/5m shadow trades to build a real-time reliability picture
of the market. When micro signals cross-validate higher TF signals,
we boost confidence. When micro signals are failing, we know the
market is choppy and reduce exposure.

Key concepts:
  - Micro signals run shadow-only (never placed as live orders)
  - A rolling window tracks micro WR & ExpR per strategy
  - When a 15m/1h signal fires AND the same strategy recently won on 3m/5m,
    the higher TF signal gets a cross-TF confidence boost
  - The overall micro success rate acts as a market-quality barometer

Feeds into effective_risk as:
  - cross_tf_mult: 1.30x (validated), 1.0x (neutral), 0.70x (failing)
  - cross_tf_conviction_boost: +5 conviction when fully validated
"""

import time
import threading
import logging
from collections import deque
from typing import Dict, Tuple, Optional

log = logging.getLogger("v13pro")

# ── Config ─────────────────────────────────────────────────────────
# Micro TFs that this module monitors (shadow-only)
MICRO_TFS = {"3m", "5m"}

# Which TFs count as "macro" — micro signals validate these
MACRO_TFS = {"15m", "30m", "1h"}

# How micro maps to macro for cross-validation
# A 3m signal validates 15m; a 5m signal validates 15m/30m
MICRO_TO_MACRO = {
    "3m": {"15m"},
    "5m": {"15m", "30m"},
}

# Rolling window of micro outcomes per strategy
MICRO_WINDOW = 40             # last N micro outcomes per strategy
MIN_MICRO_TRADES = 8          # need at least this many to score

# Cross-TF validation freshness: micro signal must have fired within
# this many minutes for it to count as a live cross-validation
FRESHNESS_MINUTES = 60

# Thresholds for micro reliability
MICRO_HOT_WR = 0.58            # strategy is hot on micro if WR >= this
MICRO_HOT_EXPR = 0.10          # and ExpR >= this
MICRO_COLD_WR = 0.35           # strategy is cold on micro if WR <= this
MICRO_FAILING_WR = 0.28        # market is failing if overall micro WR <= this

# Risk multipliers
CROSS_TF_VALIDATED_MULT = 1.30   # micro + macro aligned → boost
CROSS_TF_NEUTRAL_MULT = 1.00    # no data or mixed
CROSS_TF_FAILING_MULT = 0.70    # micro failing → reduce exposure
CROSS_TF_CONVICTION_BOOST = 5   # conviction bonus when validated

# Market barometer thresholds
BAROMETER_WINDOW = 80            # overall micro trades for market quality
BAROMETER_HOT_WR = 0.55          # market is hot if overall micro WR >= this
BAROMETER_COLD_WR = 0.38         # market is cold if overall WR <= this

# Refresh interval (recompute stats)
REFRESH_INTERVAL = 120           # every 2 minutes


class MicroTFIntelligence:
    """
    Shadow-only micro-TF intelligence engine.

    Watches 3m/5m shadow outcomes to:
    1. Score per-strategy reliability on micro TFs
    2. Provide cross-TF validation when micro aligns with macro
    3. Act as a market-quality barometer (if micro is winning, market is clean)
    """

    def __init__(self):
        self._lock = threading.RLock()

        # Per-strategy micro outcomes: strategy -> deque of {pnl_r, ts, side, tf, peak_r}
        self._strategy_outcomes: Dict[str, deque] = {}

        # Per (strategy, micro_tf) → deque of recent signals with timestamps
        self._recent_signals: Dict[Tuple[str, str], deque] = {}

        # Overall micro outcomes (all strategies combined)
        self._all_outcomes: deque = deque(maxlen=BAROMETER_WINDOW)

        # Cached stats (recomputed every REFRESH_INTERVAL)
        self._strategy_stats: Dict[str, dict] = {}  # strategy → {wr, expr, n, label}
        self._barometer: dict = {"wr": 0.5, "n": 0, "label": "NEUTRAL", "expr": 0.0}
        self._last_refresh_ts = 0.0

        # Counters
        self._total_recorded = 0
        self._total_wins = 0
        self._total_losses = 0

    # ── Record micro outcomes (called by shadow._finalize) ──────────

    def record_outcome(self, strategy: str, tf: str, pnl_r: float,
                       peak_r: float, side: str, symbol: str):
        """Record a micro-TF shadow outcome. Only accepts 3m/5m TFs."""
        if tf not in MICRO_TFS:
            return

        with self._lock:
            # Per-strategy rolling window
            if strategy not in self._strategy_outcomes:
                self._strategy_outcomes[strategy] = deque(maxlen=MICRO_WINDOW)
            self._strategy_outcomes[strategy].append({
                "pnl_r": pnl_r,
                "peak_r": peak_r,
                "side": side,
                "tf": tf,
                "symbol": symbol,
                "ts": time.time(),
            })

            # Overall barometer
            self._all_outcomes.append({
                "pnl_r": pnl_r,
                "ts": time.time(),
                "strategy": strategy,
            })

            self._total_recorded += 1
            if pnl_r > 0:
                self._total_wins += 1
            else:
                self._total_losses += 1

    def record_signal(self, strategy: str, tf: str, side: str, symbol: str):
        """Record that a micro signal fired (for freshness tracking)."""
        if tf not in MICRO_TFS:
            return

        with self._lock:
            key = (strategy, tf)
            if key not in self._recent_signals:
                self._recent_signals[key] = deque(maxlen=20)
            self._recent_signals[key].append({
                "side": side,
                "symbol": symbol,
                "ts": time.time(),
            })

    # ── Cross-TF Validation Queries ─────────────────────────────────

    def cross_tf_multiplier(self, strategy: str, macro_tf: str,
                            side: str) -> float:
        """
        Get risk multiplier for a macro-TF signal based on micro validation.

        Returns:
            1.30 if micro strategy is HOT and recently fired in same direction
            1.00 if no data or neutral
            0.70 if micro strategy is COLD/failing
        """
        self._maybe_refresh()

        with self._lock:
            stats = self._strategy_stats.get(strategy)
            if not stats or stats["n"] < MIN_MICRO_TRADES:
                return CROSS_TF_NEUTRAL_MULT

            # Check if micro recently fired (freshness)
            has_fresh_signal = self._has_fresh_micro_signal(
                strategy, macro_tf, side)

            if stats["label"] == "HOT":
                if has_fresh_signal:
                    return CROSS_TF_VALIDATED_MULT  # 1.30 — full cross-TF boost
                else:
                    return 1.10  # Hot strategy but no recent micro signal
            elif stats["label"] == "COLD":
                return CROSS_TF_FAILING_MULT  # 0.70
            else:
                return CROSS_TF_NEUTRAL_MULT  # 1.00

    def cross_tf_conviction_boost(self, strategy: str, macro_tf: str,
                                  side: str) -> int:
        """
        Get conviction bonus when micro validates macro.

        Returns +5 if fully validated, 0 otherwise.
        """
        self._maybe_refresh()

        with self._lock:
            stats = self._strategy_stats.get(strategy)
            if not stats or stats["n"] < MIN_MICRO_TRADES:
                return 0

            if stats["label"] == "HOT" and self._has_fresh_micro_signal(
                    strategy, macro_tf, side):
                return CROSS_TF_CONVICTION_BOOST

            return 0

    def market_barometer(self) -> dict:
        """
        Get overall micro-TF market quality barometer.

        Returns dict with:
            wr: overall micro win rate (0-1)
            n: number of micro outcomes in window
            label: HOT | NEUTRAL | COLD
            mult: market quality multiplier (0.85, 1.0, or 1.10)
        """
        self._maybe_refresh()

        with self._lock:
            baro = dict(self._barometer)

        if baro["label"] == "HOT":
            baro["mult"] = 1.10
        elif baro["label"] == "COLD":
            baro["mult"] = 0.85
        else:
            baro["mult"] = 1.00

        return baro

    def is_micro_validated(self, strategy: str, macro_tf: str,
                           side: str) -> bool:
        """
        Check if a macro signal has full micro cross-validation.

        True when: strategy is HOT on micro AND recently fired in
        the same direction on a micro TF that maps to this macro TF.
        """
        self._maybe_refresh()

        with self._lock:
            stats = self._strategy_stats.get(strategy)
            if not stats or stats["n"] < MIN_MICRO_TRADES:
                return False

            return (stats["label"] == "HOT" and
                    self._has_fresh_micro_signal(strategy, macro_tf, side))

    # ── Strategy Reliability Scores ─────────────────────────────────

    def strategy_reliability(self, strategy: str) -> Optional[dict]:
        """
        Get micro-TF reliability score for a strategy.

        Returns None if insufficient data, else:
            {wr, expr, n, label, hot_since}
        """
        self._maybe_refresh()
        with self._lock:
            return self._strategy_stats.get(strategy)

    def all_strategy_stats(self) -> Dict[str, dict]:
        """Get reliability stats for ALL strategies tracked on micro."""
        self._maybe_refresh()
        with self._lock:
            return dict(self._strategy_stats)

    # ── Internal ────────────────────────────────────────────────────

    def _has_fresh_micro_signal(self, strategy: str, macro_tf: str,
                                side: str) -> bool:
        """
        Check if this strategy recently fired on a micro TF that
        maps to the given macro TF, in the SAME side direction.

        Must hold _lock.
        """
        cutoff = time.time() - FRESHNESS_MINUTES * 60

        for micro_tf, macro_set in MICRO_TO_MACRO.items():
            if macro_tf not in macro_set:
                continue

            key = (strategy, micro_tf)
            signals = self._recent_signals.get(key)
            if not signals:
                continue

            # Check for any recent signal in the same direction
            for sig in reversed(signals):
                if sig["ts"] < cutoff:
                    break
                if sig["side"] == side:
                    return True

        return False

    def _maybe_refresh(self):
        """Recompute cached stats if stale."""
        now = time.time()
        if now - self._last_refresh_ts < REFRESH_INTERVAL:
            return

        with self._lock:
            # Double-check inside lock
            if now - self._last_refresh_ts < REFRESH_INTERVAL:
                return

            self._refresh_stats()
            self._last_refresh_ts = now

    def _refresh_stats(self):
        """Recompute all cached stats. Must hold _lock."""
        # Per-strategy stats
        self._strategy_stats.clear()
        for strat, outcomes in self._strategy_outcomes.items():
            if not outcomes:
                continue

            n = len(outcomes)
            wins = sum(1 for o in outcomes if o["pnl_r"] > 0)
            total_r = sum(o["pnl_r"] for o in outcomes)
            avg_peak = sum(o["peak_r"] for o in outcomes) / n if n else 0

            wr = wins / n if n else 0
            expr = total_r / n if n else 0

            if n >= MIN_MICRO_TRADES:
                if wr >= MICRO_HOT_WR and expr >= MICRO_HOT_EXPR:
                    label = "HOT"
                elif wr <= MICRO_COLD_WR:
                    label = "COLD"
                else:
                    label = "WARM"
            else:
                label = "BUILDING"

            self._strategy_stats[strat] = {
                "wr": round(wr, 3),
                "expr": round(expr, 3),
                "n": n,
                "label": label,
                "avg_peak": round(avg_peak, 3),
            }

        # Overall barometer
        all_out = list(self._all_outcomes)
        if all_out:
            n = len(all_out)
            wins = sum(1 for o in all_out if o["pnl_r"] > 0)
            total_r = sum(o["pnl_r"] for o in all_out)
            wr = wins / n if n else 0
            expr = total_r / n if n else 0

            if n >= MIN_MICRO_TRADES:
                if wr >= BAROMETER_HOT_WR:
                    label = "HOT"
                elif wr <= BAROMETER_COLD_WR:
                    label = "COLD"
                else:
                    label = "NEUTRAL"
            else:
                label = "BUILDING"

            self._barometer = {
                "wr": round(wr, 3),
                "expr": round(expr, 3),
                "n": n,
                "label": label,
            }

    # ── Dashboard / Summary ─────────────────────────────────────────

    def summary(self) -> dict:
        """Summary for dashboard display."""
        self._maybe_refresh()

        with self._lock:
            hot_strats = [s for s, st in self._strategy_stats.items()
                          if st["label"] == "HOT"]
            cold_strats = [s for s, st in self._strategy_stats.items()
                           if st["label"] == "COLD"]
            warm_strats = [s for s, st in self._strategy_stats.items()
                           if st["label"] == "WARM"]

            strat_detail = {}
            for s, st in self._strategy_stats.items():
                strat_detail[s] = (f"{st['label']} {st['wr']*100:.0f}%WR "
                                   f"{st['expr']:+.2f}R N={st['n']}")

            return {
                "barometer": dict(self._barometer),
                "total_recorded": self._total_recorded,
                "total_wins": self._total_wins,
                "total_losses": self._total_losses,
                "hot_strategies": hot_strats,
                "cold_strategies": cold_strats,
                "warm_strategies": warm_strats,
                "strategy_detail": strat_detail,
                "strategies_tracked": len(self._strategy_stats),
            }

    def log_status(self):
        """Log current state."""
        s = self.summary()
        baro = s["barometer"]
        log.info(f"[MicroTF] Barometer: {baro.get('label','?')} "
                 f"({baro.get('wr',0)*100:.0f}%WR, N={baro.get('n',0)}) | "
                 f"HOT: {s['hot_strategies']} | "
                 f"COLD: {s['cold_strategies']} | "
                 f"Tracked: {s['total_recorded']} outcomes")
