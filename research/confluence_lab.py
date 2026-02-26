"""
research/confluence_lab.py  —  MULTI-CONFLUENCE + ASYMMETRIC RR LAB

Previous findings:
  - Single signals = coin flip (~48-50% WR with 1R TP, need 52%)
  - RSI-REV 1.5R was closest (41.3% vs 41.6% breakeven)
  - Simply strategies lose to fees on 5m

THIS TIME we:
  1. Stack 2-3 signals for each entry (only take the BEST setups)
  2. Use wider TP (2.0R, 2.5R, 3.0R) so winners dwarf losers
  3. Accept fewer trades but demand higher quality

Breakeven WR with 0.04R fee:
  2.0R TP -> 34.7% WR needed
  2.5R TP -> 29.7% WR needed
  3.0R TP -> 26.0% WR needed

STRATEGIES:

  1. TREND-RSI     EMA trend + RSI extreme + bullish/bearish candle
                   -> 2.0R and 2.5R TP

  2. IB-TREND      Inside Bar + EMA trend + volume expansion on BO
                   -> 2.0R and 2.5R TP

  3. ENGULF-STACK  Engulfing candle + RSI extreme + EMA trend
                   -> 2.5R and 3.0R TP  (triple confluence = widest TP)

  4. MOMENTUM      Big body candle (>1.5x avg) + RSI direction + EMA trend
                   -> 2.5R and 3.0R TP

  5. TREND-PB-ATR  Strong EMA trend (gap) + pullback to fast EMA + bounce
                   ATR-based stop for volatility adaptation
                   -> 2.0R and 3.0R TP

All:
  - Session-faithful (asia/london/ny), daily resets
  - Max 2 concurrent, 1 per pair per session
  - 0.04R fee, $50 start, 2% risk (safest per Monte Carlo)
  - Random pair shuffle (no priority bias)
  - Test on BOTH Bybit (128 pairs) + Binance (58 pairs)
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
#  INDICATORS  (pure Python, no numpy/pandas)
# ═══════════════════════════════════════════════════════════════════

def _ema(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    if n < period:
        return [None] * n
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = [None] * (period - 1)
    sma = sum(closes[:period]) / period
    out.append(sma)
    for i in range(period, n):
        out.append(closes[i] * k + out[-1] * (1 - k))
    return out


def _rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    if n < period + 1:
        return [None] * n
    out: List[Optional[float]] = [None] * period
    ag = al = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            ag += d
        else:
            al -= d
    ag /= period
    al /= period
    out.append(100.0 if al < 1e-15 else 100.0 - 100.0 / (1.0 + ag / al))
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g, lo = max(d, 0), max(-d, 0)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + lo) / period
        out.append(100.0 if al < 1e-15 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def _atr(candles: List[Candle], period: int = 14) -> List[Optional[float]]:
    """Average True Range using Wilder smoothing."""
    n = len(candles)
    if n < period + 1:
        return [None] * n
    # Calculate TR for each candle starting at index 1
    trs: List[float] = [0.0]  # index 0 has no previous candle
    for i in range(1, n):
        h, l, pc = candles[i].h, candles[i].l, candles[i - 1].c
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    out: List[Optional[float]] = [None] * period
    avg = sum(trs[1:period + 1]) / period
    out.append(avg)
    for i in range(period + 1, n):
        avg = (avg * (period - 1) + trs[i]) / period
        out.append(avg)
    return out


def _avg_body(candles: List[Candle], period: int = 20) -> List[Optional[float]]:
    """Rolling average absolute body size over `period` candles."""
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    bodies = [abs(c.c - c.o) for c in candles]
    s = 0.0
    for i in range(n):
        s += bodies[i]
        if i >= period:
            s -= bodies[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def precompute(pair_data: Dict[str, List[Candle]]) -> Dict[str, dict]:
    """Pre-compute all indicators for every pair."""
    inds: Dict[str, dict] = {}
    for pair, candles in pair_data.items():
        closes = [c.c for c in candles]
        inds[pair] = {
            "ema10": _ema(closes, 10),
            "ema30": _ema(closes, 30),
            "rsi14": _rsi(closes, 14),
            "atr14": _atr(candles, 14),
            "avg_body20": _avg_body(candles, 20),
        }
    return inds


# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CC:
    name: str = ""
    strategy: str = "trend_rsi"
    tp_r: float = 2.0
    risk_pct: float = 0.02
    fee_r: float = 0.04
    max_concurrent: int = 2
    start_equity: float = 50.0
    # Thresholds
    rsi_low: float = 35.0       # oversold in uptrend
    rsi_high: float = 65.0      # overbought in downtrend
    engulf_rsi_low: float = 30.0
    engulf_rsi_high: float = 70.0
    momentum_body_mult: float = 1.5  # body > 1.5x avg body = momentum
    trend_ema_gap_pct: float = 0.003  # EMA gap as % of price for "strong trend"
    min_engulf_body: float = 0.50


# ═══════════════════════════════════════════════════════════════════
#  TRADE MANAGEMENT  (fixed TP + SL — same for all strategies)
# ═══════════════════════════════════════════════════════════════════

def manage_trade(candle: Candle, trade: OpenTrade, tp_r: float):
    h, l = candle.h, candle.l
    risk = trade.risk_per_unit
    if trade.direction == "long":
        if l <= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return
        tp = trade.entry_price + tp_r * risk
        if h >= tp:
            trade.close(tp, candle.dt, "tp")
    else:
        if h >= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return
        tp = trade.entry_price - tp_r * risk
        if l <= tp:
            trade.close(tp, candle.dt, "tp")


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 1:  TREND-RSI
#  Confluence: EMA10 > EMA30 (uptrend) + RSI < threshold (dip) +
#              bullish candle close
#  This catches "oversold in an uptrend" = high probability reversal
# ═══════════════════════════════════════════════════════════════════

def _trend_rsi_entry(pair, idx, candles, inds, cfg):
    if idx < 2:
        return None
    ef = inds["ema10"]
    es = inds["ema30"]
    rsi = inds["rsi14"]
    if idx >= len(ef) or ef[idx] is None or es[idx] is None or rsi[idx] is None:
        return None
    if rsi[idx - 1] is None:
        return None

    curr = candles[idx]
    prev = candles[idx - 1]

    # LONG: uptrend + RSI was oversold + current candle bullish
    if ef[idx] > es[idx]:
        # RSI dipped below threshold on prev or current, now recovering
        if (rsi[idx - 1] <= cfg.rsi_low or rsi[idx] <= cfg.rsi_low) and curr.c > curr.o:
            sl = min(candles[j].l for j in range(max(0, idx - 4), idx + 1))
            risk = curr.c - sl
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("long", curr.c, sl)

    # SHORT: downtrend + RSI was overbought + current candle bearish
    if ef[idx] < es[idx]:
        if (rsi[idx - 1] >= cfg.rsi_high or rsi[idx] >= cfg.rsi_high) and curr.c < curr.o:
            sl = max(candles[j].h for j in range(max(0, idx - 4), idx + 1))
            risk = sl - curr.c
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 2:  IB-TREND
#  Confluence: Inside Bar + EMA trend alignment + volume expansion
#  Inside bar = compression; breakout in trend direction with volume
# ═══════════════════════════════════════════════════════════════════

def _ib_trend_entry(pair, idx, candles, inds, cfg):
    if idx < 3:
        return None
    ef = inds["ema10"]
    es = inds["ema30"]
    if idx >= len(ef) or ef[idx] is None or es[idx] is None:
        return None

    mother = candles[idx - 2]
    ib = candles[idx - 1]
    bo = candles[idx]

    # Inside bar: completely inside mother
    if ib.h >= mother.h or ib.l <= mother.l:
        return None
    ib_range = ib.h - ib.l
    if ib_range <= 0:
        return None
    mid = (ib.h + ib.l) / 2
    if mid <= 0 or ib_range / mid < 0.001:
        return None

    # Volume expansion: breakout candle volume > inside bar volume
    if bo.v <= ib.v:
        return None

    # Breakout + trend alignment
    if bo.c > ib.h and ef[idx] > es[idx]:  # long breakout + uptrend
        sl = ib.l
        risk = bo.c - sl
        if risk <= 0 or risk / bo.c < 0.0005:
            return None
        return ("long", bo.c, sl)

    if bo.c < ib.l and ef[idx] < es[idx]:  # short breakout + downtrend
        sl = ib.h
        risk = sl - bo.c
        if risk <= 0 or risk / bo.c < 0.0005:
            return None
        return ("short", bo.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 3:  ENGULF-STACK  (TRIPLE confluence)
#  Engulfing candle + RSI extreme + EMA trend
#  Strictest filter = fewest trades, widest TP
# ═══════════════════════════════════════════════════════════════════

def _engulf_stack_entry(pair, idx, candles, inds, cfg):
    if idx < 2:
        return None
    ef = inds["ema10"]
    es = inds["ema30"]
    rsi = inds["rsi14"]
    if idx >= len(ef) or ef[idx] is None or es[idx] is None or rsi[idx] is None:
        return None

    prev = candles[idx - 1]
    curr = candles[idx]

    # Engulfing check
    p_top = max(prev.o, prev.c)
    p_bot = min(prev.o, prev.c)
    c_top = max(curr.o, curr.c)
    c_bot = min(curr.o, curr.c)
    if c_top <= p_top or c_bot >= p_bot:
        return None
    full = curr.h - curr.l
    if full <= 0:
        return None
    if (c_top - c_bot) / full < cfg.min_engulf_body:
        return None

    # BULLISH engulf + RSI oversold + uptrend
    if curr.c > curr.o and prev.c < prev.o:
        if rsi[idx] <= cfg.engulf_rsi_low and ef[idx] > es[idx]:
            sl = curr.l
            risk = curr.c - sl
            if risk <= 0 or risk / curr.c < 0.001:
                return None
            return ("long", curr.c, sl)
        # Also allow: not yet uptrend but RSI very extreme (bottom reversal)
        if rsi[idx] <= 20:
            sl = curr.l
            risk = curr.c - sl
            if risk <= 0 or risk / curr.c < 0.001:
                return None
            return ("long", curr.c, sl)

    # BEARISH engulf + RSI overbought + downtrend
    if curr.c < curr.o and prev.c > prev.o:
        if rsi[idx] >= cfg.engulf_rsi_high and ef[idx] < es[idx]:
            sl = curr.h
            risk = sl - curr.c
            if risk <= 0 or risk / curr.c < 0.001:
                return None
            return ("short", curr.c, sl)
        if rsi[idx] >= 80:
            sl = curr.h
            risk = sl - curr.c
            if risk <= 0 or risk / curr.c < 0.001:
                return None
            return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 4:  MOMENTUM
#  Big body candle (> 1.5x avg body) + RSI confirms + EMA trend
#  "Strong momentum in the trending direction"
# ═══════════════════════════════════════════════════════════════════

def _momentum_entry(pair, idx, candles, inds, cfg):
    if idx < 2:
        return None
    ef = inds["ema10"]
    es = inds["ema30"]
    rsi = inds["rsi14"]
    avg_b = inds["avg_body20"]
    if (idx >= len(ef) or ef[idx] is None or es[idx] is None
            or rsi[idx] is None or avg_b[idx] is None):
        return None

    curr = candles[idx]
    body = abs(curr.c - curr.o)
    if avg_b[idx] <= 0:
        return None
    body_ratio = body / avg_b[idx]

    if body_ratio < cfg.momentum_body_mult:
        return None  # not a momentum candle

    # LONG: bullish momentum + uptrend + RSI not overbought
    if curr.c > curr.o and ef[idx] > es[idx] and rsi[idx] < 70:
        sl = curr.l
        risk = curr.c - sl
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("long", curr.c, sl)

    # SHORT: bearish momentum + downtrend + RSI not oversold
    if curr.c < curr.o and ef[idx] < es[idx] and rsi[idx] > 30:
        sl = curr.h
        risk = sl - curr.c
        if risk <= 0 or risk / curr.c < 0.001:
            return None
        return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  STRATEGY 5:  TREND-PB-ATR
#  Strong trend (EMA gap > threshold) + price pulls back to fast EMA
#  + bounce confirmation + ATR-based stop
#  Classic "buy the dip in a trend" with adaptive stop
# ═══════════════════════════════════════════════════════════════════

def _trend_pb_atr_entry(pair, idx, candles, inds, cfg):
    if idx < 3:
        return None
    ef = inds["ema10"]
    es = inds["ema30"]
    atr = inds["atr14"]
    if (idx >= len(ef) or ef[idx] is None or es[idx] is None
            or atr[idx] is None or ef[idx - 1] is None):
        return None
    if atr[idx] <= 0:
        return None

    curr = candles[idx]
    prev = candles[idx - 1]
    price = curr.c

    # Trend strength: EMA gap as % of price
    ema_gap_pct = abs(ef[idx] - es[idx]) / price if price > 0 else 0
    if ema_gap_pct < cfg.trend_ema_gap_pct:
        return None  # not a strong enough trend

    # ATR-based stop = 1.5x ATR below/above entry
    atr_sl = atr[idx] * 1.5

    # LONG: uptrend + prev candle touched/dipped below EMA10 + current close above EMA10
    if ef[idx] > es[idx]:
        if prev.l <= ef[idx - 1] and curr.c > ef[idx] and curr.c > curr.o:
            sl = curr.c - atr_sl
            risk = curr.c - sl
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("long", curr.c, sl)

    # SHORT: downtrend + prev candle touched/rallied above EMA10 + current close below EMA10
    if ef[idx] < es[idx]:
        if prev.h >= ef[idx - 1] and curr.c < ef[idx] and curr.c < curr.o:
            sl = curr.c + atr_sl
            risk = sl - curr.c
            if risk <= 0 or risk / curr.c < 0.0005:
                return None
            return ("short", curr.c, sl)

    return None


# ═══════════════════════════════════════════════════════════════════
#  ENTRY DISPATCHER
# ═══════════════════════════════════════════════════════════════════

def check_entry(cfg: CC, pair: str, candle_idx: int,
                candles: List[Candle], inds: dict):
    """Returns (direction, entry_price, stop_loss) or None."""
    s = cfg.strategy
    if s == "trend_rsi":
        return _trend_rsi_entry(pair, candle_idx, candles, inds, cfg)
    if s == "ib_trend":
        return _ib_trend_entry(pair, candle_idx, candles, inds, cfg)
    if s == "engulf_stack":
        return _engulf_stack_entry(pair, candle_idx, candles, inds, cfg)
    if s == "momentum":
        return _momentum_entry(pair, candle_idx, candles, inds, cfg)
    if s == "trend_pb_atr":
        return _trend_pb_atr_entry(pair, candle_idx, candles, inds, cfg)
    return None


# ═══════════════════════════════════════════════════════════════════
#  SESSION-FAITHFUL SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════

def simulate(pair_data, time_idx, indicators, cfg: CC) -> dict:
    all_dts: List[datetime] = []
    for candles in pair_data.values():
        if candles:
            all_dts.extend([candles[0].dt, candles[-1].dt])
    if not all_dts:
        return _empty(cfg)

    start_date = min(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date   = max(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    pair_list  = sorted(pair_data.keys())

    equity     = cfg.start_equity
    peak_eq    = cfg.start_equity
    max_dd     = 0.0
    open_pos:  Dict[str, OpenTrade] = {}
    closed:    List[OpenTrade]      = []
    sess_ent:  Dict[str, Set[str]]  = {}
    ss_stats   = {s: {"trades": 0, "wins": 0, "total_r": 0.0} for s in SESSION_ORDER}
    exits:     Dict[str, int]       = {}
    dir_stats  = {"long": [0, 0], "short": [0, 0]}

    day = start_date
    while day <= end_date:
        sess_ent.clear()
        for sn in SESSION_ORDER:
            sh, eh = SESSIONS[sn]
            sess_ent[sn] = set()
            ss = day.replace(hour=sh, minute=0, second=0, microsecond=0)
            nc = (eh - sh) * 12

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
                        ss_stats[sn]["trades"] += 1
                        ss_stats[sn]["total_r"] += t.r_multiple
                        if t.r_multiple > 0:
                            ss_stats[sn]["wins"] += 1
                        er = t.exit_reason or "?"
                        exits[er] = exits.get(er, 0) + 1
                        d = t.direction
                        dir_stats[d][0] += 1
                        if t.r_multiple > 0:
                            dir_stats[d][1] += 1

                # ── Check entries ──
                for p in shuffled:
                    if p in sess_ent[sn]:
                        continue
                    if p in open_pos:
                        continue
                    if len(open_pos) >= cfg.max_concurrent:
                        break
                    if equity < 2.0:
                        break

                    # Get candle index for this pair at this time
                    ci_idx = time_idx.get(p, {}).get(ct)
                    if ci_idx is None:
                        continue
                    pair_candles = pair_data[p]
                    pair_inds = indicators.get(p)
                    if pair_inds is None:
                        continue

                    entry = check_entry(cfg, p, ci_idx, pair_candles, pair_inds)
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
                        range_high=0, range_low=0, range_mid=0,
                    )
                    open_pos[p] = trade
                    sess_ent[sn].add(p)

        day += timedelta(days=1)

    # Close remaining
    for p, t in list(open_pos.items()):
        if p in pair_data and pair_data[p]:
            last = pair_data[p][-1]
            t.close(last.c, last.dt, "end")
            if cfg.fee_r > 0:
                t.r_multiple -= cfg.fee_r
            equity += t.dollar_risk * t.r_multiple
            equity = max(equity, 0.01)
            closed.append(t)

    return _stats(closed, equity, peak_eq, max_dd, ss_stats, exits, dir_stats, cfg)


# ═══════════════════════════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════════════════════════

def _stats(closed, final_eq, peak_eq, max_dd, ss_stats, exits, dir_stats, cfg):
    r_vals = [t.r_multiple for t in closed if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers  = [r for r in r_vals if r <= 0]

    x10 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed):
        if t.r_multiple is None:
            continue
        eq += t.dollar_risk * t.r_multiple
        eq = max(eq, 0.01)
        if x10 is None and eq >= cfg.start_equity * 10:
            x10 = i + 1

    mc = cur = 0
    for r in r_vals:
        if r <= 0:
            cur += 1
            mc = max(mc, cur)
        else:
            cur = 0

    monthly: Dict[str, List[float]] = {}
    for t in closed:
        if t.r_multiple is not None:
            mk = t.entry_time.strftime("%Y-%m")
            monthly.setdefault(mk, []).append(t.r_multiple)

    pair_s: Dict[str, dict] = {}
    for t in closed:
        if t.r_multiple is None:
            continue
        p = t.pair.replace("/USDT:USDT", "")
        if p not in pair_s:
            pair_s[p] = {"trades": 0, "wins": 0, "total_r": 0.0}
        pair_s[p]["trades"] += 1
        pair_s[p]["total_r"] += t.r_multiple
        if t.r_multiple > 0:
            pair_s[p]["wins"] += 1

    return {
        "trades": len(r_vals), "wr": len(winners) / max(len(r_vals), 1),
        "avg_r": statistics.mean(r_vals) if r_vals else 0,
        "total_r": sum(r_vals), "max_dd": max_dd, "final_eq": final_eq,
        "x10": x10, "max_consec": mc, "r_vals": r_vals,
        "avg_win": statistics.mean(winners) if winners else 0,
        "avg_loss": statistics.mean(losers) if losers else 0,
        "monthly": monthly, "ss_stats": ss_stats, "pair_s": pair_s,
        "exits": exits, "dir_stats": dir_stats, "closed": closed, "cfg": cfg,
    }


def _empty(cfg):
    return {
        "trades": 0, "wr": 0, "avg_r": 0, "total_r": 0, "max_dd": 0,
        "final_eq": cfg.start_equity, "x10": None, "r_vals": [],
        "max_consec": 0, "avg_win": 0, "avg_loss": 0,
        "monthly": {}, "ss_stats": {}, "pair_s": {}, "exits": {},
        "dir_stats": {"long": [0, 0], "short": [0, 0]},
        "closed": [], "cfg": cfg,
    }


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

LABELS = {
    "trend_rsi": "TREND-RSI",
    "ib_trend": "IB-TREND",
    "engulf_stack": "ENGULF-STACK",
    "momentum": "MOMENTUM",
    "trend_pb_atr": "TREND-PB-ATR",
}

# Breakeven WR for each TP level (with 0.04R fee)
def be_wr(tp_r: float) -> float:
    return 1.04 / ((tp_r - 0.04) + 1.04)


def main():
    t0 = time_mod.time()
    random.seed(42)
    w = 100

    print("=" * w)
    print("  CONFLUENCE + ASYMMETRIC RR LAB")
    print("  5 multi-signal strategies  x  wider TP (2R-3R)")
    print("  Bybit (128 pairs) + Binance (58 pairs)")
    print("  Session-faithful  |  $50 start  |  2% risk  |  Max 2 concurrent")
    print()
    print("  Breakeven WR:  2.0R = 34.7%  |  2.5R = 29.7%  |  3.0R = 26.0%")
    print("=" * w)

    # ── Load data ──
    print("\n  Loading Bybit data...")
    bybit_data = load_all_pairs()
    print(f"  Bybit: {len(bybit_data)} pairs ({sum(len(c) for c in bybit_data.values()):,} candles)")

    print("  Loading Binance data...")
    binance_data = load_binance_pairs()
    print(f"  Binance: {len(binance_data)} pairs ({sum(len(c) for c in binance_data.values()):,} candles)")

    # ── Indicators ──
    print("  Computing indicators (EMA, RSI, ATR, AvgBody)...")
    ind_by = precompute(bybit_data)
    ind_bn = precompute(binance_data)
    ti_by  = build_time_index(bybit_data)
    ti_bn  = build_time_index(binance_data)
    print("  Done.\n")

    # ── Define test matrix ──
    # (strategy, tp_r_list)
    strat_tp = [
        ("trend_rsi",    [2.0, 2.5]),
        ("ib_trend",     [2.0, 2.5]),
        ("engulf_stack", [2.5, 3.0]),
        ("momentum",     [2.5, 3.0]),
        ("trend_pb_atr", [2.0, 3.0]),
    ]
    exchanges = [
        ("Bybit",   bybit_data, ti_by, ind_by),
        ("Binance", binance_data, ti_bn, ind_bn),
    ]

    configs = []
    for strat, tps in strat_tp:
        for tp in tps:
            for ex_n, pd, ti, ind in exchanges:
                label = LABELS[strat]
                name = f"{label}-{tp}R-{ex_n}"
                cfg = CC(name=name, strategy=strat, tp_r=tp)
                configs.append((cfg, pd, ti, ind))

    total = len(configs)
    print(f"  Running {total} simulations ...\n")

    results = []
    for i, (cfg, pd, ti, ind) in enumerate(configs):
        sys.stdout.write(f"\r  {i+1:3d}/{total}: {cfg.name:<35s}")
        sys.stdout.flush()
        r = simulate(pd, ti, ind, cfg)
        results.append(r)

    print(f"\r  Done: {total} simulations.{' ' * 50}\n")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1:  ALL RESULTS  (sorted by Total R)
    # ══════════════════════════════════════════════════════════════

    sr = sorted(results, key=lambda r: (-r["total_r"], -r["wr"]))

    print("=" * w)
    print("  SECTION 1: ALL RESULTS (sorted by Total R)")
    print("=" * w)

    hdr = (f"  {'Strategy':<30s}  {'#':>5s}  {'WR':>5s}  {'BE':>5s}"
           f"  {'AvgR':>7s}  {'TotR':>7s}  {'MaxDD':>6s}  {'CL':>3s}"
           f"  {'x10':>5s}  {'Final$':>9s}  {'Exits':>16s}")
    print(hdr)
    print(f"  {'-'*30}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*3}  {'-'*5}  {'-'*9}  {'-'*16}")

    for r in sr:
        c = r["cfg"]
        exs = " ".join(f"{k}={v}" for k, v in sorted(r["exits"].items()))
        x10s = str(r["x10"]) if r["x10"] else "-"
        bw = be_wr(c.tp_r)
        edge = "+" if r["wr"] > bw and r["trades"] >= 20 else " "
        print(f" {edge}{c.name:<30s}  {r['trades']:5d}  {r['wr']*100:4.1f}%"
              f"  {bw*100:4.1f}%  {r['avg_r']:+.4f}  {r['total_r']:+7.1f}"
              f"  {r['max_dd']*100:5.1f}%  {r['max_consec']:3d}"
              f"  {x10s:>5s}  ${r['final_eq']:8.0f}  {exs}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2:  BYBIT vs BINANCE (side-by-side)
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 2: BYBIT vs BINANCE  (same strategy, side-by-side)")
    print("  + = WR above breakeven (potential edge)")
    print(f"{'=' * w}\n")

    # Group by strategy-TP
    pairs_map: Dict[str, Dict[str, dict]] = {}
    for r in results:
        c = r["cfg"]
        key = f"{LABELS[c.strategy]}-{c.tp_r}R"
        ex = "Bybit" if "Bybit" in c.name else "Binance"
        pairs_map.setdefault(key, {})[ex] = r

    hdr2 = (f"  {'Strategy':<20s}  {'BE':>5s}  "
            f"{'By #':>5s}  {'By WR':>6s}  {'By TR':>7s}  {'By DD':>6s}  "
            f"{'Bn #':>5s}  {'Bn WR':>6s}  {'Bn TR':>7s}  {'Bn DD':>6s}  "
            f"{'Gap':>5s}  {'Edge?':>6s}")
    print(hdr2)
    print(f"  {'-'*20}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*6}")

    for key in sorted(pairs_map.keys()):
        pm = pairs_map[key]
        tp = float(key.rsplit("-", 1)[-1].replace("R", ""))
        bw = be_wr(tp)
        by = pm.get("Bybit", _empty(CC()))
        bn = pm.get("Binance", _empty(CC()))
        gap = by["wr"] - bn["wr"]
        # Edge if BOTH exchanges are above breakeven
        both_edge = "YES" if by["wr"] > bw and bn["wr"] > bw and by["trades"] >= 20 and bn["trades"] >= 20 else "no"
        print(f"  {key:<20s}  {bw*100:4.1f}%  "
              f"{by['trades']:5d}  {by['wr']*100:5.1f}%  {by['total_r']:+7.1f}  {by['max_dd']*100:5.1f}%  "
              f"{bn['trades']:5d}  {bn['wr']*100:5.1f}%  {bn['total_r']:+7.1f}  {bn['max_dd']*100:5.1f}%  "
              f"{gap*100:+4.1f}%  {both_edge:>6s}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3:  TOP 5 DETAILED
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 3: TOP 5 DETAILED  (by Total R)")
    print(f"{'=' * w}")

    for rank, r in enumerate(sr[:5], 1):
        c = r["cfg"]
        bw = be_wr(c.tp_r)
        edge_flag = " >>> ABOVE BREAKEVEN <<<" if r["wr"] > bw and r["trades"] >= 20 else ""
        print(f"\n  #{rank}: {c.name}{edge_flag}")
        print(f"  {'~'*60}")
        print(f"    Trades: {r['trades']}  |  WR: {r['wr']*100:.1f}% (BE: {bw*100:.1f}%)"
              f"  |  Avg R: {r['avg_r']:+.4f}  |  Total R: {r['total_r']:+.1f}")
        if r["trades"] > 0 and r["avg_loss"] != 0:
            pr = abs(r['avg_win']) / abs(r['avg_loss'])
            print(f"    Avg Win: {r['avg_win']:+.3f}  |  Avg Loss: {r['avg_loss']:+.3f}"
                  f"  |  Payoff: {pr:.2f}")
        print(f"    DD: {r['max_dd']*100:.1f}%  |  Consec Loss: {r['max_consec']}"
              f"  |  $50 -> ${r['final_eq']:.2f}")
        x10s = f"trade #{r['x10']}" if r['x10'] else "never"
        print(f"    x10: {x10s}")

        ds = r.get("dir_stats", {})
        for d in ["long", "short"]:
            tot, wins = ds.get(d, [0, 0])
            wr_d = wins / tot * 100 if tot > 0 else 0
            print(f"    {d.upper():>6s}: {tot} trades, WR={wr_d:.0f}%")

        ss = r.get("ss_stats", {})
        for sn in SESSION_ORDER:
            s = ss.get(sn, {})
            st, sw, sr_ = s.get("trades", 0), s.get("wins", 0), s.get("total_r", 0)
            swr = sw / st * 100 if st > 0 else 0
            print(f"    {sn.upper():>8s}: {st:4d}t  WR={swr:4.0f}%  R={sr_:+7.1f}")

        if r["monthly"]:
            print(f"    Monthly:")
            for mk in sorted(r["monthly"].keys()):
                rs = r["monthly"][mk]
                mwr = sum(1 for x in rs if x > 0) / len(rs) if rs else 0
                mtr = sum(rs)
                flag = " !!!" if mtr < -5 else ""
                print(f"      {mk}: {len(rs):3d}t  WR={mwr*100:4.0f}%  R={mtr:+7.1f}{flag}")

        ps = r.get("pair_s", {})
        if ps:
            by_r = sorted(ps.items(), key=lambda x: x[1]["total_r"], reverse=True)
            print(f"    Top 5 pairs:")
            for p, s in by_r[:5]:
                wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                print(f"      {p:>10s}  {s['trades']:3d}t  WR={wr:.0f}%  R={s['total_r']:+.1f}")
            if len(by_r) > 5:
                print(f"    Bottom 5 pairs:")
                for p, s in by_r[-5:]:
                    wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                    print(f"      {p:>10s}  {s['trades']:3d}t  WR={wr:.0f}%  R={s['total_r']:+.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4:  MONTE CARLO  (profitable strategies only)
    # ══════════════════════════════════════════════════════════════

    profitable = [r for r in sr if r["total_r"] > 0 and r["trades"] >= 20]

    print(f"\n{'=' * w}")
    print("  SECTION 4: MONTE CARLO STRESS TEST (2000 trials)")
    print(f"{'=' * w}")

    if not profitable:
        print("\n  No profitable strategies — skipping Monte Carlo.")
    else:
        for rank, r in enumerate(profitable[:5], 1):
            c = r["cfg"]
            rv = r["r_vals"]
            dr = [t.dollar_risk for t in r["closed"] if t.r_multiple is not None]
            if not rv or not dr:
                continue
            mcr = monte_carlo(rv, dr, 2000, c.start_equity)
            print(f"\n  #{rank}: {c.name}  ({len(rv)} trades, WR={r['wr']*100:.1f}%)")
            print(f"    Median DD:  {mcr['median_dd']*100:5.1f}%")
            print(f"    95th pctl:  {mcr['p95_dd']*100:5.1f}%  <- PLAN FOR THIS")
            print(f"    Bust (<$5): {mcr['bust_pct']*100:5.1f}%")
            print(f"    x10 chance: {mcr['x10_pct']*100:5.1f}%")
            if mcr.get("x10_median"):
                print(f"    x10 median: trade #{mcr['x10_median']}")
            print(f"    Median eq:  ${mcr['median_final']:,.0f}")
            print(f"    10th pctl:  ${mcr['p10_final']:,.0f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5:  VERDICT
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 5: VERDICT")
    print(f"{'=' * w}")

    # Strategies with edge on BOTH exchanges
    both_edge = []
    for key, pm in pairs_map.items():
        tp = float(key.rsplit("-", 1)[-1].replace("R", ""))
        bw = be_wr(tp)
        by = pm.get("Bybit", _empty(CC()))
        bn = pm.get("Binance", _empty(CC()))
        if (by["wr"] > bw and bn["wr"] > bw
                and by["trades"] >= 20 and bn["trades"] >= 20):
            both_edge.append((key, by, bn, bw))

    if both_edge:
        print(f"\n  >>> EDGE CONFIRMED ON BOTH EXCHANGES <<<")
        for key, by, bn, bw in both_edge:
            print(f"\n  {key}:")
            print(f"    Breakeven WR: {bw*100:.1f}%")
            print(f"    Bybit:   {by['trades']}t  WR={by['wr']*100:.1f}%  TotR={by['total_r']:+.1f}  DD={by['max_dd']*100:.1f}%")
            print(f"    Binance: {bn['trades']}t  WR={bn['wr']*100:.1f}%  TotR={bn['total_r']:+.1f}  DD={bn['max_dd']*100:.1f}%")
    else:
        print(f"\n  No strategy has edge on BOTH exchanges simultaneously.")

    # Best OOS (Binance) results
    bnce = sorted([r for r in results if "Binance" in r["cfg"].name],
                  key=lambda r: -r["total_r"])
    print(f"\n  STRATEGY RANKING (Binance OOS, best config each):")
    seen = set()
    for r in bnce:
        s = r["cfg"].strategy
        if s in seen:
            continue
        seen.add(s)
        c = r["cfg"]
        bw = be_wr(c.tp_r)
        flag = " EDGE" if r["wr"] > bw and r["trades"] >= 20 else ""
        print(f"    {LABELS[s]:<14s}  WR={r['wr']*100:4.1f}% (BE:{bw*100:.0f}%)"
              f"  TotR={r['total_r']:+7.1f}  DD={r['max_dd']*100:4.1f}%"
              f"  ({c.name}){flag}")

    elapsed = time_mod.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)\n")


if __name__ == "__main__":
    main()
