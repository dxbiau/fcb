"""
v13pro/signal_quality.py -- Adaptive Signal Quality Scoring Engine

Reads shadow outcome data on startup and periodically refreshes.
Computes per-(strategy, tf) statistics across multiple condition dimensions:
  - 1m confirmation direction
  - Sentiment state
  - Grade performance
  - OF alignment

Produces a quality_multiplier (0.5x to 2.0x) for position sizing.
No hardcoded rules — purely data-driven from shadow outcomes.
Self-corrects as more data accumulates.

Used by bot.py to adjust position sizing based on signal quality.
"""

import json
import glob
import os
import time
import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")

# Minimum sample size before we trust a bucket's statistics
MIN_SAMPLES = 8

# How often to reload shadow data (seconds)
REFRESH_INTERVAL = 600  # 10 minutes

# Scoring weights: how much each dimension contributes to quality
# These control the RANGE of the multiplier, not fixed rules
DIMENSION_WEIGHT = {
    "1m_direction": 0.30,   # strongest predictor per analysis
    "sentiment": 0.30,      # strong edge in many combos
    "grade_perf": 0.20,     # grade reliability varies
    "of_alignment": 0.20,   # orderflow alignment
}


class SignalQualityEngine:
    """
    Data-driven signal quality scoring.
    
    Reads shadow outcomes, computes conditional win rates per strategy/tf,
    and produces a quality multiplier for position sizing.
    
    No hardcoded strategy-specific rules.
    """

    def __init__(self):
        self._stats = {}          # (strat, tf) -> dimension stats
        self._global_wr = 0.40    # fallback baseline
        self._last_load = 0
        self._lock = threading.Lock()
        self._outcomes = []
        self.reload()

    def reload(self):
        """Load shadow outcomes and recompute all statistics."""
        try:
            outcomes = self._load_outcomes()
            if not outcomes:
                log.info("SignalQuality: no shadow data yet, using neutral scoring")
                return
            
            stats = self._compute_stats(outcomes)
            
            with self._lock:
                self._outcomes = outcomes
                self._stats = stats
                total = len(outcomes)
                wins = sum(1 for o in outcomes if o.get("pnl_r", 0) > 0)
                self._global_wr = wins / total if total > 0 else 0.40
                self._last_load = time.time()
            
            log.info(f"SignalQuality: loaded {len(outcomes)} outcomes, "
                     f"{len(stats)} strategy/tf combos, "
                     f"global WR={self._global_wr:.1%}")
        except Exception as e:
            log.warning(f"SignalQuality reload error: {e}")

    def _maybe_refresh(self):
        """Reload if stale."""
        if time.time() - self._last_load > REFRESH_INTERVAL:
            self.reload()

    def _load_outcomes(self):
        """Load all shadow outcome records."""
        rows = []
        for f in sorted(glob.glob(os.path.join(SHADOW_DIR, "shadow_*.jsonl"))):
            with open(f) as fh:
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
        return rows

    def _compute_stats(self, outcomes):
        """
        Compute conditional WR statistics per (strategy, tf) for each dimension.
        
        Returns: {(strat, tf): {dimension: {bucket: (wr, n, avg_r)}}}
        """
        # Group by strategy/tf
        by_strat_tf = defaultdict(list)
        for o in outcomes:
            by_strat_tf[(o["strategy"], o["tf"])].append(o)

        stats = {}
        for (strat, tf), outs in by_strat_tf.items():
            if len(outs) < MIN_SAMPLES:
                continue
            
            base_wr = self._calc_wr(outs)
            
            dim_stats = {
                "_base_wr": base_wr,
                "_n": len(outs),
            }
            
            # === Dimension 1: 1m confirmation direction ===
            dim_stats["1m_direction"] = self._compute_1m_buckets(outs, base_wr)
            
            # === Dimension 2: Sentiment state ===
            dim_stats["sentiment"] = self._compute_sentiment_buckets(outs, base_wr)
            
            # === Dimension 3: Grade performance ===
            dim_stats["grade_perf"] = self._compute_grade_buckets(outs, base_wr)
            
            # === Dimension 4: OF alignment ===
            dim_stats["of_alignment"] = self._compute_of_buckets(outs, base_wr)
            
            stats[(strat, tf)] = dim_stats
        
        return stats

    def _calc_wr(self, outcomes):
        """Win rate of outcomes."""
        if not outcomes:
            return 0.0
        wins = sum(1 for o in outcomes if o.get("pnl_r", 0) > 0)
        return wins / len(outcomes)

    def _calc_avg_r(self, outcomes):
        """Average pnl_r."""
        if not outcomes:
            return 0.0
        return sum(o.get("pnl_r", 0) for o in outcomes) / len(outcomes)

    def _compute_1m_buckets(self, outs, base_wr):
        """Split outcomes by 1m checkpoint direction."""
        buckets = defaultdict(list)
        for o in outs:
            cps = o.get("checkpoints", [])
            cp1 = None
            for cp in cps:
                if cp.get("minutes", 0) == 1:
                    cp1 = cp
                    break
            if cp1 is None:
                buckets["unknown"].append(o)
                continue
            mr = cp1.get("move_r", 0)
            if mr > 0:
                buckets["confirmed"].append(o)
            elif mr < -0.3:
                buckets["against"].append(o)
            else:
                buckets["neutral"].append(o)
        
        result = {}
        for bucket, items in buckets.items():
            if len(items) >= MIN_SAMPLES:
                wr = self._calc_wr(items)
                result[bucket] = {
                    "wr": wr,
                    "n": len(items),
                    "avg_r": self._calc_avg_r(items),
                    "edge": wr - base_wr,  # how much better/worse than baseline
                }
        return result

    def _compute_sentiment_buckets(self, outs, base_wr):
        """Split outcomes by sentiment state."""
        buckets = defaultdict(list)
        for o in outs:
            sent = o.get("sentiment", {})
            if not sent:
                buckets["unknown"].append(o)
                continue
            label = sent.get("label", "")
            if label:
                buckets[label.lower()].append(o)
            else:
                score = sent.get("score", 0)
                if score > 0.3:
                    buckets["bull"].append(o)
                elif score < -0.3:
                    buckets["bear"].append(o)
                else:
                    buckets["neutral"].append(o)
        
        result = {}
        for bucket, items in buckets.items():
            if len(items) >= MIN_SAMPLES:
                wr = self._calc_wr(items)
                result[bucket] = {
                    "wr": wr,
                    "n": len(items),
                    "avg_r": self._calc_avg_r(items),
                    "edge": wr - base_wr,
                }
        return result

    def _compute_grade_buckets(self, outs, base_wr):
        """Split outcomes by grade."""
        buckets = defaultdict(list)
        for o in outs:
            g = o.get("grade", "?")
            buckets[g].append(o)
        
        result = {}
        for bucket, items in buckets.items():
            if len(items) >= MIN_SAMPLES:
                wr = self._calc_wr(items)
                result[bucket] = {
                    "wr": wr,
                    "n": len(items),
                    "avg_r": self._calc_avg_r(items),
                    "edge": wr - base_wr,
                }
        return result

    def _compute_of_buckets(self, outs, base_wr):
        """Split outcomes by orderflow alignment."""
        buckets = defaultdict(list)
        for o in outs:
            of_snap = o.get("orderflow", {})
            if not of_snap:
                buckets["unknown"].append(o)
                continue
            imb = of_snap.get("imbalance_pct", 0)
            side = o.get("side", "long")
            if side == "long":
                if imb > 10:
                    buckets["aligned"].append(o)
                elif imb < -10:
                    buckets["against"].append(o)
                else:
                    buckets["neutral"].append(o)
            else:
                if imb < -10:
                    buckets["aligned"].append(o)
                elif imb > 10:
                    buckets["against"].append(o)
                else:
                    buckets["neutral"].append(o)
        
        result = {}
        for bucket, items in buckets.items():
            if len(items) >= MIN_SAMPLES:
                wr = self._calc_wr(items)
                result[bucket] = {
                    "wr": wr,
                    "n": len(items),
                    "avg_r": self._calc_avg_r(items),
                    "edge": wr - base_wr,
                }
        return result

    def score_signal(self, strategy: str, tf: str,
                     grade: str = "",
                     sentiment: dict = None,
                     orderflow: dict = None,
                     side: str = "long") -> Dict:
        """
        Score a signal's quality based on current conditions.
        
        Returns:
            {
                "quality_mult": float,  # 0.5 to 2.0 position sizing multiplier
                "quality_score": float, # -1.0 to +1.0 composite score
                "dimensions": dict,     # per-dimension breakdown
                "reason": str,          # human-readable summary
            }
        
        Logic:
            For each dimension, look up the current condition's bucket.
            If the bucket has an edge (WR above/below baseline), contribute
            proportionally to the quality score.
            Convert total score to a multiplier range [0.5, 2.0].
        """
        self._maybe_refresh()
        
        with self._lock:
            strat_stats = self._stats.get((strategy, tf))
        
        if not strat_stats:
            return {
                "quality_mult": 1.0,
                "quality_score": 0.0,
                "dimensions": {},
                "reason": "no data",
            }
        
        base_wr = strat_stats.get("_base_wr", self._global_wr)
        dimensions = {}
        total_score = 0.0
        reasons = []
        
        # === Dimension 1: Sentiment ===
        sent_label = self._classify_sentiment(sentiment)
        sent_stats = strat_stats.get("sentiment", {}).get(sent_label, {})
        if sent_stats:
            edge = sent_stats["edge"]
            weight = DIMENSION_WEIGHT["sentiment"]
            # Normalize edge to [-1, 1] range: edge of +-0.3 WR = full score
            normalized = max(-1.0, min(1.0, edge / 0.30))
            dim_score = normalized * weight
            total_score += dim_score
            dimensions["sentiment"] = {
                "bucket": sent_label,
                "edge": round(edge, 3),
                "wr": round(sent_stats["wr"], 3),
                "n": sent_stats["n"],
                "contribution": round(dim_score, 3),
            }
            if abs(edge) > 0.10:
                reasons.append(f"sent={sent_label}({sent_stats['wr']:.0%})")
        
        # === Dimension 2: Grade performance ===
        grade_stats = strat_stats.get("grade_perf", {}).get(grade, {})
        if grade_stats:
            edge = grade_stats["edge"]
            weight = DIMENSION_WEIGHT["grade_perf"]
            normalized = max(-1.0, min(1.0, edge / 0.30))
            dim_score = normalized * weight
            total_score += dim_score
            dimensions["grade"] = {
                "bucket": grade,
                "edge": round(edge, 3),
                "wr": round(grade_stats["wr"], 3),
                "n": grade_stats["n"],
                "contribution": round(dim_score, 3),
            }
            if abs(edge) > 0.10:
                reasons.append(f"grade={grade}({grade_stats['wr']:.0%})")
        
        # === Dimension 3: OF alignment ===
        of_label = self._classify_of(orderflow, side)
        of_stats = strat_stats.get("of_alignment", {}).get(of_label, {})
        if of_stats:
            edge = of_stats["edge"]
            weight = DIMENSION_WEIGHT["of_alignment"]
            normalized = max(-1.0, min(1.0, edge / 0.30))
            dim_score = normalized * weight
            total_score += dim_score
            dimensions["orderflow"] = {
                "bucket": of_label,
                "edge": round(edge, 3),
                "wr": round(of_stats["wr"], 3),
                "n": of_stats["n"],
                "contribution": round(dim_score, 3),
            }
            if abs(edge) > 0.10:
                reasons.append(f"of={of_label}({of_stats['wr']:.0%})")
        
        # Note: 1m_direction is POST-entry, cannot be used for pre-entry sizing.
        # It will be used by guardian for early exit decisions instead.
        
        # === Convert composite score to multiplier ===
        # score range: roughly [-1, +1]
        # mult range: [0.5, 2.0]
        # neutral = 1.0, maximum quality = 2.0, worst quality = 0.5
        quality_mult = 1.0 + total_score  # maps [-1,1] to [0, 2]
        quality_mult = max(0.5, min(2.0, quality_mult))
        
        reason = ", ".join(reasons) if reasons else "neutral"
        
        return {
            "quality_mult": round(quality_mult, 2),
            "quality_score": round(total_score, 3),
            "dimensions": dimensions,
            "reason": reason,
        }

    def get_1m_stats(self, strategy: str, tf: str) -> Dict:
        """
        Get 1m confirmation stats for a strategy/tf.
        
        Used by guardian to evaluate early exit based on 1m direction.
        Returns edge data for 'confirmed', 'neutral', 'against' buckets.
        """
        with self._lock:
            strat_stats = self._stats.get((strategy, tf), {})
        return strat_stats.get("1m_direction", {})

    def _classify_sentiment(self, sent: Optional[dict]) -> str:
        if not sent:
            return "unknown"
        label = sent.get("label", "") or sent.get("bias", "")
        if label:
            return label.lower()
        score = sent.get("score", 0) or sent.get("confidence", 0)
        if score > 0.3:
            return "bull"
        elif score < -0.3:
            return "bear"
        return "neutral"

    def _classify_of(self, of_snap: Optional[dict], side: str) -> str:
        if not of_snap:
            return "unknown"
        imb = of_snap.get("imbalance_pct", 0) or of_snap.get("imbalance", 0)
        if side == "long":
            if imb > 10:
                return "aligned"
            elif imb < -10:
                return "against"
        else:
            if imb < -10:
                return "aligned"
            elif imb > 10:
                return "against"
        return "neutral"

    def get_stats_summary(self) -> Dict:
        """Return summary for dashboard/logging."""
        with self._lock:
            return {
                "combos_tracked": len(self._stats),
                "total_outcomes": len(self._outcomes),
                "global_wr": round(self._global_wr, 3),
                "last_refresh": self._last_load,
            }
