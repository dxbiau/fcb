"""
research/new_strategies.py  —  5 NEW STRATEGY DISCOVERY LAB

FCB failed out-of-sample (49.4% WR, -72R).  Time to find what actually works.

Tests 5 fundamentally different approaches on BOTH Bybit (128 pairs)
and Binance (58 pairs) to find genuine, generalizable edge.

STRATEGIES:
  1. ORB       Opening Range Breakout (first 15min defines session range)
  2. IB        Inside Bar Breakout (compression → expansion)
  3. EMA-PB    EMA Pullback (trend-confirmed pullback entry)
  4. RSI-REV   RSI Extreme Reversal (buy oversold, sell overbought)
  5. ENGULF    Engulfing Candle (strong reversal pattern)

All use:
  - Session-faithful sim (asia/london/ny, daily resets)
  - Fixed TP at 1R or 1.5R
  - SL at strategy-specific level
  - 0.04R round-trip fee
  - Max 2 concurrent, 1 entry per pair per session
  - $50 start, 2% or 3% risk
  - Random pair shuffle per session (no priority bias)

Breakeven WR with 0.04R fee:
  1.0R TP → 52.0%
  1.5R TP → 41.6%
"""

from __future__ import annotations
import csv, glob, math, os, sys, time as time_mod, statistics, random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))

from research.session_sim import (
    load_all_pairs, build_time_index, get_candle_at,
    Candle, OpenTrade, SESSIONS, SESSION_ORDER, monte_carlo,
)
from research.oos_test import load_binance_pairs


# ═══════════════════════════════════════════════════════════════════
#  INDICATORS  (pure Python — no numpy/pandas)
# ═══════════════════════════════════════════════════════════════════

def compute_ema(closes: List[float], period: int) -> List[Optional[float]]:
    """Exponential Moving Average.  None for first (period-1) entries."""
    n = len(closes)
    if n < period:
        return [None] * n
    k = 2.0 / (period + 1)
    result: List[Optional[float]] = [None] * (period - 1)
    sma = sum(closes[:period]) / period
    result.append(sma)
    for i in range(period, n):
        val = closes[i] * k + result[-1] * (1 - k)
        result.append(val)
    return result


def compute_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI using Wilder smoothing.  None for first `period` entries."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    result: List[Optional[float]] = [None] * period
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            avg_gain += d
        else:
            avg_loss -= d
    avg_gain /= period
    avg_loss /= period
    if avg_loss < 1e-15:
        result.append(100.0)
    else:
        result.append(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = max(d, 0.0)
        lo = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
        if avg_loss < 1e-15:
            result.append(100.0)
        else:
            result.append(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return result


def precompute_indicators(pair_data: Dict[str, List[Candle]]) -> Dict[str, dict]:
    """Pre-compute EMA(10), EMA(30), RSI(14) for every pair."""
    indicators: Dict[str, dict] = {}
    for pair, candles in pair_data.items():
        closes = [c.c for c in candles]
        indicators[pair] = {
            "ema10": compute_ema(closes, 10),
            "ema30": compute_ema(closes, 30),
            "rsi14": compute_rsi(closes, 14),
        }
    return indicators


# ═══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SC:
    """Strategy config."""
    name: str = ""
    strategy: str = "orb"       # orb | ib | ema_pb | rsi_rev | engulf
    risk_pct: float = 0.02
    tp_r: float = 1.0
    fee_r: float = 0.04
    max_concurrent: int = 2
    start_equity: float = 50.0
    # ORB
    orb_candles: int = 3        # first N candles define range (15 min)
    orb_window: int = 18        # how far into session to look for BO (90 min)
    # RSI
    rsi_low: float = 25.0
    rsi_high: float = 75.0
    # Engulf
    min_engulf_body: float = 0.50
    # Min range filter (shared)
    min_range_pct: float = 0.002


# ═══════════════════════════════════════════════════════════════════
#  SHARED TRADE MANAGEMENT  (fixed TP + SL, all strategies)
# ═══════════════════════════════════════════════════════════════════

def manage_trade(candle: Candle, trade: OpenTrade, tp_r: float):
    """Fixed TP + SL.  Check SL first (conservative)."""
    h, l = candle.h, candle.l
    risk = trade.risk_per_unit
    if trade.direction == "long":
        if l <= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return
        tp = trade.entry_price + tp_r * risk
        if h >= tp:
            trade.close(tp, candle.dt, "tp")
    else:   # short
        if h >= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return
        tp = trade.entry_price - tp_r * risk
        if l <= tp:
            trade.close(tp, candle.dt, "tp")


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 1:  ORB — Opening Range Breakout
#  First 3 candles (15 min) of each session define the range.
#  Trade when price closes beyond range.  Stop at range midpoint.
# ═══════════════════════════════════════════════════════════════════

def _orb_entry(pair, ct, c_idx, pair_data, time_idx, orb_ranges, cfg):
    if c_idx < cfg.orb_candles or c_idx >= cfg.orb_window:
        return None
    rng = orb_ranges.get(pair)
    if rng is None or rng[2] < cfg.orb_candles:
        return None                     # incomplete ORB data
    orb_h, orb_l, _ = rng
    orb_range = orb_h - orb_l
    if orb_range <= 0:
        return None
    mid = (orb_h + orb_l) / 2.0
    if mid <= 0 or orb_range / mid < cfg.min_range_pct:
        return None
    c = get_candle_at(pair, ct, pair_data, time_idx)
    if c is None:
        return None
    if c.c > orb_h:                     # breakout LONG
        return ("long", c.c, mid)
    if c.c < orb_l:                     # breakdown SHORT
        return ("short", c.c, mid)
    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 2:  IB — Inside Bar Breakout
#  Candle whose H < prev H AND L > prev L → compression.
#  Next candle that closes beyond IB range → entry.
# ═══════════════════════════════════════════════════════════════════

def _ib_entry(pair, ct, c_idx, pair_data, time_idx, cfg):
    if c_idx < 2:
        return None
    idx = time_idx.get(pair, {}).get(ct)
    if idx is None or idx < 2:
        return None
    candles = pair_data[pair]
    mother = candles[idx - 2]           # the "mother" candle
    ib     = candles[idx - 1]           # potential inside bar
    bo     = candles[idx]               # current candle = breakout
    # Inside bar check:  ib completely inside mother
    if ib.h >= mother.h or ib.l <= mother.l:
        return None
    ib_range = ib.h - ib.l
    if ib_range <= 0:
        return None
    mid = (ib.h + ib.l) / 2.0
    if mid <= 0 or ib_range / mid < 0.001:
        return None
    # Breakout of IB range
    if bo.c > ib.h:                     # long
        return ("long", bo.c, ib.l)
    if bo.c < ib.l:                     # short
        return ("short", bo.c, ib.h)
    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 3:  EMA-PB — EMA Pullback
#  EMA(10) > EMA(30) = uptrend.  Price dips to EMA(10) then bounces → long.
#  EMA(10) < EMA(30) = downtrend.  Price rallies to EMA(10) then rejects → short.
#  Stop at recent 3-candle swing low/high.
# ═══════════════════════════════════════════════════════════════════

def _ema_pb_entry(pair, ct, c_idx, pair_data, time_idx, indicators, cfg):
    ind = indicators.get(pair)
    if ind is None:
        return None
    idx = time_idx.get(pair, {}).get(ct)
    if idx is None or idx < 3:
        return None
    ema_f = ind["ema10"]
    ema_s = ind["ema30"]
    n = len(ema_f)
    if idx >= n or idx - 1 >= n:
        return None
    ef_now = ema_f[idx]
    es_now = ema_s[idx]
    ef_prev = ema_f[idx - 1]
    if ef_now is None or es_now is None or ef_prev is None:
        return None
    candles = pair_data[pair]
    curr = candles[idx]
    prev = candles[idx - 1]

    if ef_now > es_now:                 # UPTREND
        # Previous candle dipped to/below fast EMA, current closes above
        if prev.l <= ef_prev and curr.c > ef_now:
            sl = min(candles[j].l for j in range(max(0, idx - 2), idx + 1))
            risk = curr.c - sl
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("long", curr.c, sl)

    elif ef_now < es_now:               # DOWNTREND
        # Previous candle rallied to/above fast EMA, current closes below
        if prev.h >= ef_prev and curr.c < ef_now:
            sl = max(candles[j].h for j in range(max(0, idx - 2), idx + 1))
            risk = sl - curr.c
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 4:  RSI-REV — RSI Extreme Reversal
#  RSI(14) drops below 25 + bullish candle → long
#  RSI(14) above 75 + bearish candle → short
#  Stop at 5-candle swing low/high.
# ═══════════════════════════════════════════════════════════════════

def _rsi_entry(pair, ct, c_idx, pair_data, time_idx, indicators, cfg):
    ind = indicators.get(pair)
    if ind is None:
        return None
    idx = time_idx.get(pair, {}).get(ct)
    if idx is None or idx < 5:
        return None
    rsi_arr = ind["rsi14"]
    if idx >= len(rsi_arr) or rsi_arr[idx] is None:
        return None
    rsi = rsi_arr[idx]
    candles = pair_data[pair]
    curr = candles[idx]

    if rsi <= cfg.rsi_low and curr.c > curr.o:          # oversold + bullish
        sl = min(candles[j].l for j in range(max(0, idx - 4), idx + 1))
        risk = curr.c - sl
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("long", curr.c, sl)

    if rsi >= cfg.rsi_high and curr.c < curr.o:          # overbought + bearish
        sl = max(candles[j].h for j in range(max(0, idx - 4), idx + 1))
        risk = sl - curr.c
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 5:  ENGULF — Engulfing Candle
#  Current candle's body completely engulfs previous candle's body.
#  Direction: bullish engulf (was bear, now bull) → long.
#  Stop at engulfing candle extreme.
# ═══════════════════════════════════════════════════════════════════

def _engulf_entry(pair, ct, c_idx, pair_data, time_idx, cfg):
    idx = time_idx.get(pair, {}).get(ct)
    if idx is None or idx < 1:
        return None
    candles = pair_data[pair]
    prev = candles[idx - 1]
    curr = candles[idx]
    # Previous and current body bounds
    p_top = max(prev.o, prev.c)
    p_bot = min(prev.o, prev.c)
    c_top = max(curr.o, curr.c)
    c_bot = min(curr.o, curr.c)
    # Engulfing: current body completely contains previous body
    if c_top <= p_top or c_bot >= p_bot:
        return None
    # Quality: body ratio filter
    full = curr.h - curr.l
    if full <= 0:
        return None
    if (c_top - c_bot) / full < cfg.min_engulf_body:
        return None
    # Direction must be OPPOSITE to previous
    if curr.c > curr.o and prev.c < prev.o:         # bullish engulf
        sl = curr.l
        risk = curr.c - sl
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("long", curr.c, sl)
    if curr.c < curr.o and prev.c > prev.o:         # bearish engulf
        sl = curr.h
        risk = sl - curr.c
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("short", curr.c, sl)
    return None


# ═══════════════════════════════════════════════════════════════════
#  ENTRY DISPATCHER
# ═══════════════════════════════════════════════════════════════════

def check_entry(cfg, pair, ct, c_idx, pair_data, time_idx, indicators,
                orb_ranges):
    """Returns (direction, entry_price, stop_loss) or None."""
    s = cfg.strategy
    if s == "orb":
        return _orb_entry(pair, ct, c_idx, pair_data, time_idx, orb_ranges, cfg)
    if s == "ib":
        return _ib_entry(pair, ct, c_idx, pair_data, time_idx, cfg)
    if s == "ema_pb":
        return _ema_pb_entry(pair, ct, c_idx, pair_data, time_idx, indicators, cfg)
    if s == "rsi_rev":
        return _rsi_entry(pair, ct, c_idx, pair_data, time_idx, indicators, cfg)
    if s == "engulf":
        return _engulf_entry(pair, ct, c_idx, pair_data, time_idx, cfg)
    return None


# ═══════════════════════════════════════════════════════════════════
#  SESSION-FAITHFUL SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════

def simulate(pair_data, time_idx, indicators, cfg: SC) -> dict:
    """Walk every day × session × candle, exactly like the live bot."""

    all_dts: List[datetime] = []
    for candles in pair_data.values():
        if candles:
            all_dts.append(candles[0].dt)
            all_dts.append(candles[-1].dt)
    if not all_dts:
        return _empty_result(cfg)

    start_date = min(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date   = max(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    pair_list  = sorted(pair_data.keys())

    # State
    equity       = cfg.start_equity
    peak_eq      = cfg.start_equity
    max_dd       = 0.0
    open_pos:  Dict[str, OpenTrade] = {}
    closed:    List[OpenTrade]      = []
    sess_ent:  Dict[str, Set[str]]  = {}
    sess_stats = {s: {"trades": 0, "wins": 0, "total_r": 0.0} for s in SESSION_ORDER}
    exits:     Dict[str, int]       = {}
    direction_stats = {"long": [0, 0], "short": [0, 0]}  # [total, wins]

    day = start_date
    while day <= end_date:
        sess_ent.clear()

        for sn in SESSION_ORDER:
            sh, eh = SESSIONS[sn]
            sess_ent[sn] = set()
            ss = day.replace(hour=sh, minute=0, second=0, microsecond=0)
            nc = (eh - sh) * 12             # candles per session (96 for 8h)

            orb_ranges: Dict[str, Tuple[float, float, int]] = {}
            # Shuffle pairs for fair slot allocation
            shuffled = list(pair_list)
            random.shuffle(shuffled)

            for ci in range(nc):
                ct = ss + timedelta(minutes=ci * 5)

                # ── Manage open trades ──
                for p in list(open_pos.keys()):
                    t = open_pos[p]
                    candle = get_candle_at(p, ct, pair_data, time_idx)
                    if candle is None:
                        continue
                    manage_trade(candle, t, cfg.tp_r)
                    if not t.is_open:
                        if cfg.fee_r > 0:
                            t.r_multiple -= cfg.fee_r
                        pnl = t.dollar_risk * t.r_multiple
                        equity += pnl
                        equity = max(equity, 0.01)
                        if equity > peak_eq:
                            peak_eq = equity
                        dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
                        max_dd = max(max_dd, dd)
                        closed.append(t)
                        del open_pos[p]
                        # Track stats
                        sess_stats[sn]["trades"] += 1
                        sess_stats[sn]["total_r"] += t.r_multiple
                        if t.r_multiple > 0:
                            sess_stats[sn]["wins"] += 1
                        er = t.exit_reason or "?"
                        exits[er] = exits.get(er, 0) + 1
                        d = t.direction
                        direction_stats[d][0] += 1
                        if t.r_multiple > 0:
                            direction_stats[d][1] += 1

                # ── ORB range-building phase ──
                if cfg.strategy == "orb" and ci < cfg.orb_candles:
                    for p in pair_list:
                        candle = get_candle_at(p, ct, pair_data, time_idx)
                        if candle is None:
                            continue
                        if p not in orb_ranges:
                            orb_ranges[p] = (candle.h, candle.l, 1)
                        else:
                            oh, ol, cnt = orb_ranges[p]
                            orb_ranges[p] = (max(oh, candle.h),
                                             min(ol, candle.l), cnt + 1)
                    continue        # no entries during ORB build

                # ── Check entries (pairs in shuffled order) ──
                for p in shuffled:
                    if p in sess_ent[sn]:
                        continue
                    if p in open_pos:
                        continue
                    if len(open_pos) >= cfg.max_concurrent:
                        break
                    if equity < 2.0:
                        break

                    entry = check_entry(cfg, p, ct, ci, pair_data, time_idx,
                                        indicators, orb_ranges)
                    if entry is None:
                        continue

                    direction, ep, sl = entry
                    rpu = abs(ep - sl)
                    if rpu <= 0:
                        continue
                    if len(open_pos) >= cfg.max_concurrent:
                        break

                    dr = equity * cfg.risk_pct
                    trade = OpenTrade(
                        pair=p, session=sn, direction=direction,
                        entry_price=ep, entry_time=ct,
                        stop_loss=sl, risk_per_unit=rpu,
                        dollar_risk=dr, entry_equity=equity,
                        range_high=0.0, range_low=0.0, range_mid=0.0,
                    )
                    open_pos[p] = trade
                    sess_ent[sn].add(p)

        day += timedelta(days=1)

    # Close remaining open trades at last available price
    for p, t in list(open_pos.items()):
        if p in pair_data and pair_data[p]:
            last = pair_data[p][-1]
            t.close(last.c, last.dt, "end")
            if cfg.fee_r > 0:
                t.r_multiple -= cfg.fee_r
            equity += t.dollar_risk * t.r_multiple
            equity = max(equity, 0.01)
            closed.append(t)

    return _compute_stats(closed, equity, peak_eq, max_dd,
                          sess_stats, exits, direction_stats, cfg)


# ═══════════════════════════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════════════════════════

def _compute_stats(closed, final_eq, peak_eq, max_dd,
                   sess_stats, exits, direction_stats, cfg) -> dict:
    r_vals = [t.r_multiple for t in closed if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers  = [r for r in r_vals if r <= 0]

    # x10 milestone
    x10 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed):
        if t.r_multiple is None:
            continue
        eq += t.dollar_risk * t.r_multiple
        eq = max(eq, 0.01)
        if x10 is None and eq >= cfg.start_equity * 10:
            x10 = i + 1

    # Consecutive losses
    max_consec = cur = 0
    for r in r_vals:
        if r <= 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    # Monthly
    monthly: Dict[str, List[float]] = {}
    for t in closed:
        if t.r_multiple is None:
            continue
        mk = t.entry_time.strftime("%Y-%m")
        monthly.setdefault(mk, []).append(t.r_multiple)

    # Per-pair
    pair_stats: Dict[str, dict] = {}
    for t in closed:
        if t.r_multiple is None:
            continue
        p = t.pair.replace("/USDT:USDT", "")
        if p not in pair_stats:
            pair_stats[p] = {"trades": 0, "wins": 0, "total_r": 0.0}
        pair_stats[p]["trades"] += 1
        pair_stats[p]["total_r"] += t.r_multiple
        if t.r_multiple > 0:
            pair_stats[p]["wins"] += 1

    return {
        "trades": len(r_vals),
        "wr": len(winners) / len(r_vals) if r_vals else 0,
        "avg_r": statistics.mean(r_vals) if r_vals else 0,
        "total_r": sum(r_vals),
        "avg_win": statistics.mean(winners) if winners else 0,
        "avg_loss": statistics.mean(losers) if losers else 0,
        "max_dd": max_dd,
        "final_eq": final_eq,
        "x10": x10,
        "max_consec": max_consec,
        "r_vals": r_vals,
        "monthly": monthly,
        "sess_stats": sess_stats,
        "pair_stats": pair_stats,
        "exits": exits,
        "direction_stats": direction_stats,
        "closed": closed,
        "cfg": cfg,
    }


def _empty_result(cfg):
    return {
        "trades": 0, "wr": 0, "avg_r": 0, "total_r": 0, "max_dd": 0,
        "final_eq": cfg.start_equity, "x10": None, "r_vals": [],
        "monthly": {}, "cfg": cfg, "max_consec": 0, "avg_win": 0,
        "avg_loss": 0, "exits": {}, "sess_stats": {}, "pair_stats": {},
        "closed": [], "direction_stats": {"long": [0, 0], "short": [0, 0]},
    }


# ═══════════════════════════════════════════════════════════════════
#  MAIN — Run everything
# ═══════════════════════════════════════════════════════════════════

STRAT_LABELS = {
    "orb":     "ORB",
    "ib":      "IB",
    "ema_pb":  "EMA-PB",
    "rsi_rev": "RSI-REV",
    "engulf":  "ENGULF",
}


def main():
    t0 = time_mod.time()
    random.seed(42)
    w = 100

    print("=" * w)
    print("  NEW STRATEGY DISCOVERY LAB")
    print("  5 strategies  x  Bybit (128 pairs) + Binance (58 pairs)")
    print("  Session-faithful  |  Fixed TP  |  $50 start  |  Max 2 concurrent")
    print("  Breakeven:  1.0R TP = 52.0% WR  |  1.5R TP = 41.6% WR")
    print("=" * w)

    # ── Load data ──
    print("\n  Loading Bybit data...")
    bybit_data = load_all_pairs()
    b_candles = sum(len(c) for c in bybit_data.values())
    print(f"  Bybit: {len(bybit_data)} pairs ({b_candles:,} candles)")

    print("  Loading Binance data...")
    binance_data = load_binance_pairs()
    n_candles_total = sum(len(c) for c in binance_data.values())
    print(f"  Binance: {len(binance_data)} pairs ({n_candles_total:,} candles)")

    # ── Pre-compute indicators ──
    print("  Computing indicators (EMA, RSI) for all pairs...")
    ind_bybit   = precompute_indicators(bybit_data)
    ind_binance = precompute_indicators(binance_data)
    print("  Done.")

    # ── Build time indices ──
    ti_bybit   = build_time_index(bybit_data)
    ti_binance = build_time_index(binance_data)

    # ── Define test matrix ──
    strategies = ["orb", "ib", "ema_pb", "rsi_rev", "engulf"]
    risks  = [0.02, 0.03]
    tp_rs  = [1.0, 1.5]
    exchanges = [
        ("Bybit",   bybit_data,   ti_bybit,   ind_bybit),
        ("Binance", binance_data, ti_binance, ind_binance),
    ]

    configs: List[Tuple[SC, dict, dict, dict]] = []
    for strat in strategies:
        for tp in tp_rs:
            for risk in risks:
                for ex_name, pd, ti, ind in exchanges:
                    label = STRAT_LABELS[strat]
                    name = f"{label}-{tp}R-{int(risk*100)}%-{ex_name}"
                    cfg = SC(name=name, strategy=strat, risk_pct=risk, tp_r=tp)
                    configs.append((cfg, pd, ti, ind))

    total = len(configs)
    print(f"\n  Running {total} simulations ...\n")

    results = []
    for i, (cfg, pd, ti, ind) in enumerate(configs):
        sys.stdout.write(f"\r  {i+1:3d}/{total}: {cfg.name:<35s}")
        sys.stdout.flush()
        r = simulate(pd, ti, ind, cfg)
        results.append(r)

    print(f"\r  Done: {total} simulations.{' ' * 50}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1:  ALL RESULTS  (sorted by Total R, then WR)
    # ══════════════════════════════════════════════════════════════

    sorted_all = sorted(results, key=lambda r: (-r["total_r"], -r["wr"]))

    print(f"\n{'=' * w}")
    print("  SECTION 1: ALL RESULTS (sorted by Total R)")
    print(f"{'=' * w}\n")

    hdr = (f"  {'Strategy':<35s}  {'#':>5s}  {'WR':>5s}  {'AvgR':>7s}"
           f"  {'TotR':>7s}  {'MaxDD':>6s}  {'CL':>3s}  {'x10':>5s}"
           f"  {'Final$':>9s}  {'Exits':>12s}")
    sep = (f"  {'-'*35}  {'-'*5}  {'-'*5}  {'-'*7}"
           f"  {'-'*7}  {'-'*6}  {'-'*3}  {'-'*5}"
           f"  {'-'*9}  {'-'*12}")
    print(hdr)
    print(sep)

    for r in sorted_all:
        c = r["cfg"]
        exs = r.get("exits", {})
        exit_str = " ".join(f"{k}={v}" for k, v in sorted(exs.items()))
        x10s = str(r["x10"]) if r["x10"] else "-"
        print(f"  {c.name:<35s}  {r['trades']:5d}  {r['wr']*100:4.1f}%"
              f"  {r['avg_r']:+.4f}  {r['total_r']:+7.1f}"
              f"  {r['max_dd']*100:5.1f}%  {r['max_consec']:3d}"
              f"  {x10s:>5s}  ${r['final_eq']:8.0f}  {exit_str}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2:  STRATEGY × EXCHANGE COMPARISON
    #  Average across risk levels for each strategy-TP-exchange
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 2: STRATEGY COMPARISON  (averaged across risk levels)")
    print("  Key column: Binance WR — must be above breakeven to deploy")
    print(f"{'=' * w}\n")

    groups: Dict[str, Dict[str, List[dict]]] = {}
    for r in results:
        c = r["cfg"]
        label = f"{STRAT_LABELS[c.strategy]}-{c.tp_r}R"
        exch = "Binance" if "Binance" in c.name else "Bybit"
        groups.setdefault(label, {}).setdefault(exch, []).append(r)

    hdr2 = (f"  {'Strategy':<14s}  {'BE WR':>5s}  "
            f"{'Bybit WR':>8s}  {'Bybit DD':>8s}  {'Bybit TR':>8s}  "
            f"{'Bnce WR':>8s}  {'Bnce DD':>8s}  {'Bnce TR':>8s}  "
            f"{'Gap':>5s}")
    print(hdr2)
    print(f"  {'-'*14}  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*5}")

    for label in sorted(groups.keys()):
        g = groups[label]
        # label is like "ORB-1.0R" or "EMA-PB-1.5R" — TP is always last part
        tp = float(label.rsplit("-", 1)[-1].replace("R", ""))
        be_wr = 1.04 / ((tp - 0.04) + 1.04)

        def avg(rs, m):
            return statistics.mean([r[m] for r in rs]) if rs else 0.0

        bwr = avg(g.get("Bybit", []),  "wr")
        bdd = avg(g.get("Bybit", []),  "max_dd")
        btr = avg(g.get("Bybit", []),  "total_r")
        nwr = avg(g.get("Binance", []), "wr")
        ndd = avg(g.get("Binance", []), "max_dd")
        ntr = avg(g.get("Binance", []), "total_r")
        gap = bwr - nwr

        be_s = f"{be_wr*100:.0f}%"
        flag_b = " *" if bwr > be_wr else ""
        flag_n = " *" if nwr > be_wr else ""
        print(f"  {label:<14s}  {be_s:>5s}  "
              f"{bwr*100:7.1f}%  {bdd*100:7.1f}%  {btr:+7.1f}  "
              f"{nwr*100:7.1f}%  {ndd*100:7.1f}%  {ntr:+7.1f}  "
              f"{gap*100:+4.1f}%")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3:  TOP 5 DETAILED
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 3: TOP 5 DETAILED  (by Total R)")
    print(f"{'=' * w}")

    for rank, r in enumerate(sorted_all[:5], 1):
        c = r["cfg"]
        print(f"\n  #{rank}: {c.name}")
        print(f"  {'~'*55}")
        print(f"    Trades: {r['trades']}  |  WR: {r['wr']*100:.1f}%"
              f"  |  Avg R: {r['avg_r']:+.4f}  |  Total R: {r['total_r']:+.1f}")
        if r["trades"] > 0:
            pr = (abs(r['avg_win']) / abs(r['avg_loss'])
                  if r['avg_loss'] != 0 else 0)
            print(f"    Avg Win: {r['avg_win']:+.3f}  |  Avg Loss: {r['avg_loss']:+.3f}"
                  f"  |  Payoff: {pr:.2f}")
        print(f"    DD: {r['max_dd']*100:.1f}%  |  Consec Loss: {r['max_consec']}"
              f"  |  $50 -> ${r['final_eq']:.2f}")
        x10s = f"trade #{r['x10']}" if r['x10'] else "never"
        print(f"    x10: {x10s}")

        # Direction split
        ds = r.get("direction_stats", {})
        for d in ["long", "short"]:
            tot, wins = ds.get(d, [0, 0])
            wr_d = wins / tot * 100 if tot > 0 else 0
            print(f"    {d.upper():>6s}: {tot} trades, WR={wr_d:.0f}%")

        # Session split
        ss = r.get("sess_stats", {})
        for sn in SESSION_ORDER:
            s = ss.get(sn, {})
            st = s.get("trades", 0)
            sw = s.get("wins", 0)
            sr = s.get("total_r", 0)
            swr = sw / st * 100 if st > 0 else 0
            print(f"    {sn.upper():>8s}: {st:4d}t  WR={swr:4.0f}%  R={sr:+7.1f}")

        # Monthly
        if r["monthly"]:
            print(f"    Monthly:")
            for mk in sorted(r["monthly"].keys()):
                rs = r["monthly"][mk]
                mwr = sum(1 for x in rs if x > 0) / len(rs) if rs else 0
                mtr = sum(rs)
                print(f"      {mk}: {len(rs):3d}t  WR={mwr*100:4.0f}%  R={mtr:+7.1f}")

        # Top/bottom pairs
        ps = r.get("pair_stats", {})
        if ps:
            by_r = sorted(ps.items(), key=lambda x: x[1]["total_r"], reverse=True)
            print(f"    Top 5 pairs:")
            for p, s in by_r[:5]:
                wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                print(f"      {p:>10s}  {s['trades']:3d}t  WR={wr:.0f}%  R={s['total_r']:+.1f}")
            print(f"    Bottom 5 pairs:")
            for p, s in by_r[-5:]:
                wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                print(f"      {p:>10s}  {s['trades']:3d}t  WR={wr:.0f}%  R={s['total_r']:+.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4:  MONTE CARLO  (top 3 profitable)
    # ══════════════════════════════════════════════════════════════

    profitable = [r for r in sorted_all if r["total_r"] > 0 and r["trades"] >= 30]

    print(f"\n{'=' * w}")
    print("  SECTION 4: MONTE CARLO STRESS TEST (2000 trials)")
    print(f"{'=' * w}")

    if not profitable:
        print("\n  No profitable strategies found — skipping Monte Carlo.")
    else:
        for rank, r in enumerate(profitable[:3], 1):
            c = r["cfg"]
            r_vals = r["r_vals"]
            d_risks = [t.dollar_risk for t in r["closed"]
                       if t.r_multiple is not None]
            if not r_vals or not d_risks:
                continue
            mc = monte_carlo(r_vals, d_risks, 2000, c.start_equity)
            print(f"\n  #{rank}: {c.name}  ({len(r_vals)} trades, WR={r['wr']*100:.1f}%)")
            print(f"    Median DD:  {mc['median_dd']*100:5.1f}%")
            print(f"    95th pctl:  {mc['p95_dd']*100:5.1f}%  <- PLAN FOR THIS")
            print(f"    Bust (<$5): {mc['bust_pct']*100:5.1f}%")
            print(f"    x10 chance: {mc['x10_pct']*100:5.1f}%")
            if mc.get("x10_median"):
                print(f"    x10 median: trade #{mc['x10_median']}")
            print(f"    Median eq:  ${mc['median_final']:,.0f}")
            print(f"    10th pctl:  ${mc['p10_final']:,.0f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5:  VERDICT
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 5: VERDICT")
    print(f"{'=' * w}")

    # Find best BINANCE result (true OOS)
    bnce = [r for r in results if "Binance" in r["cfg"].name]
    bnce_sorted = sorted(bnce, key=lambda r: (-r["total_r"], -r["wr"]))

    if bnce_sorted and bnce_sorted[0]["total_r"] > 0:
        best = bnce_sorted[0]
        bc = best["cfg"]
        tp = bc.tp_r
        be_wr = 1.04 / ((tp - 0.04) + 1.04)
        print(f"\n  BEST OOS (Binance) RESULT:")
        print(f"    {bc.name}")
        print(f"    WR: {best['wr']*100:.1f}%  (breakeven: {be_wr*100:.1f}%)")
        print(f"    Total R: {best['total_r']:+.1f}  |  DD: {best['max_dd']*100:.1f}%")
        print(f"    Trades: {best['trades']}  |  Final: ${best['final_eq']:.2f}")
        if best["wr"] > be_wr:
            print(f"\n  >>> EDGE DETECTED ON OOS DATA <<<")
        else:
            print(f"\n  WR below breakeven — no edge.")
    else:
        print(f"\n  NO strategy is profitable on Binance (OOS) data.")
        print("  All strategies lose money on unseen pairs.")

    # Best Bybit result
    bybt = [r for r in results if "Bybit" in r["cfg"].name]
    bybt_sorted = sorted(bybt, key=lambda r: (-r["total_r"], -r["wr"]))
    if bybt_sorted and bybt_sorted[0]["total_r"] > 0:
        best_b = bybt_sorted[0]
        bc = best_b["cfg"]
        print(f"\n  BEST IN-SAMPLE (Bybit) RESULT:")
        print(f"    {bc.name}")
        print(f"    WR: {best_b['wr']*100:.1f}%  |  Total R: {best_b['total_r']:+.1f}"
              f"  |  DD: {best_b['max_dd']*100:.1f}%")
    else:
        print(f"\n  No profitable Bybit result either.")

    # Summary by strategy type
    print(f"\n  STRATEGY RANKING (OOS Binance, best config each):")
    strat_best: Dict[str, dict] = {}
    for r in bnce_sorted:
        s = r["cfg"].strategy
        if s not in strat_best or r["total_r"] > strat_best[s]["total_r"]:
            strat_best[s] = r
    ranked = sorted(strat_best.values(), key=lambda r: -r["total_r"])
    for i, r in enumerate(ranked, 1):
        c = r["cfg"]
        print(f"    {i}. {STRAT_LABELS[c.strategy]:<8s}  WR={r['wr']*100:4.1f}%"
              f"  TotR={r['total_r']:+7.1f}  DD={r['max_dd']*100:4.1f}%"
              f"  ({c.name})")

    elapsed = time_mod.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print()


if __name__ == "__main__":
    main()
