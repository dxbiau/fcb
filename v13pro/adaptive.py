"""
v13pro/adaptive.py -- Adaptive Parameter Engine

ZERO hardcoded magic numbers for trading gates.
Every parameter is computed from rolling shadow outcome data.
Falls back to cfg defaults only when insufficient data (< MIN_SAMPLES).

Refreshes every REFRESH_INTERVAL seconds from shadow JSONL files.
All getters are O(1) lookups into a pre-computed cache.

Parameters made adaptive:
  1. OF block threshold   — from imbalance-vs-WR buckets
  2. OF boost / penalty   — proportional to measured OF edge
  3. MIN_KEY_LEVEL_SCORE  — from key_level score-vs-WR correlation
  4. CONVICTION_MULTIPLIER — from actual grade WR/ExpR
  5. DNA_BOOST_CAP        — from boosted-vs-unboosted WR delta
  6. Pair cooldown         — per-pair recovery speed from shadow
  7. Optimal TP_R          — per-strategy from peak_r percentiles
"""

import glob
import json
import math
import os
import time
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")
STATE_FILE = os.path.join(cfg.BASE_DIR, "adaptive_state.json")

# Minimum sample size before trusting data over defaults
MIN_SAMPLES = 15
# Minimum per-bucket sample size
MIN_BUCKET = 5
# How often to recalculate (seconds)
REFRESH_INTERVAL = 1200   # 20 minutes
# Smoothing: how much to move toward new value each refresh (0-1)
# Prevents wild swings — blends old and new values
EWMA_ALPHA = 0.3


class AdaptiveParams:
    """
    Data-driven parameter engine.

    Every getter computes from shadow outcomes. If insufficient data,
    falls back to the static config default. As more data accumulates,
    parameters converge to their optimal values.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._outcomes: List[dict] = []
        self._last_refresh = 0.0
        self._n_refreshes = 0

        # Cached adaptive values (populated by _recompute)
        self._cache: Dict[str, any] = {}

        # Smoothed values (EWMA to prevent jitter)
        self._smoothed: Dict[str, float] = {}

        # Load persisted smoothed state
        self._load_state()

        # Initial computation
        self.refresh()

    # ═══════════════════════════════════════════════════════════
    #  DATA LOADING
    # ═══════════════════════════════════════════════════════════

    def refresh(self):
        """Reload shadow outcomes and recompute all adaptive parameters."""
        try:
            outcomes = self._load_shadow_outcomes()
            with self._lock:
                self._outcomes = outcomes
                self._recompute()
                self._last_refresh = time.time()
                self._n_refreshes += 1
                self._save_state()

            if self._n_refreshes <= 1 and outcomes:
                log.info(f"Adaptive: loaded {len(outcomes)} shadow outcomes, "
                         f"computing parameters from data")
        except Exception as e:
            _log.warning(f"Adaptive refresh error: {e}")

    def maybe_refresh(self):
        """Refresh if stale — call this from the hot path."""
        if time.time() - self._last_refresh > REFRESH_INTERVAL:
            self.refresh()

    def _load_shadow_outcomes(self) -> List[dict]:
        """Load all shadow_outcome records from JSONL files."""
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

    # ═══════════════════════════════════════════════════════════
    #  CORE RECOMPUTATION
    # ═══════════════════════════════════════════════════════════

    def _recompute(self):
        """Recompute all adaptive parameters from shadow data."""
        data = self._outcomes
        if not data:
            return

        # Only use longs (we're in LONG_ONLY_MODE)
        longs = [o for o in data if o.get("side", "").lower() == "long"]

        # For gate-driving parameters (OF threshold, grade multipliers),
        # use PASSED-ONLY longs when LONG_ONLY_MODE is active.
        # Rejected longs were correctly rejected by the conviction pipeline
        # — learning from their hypothetical outcomes poisons the gates.
        # Example: rejected A+ have 72% WR which inflates baseline,
        # making passed A+ (58.5% WR) look average → grade inversion.
        # Example: 3451 rejected-unaligned longs (41.8% WR) dilute
        # 593 passed-unaligned (55.6% WR) → OF threshold doubles.
        if cfg.LONG_ONLY_MODE:
            passed_longs = [o for o in longs if o.get("passed", False)]
        else:
            passed_longs = longs

        n_skipped = len(longs) - len(passed_longs)
        if n_skipped > 0 and self._n_refreshes <= 1:
            log.info(f"Adaptive: using {len(passed_longs)} passed-longs "
                     f"(skipped {n_skipped} rejected for gate params)")

        self._compute_of_params(passed_longs)
        self._compute_key_level_params(passed_longs)
        self._compute_grade_multipliers(passed_longs)
        self._compute_dna_cap(passed_longs)
        self._compute_pair_cooldowns(passed_longs)
        self._compute_tp_r(passed_longs)

    # ─── 1. ORDER FLOW PARAMETERS ────────────────────────────

    def _compute_of_params(self, longs: List[dict]):
        """
        Compute OF block threshold, boost, and penalty from data.

        Method: bucket longs by OF imbalance, find the threshold where
        WR drops below break-even (~45%, accounting for fee drag).
        """
        # Separate into buckets by imbalance
        buckets = defaultdict(list)
        aligned_outcomes = []
        unaligned_outcomes = []

        for o in longs:
            of_data = o.get("orderflow", {})
            if not of_data:
                continue
            imb = of_data.get("imbalance")
            if imb is None:
                continue
            aligned = of_data.get("side_aligned", False)
            if aligned:
                aligned_outcomes.append(o)
            else:
                unaligned_outcomes.append(o)
                # Bucket by imbalance range (0.10 wide)
                bucket_key = round(math.floor(imb / 0.10) * 0.10, 2)
                buckets[bucket_key].append(o)

        # --- OF block threshold ---
        # Find the imbalance level where WR drops below 45%
        # Start from most negative and move toward 0
        block_threshold = cfg.OF_HARD_BLOCK_IMB  # default
        if len(unaligned_outcomes) >= MIN_SAMPLES:
            # Sort buckets from most negative to least
            sorted_buckets = sorted(buckets.items(), key=lambda x: x[0])
            # Walk from most negative: find where WR is consistently bad
            bad_below = None
            for imb_val, outs in sorted_buckets:
                if len(outs) < MIN_BUCKET:
                    continue
                wr = sum(1 for o in outs if o.get("pnl_r", 0) > 0) / len(outs)
                exp_r = sum(o.get("pnl_r", 0) for o in outs) / len(outs)
                if wr < 0.45 and exp_r < 0:
                    # This bucket is a loser zone
                    bad_below = abs(imb_val)
                else:
                    # Once we hit a profitable zone, stop
                    break

            if bad_below is not None:
                block_threshold = max(0.15, min(0.60, bad_below))

        self._smooth("of_block_threshold", block_threshold)

        # --- OF boost (aligned signals) ---
        # Compute how much better aligned signals perform
        of_boost = 8  # default
        if len(aligned_outcomes) >= MIN_SAMPLES and len(unaligned_outcomes) >= MIN_SAMPLES:
            aligned_wr = sum(1 for o in aligned_outcomes if o.get("pnl_r", 0) > 0) / len(aligned_outcomes)
            unaligned_wr = sum(1 for o in unaligned_outcomes if o.get("pnl_r", 0) > 0) / len(unaligned_outcomes)
            edge = aligned_wr - unaligned_wr
            # Scale boost: every 5% WR edge = +2 conviction
            of_boost = max(2, min(15, round(edge * 40)))

        self._smooth("of_boost", float(of_boost))

        # --- OF penalty (mildly unaligned) ---
        # Compute penalty for neutral-zone signals
        of_penalty = 3  # default
        if len(unaligned_outcomes) >= MIN_SAMPLES and len(aligned_outcomes) >= MIN_SAMPLES:
            # Neutral zone: not aligned but not strongly against
            neutral = [o for o in unaligned_outcomes
                       if abs(o.get("orderflow", {}).get("imbalance", 0)) < self._cache.get("of_block_threshold", cfg.OF_HARD_BLOCK_IMB)]
            if len(neutral) >= MIN_BUCKET:
                neutral_wr = sum(1 for o in neutral if o.get("pnl_r", 0) > 0) / len(neutral)
                aligned_wr = sum(1 for o in aligned_outcomes if o.get("pnl_r", 0) > 0) / len(aligned_outcomes)
                gap = aligned_wr - neutral_wr
                # Scale penalty: every 5% WR gap = +1 penalty
                of_penalty = max(1, min(8, round(gap * 20)))

        self._smooth("of_penalty", float(of_penalty))

    # ─── 2. KEY LEVEL MINIMUM ────────────────────────────────

    def _compute_key_level_params(self, longs: List[dict]):
        """
        Find the minimum key_level score where trades are still profitable.

        Method: bucket by key_level score, find lowest bucket with WR > 50%
        and positive ExpR.
        """
        buckets = defaultdict(list)
        for o in longs:
            bd = o.get("skill_breakdown", {})
            kl = bd.get("key_level")
            if kl is None:
                continue
            # Handle tuple format (score, detail_str)
            if isinstance(kl, (list, tuple)):
                score = kl[0]
            else:
                score = kl
            try:
                score = float(score)
            except (TypeError, ValueError):
                continue
            # Bucket by 5-point ranges
            bucket = int(score // 5) * 5
            buckets[bucket].append(o)

        min_kl = float(cfg.MIN_KEY_LEVEL_SCORE)  # default fallback
        if buckets:
            # Walk from lowest score up: find first profitable bucket
            for threshold in sorted(buckets.keys()):
                outs = buckets[threshold]
                if len(outs) < MIN_BUCKET:
                    continue
                wr = sum(1 for o in outs if o.get("pnl_r", 0) > 0) / len(outs)
                exp_r = sum(o.get("pnl_r", 0) for o in outs) / len(outs)
                if wr >= 0.50 and exp_r > -0.05:
                    # This bucket is profitable — set min to this score
                    min_kl = max(0, float(threshold))
                    break

        self._smooth("min_key_level_score", min_kl)

    # ─── 3. GRADE → RISK MULTIPLIER ─────────────────────────

    def _compute_grade_multipliers(self, longs: List[dict]):
        """
        Compute conviction multiplier per grade from actual performance.

        Method: for each grade, compute ExpR relative to overall baseline.
        Grades with higher ExpR deserve more risk allocation.
        """
        grade_buckets = defaultdict(list)
        for o in longs:
            g = o.get("grade", "?")
            if g in ("X", "?"):
                continue
            grade_buckets[g].append(o)

        all_passed = [o for o in longs if o.get("passed")]
        if len(all_passed) < MIN_SAMPLES:
            return

        baseline_exp = sum(o.get("pnl_r", 0) for o in all_passed) / len(all_passed) if all_passed else 0

        for grade in ["A+", "A", "B", "C", "D"]:
            outs = grade_buckets.get(grade, [])
            # Only adjust live trades (passed=True) for multiplier calc
            passed_outs = [o for o in outs if o.get("passed")]
            if len(passed_outs) < MIN_BUCKET:
                # Keep default when insufficient data
                continue

            wr = sum(1 for o in passed_outs if o.get("pnl_r", 0) > 0) / len(passed_outs)
            exp_r = sum(o.get("pnl_r", 0) for o in passed_outs) / len(passed_outs)

            # Scale multiplier relative to baseline:
            # If grade ExpR is 2x baseline → 1.5x multiplier
            # If grade ExpR is 0.5x baseline → 0.75x multiplier
            # If grade ExpR is negative → 0.5x multiplier
            if baseline_exp > 0.01:
                ratio = exp_r / baseline_exp
                mult = max(0.40, min(2.00, 0.50 + ratio * 0.50))
            elif exp_r > 0:
                # Baseline is near zero but this grade is positive
                mult = max(0.75, min(1.50, 1.0 + exp_r * 2.0))
            else:
                # Both baseline and this grade are negative/zero
                mult = max(0.40, min(1.0, 1.0 + exp_r))

            cache_key = f"conv_mult_{grade}"
            self._smooth(cache_key, mult)

    # ─── 4. DNA BOOST CAP ───────────────────────────────────

    def _compute_dna_cap(self, longs: List[dict]):
        """
        Compute max DNA boost from delta between boosted and unboosted WR.

        Method: compare signals that had DNA boost > 0 vs those without.
        If boost doesn't improve outcomes, cap it lower.
        """
        boosted = []
        unboosted = []
        for o in longs:
            if not o.get("passed"):
                continue
            ba = o.get("bayes_adjustment", 0)
            if ba > 0:
                boosted.append(o)
            else:
                unboosted.append(o)

        dna_cap = float(cfg.DNA_BOOST_CAP)  # default
        if len(boosted) >= MIN_SAMPLES and len(unboosted) >= MIN_SAMPLES:
            boosted_wr = sum(1 for o in boosted if o.get("pnl_r", 0) > 0) / len(boosted)
            unboosted_wr = sum(1 for o in unboosted if o.get("pnl_r", 0) > 0) / len(unboosted)
            boosted_exp = sum(o.get("pnl_r", 0) for o in boosted) / len(boosted)
            unboosted_exp = sum(o.get("pnl_r", 0) for o in unboosted) / len(unboosted)

            edge = boosted_exp - unboosted_exp
            # More edge from DNA → allow higher cap
            # Every +0.10 ExpR edge = +1 cap
            if edge > 0:
                dna_cap = max(3, min(12, 4 + round(edge * 10)))
            else:
                # DNA boost isn't helping — reduce cap
                dna_cap = max(2, min(6, 4 + round(edge * 10)))

        self._smooth("dna_boost_cap", dna_cap)

    # ─── 5. PER-PAIR COOLDOWN ───────────────────────────────

    def _compute_pair_cooldowns(self, longs: List[dict]):
        """
        Compute per-pair cooldown based on recovery speed.

        Method: after a loss, how many minutes until the next WIN for that pair?
        Pairs that recover fast → shorter cooldown.
        Pairs that take long → longer cooldown.
        """
        # Group by pair, sorted by timestamp
        by_pair = defaultdict(list)
        for o in longs:
            if not o.get("passed"):
                continue
            sym = o.get("symbol", "")
            ts = o.get("ts_ms", 0)
            by_pair[sym].append({
                "ts_ms": ts,
                "win": o.get("pnl_r", 0) > 0,
                "pnl_r": o.get("pnl_r", 0),
            })

        pair_cooldowns = {}
        for sym, trades in by_pair.items():
            if len(trades) < MIN_BUCKET:
                continue
            trades.sort(key=lambda x: x["ts_ms"])

            # Find recovery times: loss → next win (minutes)
            recovery_times = []
            for i, t in enumerate(trades):
                if not t["win"]:
                    # Look for next win
                    for j in range(i + 1, len(trades)):
                        if trades[j]["win"]:
                            dt_min = (trades[j]["ts_ms"] - t["ts_ms"]) / 60_000
                            if 0 < dt_min < 1440:  # sanity: under 24h
                                recovery_times.append(dt_min)
                            break

            if len(recovery_times) >= 3:
                # Use median recovery time as cooldown
                recovery_times.sort()
                median_recovery = recovery_times[len(recovery_times) // 2]
                # Clamp to reasonable range (10 min to 120 min)
                cooldown = max(10, min(120, median_recovery * 0.8))
                pair_cooldowns[sym] = cooldown

        with self._lock:
            self._cache["pair_cooldowns"] = pair_cooldowns

    # ─── 6. OPTIMAL TP_R PER STRATEGY ───────────────────────

    def _compute_tp_r(self, longs: List[dict]):
        """
        Compute optimal TP in R-multiples per strategy/tf.

        Method: use peak_r percentiles from shadow data.
        The 60th percentile of peak_r for winning trades gives us
        the TP that captures 60% of potential profits.
        """
        by_combo = defaultdict(list)
        for o in longs:
            if not o.get("passed"):
                continue
            strat = o.get("strategy", "")
            tf = o.get("tf", "")
            peak_r = o.get("peak_r", 0)
            pnl_r = o.get("pnl_r", 0)
            if peak_r > 0:  # only trades that went green
                by_combo[(strat, tf)].append(peak_r)

        optimal_tp = {}
        for (strat, tf), peaks in by_combo.items():
            if len(peaks) < MIN_SAMPLES:
                continue
            peaks.sort()
            # 60th percentile — captures good runners without being greedy
            idx = int(len(peaks) * 0.60)
            p60 = peaks[min(idx, len(peaks) - 1)]
            # Clamp to reasonable range (1.0R to 5.0R)
            tp = max(1.0, min(5.0, round(p60, 2)))
            optimal_tp[(strat, tf)] = tp

        with self._lock:
            self._cache["optimal_tp"] = optimal_tp

    # ═══════════════════════════════════════════════════════════
    #  EWMA SMOOTHING
    # ═══════════════════════════════════════════════════════════

    def _smooth(self, key: str, new_value: float):
        """Apply EWMA smoothing to prevent jittery parameter changes."""
        if key in self._smoothed:
            old = self._smoothed[key]
            smoothed = old * (1 - EWMA_ALPHA) + new_value * EWMA_ALPHA
        else:
            smoothed = new_value  # first value: no smoothing
        self._smoothed[key] = smoothed
        self._cache[key] = smoothed

    # ═══════════════════════════════════════════════════════════
    #  PUBLIC GETTERS — call these instead of cfg.* constants
    # ═══════════════════════════════════════════════════════════

    @property
    def of_block_threshold(self) -> float:
        """OF imbalance threshold to hard-block entry. Adaptive from shadow data."""
        with self._lock:
            return self._cache.get("of_block_threshold", cfg.OF_HARD_BLOCK_IMB)

    @property
    def of_boost(self) -> int:
        """Conviction boost for OF-aligned signals."""
        with self._lock:
            return round(self._cache.get("of_boost", 8))

    @property
    def of_penalty(self) -> int:
        """Conviction penalty for mildly unaligned OF."""
        with self._lock:
            return round(self._cache.get("of_penalty", 3))

    @property
    def min_key_level_score(self) -> float:
        """Minimum key level score to accept a trade."""
        with self._lock:
            return self._cache.get("min_key_level_score", float(cfg.MIN_KEY_LEVEL_SCORE))

    def conviction_multiplier(self, grade: str) -> float:
        """Risk multiplier for a given grade. Adaptive from actual grade performance."""
        with self._lock:
            key = f"conv_mult_{grade}"
            if key in self._cache:
                return round(self._cache[key], 3)
        # Fallback to static config
        return cfg.CONVICTION_MULTIPLIER.get(grade, 1.0)

    @property
    def dna_boost_cap(self) -> int:
        """Maximum DNA conviction boost."""
        with self._lock:
            return round(self._cache.get("dna_boost_cap", float(cfg.DNA_BOOST_CAP)))

    def pair_cooldown(self, symbol: str) -> float:
        """Per-pair cooldown in minutes. Falls back to cfg.PAIR_COOLDOWN_MINUTES."""
        with self._lock:
            pair_map = self._cache.get("pair_cooldowns", {})
            return pair_map.get(symbol, cfg.PAIR_COOLDOWN_MINUTES)

    def optimal_tp_r(self, strategy: str, tf: str) -> Optional[float]:
        """Optimal TP in R-multiples for this strategy/tf. None if insufficient data."""
        with self._lock:
            tp_map = self._cache.get("optimal_tp", {})
            return tp_map.get((strategy, tf))

    # ═══════════════════════════════════════════════════════════
    #  PERSISTENCE
    # ═══════════════════════════════════════════════════════════

    def _load_state(self):
        """Load persisted smoothed values."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._smoothed = {k: float(v) for k, v in data.get("smoothed", {}).items()
                                  if isinstance(v, (int, float))}
                self._cache = dict(self._smoothed)
                n = len(self._smoothed)
                if n:
                    log.info(f"Adaptive: restored {n} smoothed params from disk")
            except Exception as e:
                _log.warning(f"Adaptive: load state error: {e}")

    def _save_state(self):
        """Persist smoothed values for crash recovery."""
        try:
            data = {
                "smoothed": {k: round(v, 4) if isinstance(v, float) else v
                             for k, v in self._smoothed.items()},
                "refreshes": self._n_refreshes,
                "ts": time.time(),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    #  STATUS / LOGGING
    # ═══════════════════════════════════════════════════════════

    def log_status(self):
        """Log current adaptive parameters vs config defaults."""
        with self._lock:
            n = len(self._outcomes)

        of_t = self.of_block_threshold
        of_b = self.of_boost
        of_p = self.of_penalty
        kl = self.min_key_level_score
        dna = self.dna_boost_cap

        log.info(f"Adaptive ({n} outcomes, {self._n_refreshes} refreshes):")
        log.info(f"  OF: block={of_t:.2f} (cfg={cfg.OF_HARD_BLOCK_IMB}) "
                 f"boost=+{of_b} (cfg=8) penalty=-{of_p} (cfg=3)")
        log.info(f"  KL: min_score={kl:.1f} (cfg={cfg.MIN_KEY_LEVEL_SCORE})")
        log.info(f"  DNA: cap={dna} (cfg={cfg.DNA_BOOST_CAP})")

        for grade in ["A+", "A", "B", "C", "D"]:
            adaptive_m = self.conviction_multiplier(grade)
            static_m = cfg.CONVICTION_MULTIPLIER.get(grade, 1.0)
            marker = " *** " if abs(adaptive_m - static_m) > 0.05 else ""
            log.info(f"  Grade {grade}: mult={adaptive_m:.2f}x "
                     f"(cfg={static_m:.2f}x){marker}")

        # Show top pairs with custom cooldowns
        with self._lock:
            pair_cd = self._cache.get("pair_cooldowns", {})
        if pair_cd:
            sorted_cd = sorted(pair_cd.items(), key=lambda x: x[1])
            fast = sorted_cd[:3]
            slow = sorted_cd[-3:]
            fast_str = ", ".join(f"{p.replace('/USDT:USDT','')}={int(m)}m" for p, m in fast)
            slow_str = ", ".join(f"{p.replace('/USDT:USDT','')}={int(m)}m" for p, m in slow)
            log.info(f"  Cooldowns: fastest=[{fast_str}] "
                     f"slowest=[{slow_str}] "
                     f"(cfg default={cfg.PAIR_COOLDOWN_MINUTES}m)")

        # Show optimal TPs that differ from default
        with self._lock:
            tp_map = self._cache.get("optimal_tp", {})
        if tp_map:
            sorted_tp = sorted(tp_map.items(), key=lambda x: x[1], reverse=True)
            tp_str = ", ".join(f"{s}/{t}={r:.1f}R" for (s, t), r in sorted_tp[:5])
            log.info(f"  TP_R: top=[{tp_str}] "
                     f"(cfg default={cfg.TP_R}R)")

    def get_summary(self) -> dict:
        """Get summary dict for preflight/debug."""
        with self._lock:
            return {
                "outcomes": len(self._outcomes),
                "refreshes": self._n_refreshes,
                "of_block": self.of_block_threshold,
                "of_boost": self.of_boost,
                "of_penalty": self.of_penalty,
                "min_kl": self.min_key_level_score,
                "dna_cap": self.dna_boost_cap,
                "grade_mults": {g: self.conviction_multiplier(g)
                                for g in ["A+", "A", "B", "C", "D"]},
                "n_pair_cooldowns": len(self._cache.get("pair_cooldowns", {})),
                "n_optimal_tps": len(self._cache.get("optimal_tp", {})),
            }


def print_report():
    """CLI: Print adaptive parameter report."""
    ap = AdaptiveParams()
    print("\n" + "=" * 65)
    print("  ADAPTIVE PARAMETER REPORT")
    print("=" * 65)
    s = ap.get_summary()
    print(f"\n  Shadow outcomes: {s['outcomes']}")
    print(f"  Refreshes: {s['refreshes']}")
    print(f"\n  {'Parameter':<25} {'Adaptive':>10} {'Config':>10} {'Delta':>8}")
    print("  " + "-" * 55)

    rows = [
        ("OF block threshold", s["of_block"], cfg.OF_HARD_BLOCK_IMB),
        ("OF boost", s["of_boost"], 8),
        ("OF penalty", s["of_penalty"], 3),
        ("Min key level", s["min_kl"], cfg.MIN_KEY_LEVEL_SCORE),
        ("DNA boost cap", s["dna_cap"], cfg.DNA_BOOST_CAP),
    ]
    for label, adaptive, static in rows:
        delta = adaptive - static
        d_str = f"{delta:+.2f}" if delta != 0 else "="
        print(f"  {label:<25} {adaptive:>10.2f} {static:>10.2f} {d_str:>8}")

    print(f"\n  {'Grade':<10} {'Adaptive':>10} {'Config':>10}")
    print("  " + "-" * 32)
    for grade in ["A+", "A", "B", "C", "D"]:
        a = s["grade_mults"][grade]
        c = cfg.CONVICTION_MULTIPLIER.get(grade, 1.0)
        print(f"  {grade:<10} {a:>10.3f} {c:>10.3f}")

    print(f"\n  Per-pair cooldowns: {s['n_pair_cooldowns']} pairs customized "
          f"(default: {cfg.PAIR_COOLDOWN_MINUTES}m)")
    print(f"  Per-strategy TP_R: {s['n_optimal_tps']} combos optimized "
          f"(default: {cfg.TP_R}R)")

    # Show detail for pair cooldowns
    pair_cd = ap._cache.get("pair_cooldowns", {})
    if pair_cd:
        sorted_cd = sorted(pair_cd.items(), key=lambda x: x[1])
        print(f"\n  Per-pair cooldowns (adaptive):")
        for sym, mins in sorted_cd:
            short = sym.replace("/USDT:USDT", "")
            print(f"    {short:<12} {mins:>5.0f}m")

    # Show optimal TPs
    tp_map = ap._cache.get("optimal_tp", {})
    if tp_map:
        sorted_tp = sorted(tp_map.items(), key=lambda x: x[1], reverse=True)
        print(f"\n  Optimal TP_R per strategy (from peak_r P60):")
        for (strat, tf), tp_r in sorted_tp:
            print(f"    {strat}/{tf:<4}  {tp_r:.2f}R")


if __name__ == "__main__":
    print_report()
