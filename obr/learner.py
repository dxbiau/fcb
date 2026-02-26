"""
obr/learner.py -- Bayesian Feature Learner + Pair DNA Profiler

Pure-Python Bayesian system that learns which setup features predict
wins vs losses.  Uses Beta-distribution conjugate priors (the simplest
Bayesian model -- zero dependencies, mathematically optimal for
binary outcomes with small samples).

How it works:
  For every trade, ~15 discrete categorical features are extracted from
  the scored setup (body-ratio tier, volume tier, trend state, key-level
  type, session, direction, hour block, etc.).

  Each (feature_name, feature_value) pair tracks a Beta(α, β) posterior:
    - α starts at 1  (prior pseudocount for wins)
    - β starts at 1  (prior pseudocount for losses)
    - On win:  α += 1
    - On loss: β += 1
    - Posterior win probability = α / (α + β)
    - Edge = posterior - 0.5  (positive = better-than-random)
    - Confidence = α + β - 2  (total real observations)

  A conviction adjustment (+/- up to 15 pts) is computed from the
  combined edge of the setup's active features, weighted by their
  confidence.  This lets the system learn "volume spikes predict wins"
  or "counter-trend fades lose money" from actual data.

PairDNA tracks per-symbol performance:
  - Beta(α, β) for each pair's win probability
  - Running PnL, streak, feature affinity
  - Hot/Warm/Cold/Unknown classification for pair priority

Persistence:
  Saves to obr/logs/learner_memory.json every update.
  Crash-safe: loads on boot, merges defaults for new fields.

No numpy/pandas/scipy -- pure Python stdlib + math.
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from obr import config as cfg
from obr import logger as log


# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════

LEARNER_FILE = os.path.join(cfg.LOG_DIR, "learner_memory.json")

# Adjustment bounds: how much the learner can shift the base score
MAX_BONUS = 12.0       # max upward adjustment from learned features
MAX_PENALTY = -10.0     # max downward penalty
MIN_CONFIDENCE = 3      # min observations before a feature influences scoring

# Pair DNA thresholds
PAIR_HOT_WR = 0.58      # posterior WR > 58% = hot
PAIR_COLD_WR = 0.38     # posterior WR < 38% = cold
PAIR_MIN_TRADES = 3     # min trades before hot/cold classification

# Session boundaries (UTC hours)
SESSIONS = {
    "asia":    (0, 8),    # 00:00 - 08:00 UTC
    "london":  (8, 13),   # 08:00 - 13:00 UTC
    "overlap": (13, 17),  # 13:00 - 17:00 UTC (London+NY)
    "ny":      (17, 22),  # 17:00 - 22:00 UTC
    "late":    (22, 24),  # 22:00 - 00:00 UTC
}

# Hour blocks
HOUR_BLOCKS = {
    0: "h00_03", 1: "h00_03", 2: "h00_03", 3: "h00_03",
    4: "h04_07", 5: "h04_07", 6: "h04_07", 7: "h04_07",
    8: "h08_11", 9: "h08_11", 10: "h08_11", 11: "h08_11",
    12: "h12_15", 13: "h12_15", 14: "h12_15", 15: "h12_15",
    16: "h16_19", 17: "h16_19", 18: "h16_19", 19: "h16_19",
    20: "h20_23", 21: "h20_23", 22: "h20_23", 23: "h20_23",
}

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ═══════════════════════════════════════════════════════════════
#  FEATURE EXTRACTION
# ═══════════════════════════════════════════════════════════════

def _classify_body_ratio(ob_candle: dict) -> str:
    """Classify OB candle body/range ratio into tier."""
    high = ob_candle.get("high", 0)
    low = ob_candle.get("low", 0)
    opn = ob_candle.get("open", 0)
    close = ob_candle.get("close", 0)
    rng = high - low
    if rng <= 0:
        return "zero"
    body = abs(close - opn)
    ratio = body / rng
    if ratio >= 0.80:
        return "huge"       # >80% body
    elif ratio >= 0.60:
        return "large"      # 60-80%
    elif ratio >= 0.40:
        return "medium"     # 40-60%
    elif ratio >= 0.20:
        return "small"      # 20-40%
    else:
        return "tiny"       # <20% (doji-like)


def _classify_ob_range(ob_candle: dict, candles: List[dict]) -> str:
    """Classify OB range relative to recent ATR."""
    high = ob_candle.get("high", 0)
    low = ob_candle.get("low", 0)
    ob_range = high - low
    if ob_range <= 0:
        return "zero"

    # ATR from recent candles
    if len(candles) < 5:
        return "unknown"
    recent = candles[-14:] if len(candles) >= 14 else candles
    atr = sum(c["high"] - c["low"] for c in recent) / len(recent)
    if atr <= 0:
        return "unknown"

    ratio = ob_range / atr
    if ratio >= 2.5:
        return "huge"       # 2.5x+ ATR
    elif ratio >= 1.5:
        return "large"      # 1.5-2.5x ATR
    elif ratio >= 0.8:
        return "medium"     # 0.8-1.5x ATR
    else:
        return "small"      # <0.8x ATR


def _classify_volume(ob_candle: dict, candles: List[dict]) -> str:
    """Classify volume spike tier."""
    vol = ob_candle.get("volume", 0)
    if vol <= 0 or len(candles) < 5:
        return "unknown"
    recent = candles[-20:] if len(candles) >= 20 else candles
    avg_vol = sum(c.get("volume", 0) for c in recent) / len(recent)
    if avg_vol <= 0:
        return "unknown"
    ratio = vol / avg_vol
    if ratio >= 3.0:
        return "spike"      # 3x+ average
    elif ratio >= 1.8:
        return "high"       # 1.8-3x
    elif ratio >= 1.0:
        return "avg"        # 1-1.8x
    else:
        return "low"        # below average


def _classify_trend(breakdown: dict) -> str:
    """Extract trend state from score breakdown."""
    trend_info = breakdown.get("trend", ("", ""))
    detail = trend_info[1] if len(trend_info) > 1 else ""
    if "with_trend" in detail:
        return "with_trend"
    elif "counter_trend" in detail:
        return "counter_trend"
    elif "flat" in detail:
        return "flat"
    return "neutral"


def _classify_fee(fee_r: float) -> str:
    """Classify fee efficiency."""
    if fee_r <= 0.08:
        return "great"
    elif fee_r <= 0.15:
        return "good"
    elif fee_r <= 0.25:
        return "fair"
    else:
        return "poor"


def _classify_key_level(level_info: dict) -> Tuple[str, str]:
    """
    Extract key-level type and proximity tier from skill's level_info.

    level_info structure:
      { "levels": [float, ...],
        "nearest_dist_pct": float,
        "nearest_level": float,
        "level_types": {"swing": [...], "pivot": [...], "round": [...],
                        "session": [...], "vwap": [...]},
        "level_count_nearby": int }

    Returns (level_type, proximity_tier).
    """
    nearest = level_info.get("nearest_level", 0)
    distance_pct = level_info.get("nearest_dist_pct", 999)
    level_types = level_info.get("level_types", {})

    if distance_pct > 1.0 or nearest == 0:
        return "none", "far"

    # Determine what type of level the nearest one is
    ltype = "unknown"
    min_dist = float("inf")
    for lt_name, lt_levels in level_types.items():
        for lv in lt_levels:
            d = abs(lv - nearest)
            if d < min_dist:
                min_dist = d
                ltype = lt_name

    # Proximity tier
    if distance_pct <= 0.05:
        prox = "touching"
    elif distance_pct <= 0.15:
        prox = "near"
    elif distance_pct <= 0.25:
        prox = "moderate"
    else:
        prox = "far"

    return ltype, prox


def _classify_price_magnitude(price: float) -> str:
    """Classify price into magnitude bucket."""
    if price <= 0.01:
        return "micro"       # sub-penny
    elif price <= 0.10:
        return "sub_dime"
    elif price <= 1.0:
        return "sub_dollar"
    elif price <= 10.0:
        return "single_digit"
    elif price <= 100.0:
        return "tens"
    elif price <= 1000.0:
        return "hundreds"
    else:
        return "thousands"


def _classify_confirm_strength(confirm_candle: dict, ob_candle: dict,
                                direction: str) -> str:
    """Classify confirmation candle strength."""
    c_close = confirm_candle.get("close", 0)
    c_open = confirm_candle.get("open", 0)
    c_range = confirm_candle.get("high", 0) - confirm_candle.get("low", 0)
    ob_range = ob_candle.get("high", 0) - ob_candle.get("low", 0)

    if c_range <= 0 or ob_range <= 0:
        return "unknown"

    c_body = abs(c_close - c_open)
    body_ratio = c_body / c_range

    # Check direction alignment
    if direction == "long":
        aligned = c_close > c_open   # bullish close for long
    else:
        aligned = c_close < c_open   # bearish close for short

    if not aligned:
        return "weak"

    size_ratio = c_range / ob_range
    if body_ratio >= 0.6 and size_ratio >= 0.5:
        return "strong"
    elif body_ratio >= 0.4:
        return "moderate"
    else:
        return "weak"


def _get_session(utc_hour: int) -> str:
    """Get trading session from UTC hour."""
    for session, (start, end) in SESSIONS.items():
        if start <= utc_hour < end:
            return session
    return "late"


def extract_features(
    ob_candle: dict,
    prev_candle: dict,
    confirm_candle: dict,
    direction: str,
    fee_r: float,
    current_price: float,
    candles: List[dict],
    breakdown: dict,
    level_info: dict,
    symbol: str = "",
    market_regime: str = "unknown",
    equity_phase: str = "unknown",
    drawdown_zone: str = "normal",
) -> Dict[str, str]:
    """
    Extract ~18 discrete categorical features from a scored OBR setup.

    Mod 9: Added market_regime, equity_phase, drawdown_zone features.

    Returns dict mapping feature_name → feature_value (all strings).
    """
    now = datetime.now(timezone.utc)

    # Key level features
    kl_type, kl_prox = _classify_key_level(level_info)

    features = {
        "direction":         direction,
        "body_ratio":        _classify_body_ratio(ob_candle),
        "ob_range":          _classify_ob_range(ob_candle, candles),
        "volume":            _classify_volume(ob_candle, candles),
        "trend":             _classify_trend(breakdown),
        "fee_tier":          _classify_fee(fee_r),
        "key_level_type":    kl_type,
        "key_level_prox":    kl_prox,
        "price_magnitude":   _classify_price_magnitude(current_price),
        "confirm_strength":  _classify_confirm_strength(confirm_candle,
                                                        ob_candle, direction),
        "session":           _get_session(now.hour),
        "hour_block":        HOUR_BLOCKS.get(now.hour, "h00_03"),
        "day":               DAYS[now.weekday()],
    }

    # Composite features (interactions)
    features["trend_x_dir"] = f"{features['trend']}_{direction}"
    features["session_x_dir"] = f"{features['session']}_{direction}"

    # Mod 9: x1000 contextual features
    features["market_regime"] = str(market_regime)
    features["equity_phase"] = str(equity_phase)
    features["drawdown_zone"] = str(drawdown_zone)

    return features


# ═══════════════════════════════════════════════════════════════
#  BAYESIAN TRACKER  (Beta-distribution conjugate model)
# ═══════════════════════════════════════════════════════════════

class BetaTracker:
    """
    Tracks a single Beta(α, β) posterior for one (feature, value) pair.
    Pure Python -- no scipy needed.

    Beta distribution is conjugate prior for Bernoulli likelihood:
      Prior:     Beta(α₀, β₀)   -- default (1, 1) = uniform
      Win:       α += 1
      Loss:      β += 1
      Posterior mean: α / (α + β)   = estimated win probability
      Edge:     posterior - 0.5     (positive = better than random)
      Confidence: α + β - 2        (real observation count)
    """

    __slots__ = ("alpha", "beta")

    def __init__(self, alpha: float = 1.0, beta: float = 1.0):
        self.alpha = alpha
        self.beta = beta

    def update(self, is_win: bool):
        if is_win:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    @property
    def posterior(self) -> float:
        """Estimated win probability."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def edge(self) -> float:
        """Edge over random (0.5). Positive = better than coin flip."""
        return self.posterior - 0.5

    @property
    def confidence(self) -> int:
        """Number of real observations (excluding prior pseudo-counts)."""
        return int(self.alpha + self.beta - 2)

    @property
    def variance(self) -> float:
        """Posterior variance -- shrinks as evidence grows."""
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    def to_dict(self) -> dict:
        return {"a": round(self.alpha, 2), "b": round(self.beta, 2)}

    @classmethod
    def from_dict(cls, d: dict) -> "BetaTracker":
        return cls(alpha=d.get("a", 1.0), beta=d.get("b", 1.0))


# ═══════════════════════════════════════════════════════════════
#  PAIR DNA  (per-symbol behaviour profile)
# ═══════════════════════════════════════════════════════════════

class PairDNA:
    """Per-symbol performance tracker with Bayesian win probability."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.tracker = BetaTracker()
        self.total_r: float = 0.0
        self.streak: int = 0         # positive = win streak, negative = loss streak
        self.best_r: float = 0.0
        self.worst_r: float = 0.0
        self.last_trade_ts: str = ""
        self.feature_affinity: Dict[str, Dict[str, int]] = {}  # feature→value→win_count

    def update(self, is_win: bool, pnl_r: float,
               features: Dict[str, str]):
        """Record a trade outcome."""
        self.tracker.update(is_win)
        self.total_r += pnl_r
        self.best_r = max(self.best_r, pnl_r)
        self.worst_r = min(self.worst_r, pnl_r)
        self.last_trade_ts = datetime.now(timezone.utc).isoformat()

        # Streak
        if is_win:
            self.streak = max(0, self.streak) + 1
        else:
            self.streak = min(0, self.streak) - 1

        # Feature affinity: track which feature values win for this pair
        if is_win:
            for fname, fval in features.items():
                if fname not in self.feature_affinity:
                    self.feature_affinity[fname] = {}
                self.feature_affinity[fname][fval] = (
                    self.feature_affinity[fname].get(fval, 0) + 1
                )

    @property
    def status(self) -> str:
        """Classify pair: hot / warm / cold / unknown."""
        n = self.tracker.confidence
        if n < PAIR_MIN_TRADES:
            return "unknown"
        wr = self.tracker.posterior
        if wr >= PAIR_HOT_WR:
            return "hot"
        elif wr <= PAIR_COLD_WR:
            return "cold"
        else:
            return "warm"

    @property
    def wins(self) -> int:
        return int(self.tracker.alpha - 1)

    @property
    def losses(self) -> int:
        return int(self.tracker.beta - 1)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "tracker": self.tracker.to_dict(),
            "total_r": round(self.total_r, 4),
            "streak": self.streak,
            "best_r": round(self.best_r, 4),
            "worst_r": round(self.worst_r, 4),
            "last_trade": self.last_trade_ts,
            "affinity": self.feature_affinity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PairDNA":
        p = cls(d["symbol"])
        p.tracker = BetaTracker.from_dict(d.get("tracker", {}))
        p.total_r = d.get("total_r", 0.0)
        p.streak = d.get("streak", 0)
        p.best_r = d.get("best_r", 0.0)
        p.worst_r = d.get("worst_r", 0.0)
        p.last_trade_ts = d.get("last_trade", "")
        p.feature_affinity = d.get("affinity", {})
        return p


# ═══════════════════════════════════════════════════════════════
#  BAYESIAN LEARNER  (main class)
# ═══════════════════════════════════════════════════════════════

class BayesianLearner:
    """
    Adaptive learning engine using Beta-distribution Bayesian inference.

    Maintains:
      - Feature trackers: Beta(α,β) for each (feature_name, feature_value)
      - Pair DNA: per-symbol performance profile
      - Pending contexts: feature snapshots awaiting outcome

    Integration with PerformanceSkill:
      1. On evaluate: extract_features → compute_adjustment → adjust score
      2. On outcome: update trackers with win/loss
      3. Pair DNA informs hunter priority (hot pairs get preference)

    Thread-safe, persisted to disk.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._feature_trackers: Dict[str, Dict[str, BetaTracker]] = {}
        self._pair_dna: Dict[str, PairDNA] = {}
        self._pending: Dict[str, Dict[str, str]] = {}  # symbol → features
        self._hydrate()

    # ─── Persistence ─────────────────────────────────────────

    def _default_data(self) -> dict:
        return {
            "feature_trackers": {},  # {fname: {fval: {a, b}}}
            "pair_dna": {},          # {symbol: PairDNA.to_dict()}
            "total_updates": 0,
            "created": datetime.now(timezone.utc).isoformat(),
            "last_update": None,
            "version": 1,
        }

    def _load(self) -> dict:
        """Load learner state from disk."""
        default = self._default_data()
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            if os.path.exists(LEARNER_FILE):
                with open(LEARNER_FILE, "r") as f:
                    data = json.load(f)
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            log.debug(f"BayesianLearner: fresh state (load error: {e})")
        return default

    def _save(self):
        """Persist learner state to disk."""
        try:
            # Serialize current state
            data = {
                "feature_trackers": {},
                "pair_dna": {},
                "total_updates": self._data.get("total_updates", 0),
                "created": self._data.get("created",
                                          datetime.now(timezone.utc).isoformat()),
                "last_update": datetime.now(timezone.utc).isoformat(),
                "version": 1,
            }
            for fname, values in self._feature_trackers.items():
                data["feature_trackers"][fname] = {
                    fval: bt.to_dict() for fval, bt in values.items()
                }
            for symbol, dna in self._pair_dna.items():
                data["pair_dna"][symbol] = dna.to_dict()

            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            with open(LEARNER_FILE, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"BayesianLearner: save error: {e}")

    def _hydrate(self):
        """Reconstruct in-memory objects from loaded data."""
        # Feature trackers
        for fname, values in self._data.get("feature_trackers", {}).items():
            self._feature_trackers[fname] = {}
            for fval, bt_dict in values.items():
                self._feature_trackers[fname][fval] = (
                    BetaTracker.from_dict(bt_dict)
                )
        # Pair DNA
        for symbol, dna_dict in self._data.get("pair_dna", {}).items():
            self._pair_dna[symbol] = PairDNA.from_dict(dna_dict)

    # ─── Feature-based scoring adjustment ────────────────────

    def compute_adjustment(self, features: Dict[str, str]) -> float:
        """
        Compute a conviction adjustment from learned feature edges.

        For each feature in the setup:
          - Look up its Beta tracker
          - If confidence >= MIN_CONFIDENCE, compute weighted edge
          - Weight = min(confidence, 20) / 20  (saturates at 20 obs)
          - Weighted edge = edge * weight
        
        Sum all weighted edges, scale to [-MAX_PENALTY, +MAX_BONUS].

        Returns:
            float adjustment to add to base conviction score.
        """
        with self._lock:
            if not self._feature_trackers:
                return 0.0

            total_weighted_edge = 0.0
            contributing_features = 0

            for fname, fval in features.items():
                tracker = self._feature_trackers.get(fname, {}).get(fval)
                if tracker is None:
                    continue
                if tracker.confidence < MIN_CONFIDENCE:
                    continue

                # Weight saturates at 20 observations
                weight = min(tracker.confidence, 20) / 20.0
                total_weighted_edge += tracker.edge * weight
                contributing_features += 1

            if contributing_features == 0:
                return 0.0

            # Normalize: average weighted edge per contributing feature
            avg_edge = total_weighted_edge / contributing_features

            # Scale: map [-0.5, +0.5] avg_edge → [MAX_PENALTY, MAX_BONUS]
            # With realistic data, avg_edge rarely exceeds ±0.2
            # Use 0.25 as full-scale reference
            scale_factor = (MAX_BONUS - MAX_PENALTY) / 0.50  # = 44
            adjustment = avg_edge * scale_factor

            # Clamp
            adjustment = max(MAX_PENALTY, min(MAX_BONUS, adjustment))

            return round(adjustment, 2)

    # ─── Pair DNA queries ────────────────────────────────────

    def get_pair_status(self, symbol: str) -> dict:
        """
        Get pair DNA status.
        Returns {status, wr, trades, total_r, streak}.
        """
        with self._lock:
            dna = self._pair_dna.get(symbol)
            if dna is None:
                return {
                    "status": "unknown", "wr": 0.5,
                    "trades": 0, "total_r": 0.0, "streak": 0,
                }
            return {
                "status": dna.status,
                "wr": round(dna.tracker.posterior, 3),
                "trades": dna.tracker.confidence,
                "total_r": round(dna.total_r, 4),
                "streak": dna.streak,
            }

    def get_pair_adjustment(self, symbol: str) -> float:
        """
        Small conviction bonus/penalty based on pair DNA.
        Hot pairs: +3 pts, Cold pairs: -5 pts.
        """
        with self._lock:
            dna = self._pair_dna.get(symbol)
            if dna is None or dna.tracker.confidence < PAIR_MIN_TRADES:
                return 0.0
            status = dna.status
            if status == "hot":
                return 3.0
            elif status == "cold":
                return -5.0
            return 0.0

    def get_hot_pairs(self) -> List[str]:
        """Return list of symbols classified as 'hot'."""
        with self._lock:
            return [s for s, d in self._pair_dna.items()
                    if d.status == "hot"]

    def get_cold_pairs(self) -> List[str]:
        """Return list of symbols classified as 'cold'."""
        with self._lock:
            return [s for s, d in self._pair_dna.items()
                    if d.status == "cold"]

    # ─── Pending context management ──────────────────────────

    def store_pending(self, symbol: str, features: Dict[str, str]):
        """Store feature snapshot for a trade awaiting outcome."""
        with self._lock:
            self._pending[symbol] = features.copy()

    def pop_pending(self, symbol: str) -> Optional[Dict[str, str]]:
        """Pop and return stored features for a closed trade."""
        with self._lock:
            return self._pending.pop(symbol, None)

    # ─── Learning update ─────────────────────────────────────

    def update(self, symbol: str, is_win: bool, pnl_r: float,
               features: Optional[Dict[str, str]] = None):
        """
        Update all trackers with a trade outcome.

        If features not provided, tries to pop from pending context.
        """
        with self._lock:
            # Get features
            if features is None:
                features = self._pending.pop(symbol, None)
            else:
                self._pending.pop(symbol, None)  # clean up if exists

            # Update global feature trackers
            if features:
                for fname, fval in features.items():
                    if fname not in self._feature_trackers:
                        self._feature_trackers[fname] = {}
                    if fval not in self._feature_trackers[fname]:
                        self._feature_trackers[fname][fval] = BetaTracker()
                    self._feature_trackers[fname][fval].update(is_win)

            # Update pair DNA
            if symbol not in self._pair_dna:
                self._pair_dna[symbol] = PairDNA(symbol)
            self._pair_dna[symbol].update(
                is_win, pnl_r, features or {}
            )

            self._data["total_updates"] = self._data.get("total_updates", 0) + 1
            self._save()

    # ─── Insights / reporting ────────────────────────────────

    def get_insights(self) -> dict:
        """
        Return top winning and losing features + pair rankings.

        Returns:
            {
                "winning_features": [(fname, fval, posterior, confidence), ...],
                "losing_features": [(fname, fval, posterior, confidence), ...],
                "hot_pairs": [(symbol, wr, trades, total_r), ...],
                "cold_pairs": [(symbol, wr, trades, total_r), ...],
                "total_updates": int,
            }
        """
        with self._lock:
            # Collect all features with sufficient confidence
            all_features = []
            for fname, values in self._feature_trackers.items():
                for fval, bt in values.items():
                    if bt.confidence >= MIN_CONFIDENCE:
                        all_features.append(
                            (fname, fval, bt.posterior, bt.confidence,
                             bt.edge)
                        )

            # Sort by edge
            all_features.sort(key=lambda x: x[4], reverse=True)
            winners = [(f[0], f[1], round(f[2], 3), f[3])
                       for f in all_features[:5] if f[4] > 0]
            losers = [(f[0], f[1], round(f[2], 3), f[3])
                      for f in all_features[-5:] if f[4] < 0]
            losers.reverse()  # worst first

            # Pair rankings
            pair_list = []
            for symbol, dna in self._pair_dna.items():
                if dna.tracker.confidence >= 2:
                    pair_list.append((
                        symbol, round(dna.tracker.posterior, 3),
                        dna.tracker.confidence, round(dna.total_r, 4),
                    ))
            pair_list.sort(key=lambda x: x[1], reverse=True)

            hot = [p for p in pair_list if p[1] >= PAIR_HOT_WR]
            cold = [p for p in pair_list if p[1] <= PAIR_COLD_WR]

            return {
                "winning_features": winners,
                "losing_features": losers,
                "hot_pairs": hot,
                "cold_pairs": cold,
                "total_updates": self._data.get("total_updates", 0),
            }

    def log_status(self):
        """Log learner status summary."""
        insights = self.get_insights()
        n = insights["total_updates"]
        if n == 0:
            return

        log.info(f"  📊 Learner: {n} outcomes tracked")

        if insights["winning_features"]:
            top = insights["winning_features"][0]
            log.info(f"    🟢 Best feature: {top[0]}={top[1]} "
                     f"(WR={top[2]*100:.0f}%, n={top[3]})")

        if insights["losing_features"]:
            worst = insights["losing_features"][0]
            log.info(f"    🔴 Worst feature: {worst[0]}={worst[1]} "
                     f"(WR={worst[2]*100:.0f}%, n={worst[3]})")

        hot = insights["hot_pairs"]
        cold = insights["cold_pairs"]
        if hot:
            names = [p[0].split("/")[0] for p in hot[:3]]
            log.info(f"    🔥 Hot pairs: {', '.join(names)}")
        if cold:
            names = [p[0].split("/")[0] for p in cold[:3]]
            log.info(f"    🧊 Cold pairs: {', '.join(names)}")

    @property
    def total_updates(self) -> int:
        with self._lock:
            return self._data.get("total_updates", 0)
