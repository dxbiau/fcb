"""
obr/skill.py -- PerformanceSkill: Conviction scoring + key-level detection
               + agentic self-tuning feedback loop.

Purpose:
  Amplify TP hit-rate by ensuring every trade fires at a structurally
  significant price level.  Multi-factor conviction scoring ranks each
  OBR setup 0-100.  An agentic loop continuously learns from outcomes
  and auto-adjusts the minimum conviction threshold so only the
  highest-edge setups get executed.

Key-level engine (zero extra API calls -- derived from existing candle data):
  - Swing highs/lows (local extremes from 5m candles)
  - Pivot points (classic floor pivots from aggregated 1h bars)
  - Round / psychological numbers (e.g., 0.10, 0.50, 1.00, 100, 50000)
  - Previous-session high/low/close (Asia/London/NY boundaries)
  - VWAP approximation from candle data

Conviction scorer (0-100):
  - Key level proximity   (0-30 pts)
  - OB candle quality     (0-25 pts)
  - Volume context        (0-15 pts)
  - Trend alignment (HTF) (0-15 pts)
  - Fee efficiency        (0-15 pts)

Agentic loop:
  - Tracks outcomes per conviction bucket
  - Every RECAL_INTERVAL trades, recalibrates min_conviction
  - Persists memory to JSON for crash recovery
  - Logs calibration shifts so human can audit

Integration:
  - Called by pair_hunter and bot._scan_pair AFTER OBR signal detected
  - Returns (score, breakdown_dict, pass/reject)
  - On trade close, record_outcome() feeds the loop

No numpy/pandas -- pure Python stdlib.
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Set

from obr import config as cfg
from obr import logger as log
from obr.learner import BayesianLearner, extract_features


# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════

MEMORY_FILE = os.path.join(cfg.LOG_DIR, "skill_memory.json")
RECAL_INTERVAL = 10          # recalibrate every N closed outcomes
MIN_CONVICTION_DEFAULT = 40  # initial minimum score to pass filter
MIN_CONVICTION_FLOOR = 25    # never drop below this
MIN_CONVICTION_CEIL = 75     # never require above this
SWING_LOOKBACK = 30          # candles to scan for swing points
PIVOT_BARS = 12              # 12 x 5m = 1h aggregated pivot
PROXIMITY_PCT = 0.25         # key level "near" threshold (% of price)
ROUND_NUMBER_TIERS = [       # psychological levels
    1000, 500, 100, 50, 10, 5, 1, 0.50, 0.10, 0.05, 0.01, 0.005,
]


# ═══════════════════════════════════════════════════════════════
#  KEY LEVEL ENGINE
# ═══════════════════════════════════════════════════════════════

def _find_swing_highs(candles: List[dict], order: int = 3) -> List[float]:
    """Find local maxima (swing highs) from candle list."""
    highs = []
    for i in range(order, len(candles) - order):
        h = candles[i]["high"]
        is_swing = True
        for j in range(1, order + 1):
            if candles[i - j]["high"] >= h or candles[i + j]["high"] >= h:
                is_swing = False
                break
        if is_swing:
            highs.append(h)
    return highs


def _find_swing_lows(candles: List[dict], order: int = 3) -> List[float]:
    """Find local minima (swing lows) from candle list."""
    lows = []
    for i in range(order, len(candles) - order):
        lo = candles[i]["low"]
        is_swing = True
        for j in range(1, order + 1):
            if candles[i - j]["low"] <= lo or candles[i + j]["low"] <= lo:
                is_swing = False
                break
        if is_swing:
            lows.append(lo)
    return lows


def _aggregate_to_period(candles_5m: List[dict], bars: int) -> List[dict]:
    """Aggregate 5m candles into higher-period bars (e.g., 12 bars = 1h)."""
    agg = []
    for i in range(0, len(candles_5m) - bars + 1, bars):
        chunk = candles_5m[i:i + bars]
        agg.append({
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(c.get("volume", 0) for c in chunk),
            "ts": chunk[0].get("ts", 0),
        })
    return agg


def _classic_pivots(bar: dict) -> Dict[str, float]:
    """Classic floor pivot points from a single period bar."""
    h, l, c = bar["high"], bar["low"], bar["close"]
    p = (h + l + c) / 3
    return {
        "P":  p,
        "R1": 2 * p - l,
        "S1": 2 * p - h,
        "R2": p + (h - l),
        "S2": p - (h - l),
    }


def _round_numbers(price: float) -> List[float]:
    """
    Generate nearest psychological/round numbers for a given price.
    Adapts tier based on price magnitude.
    """
    levels = []
    for tier in ROUND_NUMBER_TIERS:
        if tier > price * 2:
            continue  # skip tiers bigger than 2x price
        if tier < price * 0.0001:
            continue  # skip tiers too granular
        # Nearest round number at this tier
        nearest = round(price / tier) * tier
        levels.append(nearest)
        # Also ±1 tier away
        levels.append(nearest + tier)
        levels.append(nearest - tier)
    return list(set(levels))


def _vwap_approx(candles: List[dict]) -> float:
    """Approximate VWAP from candle data (typical price * volume weighted)."""
    total_vp = 0.0
    total_v = 0.0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume", 0)
        if v > 0:
            total_vp += tp * v
            total_v += v
    return total_vp / total_v if total_v > 0 else 0.0


def _session_levels(candles: List[dict]) -> List[float]:
    """
    Extract previous session's high/low/close as key levels.
    Uses last ~48 candles (4h) as proxy for previous session boundary.
    """
    if len(candles) < 24:
        return []
    prev_session = candles[-48:-12] if len(candles) >= 48 else candles[:-12]
    if not prev_session:
        return []
    sh = max(c["high"] for c in prev_session)
    sl = min(c["low"] for c in prev_session)
    sc = prev_session[-1]["close"]
    return [sh, sl, sc]


def detect_key_levels(candles: List[dict], current_price: float) -> Dict:
    """
    Detect all key levels from candle data.

    Args:
        candles: List of 5m candle dicts (30+ preferred), newest last
        current_price: current market price

    Returns:
        { "levels": [float, ...],
          "nearest_dist_pct": float,  # distance to nearest level as % of price
          "nearest_level": float,
          "level_types": {"swing": [...], "pivot": [...], "round": [...], ...},
          "level_count_nearby": int }  # levels within PROXIMITY_PCT
    """
    all_levels: Dict[str, List[float]] = {
        "swing": [],
        "pivot": [],
        "round": [],
        "session": [],
        "vwap": [],
    }

    # 1) Swing highs/lows
    if len(candles) >= 7:
        all_levels["swing"].extend(_find_swing_highs(candles, order=3))
        all_levels["swing"].extend(_find_swing_lows(candles, order=3))

    # 2) Pivot points from 1h aggregation
    if len(candles) >= PIVOT_BARS * 2:
        hourly = _aggregate_to_period(candles, PIVOT_BARS)
        if len(hourly) >= 2:
            # Use second-to-last completed hourly bar for pivots
            pivots = _classic_pivots(hourly[-2])
            all_levels["pivot"].extend(pivots.values())

    # 3) Round/psychological numbers
    all_levels["round"] = _round_numbers(current_price)

    # 4) Previous session levels
    all_levels["session"] = _session_levels(candles)

    # 5) VWAP
    if len(candles) >= 12:
        vwap = _vwap_approx(candles[-48:] if len(candles) >= 48 else candles)
        if vwap > 0:
            all_levels["vwap"] = [vwap]

    # Flatten and deduplicate
    flat = []
    for levels in all_levels.values():
        flat.extend(levels)
    flat = sorted(set(flat))

    # Find nearest level and count nearby levels
    nearest_dist_pct = 999.0
    nearest_level = 0.0
    nearby_count = 0
    thresh = current_price * PROXIMITY_PCT / 100

    for lv in flat:
        dist = abs(lv - current_price)
        dist_pct = dist / current_price * 100 if current_price > 0 else 999
        if dist_pct < nearest_dist_pct:
            nearest_dist_pct = dist_pct
            nearest_level = lv
        if dist <= thresh:
            nearby_count += 1

    return {
        "levels": flat,
        "nearest_dist_pct": round(nearest_dist_pct, 4),
        "nearest_level": round(nearest_level, 6),
        "level_types": all_levels,
        "level_count_nearby": nearby_count,
    }


# ═══════════════════════════════════════════════════════════════
#  CONVICTION SCORER
# ═══════════════════════════════════════════════════════════════

def _score_key_level(level_info: dict) -> Tuple[float, str]:
    """
    Score key-level proximity (0-30 pts).
    Closer to a key level = higher score.
    Multiple nearby levels = confluence bonus.
    """
    dist = level_info["nearest_dist_pct"]
    nearby = level_info["level_count_nearby"]

    # Distance scoring (closer = better)
    if dist <= 0.05:
        base = 25.0   # right on the level
    elif dist <= 0.10:
        base = 22.0
    elif dist <= 0.15:
        base = 18.0
    elif dist <= 0.20:
        base = 14.0
    elif dist <= 0.30:
        base = 10.0
    elif dist <= 0.50:
        base = 5.0
    else:
        base = 0.0

    # Confluence bonus: multiple levels stacking
    confluence = min(nearby * 1.5, 5.0)

    score = min(base + confluence, 30.0)
    detail = f"dist={dist:.3f}% near={nearby} base={base:.0f}+conf={confluence:.1f}"
    return score, detail


def _score_ob_quality(ob_candle: dict, prev_candle: dict,
                      current_price: float) -> Tuple[float, str]:
    """
    Score OB candle quality (0-25 pts).
    - Body/range ratio (strong close = high conviction)
    - Engulfment magnitude (how far beyond prev range)
    - Absolute range as % of price (bigger = more decisive)
    """
    ob_range = ob_candle["high"] - ob_candle["low"]
    ob_body = abs(ob_candle["close"] - ob_candle["open"])
    prev_range = prev_candle["high"] - prev_candle["low"]

    if ob_range <= 0 or current_price <= 0:
        return 0.0, "flat_ob"

    # Body/range ratio (0-10): strong close = full commitment
    body_ratio = ob_body / ob_range
    body_score = body_ratio * 10.0

    # Engulfment magnitude (0-8): how much bigger than prev
    if prev_range > 0:
        engulf_ratio = ob_range / prev_range
        if engulf_ratio >= 3.0:
            engulf_score = 8.0
        elif engulf_ratio >= 2.0:
            engulf_score = 6.0
        elif engulf_ratio >= 1.5:
            engulf_score = 4.0
        else:
            engulf_score = 2.0
    else:
        engulf_score = 4.0

    # Range as % of price (0-7): bigger candle = more conviction
    range_pct = ob_range / current_price * 100
    if range_pct >= 1.0:
        range_score = 7.0
    elif range_pct >= 0.5:
        range_score = 5.0
    elif range_pct >= 0.3:
        range_score = 3.0
    elif range_pct >= 0.2:
        range_score = 1.5
    else:
        range_score = 0.0

    score = min(body_score + engulf_score + range_score, 25.0)
    detail = (f"body={body_ratio:.2f}({body_score:.1f}) "
              f"engulf={engulf_ratio if prev_range > 0 else 0:.1f}({engulf_score:.0f}) "
              f"range={range_pct:.2f}%({range_score:.1f})")
    return score, detail


def _score_volume(ob_candle: dict, candles: List[dict]) -> Tuple[float, str]:
    """
    Score volume context (0-15 pts).
    OB candle volume vs average = volume spike = smart money activity.
    """
    ob_vol = ob_candle.get("volume", 0)
    if ob_vol <= 0 or len(candles) < 5:
        return 5.0, "no_vol_data(default=5)"

    avg_vol = sum(c.get("volume", 0) for c in candles[-20:]) / min(len(candles), 20)
    if avg_vol <= 0:
        return 5.0, "zero_avg_vol(default=5)"

    vol_ratio = ob_vol / avg_vol

    if vol_ratio >= 3.0:
        score = 15.0
    elif vol_ratio >= 2.5:
        score = 13.0
    elif vol_ratio >= 2.0:
        score = 11.0
    elif vol_ratio >= 1.5:
        score = 8.0
    elif vol_ratio >= 1.2:
        score = 5.0
    elif vol_ratio >= 0.8:
        score = 3.0
    else:
        score = 1.0  # below-average volume = weak signal

    detail = f"vol_ratio={vol_ratio:.2f}"
    return score, detail


def _score_trend_alignment(candles: List[dict],
                           direction: str) -> Tuple[float, str]:
    """
    Score higher-timeframe trend alignment (0-15 pts).
    Reversal in direction of the bigger trend = highest conviction.
    Uses simple EMA-like calculation from 5m candles (~2h lookback).
    """
    if len(candles) < 12:
        return 7.0, "insufficient_data(default=7)"

    # Simple moving average of close over last 24 candles (2h on 5m)
    recent = candles[-24:] if len(candles) >= 24 else candles
    sma = sum(c["close"] for c in recent) / len(recent)

    # Shorter SMA (last 6 candles = 30min)
    short_sma = sum(c["close"] for c in candles[-6:]) / min(len(candles), 6)

    current = candles[-1]["close"]

    # Trend direction
    if short_sma > sma:
        trend = "up"
    elif short_sma < sma:
        trend = "down"
    else:
        trend = "flat"

    # OBR is a REVERSAL strategy that FADES exhaustion
    # Long signal = bearish OB → fade into reversal UP
    # Short signal = bullish OB → fade into reversal DOWN
    #
    # Best edge: reversal BACK into the prevailing trend
    # (exhaustion move was counter-trend, reversal restores trend)
    if direction == "long" and trend == "up":
        score = 15.0  # fading bearish exhaustion back into uptrend
        label = "with_trend"
    elif direction == "short" and trend == "down":
        score = 15.0  # fading bullish exhaustion back into downtrend
        label = "with_trend"
    elif trend == "flat":
        score = 8.0   # ranging = neutral
        label = "flat"
    elif direction == "long" and trend == "down":
        score = 4.0   # counter-trend reversal = riskier
        label = "counter_trend"
    elif direction == "short" and trend == "up":
        score = 4.0
        label = "counter_trend"
    else:
        score = 7.0
        label = "neutral"

    detail = f"{label} sma_gap={((short_sma-sma)/sma*100):.3f}%"
    return score, detail


def _score_fee_efficiency(fee_r: float) -> Tuple[float, str]:
    """Score fee efficiency (0-15 pts). Lower fee_r = more room for profit."""
    if fee_r <= 0.05:
        score = 15.0
    elif fee_r <= 0.08:
        score = 13.0
    elif fee_r <= 0.10:
        score = 11.0
    elif fee_r <= 0.15:
        score = 8.0
    elif fee_r <= 0.20:
        score = 5.0
    elif fee_r <= 0.25:
        score = 2.0
    else:
        score = 0.0
    return score, f"fee_r={fee_r:.3f}"


def score_setup(
    ob_candle: dict,
    prev_candle: dict,
    confirm_candle: dict,
    direction: str,
    fee_r: float,
    current_price: float,
    candles: List[dict],   # full candle history (30+ preferred)
) -> Dict:
    """
    Score an OBR setup on conviction (0-100).

    Returns:
        {
            "score": float,
            "grade": str,       # "A+", "A", "B", "C", "D"
            "breakdown": {
                "key_level": (score, detail),
                "ob_quality": (score, detail),
                "volume": (score, detail),
                "trend": (score, detail),
                "fee": (score, detail),
            },
            "level_info": dict,  # from detect_key_levels
            "pass": bool,        # whether it meets minimum threshold
        }
    """
    # Detect key levels
    level_info = detect_key_levels(candles, current_price)

    # Score each dimension
    kl_score, kl_detail = _score_key_level(level_info)
    ob_score, ob_detail = _score_ob_quality(ob_candle, prev_candle, current_price)
    vol_score, vol_detail = _score_volume(ob_candle, candles)
    trend_score, trend_detail = _score_trend_alignment(candles, direction)
    fee_score, fee_detail = _score_fee_efficiency(fee_r)

    total = kl_score + ob_score + vol_score + trend_score + fee_score

    # Grade
    if total >= 80:
        grade = "A+"
    elif total >= 65:
        grade = "A"
    elif total >= 50:
        grade = "B"
    elif total >= 35:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": round(total, 1),
        "grade": grade,
        "breakdown": {
            "key_level": (round(kl_score, 1), kl_detail),
            "ob_quality": (round(ob_score, 1), ob_detail),
            "volume": (round(vol_score, 1), vol_detail),
            "trend": (round(trend_score, 1), trend_detail),
            "fee": (round(fee_score, 1), fee_detail),
        },
        "level_info": level_info,
    }


# ═══════════════════════════════════════════════════════════════
#  PERFORMANCE SKILL  (agentic self-tuning)
# ═══════════════════════════════════════════════════════════════

class PerformanceSkill:
    """
    Agentic conviction filter with self-tuning feedback loop.

    Flow:
      1. evaluate(setup_data) → score + pass/reject
      2. On trade close: record_outcome(trade_id, score, pnl_r)
      3. Every RECAL_INTERVAL outcomes: _recalibrate()
         - Adjusts min_conviction based on which buckets actually win
         - Persists to disk for crash recovery
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._memory = self._load_memory()
        self._pending_scores: Dict[str, float] = {}  # symbol → conviction score
        self._pending_contexts: Dict[str, dict] = {}  # symbol → full eval context
        self._learner = BayesianLearner()

    # ─── Memory persistence ─────────────────────────────────

    def _load_memory(self) -> dict:
        """Load skill memory from disk."""
        default = {
            "min_conviction": MIN_CONVICTION_DEFAULT,
            "outcomes": [],           # [{score, pnl_r, grade, ts}, ...]
            "buckets": {},            # {"60-70": {wins, losses, total_r}, ...}
            "grade_stats": {},        # {"A+": {wins, losses}, "A": ...}
            "total_evaluated": 0,
            "total_passed": 0,
            "total_rejected": 0,
            "recal_count": 0,
            "created": datetime.now(timezone.utc).isoformat(),
            "last_recal": None,
        }
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r") as f:
                    data = json.load(f)
                # Merge with defaults for new fields
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            log.debug(f"PerformanceSkill: fresh memory (load error: {e})")
        return default

    def _save_memory(self):
        """Persist skill memory to disk."""
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            with open(MEMORY_FILE, "w") as f:
                json.dump(self._memory, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"PerformanceSkill: save error: {e}")

    # ─── Evaluation ──────────────────────────────────────────

    def evaluate(
        self,
        ob_candle: dict,
        prev_candle: dict,
        confirm_candle: dict,
        direction: str,
        fee_r: float,
        current_price: float,
        candles: List[dict],
        symbol: str = "",
        market_regime: str = "unknown",
        equity_phase: str = "unknown",
        drawdown_zone: str = "normal",
    ) -> Dict:
        """
        Evaluate an OBR setup's conviction.

        Returns:
            {
                "score": float,
                "grade": str,
                "pass": bool,
                "min_conviction": float,
                "breakdown": {...},
                "level_info": {...},
            }
        """
        result = score_setup(
            ob_candle=ob_candle,
            prev_candle=prev_candle,
            confirm_candle=confirm_candle,
            direction=direction,
            fee_r=fee_r,
            current_price=current_price,
            candles=candles,
        )

        # ── Bayesian Feature Learner adjustment ──
        features = extract_features(
            ob_candle=ob_candle, prev_candle=prev_candle,
            confirm_candle=confirm_candle, direction=direction,
            fee_r=fee_r, current_price=current_price,
            candles=candles, breakdown=result["breakdown"],
            level_info=result["level_info"], symbol=symbol,
            market_regime=market_regime, equity_phase=equity_phase,
            drawdown_zone=drawdown_zone,
        )
        bayes_adj = self._learner.compute_adjustment(features)
        pair_adj = self._learner.get_pair_adjustment(symbol) if symbol else 0.0
        total_adj = bayes_adj + pair_adj

        # Adjust score (clamped 0-100)
        base_score = result["score"]
        adjusted = max(0.0, min(100.0, base_score + total_adj))
        result["score"] = round(adjusted, 1)
        result["bayes_adjustment"] = round(total_adj, 1)

        # Re-grade after adjustment
        if adjusted >= 80:
            result["grade"] = "A+"
        elif adjusted >= 65:
            result["grade"] = "A"
        elif adjusted >= 50:
            result["grade"] = "B"
        elif adjusted >= 35:
            result["grade"] = "C"
        else:
            result["grade"] = "D"

        with self._lock:
            min_conv = self._memory["min_conviction"]
            self._memory["total_evaluated"] += 1

            passed = result["score"] >= min_conv
            if passed:
                self._memory["total_passed"] += 1
                if symbol:
                    self._pending_scores[symbol] = result["score"]
                    self._pending_contexts[symbol] = {
                        "features": features,
                        "base_score": base_score,
                        "adjustment": total_adj,
                    }
                    self._learner.store_pending(symbol, features)
            else:
                self._memory["total_rejected"] += 1

            self._save_memory()

        result["pass"] = passed
        result["min_conviction"] = min_conv
        result["pair_status"] = self._learner.get_pair_status(symbol) if symbol else {}
        return result

    # ─── Outcome recording ───────────────────────────────────

    def record_outcome(self, symbol: str, pnl_r: float, pnl_usd: float = 0):
        """
        Record trade outcome against its conviction score.
        Called by bot on position close.
        """
        with self._lock:
            score = self._pending_scores.pop(symbol, None)
            if score is None:
                return  # trade wasn't scored (e.g., pre-skill trade)

            is_win = pnl_r > 0

            # Determine bucket (10-point ranges)
            bucket = f"{int(score // 10) * 10}-{int(score // 10) * 10 + 10}"

            # Determine grade
            if score >= 80:
                grade = "A+"
            elif score >= 65:
                grade = "A"
            elif score >= 50:
                grade = "B"
            elif score >= 35:
                grade = "C"
            else:
                grade = "D"

            # Update outcomes list
            outcome = {
                "symbol": symbol,
                "score": score,
                "grade": grade,
                "pnl_r": round(pnl_r, 4),
                "pnl_usd": round(pnl_usd, 2),
                "win": is_win,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self._memory["outcomes"].append(outcome)
            # Keep last 500 outcomes
            if len(self._memory["outcomes"]) > 500:
                self._memory["outcomes"] = self._memory["outcomes"][-500:]

            # Update bucket stats
            if bucket not in self._memory["buckets"]:
                self._memory["buckets"][bucket] = {
                    "wins": 0, "losses": 0, "total_r": 0.0
                }
            b = self._memory["buckets"][bucket]
            if is_win:
                b["wins"] += 1
            else:
                b["losses"] += 1
            b["total_r"] += pnl_r

            # Update grade stats
            if grade not in self._memory["grade_stats"]:
                self._memory["grade_stats"][grade] = {
                    "wins": 0, "losses": 0, "total_r": 0.0
                }
            g = self._memory["grade_stats"][grade]
            if is_win:
                g["wins"] += 1
            else:
                g["losses"] += 1
            g["total_r"] += pnl_r

            self._save_memory()

            # Feed Bayesian learner
            self._learner.update(
                symbol=symbol, is_win=is_win, pnl_r=pnl_r,
            )
            # Clean up pending context
            self._pending_contexts.pop(symbol, None)

            # Check if recalibration is due
            total_outcomes = sum(
                b2["wins"] + b2["losses"]
                for b2 in self._memory["buckets"].values()
            )
            if total_outcomes > 0 and total_outcomes % RECAL_INTERVAL == 0:
                self._recalibrate()

    # ─── Agentic recalibration ───────────────────────────────

    def _recalibrate(self):
        """
        Agentic self-tuning: adjust min_conviction based on what's working.

        Strategy:
          - Calculate win rate per bucket
          - Find the conviction threshold where WR > 50%
          - Gradually shift min_conviction toward that edge
          - Never move more than ±5 per recalibration (stability)
        """
        old_min = self._memory["min_conviction"]
        buckets = self._memory["buckets"]

        if not buckets:
            return

        # Find the lowest bucket with positive expectancy
        best_threshold = MIN_CONVICTION_DEFAULT
        found_edge = False

        # Sort buckets by score range (ascending)
        sorted_buckets = sorted(buckets.items(),
                                key=lambda x: int(x[0].split("-")[0]))

        # Walk from high scores down -- find where edge disappears
        cumulative_wins = 0
        cumulative_losses = 0
        cumulative_r = 0.0

        for bucket_range, stats in reversed(sorted_buckets):
            cumulative_wins += stats["wins"]
            cumulative_losses += stats["losses"]
            cumulative_r += stats["total_r"]

            total = cumulative_wins + cumulative_losses
            if total < 3:
                continue  # need minimum sample

            wr = cumulative_wins / total * 100
            if wr >= 45 and cumulative_r > 0:
                # This bucket and above has positive edge
                best_threshold = int(bucket_range.split("-")[0])
                found_edge = True

        if not found_edge:
            # No clear edge -- tighten slightly
            best_threshold = min(old_min + 3, MIN_CONVICTION_CEIL)

        # Gradual shift (max ±5 per recal for stability)
        delta = best_threshold - old_min
        delta = max(-5, min(5, delta))
        new_min = old_min + delta
        new_min = max(MIN_CONVICTION_FLOOR, min(MIN_CONVICTION_CEIL, new_min))

        self._memory["min_conviction"] = new_min
        self._memory["recal_count"] = self._memory.get("recal_count", 0) + 1
        self._memory["last_recal"] = datetime.now(timezone.utc).isoformat()
        self._save_memory()

        # Log the recalibration
        total_outcomes = sum(
            b["wins"] + b["losses"] for b in buckets.values()
        )
        total_wins = sum(b["wins"] for b in buckets.values())
        total_r = sum(b["total_r"] for b in buckets.values())
        wr = total_wins / total_outcomes * 100 if total_outcomes > 0 else 0

        direction = "↑" if new_min > old_min else "↓" if new_min < old_min else "="
        log.info(f"  🧠 SKILL RECAL #{self._memory['recal_count']}: "
                 f"min_conviction {old_min:.0f} {direction} {new_min:.0f} "
                 f"| {total_outcomes} trades WR={wr:.0f}% "
                 f"R={total_r:+.2f}")

        # Log per-grade performance
        for grade in ["A+", "A", "B", "C", "D"]:
            gs = self._memory["grade_stats"].get(grade)
            if gs and (gs["wins"] + gs["losses"]) > 0:
                gt = gs["wins"] + gs["losses"]
                gwr = gs["wins"] / gt * 100
                log.info(f"    {grade:>2}: {gt} trades, "
                         f"WR={gwr:.0f}%, R={gs['total_r']:+.2f}")

    # ─── Status / reporting ──────────────────────────────────

    @property
    def min_conviction(self) -> float:
        with self._lock:
            return self._memory["min_conviction"]

    @property
    def stats(self) -> dict:
        with self._lock:
            m = self._memory
            total_outcomes = sum(
                b["wins"] + b["losses"] for b in m["buckets"].values()
            )
            total_wins = sum(b["wins"] for b in m["buckets"].values())
            return {
                "min_conviction": m["min_conviction"],
                "evaluated": m["total_evaluated"],
                "passed": m["total_passed"],
                "rejected": m["total_rejected"],
                "outcomes": total_outcomes,
                "win_rate": total_wins / total_outcomes * 100 if total_outcomes > 0 else 0,
                "recalibrations": m["recal_count"],
                "grade_stats": dict(m["grade_stats"]),
            }

    def log_status(self):
        """Log current skill status."""
        s = self.stats
        if s["outcomes"] > 0:
            log.info(f"  🧠 Skill: min={s['min_conviction']:.0f} "
                     f"eval={s['evaluated']} pass={s['passed']} "
                     f"rej={s['rejected']} "
                     f"WR={s['win_rate']:.0f}% "
                     f"recals={s['recalibrations']}")
        # Bayesian learner status
        self._learner.log_status()

    @property
    def learner(self) -> BayesianLearner:
        """Access learner for direct queries (pair DNA, insights, etc.)."""
        return self._learner
