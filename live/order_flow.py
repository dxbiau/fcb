"""
live/order_flow.py — DOM (Depth of Market) Order Flow Intelligence

Fuses real-time order book liquidity data with FCB breakout signals to:
1. CONFIRM breakout direction (bid/ask imbalance supports the move)
2. DETECT walls (large resting orders that could block price)
3. SCORE entry quality (liquidity alignment → risk multiplier)

DESIGN PRINCIPLES:
  - NEVER skip trades based on DOM alone (Kelly math needs every trade)
  - DOM is used for RISK SIZING: aligned flow → full Kelly, opposed → reduced
  - Fast & light: single fetch_order_book call (~50-100ms on Bybit)
  - Fail-open: any API error returns neutral score (no impact)

ORDER FLOW METRICS:
  bid_ask_imbalance:  ratio of bid volume to ask volume near price
                      >1.5 = buying pressure  |  <0.67 = selling pressure
  wall_detection:     large orders (>5x avg level size) blocking breakout
                      wall in breakout path = resistance | wall behind = support
  depth_ratio:        total bid depth vs ask depth within 0.5% of mid
                      confirms or denies the breakout direction
  aggressor_lean:     if spread is tight and last price near ask → buyers aggressive
                      if near bid → sellers aggressive

RISK MULTIPLIER OUTPUT:
  Strong alignment    → 1.15 (15% boost — DOM confirms breakout with conviction)
  Mild alignment      → 1.00 (neutral — let candle structure decide)
  Mild opposition     → 0.80 (20% reduction — DOM caution, but still take trade)
  Strong opposition   → 0.65 (35% reduction — DOM heavily against breakout)
  No data / error     → 1.00 (fail-open — never penalize on API failure)

Uses the bot's existing exchange connection. No extra authentication needed.
"""

from __future__ import annotations
import time
from typing import Dict, Optional, Tuple
from live import logger as log


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

# Order book depth to fetch (levels per side)
OB_DEPTH = 25

# Analysis window: how far from mid price to look (as fraction)
NEAR_PRICE_PCT = 0.003    # 0.3% — tight band around current price
WIDE_PRICE_PCT = 0.01     # 1.0% — wider band for wall detection

# Imbalance thresholds
STRONG_BID_IMBALANCE = 2.0    # bid_vol > 2x ask_vol = strong buying
MILD_BID_IMBALANCE = 1.3      # bid_vol > 1.3x = mild buying
MILD_ASK_IMBALANCE = 0.77     # bid_vol < 0.77x ask_vol = mild selling
STRONG_ASK_IMBALANCE = 0.5    # bid_vol < 0.5x = strong selling

# Wall detection: a single level with > WALL_MULTIPLIER * avg_level_size
WALL_MULTIPLIER = 5.0

# Risk multipliers based on DOM alignment
BOOST_MULT = 1.15       # DOM strongly confirms breakout
NEUTRAL_MULT = 1.00     # Neutral or insufficient data
CAUTION_MULT = 0.80     # DOM mildly opposes breakout
OPPOSE_MULT = 0.65      # DOM strongly opposes breakout


# ═══════════════════════════════════════════════════════════
#  CORE ANALYSIS
# ═══════════════════════════════════════════════════════════

def analyze_order_flow(
    ex,  # ccxt.bybit exchange instance
    symbol: str,
    direction: str,       # "long" or "short"
    entry_price: float,   # expected entry price
) -> Dict:
    """
    Analyze order book for a symbol and return DOM intelligence.

    Returns dict with:
      - risk_mult: float (0.65 to 1.15)
      - bid_ask_ratio: float (bid_vol / ask_vol near price)
      - wall_ahead: bool (large order blocking breakout path)
      - wall_behind: bool (large order supporting breakout)
      - depth_score: float (-1 to +1, positive = aligned with direction)
      - tag: str (human-readable summary)
      - latency_ms: int
    """
    t0 = time.time()
    neutral = {
        "risk_mult": NEUTRAL_MULT,
        "bid_ask_ratio": 1.0,
        "wall_ahead": False,
        "wall_behind": False,
        "depth_score": 0.0,
        "tag": "DOM:neutral",
        "latency_ms": 0,
    }

    try:
        ob = ex.fetch_order_book(symbol, limit=OB_DEPTH)
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        log.debug(f"DOM {symbol}: order book fetch failed — {e}")
        neutral["latency_ms"] = latency
        neutral["tag"] = "DOM:error"
        return neutral

    bids = ob.get("bids", [])  # [[price, qty], ...]
    asks = ob.get("asks", [])

    if not bids or not asks:
        neutral["tag"] = "DOM:empty"
        return neutral

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0

    if mid <= 0:
        return neutral

    # ── 1. Near-price imbalance (tight band) ──
    near_band = mid * NEAR_PRICE_PCT
    bid_vol_near = sum(float(b[1]) * float(b[0]) for b in bids
                       if float(b[0]) >= mid - near_band)
    ask_vol_near = sum(float(a[1]) * float(a[0]) for a in asks
                       if float(a[0]) <= mid + near_band)

    bid_ask_ratio = bid_vol_near / ask_vol_near if ask_vol_near > 0 else 2.0

    # ── 2. Wide-band depth ratio ──
    wide_band = mid * WIDE_PRICE_PCT
    bid_vol_wide = sum(float(b[1]) * float(b[0]) for b in bids
                       if float(b[0]) >= mid - wide_band)
    ask_vol_wide = sum(float(a[1]) * float(a[0]) for a in asks
                       if float(a[0]) <= mid + wide_band)
    total_depth = bid_vol_wide + ask_vol_wide
    depth_score = (bid_vol_wide - ask_vol_wide) / total_depth if total_depth > 0 else 0.0

    # ── 3. Wall detection ──
    # Average level size (USDT notional)
    all_levels = [(float(b[0]), float(b[1]) * float(b[0])) for b in bids] + \
                 [(float(a[0]), float(a[1]) * float(a[0])) for a in asks]
    avg_level = sum(l[1] for l in all_levels) / len(all_levels) if all_levels else 1
    wall_threshold = avg_level * WALL_MULTIPLIER

    wall_ahead = False
    wall_behind = False

    if direction == "long":
        # Wall ahead = large ask above entry (resistance)
        for a in asks:
            price, notional = float(a[0]), float(a[1]) * float(a[0])
            if price <= entry_price * 1.02 and notional >= wall_threshold:
                wall_ahead = True
                break
        # Wall behind = large bid below entry (support)
        for b in bids:
            price, notional = float(b[0]), float(b[1]) * float(b[0])
            if price >= entry_price * 0.995 and notional >= wall_threshold:
                wall_behind = True
                break
    else:  # short
        # Wall ahead = large bid below entry (support blocking short)
        for b in bids:
            price, notional = float(b[0]), float(b[1]) * float(b[0])
            if price >= entry_price * 0.98 and notional >= wall_threshold:
                wall_ahead = True
                break
        # Wall behind = large ask above entry (resistance supporting short)
        for a in asks:
            price, notional = float(a[0]), float(a[1]) * float(a[0])
            if price <= entry_price * 1.005 and notional >= wall_threshold:
                wall_behind = True
                break

    # ── 4. Aggressor lean (who's more aggressive?) ──
    spread = best_ask - best_bid
    if spread > 0 and entry_price > 0:
        # Where is entry relative to the spread?
        aggressor_lean = (entry_price - best_bid) / spread
        # >0.7 = price near ask = buyers aggressive
        # <0.3 = price near bid = sellers aggressive
    else:
        aggressor_lean = 0.5

    # ═══════════════════════════════════════════════════════════
    #  SCORE → RISK MULTIPLIER
    # ═══════════════════════════════════════════════════════════

    score = 0.0  # -2 to +2 range

    # Imbalance contribution (±1.0)
    if direction == "long":
        if bid_ask_ratio >= STRONG_BID_IMBALANCE:
            score += 1.0
        elif bid_ask_ratio >= MILD_BID_IMBALANCE:
            score += 0.5
        elif bid_ask_ratio <= STRONG_ASK_IMBALANCE:
            score -= 1.0
        elif bid_ask_ratio <= MILD_ASK_IMBALANCE:
            score -= 0.5
    else:  # short
        # Inverted: selling pressure = good for shorts
        if bid_ask_ratio <= STRONG_ASK_IMBALANCE:
            score += 1.0
        elif bid_ask_ratio <= MILD_ASK_IMBALANCE:
            score += 0.5
        elif bid_ask_ratio >= STRONG_BID_IMBALANCE:
            score -= 1.0
        elif bid_ask_ratio >= MILD_BID_IMBALANCE:
            score -= 0.5

    # Wall contribution (±0.5)
    if wall_ahead:
        score -= 0.5   # resistance in breakout path
    if wall_behind:
        score += 0.3   # support behind us

    # Depth contribution (±0.5)
    if direction == "long":
        score += depth_score * 0.5  # positive depth = more bids = good for longs
    else:
        score -= depth_score * 0.5  # negative depth = more asks = good for shorts

    # Map score → risk multiplier
    if score >= 1.0:
        risk_mult = BOOST_MULT
        tag = "DOM:STRONG_FLOW"
    elif score >= 0.3:
        risk_mult = NEUTRAL_MULT
        tag = "DOM:aligned"
    elif score >= -0.3:
        risk_mult = NEUTRAL_MULT
        tag = "DOM:neutral"
    elif score >= -1.0:
        risk_mult = CAUTION_MULT
        tag = "DOM:caution"
    else:
        risk_mult = OPPOSE_MULT
        tag = "DOM:OPPOSED"

    latency = int((time.time() - t0) * 1000)

    result = {
        "risk_mult": risk_mult,
        "bid_ask_ratio": round(bid_ask_ratio, 3),
        "wall_ahead": wall_ahead,
        "wall_behind": wall_behind,
        "depth_score": round(depth_score, 3),
        "score": round(score, 2),
        "aggressor_lean": round(aggressor_lean, 2),
        "tag": tag,
        "latency_ms": latency,
        "bid_vol_near": round(bid_vol_near, 2),
        "ask_vol_near": round(ask_vol_near, 2),
    }

    return result


def format_dom(result: Dict) -> str:
    """Format DOM analysis for log output."""
    tag = result.get("tag", "DOM:?")
    ratio = result.get("bid_ask_ratio", 1.0)
    risk = result.get("risk_mult", 1.0)
    wall_a = "WALL!" if result.get("wall_ahead") else ""
    wall_b = "SUPPORT" if result.get("wall_behind") else ""
    depth = result.get("depth_score", 0)
    latency = result.get("latency_ms", 0)
    walls = f" | {wall_a}" if wall_a else (f" | {wall_b}" if wall_b else "")
    return (f"{tag} bid/ask={ratio:.2f} depth={depth:+.2f}{walls} "
            f"→ risk={risk:.0%} [{latency}ms]")
