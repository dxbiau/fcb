"""
live/pair_intel.py — Pre-Session Pair Intelligence Engine

Runs BEFORE each session's pair scan to profile every candidate pair
using 3 sessions of recent 5m candle history (~24h lookback).

PURPOSE:
  The pair scanner checks IF a pair is tradeable (volume, spread, range).
  This module checks WHETHER we SHOULD trade it — and HOW.

WHAT IT MEASURES:
  1. BREAKOUT FOLLOW-THROUGH  — Does price actually run after breaking FC range?
     (Pairs that break out and immediately reverse = death for FCB.)
  2. CONGESTION ZONE DENSITY   — How much price clusters at certain levels?
     (Pairs that stall at dense price levels = our trail triggers for nothing.)
  3. CLEAN RANGE / WICK RATIO  — Do candles have bodies or just wicks?
     (Wick-heavy pairs = noise, body-heavy = directional conviction.)
  4. SESSION MOMENTUM PROFILE   — Does the upcoming session historically trend?
     (Some pairs only move in Asia, others in NY — match pair to session.)
  5. ATR-NORMALIZED VOLATILITY  — Is current ATR high/low vs recent norm?
     (Entry when vol is elevated = bigger R-moves, better trail captures.)
  6. LIQUIDATION MAGNET ZONES   — Price levels with high revisit frequency.
     (If our TP/trail path crosses a magnet zone, profit gets stuck.)

OUTPUT:
  PairProfile with a composite 'fitness_score' (0-100).
  Used by pair_scanner to RANK pairs beyond just volume/spread.

INTEGRATION:
  Called by pair_scanner.scan_session_pairs() after basic filtering.
  Adds ~2-5 seconds per pair (fetches 3×8h of 5m candles = 288 candles).

NO EXTERNAL DEPENDENCIES — uses only ccxt (already imported) + stdlib math.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from live import exchange as exch
from live import logger as log
from live.config import SESSIONS, API_DELAY_SECS


# ─── Configuration ───
LOOKBACK_CANDLES   = 200       # ~16.7h of 5m data (2 sessions) — safe for Bybit API limit
ZONE_BUCKET_MULT   = 0.002     # 0.2% price bucket for zone detection
ZONE_MIN_TOUCHES   = 4         # min revisits to qualify as a congestion zone
MOMENTUM_WINDOW    = 12        # 12 candles (1h) for momentum measurement
ATR_PERIOD         = 14        # standard ATR period
VOLUME_PROFILE_BINS = 50       # number of price bins for volume profile

# ─── S/R Detection Sensitivity Presets ───
# Each preset: (swing_lookback, cluster_tolerance_pct, min_touches_strong,
#               min_touches_normal, recency_weight)
SR_PRESETS = {
    "weak":   (7, 0.0035, 5, 3, 0.20),   # only the most obvious levels
    "normal": (5, 0.0025, 3, 2, 0.30),   # balanced detection
    "strong": (3, 0.0018, 2, 1, 0.40),   # catches subtle levels too
}
SR_SENSITIVITY = "normal"  # default — can be overridden from config


@dataclass
class SRLevel:
    """A key Support or Resistance price level."""
    price: float               # cluster-center price
    level_type: str            # "support", "resistance", or "dual"
    touches: int               # number of swing touches at this level
    strength: str              # "weak", "normal", "strong"
    strength_score: float      # 0.0 - 1.0 (composite strength)
    last_touch_idx: int        # candle index of most recent touch
    avg_volume: float          # avg volume of candles at touch points
    recency_pct: float         # 0-1 how recent the last touch is (1.0=latest candle)


@dataclass
class CongestionZone:
    """A price level where price tends to stall/cluster."""
    price_low: float
    price_high: float
    midpoint: float
    touches: int              # how many candles visited this zone
    total_volume: float       # cumulative volume in this zone
    avg_dwell_candles: float  # avg consecutive candles spent here
    strength: float           # composite strength (0-1)


@dataclass
class PairProfile:
    """Complete intelligence profile for a single pair."""
    symbol: str
    session: str

    # ─── Breakout Quality ───
    breakout_follow_pct: float = 0.0     # % of FC breaks that actually ran 1R+
    breakout_reversal_pct: float = 0.0   # % that reversed immediately
    avg_follow_r: float = 0.0            # avg R-move after breakout (higher = better)

    # ─── Congestion ───
    congestion_density: float = 0.0      # % of price range covered by stall zones
    congestion_zones: List[CongestionZone] = field(default_factory=list)
    zone_near_entry: bool = False        # congestion zone right at FC boundary

    # ─── Candle Quality ───
    avg_body_ratio: float = 0.0          # avg body/range (1.0 = no wicks, 0 = all wick)
    clean_candle_pct: float = 0.0        # % candles with body > 60% of range
    directional_bias: float = 0.0        # net bullish/bearish lean (-1 to +1)

    # ─── Volatility ───
    atr_current: float = 0.0            # current ATR (5m)
    atr_mean: float = 0.0               # mean ATR over lookback
    atr_ratio: float = 0.0              # current/mean (>1 = elevated vol)
    range_expansion: float = 0.0         # how much range expanded vs normal

    # ─── Session Momentum ───
    session_trend_strength: float = 0.0  # how directional the session period is
    session_avg_range_pct: float = 0.0   # avg session range in this time window
    session_follow_through: float = 0.0  # does the session's first move continue?

    # ─── Volume Profile ───
    poc_price: float = 0.0              # Point of Control (highest volume price)
    poc_distance_pct: float = 0.0       # how far current price is from POC
    volume_asymmetry: float = 0.0       # ratio of volume above vs below current price

    # ─── Support & Resistance ───
    sr_levels: List[SRLevel] = field(default_factory=list)  # all detected S/R levels
    sr_support_near: float = 0.0       # nearest support below current price (0=none)
    sr_resistance_near: float = 0.0    # nearest resistance above current price (0=none)
    sr_range_bound: bool = False       # True if price is squeezed between strong S&R
    sr_range_pct: float = 0.0          # distance between nearest S and R as % of price
    sr_count: int = 0                  # total S/R levels found

    # ─── Composite ───
    fitness_score: float = 0.0          # 0-100 composite (higher = better to trade)
    fitness_grade: str = "?"            # S/A/B/C/D
    flags: List[str] = field(default_factory=list)


def profile_pair(exchange, symbol: str, session: str,
                 current_price: float = 0.0) -> Optional[PairProfile]:
    """
    Build a complete intelligence profile for a pair.

    Fetches 288 recent 5m candles (~24h) and computes all metrics.
    Returns PairProfile or None on failure.
    """
    try:
        candles = exch.fetch_latest_candles(exchange, symbol, n=LOOKBACK_CANDLES)
    except Exception as e:
        log.warning(f"[INTEL] {symbol}: failed to fetch candles — {e}")
        return None

    if not candles or len(candles) < 30:
        return None

    profile = PairProfile(symbol=symbol, session=session)

    if current_price <= 0 and candles:
        current_price = candles[-1]["close"]

    # ─── 1. Compute ATR & Volatility ───
    _compute_volatility(candles, profile)

    # ─── 2. Candle Quality (body/wick ratio) ───
    _compute_candle_quality(candles, profile)

    # ─── 3. Breakout Follow-Through (simulated FC breaks) ───
    _compute_breakout_quality(candles, profile)

    # ─── 4. Congestion Zone Detection ───
    _compute_congestion_zones(candles, current_price, profile)

    # ─── 5. Session Momentum Profile ───
    _compute_session_momentum(candles, session, profile)

    # ─── 6. Volume Profile ───
    _compute_volume_profile(candles, current_price, profile)

    # ─── 7. Support & Resistance Levels ───
    _compute_sr_levels(candles, current_price, profile)

    # ─── 8. Composite Fitness Score ───
    _compute_fitness_score(profile)

    return profile


# ═══════════════════════════════════════════════════════════════
#  COMPONENT ANALYZERS
# ═══════════════════════════════════════════════════════════════

def _compute_volatility(candles: List[Dict], profile: PairProfile):
    """ATR and volatility regime detection."""
    trs = []
    for i, c in enumerate(candles):
        h, l, cl = c["high"], c["low"], c["close"]
        if i == 0:
            tr = h - l
        else:
            prev_c = candles[i - 1]["close"]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    if len(trs) < ATR_PERIOD:
        return

    # Full ATR series
    atr_series = []
    atr = sum(trs[:ATR_PERIOD]) / ATR_PERIOD
    atr_series.append(atr)
    for i in range(ATR_PERIOD, len(trs)):
        atr = (atr * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD
        atr_series.append(atr)

    profile.atr_current = atr_series[-1] if atr_series else 0
    profile.atr_mean = sum(atr_series) / len(atr_series) if atr_series else 0

    if profile.atr_mean > 0:
        profile.atr_ratio = profile.atr_current / profile.atr_mean
    else:
        profile.atr_ratio = 1.0

    # Range expansion: current vs mean high-low
    recent_ranges = [(c["high"] - c["low"]) for c in candles[-24:]]
    older_ranges = [(c["high"] - c["low"]) for c in candles[:-24]] if len(candles) > 24 else recent_ranges
    if older_ranges:
        mean_old = sum(older_ranges) / len(older_ranges)
        mean_new = sum(recent_ranges) / len(recent_ranges)
        profile.range_expansion = mean_new / mean_old if mean_old > 0 else 1.0


def _compute_candle_quality(candles: List[Dict], profile: PairProfile):
    """Body-to-range ratio and directional quality."""
    body_ratios = []
    clean_count = 0
    bull_count = 0
    bear_count = 0

    for c in candles:
        rng = c["high"] - c["low"]
        if rng <= 0:
            continue
        body = abs(c["close"] - c["open"])
        ratio = body / rng
        body_ratios.append(ratio)

        if ratio > 0.60:
            clean_count += 1

        if c["close"] > c["open"]:
            bull_count += 1
        elif c["close"] < c["open"]:
            bear_count += 1

    n = len(body_ratios)
    if n > 0:
        profile.avg_body_ratio = sum(body_ratios) / n
        profile.clean_candle_pct = clean_count / n
        total_dir = bull_count + bear_count
        if total_dir > 0:
            profile.directional_bias = (bull_count - bear_count) / total_dir


def _compute_breakout_quality(candles: List[Dict], profile: PairProfile):
    """
    Simulate FC breakouts on historical candles to measure follow-through.

    For each session-aligned window, treat the first candle as FC,
    check if candle 2 breaks, then measure how far price runs.
    """
    if len(candles) < 10:
        return

    follow_runs = []
    reversals = 0
    total_breaks = 0

    # Slide through candles in groups simulating FC patterns
    # Use every 12th candle as a "session start" (every hour) for more samples
    step = max(3, min(12, len(candles) // 20))

    for i in range(0, len(candles) - 5, step):
        fc = candles[i]
        fc_high = fc["high"]
        fc_low = fc["low"]
        fc_range = fc_high - fc_low
        if fc_range <= 0:
            continue
        fc_mid = (fc_high + fc_low) / 2

        c2 = candles[i + 1]
        c2_close = c2["close"]

        # Check for breakout
        if c2_close > fc_high:
            direction = "long"
            entry = c2_close
            sl = fc_mid
            risk = entry - sl
        elif c2_close < fc_low:
            direction = "short"
            entry = c2_close
            sl = fc_mid
            risk = sl - entry
        else:
            continue  # no breakout

        if risk <= 0:
            continue

        total_breaks += 1

        # Measure follow-through over next 3 candles
        best_r = 0.0
        worst_r = 0.0
        for j in range(i + 2, min(i + 5, len(candles))):
            fj = candles[j]
            if direction == "long":
                run = fj["high"] - entry
                adverse = entry - fj["low"]
            else:
                run = entry - fj["low"]
                adverse = fj["high"] - entry

            best_r = max(best_r, run / risk)
            worst_r = max(worst_r, adverse / risk)

        follow_runs.append(best_r)

        # Reversal = price went 0.5R against before going 0.5R in favor
        if worst_r > 0.5 and best_r < 0.5:
            reversals += 1

    if total_breaks > 0:
        # % that reached at least 1R
        profile.breakout_follow_pct = sum(1 for r in follow_runs if r >= 1.0) / total_breaks
        profile.breakout_reversal_pct = reversals / total_breaks
        profile.avg_follow_r = sum(follow_runs) / len(follow_runs) if follow_runs else 0


def _compute_congestion_zones(candles: List[Dict], current_price: float,
                               profile: PairProfile):
    """
    Detect price levels where the asset repeatedly stalls.

    Uses a price-bucketing approach: divide the recent range into
    0.2% buckets, count how many candles touched each bucket,
    then identify clusters with 4+ touches as congestion zones.
    """
    if not candles or current_price <= 0:
        return

    all_highs = [c["high"] for c in candles]
    all_lows = [c["low"] for c in candles]
    price_high = max(all_highs)
    price_low = min(all_lows)
    price_range = price_high - price_low

    if price_range <= 0:
        return

    bucket_size = current_price * ZONE_BUCKET_MULT
    if bucket_size <= 0:
        return

    n_buckets = int(price_range / bucket_size) + 1
    if n_buckets > 500:  # safety cap
        bucket_size = price_range / 500
        n_buckets = 500

    # Count touches per bucket
    touches = [0] * n_buckets
    volumes = [0.0] * n_buckets
    dwell = defaultdict(list)  # bucket_idx → list of consecutive candle counts

    for ci, c in enumerate(candles):
        lo_idx = max(0, int((c["low"] - price_low) / bucket_size))
        hi_idx = min(n_buckets - 1, int((c["high"] - price_low) / bucket_size))
        vol_per_bucket = c["volume"] / max(1, hi_idx - lo_idx + 1)
        for b in range(lo_idx, hi_idx + 1):
            touches[b] += 1
            volumes[b] += vol_per_bucket

    # Identify congestion zones (consecutive high-touch buckets)
    zones = []
    i = 0
    while i < n_buckets:
        if touches[i] >= ZONE_MIN_TOUCHES:
            zone_start = i
            zone_vol = 0.0
            zone_touches = 0
            while i < n_buckets and touches[i] >= ZONE_MIN_TOUCHES:
                zone_vol += volumes[i]
                zone_touches += touches[i]
                i += 1
            zone_end = i - 1

            zone_low = price_low + zone_start * bucket_size
            zone_high = price_low + (zone_end + 1) * bucket_size
            zone_mid = (zone_low + zone_high) / 2.0
            n_buckets_in_zone = zone_end - zone_start + 1

            zones.append(CongestionZone(
                price_low=zone_low,
                price_high=zone_high,
                midpoint=zone_mid,
                touches=zone_touches,
                total_volume=zone_vol,
                avg_dwell_candles=zone_touches / max(1, n_buckets_in_zone),
                strength=min(1.0, zone_touches / (len(candles) * 0.5)),
            ))
        else:
            i += 1

    profile.congestion_zones = zones

    # Congestion density: what % of the price range is "congested"
    congested_range = sum((z.price_high - z.price_low) for z in zones)
    profile.congestion_density = congested_range / price_range if price_range > 0 else 0

    # Check if any zone is within 0.3% of current price (entry danger)
    for z in zones:
        dist_pct = abs(current_price - z.midpoint) / current_price
        if dist_pct < 0.003:
            profile.zone_near_entry = True
            profile.flags.append(f"CONGESTION_AT_PRICE({z.midpoint:.4f})")
            break


def _compute_session_momentum(candles: List[Dict], session: str,
                               profile: PairProfile):
    """
    Analyze how this pair behaves during the upcoming session's hours.

    Extracts candles from the relevant time window and measures:
    - Trend strength (are moves directional or choppy?)
    - Follow-through (does early direction persist?)
    - Average range during this session
    """
    sess_start, sess_end = SESSIONS.get(session, (0, 8))

    session_candles = []
    for c in candles:
        ts_s = c["ts"] / 1000
        hour = int((ts_s % 86400) / 3600)
        if sess_start <= hour < sess_end:
            session_candles.append(c)

    if len(session_candles) < 6:
        return

    # Trend strength: sum of signed moves / sum of absolute moves
    signed_moves = []
    abs_moves = []
    for c in session_candles:
        move = c["close"] - c["open"]
        signed_moves.append(move)
        abs_moves.append(abs(move))

    sum_abs = sum(abs_moves)
    if sum_abs > 0:
        profile.session_trend_strength = abs(sum(signed_moves)) / sum_abs

    # Average range during session
    ranges = [(c["high"] - c["low"]) / c["close"] * 100
              for c in session_candles if c["close"] > 0]
    if ranges:
        profile.session_avg_range_pct = sum(ranges) / len(ranges)

    # Follow-through: does the first 1h direction match the session result?
    if len(session_candles) >= 12:
        first_hour = session_candles[:12]
        first_move = first_hour[-1]["close"] - first_hour[0]["open"]

        full_move = session_candles[-1]["close"] - session_candles[0]["open"]
        if first_move != 0:
            # Same sign = follow-through
            profile.session_follow_through = 1.0 if (first_move * full_move > 0) else 0.0


def _compute_volume_profile(candles: List[Dict], current_price: float,
                             profile: PairProfile):
    """
    Build a simplified volume profile to find the Point of Control (POC).

    POC = price level with the most volume. If we're far from POC,
    price is likely to gravitate back (mean reversion risk).
    If we're at POC, breakout has max resistance to overcome.
    """
    if not candles or current_price <= 0:
        return

    price_high = max(c["high"] for c in candles)
    price_low = min(c["low"] for c in candles)
    price_range = price_high - price_low

    if price_range <= 0:
        return

    bin_size = price_range / VOLUME_PROFILE_BINS
    if bin_size <= 0:
        return

    volume_bins = [0.0] * VOLUME_PROFILE_BINS

    for c in candles:
        # Distribute candle volume across its range
        lo_bin = max(0, int((c["low"] - price_low) / bin_size))
        hi_bin = min(VOLUME_PROFILE_BINS - 1, int((c["high"] - price_low) / bin_size))
        n_bins = max(1, hi_bin - lo_bin + 1)
        vol_each = c["volume"] / n_bins

        for b in range(lo_bin, hi_bin + 1):
            volume_bins[b] += vol_each

    # Find POC
    poc_bin = volume_bins.index(max(volume_bins))
    profile.poc_price = price_low + (poc_bin + 0.5) * bin_size

    if current_price > 0:
        profile.poc_distance_pct = abs(current_price - profile.poc_price) / current_price * 100

    # Volume asymmetry: volume above vs below current price
    current_bin = min(VOLUME_PROFILE_BINS - 1,
                      max(0, int((current_price - price_low) / bin_size)))
    vol_above = sum(volume_bins[current_bin + 1:])
    vol_below = sum(volume_bins[:current_bin])
    total_vol = vol_above + vol_below

    if total_vol > 0:
        profile.volume_asymmetry = (vol_above - vol_below) / total_vol


# ═══════════════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE DETECTION
# ═══════════════════════════════════════════════════════════════

def _find_swing_points(candles: List[Dict], lookback: int
                       ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """
    Find swing highs and swing lows using a rolling window.

    A swing high at index i means candle[i].high is the highest high
    in the window [i - lookback, i + lookback].
    A swing low  at index i means candle[i].low is the lowest low
    in that same window.

    Returns: (swing_highs, swing_lows)
        Each is a list of (candle_index, price).
    """
    swing_highs: List[Tuple[int, float]] = []
    swing_lows: List[Tuple[int, float]] = []
    n = len(candles)

    for i in range(lookback, n - lookback):
        # ── Swing high check ──
        is_high = True
        h_i = candles[i]["high"]
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j]["high"] >= h_i:
                is_high = False
                break
        if is_high:
            swing_highs.append((i, h_i))

        # ── Swing low check ──
        is_low = True
        l_i = candles[i]["low"]
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j]["low"] <= l_i:
                is_low = False
                break
        if is_low:
            swing_lows.append((i, l_i))

    return swing_highs, swing_lows


def _cluster_sr_levels(swing_highs: List[Tuple[int, float]],
                       swing_lows: List[Tuple[int, float]],
                       candles: List[Dict],
                       tolerance_pct: float,
                       recency_weight: float,
                       min_touches_strong: int,
                       min_touches_normal: int,
                       ) -> List[SRLevel]:
    """
    Cluster nearby swing points into merged S/R levels.

    Two swing points merge if they're within tolerance_pct of each other.
    Each cluster becomes one SRLevel with aggregated statistics.
    """
    n_candles = len(candles)
    if n_candles == 0:
        return []

    # Build a unified list of swing points: (index, price, type)
    # type: 'H' for swing high (resistance), 'L' for swing low (support)
    all_swings: List[Tuple[int, float, str]] = []
    for idx, price in swing_highs:
        all_swings.append((idx, price, "H"))
    for idx, price in swing_lows:
        all_swings.append((idx, price, "L"))

    if not all_swings:
        return []

    # Sort by price for clustering
    all_swings.sort(key=lambda x: x[1])

    # Greedy clustering: merge swings within tolerance
    clusters: List[List[Tuple[int, float, str]]] = []
    current_cluster = [all_swings[0]]

    for i in range(1, len(all_swings)):
        _, prev_price, _ = current_cluster[-1]
        _, cur_price, _ = all_swings[i]

        # Merge if within tolerance of cluster's first member
        cluster_center = sum(p for _, p, _ in current_cluster) / len(current_cluster)
        if abs(cur_price - cluster_center) / cluster_center <= tolerance_pct:
            current_cluster.append(all_swings[i])
        else:
            clusters.append(current_cluster)
            current_cluster = [all_swings[i]]

    clusters.append(current_cluster)

    # Build SRLevel from each cluster
    levels: List[SRLevel] = []
    avg_vol = sum(c["volume"] for c in candles) / n_candles if n_candles else 1

    for cluster in clusters:
        touches = len(cluster)
        if touches == 0:
            continue

        # Cluster center price (volume-weighted if we had volume, simple avg here)
        center_price = sum(p for _, p, _ in cluster) / touches

        # Type: support if mostly lows, resistance if mostly highs, dual if mixed
        n_highs = sum(1 for _, _, t in cluster if t == "H")
        n_lows = sum(1 for _, _, t in cluster if t == "L")
        if n_highs > 0 and n_lows > 0:
            level_type = "dual"       # both S and R → very significant
        elif n_highs > 0:
            level_type = "resistance"
        else:
            level_type = "support"

        # Recency: how recent is the last touch?
        last_idx = max(idx for idx, _, _ in cluster)
        recency_pct = last_idx / max(1, n_candles - 1)

        # Volume at touch candles
        touch_vols = []
        for idx, _, _ in cluster:
            if 0 <= idx < n_candles:
                touch_vols.append(candles[idx]["volume"])
        avg_touch_vol = sum(touch_vols) / len(touch_vols) if touch_vols else 0

        # Strength scoring:
        #  - Touches (more = stronger): base 0.15 per touch, cap at 0.6
        #  - Recency: recent levels matter more
        #  - Volume: high volume at touches = institutional interest
        #  - Dual type: both S and R at same level = major level
        touch_score = min(0.6, touches * 0.15)
        recency_score = recency_pct * recency_weight
        vol_score = min(0.15, 0.15 * (avg_touch_vol / avg_vol)) if avg_vol > 0 else 0
        dual_bonus = 0.15 if level_type == "dual" else 0.0

        strength_score = min(1.0, touch_score + recency_score + vol_score + dual_bonus)

        # Classify
        if touches >= min_touches_strong and strength_score >= 0.55:
            strength = "strong"
        elif touches >= min_touches_normal and strength_score >= 0.30:
            strength = "normal"
        else:
            strength = "weak"

        levels.append(SRLevel(
            price=center_price,
            level_type=level_type,
            touches=touches,
            strength=strength,
            strength_score=strength_score,
            last_touch_idx=last_idx,
            avg_volume=avg_touch_vol,
            recency_pct=recency_pct,
        ))

    # Sort by strength descending
    levels.sort(key=lambda l: -l.strength_score)
    return levels


def _compute_sr_levels(candles: List[Dict], current_price: float,
                       profile: PairProfile):
    """
    Detect key Support & Resistance levels and annotate the profile.

    Uses swing-point detection with configurable sensitivity,
    then clusters nearby swings into actionable S/R levels.
    """
    if len(candles) < 20 or current_price <= 0:
        return

    # Load sensitivity preset
    try:
        from live.config import SR_SENSITIVITY as _cfg_sens
        sens = _cfg_sens
    except (ImportError, AttributeError):
        sens = SR_SENSITIVITY  # module-level default

    preset = SR_PRESETS.get(sens, SR_PRESETS["normal"])
    lookback, tolerance, min_strong, min_normal, recency_w = preset

    # Step 1: Find swing highs and lows
    swing_highs, swing_lows = _find_swing_points(candles, lookback)

    if not swing_highs and not swing_lows:
        return

    # Step 2: Cluster into S/R levels
    levels = _cluster_sr_levels(
        swing_highs, swing_lows, candles,
        tolerance_pct=tolerance,
        recency_weight=recency_w,
        min_touches_strong=min_strong,
        min_touches_normal=min_normal,
    )

    profile.sr_levels = levels
    profile.sr_count = len(levels)

    if not levels:
        return

    # Step 3: Find nearest support below and resistance above current price
    supports = [l for l in levels if l.price < current_price
                and l.level_type in ("support", "dual")]
    resistances = [l for l in levels if l.price > current_price
                   and l.level_type in ("resistance", "dual")]

    if supports:
        # Nearest support = highest price below current
        nearest_sup = max(supports, key=lambda l: l.price)
        profile.sr_support_near = nearest_sup.price
    if resistances:
        # Nearest resistance = lowest price above current
        nearest_res = min(resistances, key=lambda l: l.price)
        profile.sr_resistance_near = nearest_res.price

    # Step 4: Range-bound detection
    # If strong S and R are close together → price is squeezed
    if profile.sr_support_near > 0 and profile.sr_resistance_near > 0:
        sr_gap = profile.sr_resistance_near - profile.sr_support_near
        profile.sr_range_pct = sr_gap / current_price * 100

        # Squeezed = gap is less than 1.5% AND both levels are at least "normal"
        sup_strength = max((l.strength_score for l in supports
                           if l.price == profile.sr_support_near), default=0)
        res_strength = max((l.strength_score for l in resistances
                           if l.price == profile.sr_resistance_near), default=0)

        if profile.sr_range_pct < 1.5 and sup_strength >= 0.3 and res_strength >= 0.3:
            profile.sr_range_bound = True
            profile.flags.append(
                f"SR_SQUEEZE(S@{profile.sr_support_near:.4f} "
                f"R@{profile.sr_resistance_near:.4f} "
                f"gap={profile.sr_range_pct:.2f}%)"
            )

    # Flag strong levels near current price
    for l in levels[:5]:  # top 5 by strength
        dist_pct = abs(l.price - current_price) / current_price * 100
        if dist_pct < 0.5 and l.strength in ("strong", "normal"):
            tag = "S" if l.level_type == "support" else (
                "R" if l.level_type == "resistance" else "S/R")
            profile.flags.append(
                f"NEAR_{tag}({l.price:.4f} {l.strength} {l.touches}t)")


# ═══════════════════════════════════════════════════════════════
#  COMPOSITE FITNESS SCORING
# ═══════════════════════════════════════════════════════════════

def _compute_fitness_score(profile: PairProfile):
    """
    Compute a 0-100 fitness score from all component metrics.

    WEIGHTS (what matters most for FCB profitability):
      - Breakout follow-through:  30%  (THE most important — does it run?)
      - Low congestion:           20%  (clear path for trail to work)
      - Candle quality:           15%  (body-heavy = conviction)
      - ATR expansion:            15%  (elevated vol = bigger R-moves)
      - Session momentum:         10%  (is this pair's session active?)
      - POC distance:             10%  (far from magnet = room to run)
    """
    score = 0.0

    # ── Breakout Follow-Through (0-30 pts) ──
    # follow_pct: 0-1, where 0.5+ is excellent for FCB
    follow_pts = min(30.0, profile.breakout_follow_pct * 60.0)
    # Penalty for high reversal rate
    if profile.breakout_reversal_pct > 0.4:
        follow_pts *= 0.5
        profile.flags.append("HIGH_REVERSAL")
    score += follow_pts

    # ── Low Congestion (0-20 pts) ──
    # congestion_density: 0-1, lower is better
    if profile.congestion_density < 0.3:
        cong_pts = 20.0
    elif profile.congestion_density < 0.5:
        cong_pts = 15.0
    elif profile.congestion_density < 0.7:
        cong_pts = 8.0
    else:
        cong_pts = 2.0
        profile.flags.append("HIGH_CONGESTION")

    # Extra penalty if congestion is right at current price
    if profile.zone_near_entry:
        cong_pts *= 0.5
    score += cong_pts

    # ── Candle Quality (0-15 pts) ──
    # clean_candle_pct: prefer > 40% clean candles
    candle_pts = min(15.0, profile.clean_candle_pct * 37.5)
    # avg_body_ratio bonus
    if profile.avg_body_ratio > 0.55:
        candle_pts = min(15.0, candle_pts * 1.2)
    score += candle_pts

    # ── ATR Expansion (0-15 pts) ──
    # atr_ratio > 1 means current vol is above average (good)
    if profile.atr_ratio >= 1.3:
        atr_pts = 15.0     # elevated vol — ideal for breakouts
    elif profile.atr_ratio >= 1.0:
        atr_pts = 12.0     # normal-to-high — fine
    elif profile.atr_ratio >= 0.7:
        atr_pts = 6.0      # below average — cautious
    else:
        atr_pts = 2.0      # dead vol — avoid
        profile.flags.append("LOW_VOLATILITY")
    score += atr_pts

    # ── Session Momentum (0-10 pts) ──
    # trend_strength: 0-1, higher = more directional
    sess_pts = min(10.0, profile.session_trend_strength * 20.0)
    # Follow-through bonus
    if profile.session_follow_through > 0.5:
        sess_pts = min(10.0, sess_pts * 1.3)
    score += sess_pts

    # ── POC Distance (0-10 pts) ──
    # Further from POC = less gravitational pull = better for trend
    if profile.poc_distance_pct > 2.0:
        poc_pts = 10.0      # well away from volume magnet
    elif profile.poc_distance_pct > 1.0:
        poc_pts = 7.0
    elif profile.poc_distance_pct > 0.5:
        poc_pts = 4.0
    else:
        poc_pts = 1.0       # sitting right on POC — max resistance
        profile.flags.append("AT_POC")
    score += poc_pts

    # ── S/R Structure Quality (modifier: -10 to +5 pts) ──
    # Range-bound pairs penalized; clean S/R structure is a mild positive
    if profile.sr_range_bound:
        score -= 10         # squeezed between S&R = breakout death trap
    elif profile.sr_count >= 2:
        # Has identifiable structure — good for R:R framing
        strong_levels = sum(1 for l in profile.sr_levels if l.strength == "strong")
        if strong_levels >= 2:
            score += 5      # clear structure = better trade decisions
        elif strong_levels >= 1:
            score += 2

    # ── Clamp & Grade ──
    profile.fitness_score = max(0.0, min(100.0, score))

    if profile.fitness_score >= 75:
        profile.fitness_grade = "S"
    elif profile.fitness_score >= 60:
        profile.fitness_grade = "A"
    elif profile.fitness_score >= 45:
        profile.fitness_grade = "B"
    elif profile.fitness_score >= 30:
        profile.fitness_grade = "C"
    else:
        profile.fitness_grade = "D"


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API — called by pair_scanner
# ═══════════════════════════════════════════════════════════════

def rank_pairs(exchange, candidates: List[Dict], session: str,
               min_fitness: float = 25.0) -> List[Dict]:
    """
    Profile and rank a list of candidate pairs by fitness.

    Args:
        exchange: ccxt exchange instance
        candidates: list of dicts from pair_scanner (symbol, class, turnover, etc.)
        session: current session name ("asia", "london", "ny")
        min_fitness: minimum fitness score to include (default 25)

    Returns:
        Sorted list of candidates with profile attached, best first.
        A-class pairs are never filtered out (only re-ranked).
    """
    t0 = time.time()
    profiled = []
    failed = 0

    for cand in candidates:
        sym = cand["symbol"]
        price = cand.get("price", 0)

        try:
            profile = profile_pair(exchange, sym, session, current_price=price)
        except Exception as e:
            log.warning(f"[INTEL] {sym}: profiling failed — {e}")
            profile = None
            failed += 1

        if profile:
            cand["profile"] = profile
            cand["fitness"] = profile.fitness_score
            cand["fitness_grade"] = profile.fitness_grade
            cand["intel_flags"] = profile.flags
        else:
            # No profile — assign neutral score
            cand["profile"] = None
            cand["fitness"] = 50.0  # neutral — don't penalize API failures
            cand["fitness_grade"] = "?"
            cand["intel_flags"] = []

        profiled.append(cand)

        # Rate limiting
        time.sleep(API_DELAY_SECS)

    # Sort: A-class always on top, then by fitness score descending
    profiled.sort(key=lambda x: (
        0 if x["class"] == "A" else 1,          # A-class first
        -x["fitness"],                            # then by fitness
        -x.get("turnover", 0),                    # then by volume
    ))

    # Filter out low-fitness B-class pairs (A-class never filtered)
    result = []
    for p in profiled:
        if p["class"] == "A" or p["fitness"] >= min_fitness:
            result.append(p)
        else:
            log.info(f"[INTEL] SKIP {p['symbol']} fitness={p['fitness']:.0f} "
                     f"grade={p['fitness_grade']} flags={p['intel_flags']}")

    elapsed = time.time() - t0

    # Log summary
    grades = defaultdict(int)
    for p in result:
        grades[p["fitness_grade"]] += 1

    log.info(f"[INTEL] Profiled {len(profiled)} pairs in {elapsed:.1f}s "
             f"(failed={failed})")
    log.info(f"[INTEL] Grades: {dict(grades)} | "
             f"Passed: {len(result)}/{len(profiled)}")

    # Log top 10
    for p in result[:10]:
        prof = p.get("profile")
        flags = " ".join(p["intel_flags"]) if p["intel_flags"] else "clean"
        if prof:
            log.info(
                f"  {p['fitness_grade']}{p['fitness']:3.0f} {p['class']} "
                f"{p['symbol']:<24} "
                f"follow={prof.breakout_follow_pct:.0%} "
                f"cong={prof.congestion_density:.0%} "
                f"body={prof.avg_body_ratio:.0%} "
                f"atr×{prof.atr_ratio:.1f} "
                f"poc={prof.poc_distance_pct:.1f}% "
                f"sr={prof.sr_count}lvl "
                f"| {flags}"
            )
        else:
            log.info(f"  ?{p['fitness']:3.0f} {p['class']} {p['symbol']:<24} | no profile")

    return result


def get_congestion_zones_for_trade(profile: Optional[PairProfile],
                                    entry: float, tp: float, sl: float
                                    ) -> List[CongestionZone]:
    """
    Return any congestion zones that lie between entry and TP.

    Used by the bot to assess whether the profit path is obstructed.
    If zones exist between entry and TP, the trade has higher stall risk.
    """
    if not profile or not profile.congestion_zones:
        return []

    lo = min(entry, tp)
    hi = max(entry, tp)

    blocking = []
    for z in profile.congestion_zones:
        # Zone overlaps with the entry→TP path
        if z.price_low <= hi and z.price_high >= lo:
            # Exclude zones right at entry (those are expected)
            zone_at_entry = abs(z.midpoint - entry) / entry < 0.001
            if not zone_at_entry:
                blocking.append(z)

    return blocking


def get_sr_context_for_trade(profile: Optional[PairProfile],
                              entry: float, tp: float, sl: float,
                              direction: str) -> Dict:
    """
    Evaluate S/R context for a specific trade setup.

    Returns a dict with:
      - score_adj:    int, context score adjustment (positive=good, negative=bad)
      - flags:        list of str flags describing the S/R context
      - blocking:     list of SRLevel objects in the TP path
      - at_level:     SRLevel or None if entry is right at a key level
      - range_bound:  bool, True if squeezed between strong S&R

    Called by bot.py during the pre-entry confidence check.
    """
    result = {
        "score_adj": 0,
        "flags": [],
        "blocking": [],
        "at_level": None,
        "range_bound": False,
    }

    if not profile or not profile.sr_levels:
        return result

    levels = profile.sr_levels
    flags = result["flags"]

    # ── 1. S/R levels blocking the TP path ──
    # For longs: resistance between entry and TP
    # For shorts: support between entry and TP
    tp_lo = min(entry, tp)
    tp_hi = max(entry, tp)
    risk = abs(entry - sl)
    if risk <= 0:
        return result

    blocking_levels = []
    for lev in levels:
        if tp_lo < lev.price < tp_hi:
            # Level is in the TP path
            # For longs, resistance blocks profit; for shorts, support blocks profit
            if direction == "long" and lev.level_type in ("resistance", "dual"):
                blocking_levels.append(lev)
            elif direction == "short" and lev.level_type in ("support", "dual"):
                blocking_levels.append(lev)

    result["blocking"] = blocking_levels

    if blocking_levels:
        # Score penalty based on strongest blocking level
        strongest = max(blocking_levels, key=lambda l: l.strength_score)
        if strongest.strength == "strong":
            result["score_adj"] -= 15
            flags.append(
                f"SR_BLOCK_STRONG({strongest.level_type[0].upper()}"
                f"@{strongest.price:.4f} {strongest.touches}t)"
            )
        elif strongest.strength == "normal":
            result["score_adj"] -= 8
            flags.append(
                f"SR_BLOCK({strongest.level_type[0].upper()}"
                f"@{strongest.price:.4f} {strongest.touches}t)"
            )
        else:
            result["score_adj"] -= 3
            flags.append(
                f"SR_WEAK_BLOCK({strongest.level_type[0].upper()}"
                f"@{strongest.price:.4f})"
            )

        # Extra penalty if blocking level is close to entry (< 0.5R away)
        closest_block = min(blocking_levels,
                            key=lambda l: abs(l.price - entry))
        block_dist_r = abs(closest_block.price - entry) / risk
        if block_dist_r < 0.5 and closest_block.strength in ("strong", "normal"):
            result["score_adj"] -= 5
            flags.append(f"SR_CLOSE({block_dist_r:.2f}R)")

    # ── 2. Entry at a favorable level (bounce play) ──
    # Long at support = good (buying at demand zone)
    # Short at resistance = good (selling at supply zone)
    at_entry_tolerance = 0.003  # 0.3% of price

    for lev in levels:
        dist_pct = abs(lev.price - entry) / entry
        if dist_pct > at_entry_tolerance:
            continue

        if direction == "long" and lev.level_type in ("support", "dual"):
            bonus = 8 if lev.strength == "strong" else (5 if lev.strength == "normal" else 2)
            result["score_adj"] += bonus
            result["at_level"] = lev
            flags.append(
                f"ENTRY_AT_SUPPORT({lev.price:.4f} {lev.strength} {lev.touches}t)"
            )
            break
        elif direction == "short" and lev.level_type in ("resistance", "dual"):
            bonus = 8 if lev.strength == "strong" else (5 if lev.strength == "normal" else 2)
            result["score_adj"] += bonus
            result["at_level"] = lev
            flags.append(
                f"ENTRY_AT_RESIST({lev.price:.4f} {lev.strength} {lev.touches}t)"
            )
            break
        # Entering long into resistance or short into support = BAD
        elif direction == "long" and lev.level_type in ("resistance", "dual"):
            penalty = -8 if lev.strength == "strong" else (-4 if lev.strength == "normal" else -1)
            result["score_adj"] += penalty
            flags.append(
                f"ENTRY_INTO_RESIST({lev.price:.4f} {lev.strength})"
            )
            break
        elif direction == "short" and lev.level_type in ("support", "dual"):
            penalty = -8 if lev.strength == "strong" else (-4 if lev.strength == "normal" else -1)
            result["score_adj"] += penalty
            flags.append(
                f"ENTRY_INTO_SUPPORT({lev.price:.4f} {lev.strength})"
            )
            break

    # ── 3. Range-bound detection ──
    if profile.sr_range_bound:
        result["range_bound"] = True
        result["score_adj"] -= 12
        flags.append(f"RANGE_BOUND(gap={profile.sr_range_pct:.2f}%)")

    # ── 4. Clean path bonus ──
    # No blocking levels AND not range bound → clear to run
    if not blocking_levels and not profile.sr_range_bound:
        # Check if there's actual open space above (long) or below (short)
        if direction == "long":
            higher_res = [l for l in levels if l.price > entry
                          and l.level_type in ("resistance", "dual")
                          and l.strength in ("strong", "normal")]
            if not higher_res:
                result["score_adj"] += 5
                flags.append("SR_CLEAR_PATH")
        else:
            lower_sup = [l for l in levels if l.price < entry
                         and l.level_type in ("support", "dual")
                         and l.strength in ("strong", "normal")]
            if not lower_sup:
                result["score_adj"] += 5
                flags.append("SR_CLEAR_PATH")

    return result
