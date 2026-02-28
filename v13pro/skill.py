"""
v13pro/skill.py -- PerformanceSkill: multi-factor conviction scoring
                   + agentic self-tuning filter.

Adapted from obr/skill.py for v13pro's 12-strategy engine.

Key-level engine (zero extra API calls — derived from WS candle data):
  - Swing highs/lows (local extremes)
  - Pivot points (classic floor pivots from aggregated bars)
  - Round / psychological numbers
  - Previous-session high/low/close
  - VWAP approximation

Conviction scorer (0-100):
  - Key level proximity    (0-30 pts)
  - Signal candle quality  (0-25 pts)
  - Volume context         (0-15 pts)
  - Trend alignment        (0-15 pts)
  - Fee efficiency         (0-15 pts)

Agentic loop:
  - Tracks outcomes per conviction bucket
  - Every RECAL_INTERVAL trades, recalibrates min_conviction
  - Persists memory to JSON for crash recovery

Self-contained: no imports from obr/.
"""

import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from v13pro import config as cfg
from v13pro import logger as log
from v13pro.learner import BayesianLearner, extract_features

MEMORY_FILE = os.path.join(cfg.LOG_DIR, "skill_memory.json")
RECAL_INTERVAL = 10
MIN_CONVICTION_DEFAULT = 55
MIN_CONVICTION_FLOOR = 50
MIN_CONVICTION_CEIL = 75
PROXIMITY_PCT = 0.25
ROUND_NUMBER_TIERS = [1000, 500, 100, 50, 10, 5, 1, 0.50, 0.10, 0.05, 0.01, 0.005]


# ═══════════════════════════════════════════════════════════════
#  KEY LEVEL ENGINE
# ═══════════════════════════════════════════════════════════════

def _find_swing_highs(candles: List[dict], order: int = 3) -> List[float]:
    highs = []
    for i in range(order, len(candles) - order):
        h = candles[i]["high"]
        if all(candles[i-j]["high"] < h and candles[i+j]["high"] < h
               for j in range(1, order+1)):
            highs.append(h)
    return highs


def _find_swing_lows(candles: List[dict], order: int = 3) -> List[float]:
    lows = []
    for i in range(order, len(candles) - order):
        lo = candles[i]["low"]
        if all(candles[i-j]["low"] > lo and candles[i+j]["low"] > lo
               for j in range(1, order+1)):
            lows.append(lo)
    return lows


def _aggregate_period(candles: List[dict], bars: int) -> List[dict]:
    agg = []
    for i in range(0, len(candles) - bars + 1, bars):
        chunk = candles[i:i+bars]
        agg.append({
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(c.get("volume", 0) for c in chunk),
        })
    return agg


def _classic_pivots(bar: dict) -> Dict[str, float]:
    h, l, c = bar["high"], bar["low"], bar["close"]
    p = (h + l + c) / 3
    return {"P": p, "R1": 2*p-l, "S1": 2*p-h, "R2": p+(h-l), "S2": p-(h-l)}


def _round_numbers(price: float) -> List[float]:
    levels = []
    for tier in ROUND_NUMBER_TIERS:
        if tier > price * 2 or tier < price * 0.0001:
            continue
        nearest = round(price / tier) * tier
        levels.extend([nearest, nearest + tier, nearest - tier])
    return list(set(levels))


def _vwap_approx(candles: List[dict]) -> float:
    total_vp = total_v = 0.0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume", 0)
        if v > 0:
            total_vp += tp * v
            total_v += v
    return total_vp / total_v if total_v > 0 else 0.0


def _session_levels(candles: List[dict]) -> List[float]:
    if len(candles) < 24:
        return []
    prev = candles[-48:-12] if len(candles) >= 48 else candles[:-12]
    if not prev:
        return []
    return [max(c["high"] for c in prev),
            min(c["low"] for c in prev),
            prev[-1]["close"]]


def detect_key_levels(candles: List[dict], current_price: float) -> dict:
    """Detect all key levels from candle data."""
    all_levels: Dict[str, List[float]] = {
        "swing": [], "pivot": [], "round": [], "session": [], "vwap": []}

    if len(candles) >= 7:
        all_levels["swing"].extend(_find_swing_highs(candles, 3))
        all_levels["swing"].extend(_find_swing_lows(candles, 3))

    # Pivots from ~4-bar aggregation (depending on TF)
    pivot_bars = max(4, len(candles) // 50)
    if len(candles) >= pivot_bars * 2:
        hourly = _aggregate_period(candles, pivot_bars)
        if len(hourly) >= 2:
            all_levels["pivot"].extend(_classic_pivots(hourly[-2]).values())

    all_levels["round"] = _round_numbers(current_price)
    all_levels["session"] = _session_levels(candles)

    if len(candles) >= 12:
        vwap = _vwap_approx(candles[-48:] if len(candles) >= 48 else candles)
        if vwap > 0:
            all_levels["vwap"] = [vwap]

    flat = sorted(set(lv for group in all_levels.values() for lv in group))

    nearest_dist = 999.0
    nearest_lv = 0.0
    nearby = 0
    thresh = current_price * PROXIMITY_PCT / 100

    for lv in flat:
        d = abs(lv - current_price)
        dpct = d / current_price * 100 if current_price > 0 else 999
        if dpct < nearest_dist:
            nearest_dist = dpct
            nearest_lv = lv
        if d <= thresh:
            nearby += 1

    return {
        "levels": flat,
        "nearest_dist_pct": round(nearest_dist, 4),
        "nearest_level": round(nearest_lv, 6),
        "level_types": all_levels,
        "level_count_nearby": nearby,
    }


# ═══════════════════════════════════════════════════════════════
#  CONVICTION SCORER — adapted for 12-strategy signals
# ═══════════════════════════════════════════════════════════════

def _score_key_level(level_info: dict) -> Tuple[float, str]:
    """Key level proximity (0-30 pts)."""
    dist = level_info["nearest_dist_pct"]
    nearby = level_info["level_count_nearby"]
    if dist <= 0.05: base = 25.0
    elif dist <= 0.10: base = 22.0
    elif dist <= 0.15: base = 18.0
    elif dist <= 0.20: base = 14.0
    elif dist <= 0.30: base = 10.0
    elif dist <= 0.50: base = 5.0
    else: base = 0.0
    confluence = min(nearby * 1.5, 5.0)
    score = min(base + confluence, 30.0)
    return score, f"dist={dist:.3f}% near={nearby}"


def _score_signal_quality(candles: List[dict], direction: str) -> Tuple[float, str]:
    """Signal candle quality (0-25 pts) — adapted for v13pro strategies."""
    if not candles or len(candles) < 3:
        return 10.0, "insufficient_data"

    c = candles[-1]  # signal candle
    prev = candles[-2]

    rng = c["high"] - c["low"]
    if rng <= 0 or c["close"] <= 0:
        return 5.0, "flat"

    body = abs(c["close"] - c["open"])
    prev_rng = prev["high"] - prev["low"]

    # Body strength (0-10)
    body_ratio = body / rng
    body_score = min(body_ratio * 12, 10.0)

    # Range relative to prev (0-8): engulfment/expansion
    if prev_rng > 0:
        size_ratio = rng / prev_rng
        if size_ratio >= 2.5: engulf = 8.0
        elif size_ratio >= 1.5: engulf = 6.0
        elif size_ratio >= 1.0: engulf = 4.0
        else: engulf = 2.0
    else:
        engulf = 4.0

    # Direction alignment (0-7)
    if direction == "long" and c["close"] > c["open"]:
        align = 7.0
    elif direction == "short" and c["close"] < c["open"]:
        align = 7.0
    elif abs(c["close"] - c["open"]) < rng * 0.1:
        align = 3.0  # doji
    else:
        align = 1.0  # wrong direction

    score = min(body_score + engulf + align, 25.0)
    return score, f"body={body_ratio:.2f} size={size_ratio if prev_rng > 0 else 0:.1f}"


def _score_volume(candles: List[dict]) -> Tuple[float, str]:
    """Volume context (0-15 pts)."""
    if not candles or len(candles) < 5:
        return 5.0, "no_data"
    last_vol = candles[-1].get("volume", 0)
    avg = sum(c.get("volume", 0) for c in candles[-20:]) / min(len(candles), 20)
    if avg <= 0 or last_vol <= 0:
        return 5.0, "zero_vol"
    ratio = last_vol / avg
    if ratio >= 3.0: score = 15.0
    elif ratio >= 2.0: score = 11.0
    elif ratio >= 1.5: score = 8.0
    elif ratio >= 1.0: score = 5.0
    else: score = 2.0
    return score, f"vol_ratio={ratio:.2f}"


def _score_trend(candles: List[dict], direction: str) -> Tuple[float, str]:
    """HTF trend alignment (0-15 pts)."""
    if not candles or len(candles) < 24:
        return 7.0, "insufficient"
    sma_long = sum(c["close"] for c in candles[-50:]) / min(len(candles), 50)
    sma_short = sum(c["close"] for c in candles[-10:]) / 10
    if sma_short > sma_long: trend = "up"
    elif sma_short < sma_long: trend = "down"
    else: trend = "flat"

    # With-trend is highest conviction
    if (direction == "long" and trend == "up") or \
       (direction == "short" and trend == "down"):
        return 15.0, "with_trend"
    elif trend == "flat":
        return 8.0, "flat"
    else:
        return 4.0, "counter_trend"


def _score_fee(maker: bool = True) -> Tuple[float, str]:
    """Fee efficiency (0-15 pts)."""
    fee_r = 0.04 if maker else 0.10
    if fee_r <= 0.05: return 15.0, f"fee_r={fee_r:.3f}"
    elif fee_r <= 0.08: return 13.0, f"fee_r={fee_r:.3f}"
    elif fee_r <= 0.10: return 11.0, f"fee_r={fee_r:.3f}"
    elif fee_r <= 0.15: return 8.0, f"fee_r={fee_r:.3f}"
    else: return 5.0, f"fee_r={fee_r:.3f}"


def score_setup(candles: List[dict], direction: str,
                current_price: float, maker: bool = True) -> dict:
    """
    Score a v13pro signal on conviction (0-100).

    Args:
        candles: recent candles from WS buffer (list of dicts)
        direction: 'long' or 'short'
        current_price: current market price
        maker: whether using maker fee model

    Returns:
        { score, grade, breakdown, level_info }
    """
    level_info = detect_key_levels(candles, current_price)
    kl, kl_d = _score_key_level(level_info)
    sq, sq_d = _score_signal_quality(candles, direction)
    vl, vl_d = _score_volume(candles)
    tr, tr_d = _score_trend(candles, direction)
    fe, fe_d = _score_fee(maker)

    total = kl + sq + vl + tr + fe
    if total >= 80: grade = "A+"
    elif total >= 65: grade = "A"
    elif total >= 50: grade = "B"
    elif total >= 35: grade = "C"
    else: grade = "D"

    return {
        "score": round(total, 1),
        "grade": grade,
        "breakdown": {
            "key_level": (round(kl, 1), kl_d),
            "signal_quality": (round(sq, 1), sq_d),
            "volume": (round(vl, 1), vl_d),
            "trend": (round(tr, 1), tr_d),
            "fee": (round(fe, 1), fe_d),
        },
        "level_info": level_info,
    }


# ═══════════════════════════════════════════════════════════════
#  PERFORMANCE SKILL — agentic self-tuning
# ═══════════════════════════════════════════════════════════════

class PerformanceSkill:
    """
    Agentic conviction filter with self-tuning feedback loop.

    evaluate(candles, direction, symbol, ...) → {score, grade, pass/reject}
    record_outcome(symbol, pnl_r) → feeds the loop
    Every RECAL_INTERVAL trades → auto-adjusts min_conviction
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._memory = self._load_memory()
        self._learner = BayesianLearner()
        self._pending_scores: Dict[str, float] = {}
        self._adaptive = None  # Set via set_adaptive()

    def set_adaptive(self, adaptive):
        """Wire in AdaptiveParams for data-driven thresholds."""
        self._adaptive = adaptive

    def _load_memory(self) -> dict:
        default = {
            "min_conviction": MIN_CONVICTION_DEFAULT,
            "outcomes": [], "buckets": {}, "grade_stats": {},
            "total_evaluated": 0, "total_passed": 0, "total_rejected": 0,
            "recal_count": 0, "last_recal": None,
        }
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r") as f:
                    data = json.load(f)
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception:
            pass
        return default

    def _save_memory(self):
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            with open(MEMORY_FILE, "w") as f:
                json.dump(self._memory, f, indent=2, default=str)
        except Exception:
            pass

    def evaluate(self, candles: List[dict], direction: str,
                 current_price: float, symbol: str = "",
                 strategy: str = "", timeframe: str = "",
                 stop_dist: float = 0, entry_price: float = 0,
                 maker: bool = True,
                 equity_phase: str = "unknown",
                 drawdown_zone: str = "normal") -> dict:
        """
        Evaluate signal conviction (0-100).

        Returns:
          {score, grade, pass, min_conviction, breakdown, level_info, bayes_adj}
        """
        result = score_setup(candles, direction, current_price, maker)

        # ── Key level: sigmoid risk multiplier (replaces hard block) ──
        # Shadow audit: 243 KL-rejected longs had +0.107 ExpR, +26.0R total.
        # KL 5-10 bucket: 29.2% WR, +0.230 ExpR (clearly profitable).
        # Sigmoid scales risk: at threshold → 1.0x, at zero → 0.20x.
        kl_score = result["breakdown"]["key_level"][0]
        _kl_min = self._adaptive.min_key_level_score if self._adaptive else cfg.MIN_KEY_LEVEL_SCORE
        if kl_score >= _kl_min:
            result["kl_risk_mult"] = 1.0
        else:
            # Normalised position: 0 = at threshold, negative = below
            _kl_ratio = kl_score / max(_kl_min, 1.0)  # 0..1
            # Sigmoid: floor=0.20, ceiling=1.0, steepness=5.0
            _kl_floor, _kl_steep = 0.20, 5.0
            result["kl_risk_mult"] = _kl_floor + (1.0 - _kl_floor) / (
                1.0 + math.exp(-_kl_steep * (_kl_ratio - 0.5)))

        # ── Stop distance quality gate ──
        if stop_dist > 0 and entry_price > 0:
            stop_pct = stop_dist / entry_price * 100
            if stop_pct > cfg.MAX_STOP_DIST_PCT:
                result["pass"] = False
                result["min_conviction"] = self._memory.get("min_conviction", MIN_CONVICTION_DEFAULT)
                result["rejection_reason"] = (
                    f"stop_dist={stop_pct:.1f}%>{cfg.MAX_STOP_DIST_PCT}% "
                    f"(too wide, not a key level entry)")
                result["bayes_adjustment"] = 0
                result["pair_status"] = {}
                return result

        # Bayesian learner adjustment
        features = extract_features(
            candles=candles, direction=direction, strategy=strategy,
            timeframe=timeframe, symbol=symbol, entry_price=entry_price,
            stop_dist=stop_dist, conviction=result["score"],
            grade=result["grade"], equity_phase=equity_phase,
            drawdown_zone=drawdown_zone,
        )
        bayes_adj = self._learner.compute_adjustment(features)
        pair_adj = self._learner.get_pair_adjustment(symbol) if symbol else 0.0
        total_adj = bayes_adj + pair_adj

        base = result["score"]
        adjusted = max(0.0, min(100.0, base + total_adj))
        result["score"] = round(adjusted, 1)
        result["bayes_adjustment"] = round(total_adj, 1)

        # Re-grade
        if adjusted >= 80: result["grade"] = "A+"
        elif adjusted >= 65: result["grade"] = "A"
        elif adjusted >= 50: result["grade"] = "B"
        elif adjusted >= 35: result["grade"] = "C"
        else: result["grade"] = "D"

        with self._lock:
            min_conv = self._memory["min_conviction"]
            self._memory["total_evaluated"] += 1
            passed = adjusted >= min_conv
            if passed:
                self._memory["total_passed"] += 1
                if symbol:
                    self._pending_scores[symbol] = adjusted
                    self._learner.store_pending(symbol, features)
            else:
                self._memory["total_rejected"] += 1
            self._save_memory()

        result["pass"] = passed
        result["min_conviction"] = min_conv
        result["pair_status"] = self._learner.get_pair_status(symbol) if symbol else {}
        return result

    def record_outcome(self, symbol: str, pnl_r: float, pnl_usd: float = 0):
        """Record trade outcome against conviction score."""
        with self._lock:
            score = self._pending_scores.pop(symbol, None)
            if score is None:
                return

            is_win = pnl_r > 0
            bucket = f"{int(score // 10) * 10}-{int(score // 10) * 10 + 10}"
            if score >= 80: grade = "A+"
            elif score >= 65: grade = "A"
            elif score >= 50: grade = "B"
            elif score >= 35: grade = "C"
            else: grade = "D"

            self._memory["outcomes"].append({
                "symbol": symbol, "score": score, "grade": grade,
                "pnl_r": round(pnl_r, 4), "win": is_win,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            if len(self._memory["outcomes"]) > 500:
                self._memory["outcomes"] = self._memory["outcomes"][-500:]

            b = self._memory["buckets"].setdefault(bucket, {"wins": 0, "losses": 0, "total_r": 0.0})
            if is_win: b["wins"] += 1
            else: b["losses"] += 1
            b["total_r"] += pnl_r

            g = self._memory["grade_stats"].setdefault(grade, {"wins": 0, "losses": 0, "total_r": 0.0})
            if is_win: g["wins"] += 1
            else: g["losses"] += 1
            g["total_r"] += pnl_r

            self._save_memory()
            self._learner.update(symbol, is_win, pnl_r)

            total_out = sum(b2["wins"] + b2["losses"] for b2 in self._memory["buckets"].values())
            if total_out > 0 and total_out % RECAL_INTERVAL == 0:
                self._recalibrate()

    def _recalibrate(self):
        """Self-tune min_conviction based on which buckets actually win."""
        old = self._memory["min_conviction"]
        buckets = self._memory["buckets"]
        if not buckets:
            return

        best_thresh = MIN_CONVICTION_DEFAULT
        found = False
        sorted_b = sorted(buckets.items(), key=lambda x: int(x[0].split("-")[0]))

        cum_w = cum_l = 0
        cum_r = 0.0
        for br, stats in reversed(sorted_b):
            cum_w += stats["wins"]
            cum_l += stats["losses"]
            cum_r += stats["total_r"]
            total = cum_w + cum_l
            if total < 3:
                continue
            wr = cum_w / total * 100
            if wr >= 45 and cum_r > 0:
                best_thresh = int(br.split("-")[0])
                found = True

        if not found:
            best_thresh = min(old + 3, MIN_CONVICTION_CEIL)

        delta = max(-5, min(5, best_thresh - old))
        new = max(MIN_CONVICTION_FLOOR, min(MIN_CONVICTION_CEIL, old + delta))
        self._memory["min_conviction"] = new
        self._memory["recal_count"] = self._memory.get("recal_count", 0) + 1
        self._memory["last_recal"] = datetime.now(timezone.utc).isoformat()
        self._save_memory()

        total_out = sum(b["wins"] + b["losses"] for b in buckets.values())
        total_wins = sum(b["wins"] for b in buckets.values())
        wr = total_wins / total_out * 100 if total_out > 0 else 0
        arrow = "^" if new > old else "v" if new < old else "="
        log.info(f"  SKILL RECAL #{self._memory['recal_count']}: "
                 f"min {old:.0f}{arrow}{new:.0f} | "
                 f"{total_out} trades WR={wr:.0f}%")

        for gr in ["A+", "A", "B", "C", "D"]:
            gs = self._memory["grade_stats"].get(gr)
            if gs and gs["wins"] + gs["losses"] > 0:
                gt = gs["wins"] + gs["losses"]
                gwr = gs["wins"] / gt * 100
                log.info(f"    {gr:>2}: {gt} trades WR={gwr:.0f}% R={gs['total_r']:+.2f}")

    @property
    def min_conviction(self):
        with self._lock:
            return self._memory["min_conviction"]

    @property
    def stats(self) -> dict:
        with self._lock:
            m = self._memory
            total = sum(b["wins"] + b["losses"] for b in m["buckets"].values())
            wins = sum(b["wins"] for b in m["buckets"].values())
            return {
                "min_conviction": m["min_conviction"],
                "evaluated": m["total_evaluated"],
                "passed": m["total_passed"],
                "rejected": m["total_rejected"],
                "outcomes": total,
                "win_rate": wins / total * 100 if total > 0 else 0,
                "recalibrations": m["recal_count"],
                "grade_stats": dict(m["grade_stats"]),
            }

    def log_status(self):
        s = self.stats
        if s["outcomes"] > 0:
            log.info(f"  Skill: min={s['min_conviction']:.0f} "
                     f"eval={s['evaluated']} pass={s['passed']} "
                     f"rej={s['rejected']} WR={s['win_rate']:.0f}%")
        self._learner.log_status()

    @property
    def learner(self) -> BayesianLearner:
        return self._learner
