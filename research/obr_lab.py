"""
research/obr_lab.py  —  OUTSIDE BAR REVERSAL (OBR) Lab

Inspired by the SimpleCandleStrategy notebook:
  - Detect Outside Bars (H > prev H AND L < prev L)
  - If bearish OB closes below prev Low → enter LONG (reversal)
  - If bullish OB closes above prev High → enter SHORT (reversal)
  - Entry at NEXT candle's open (wait for confirmation)

The logic: extreme exhaustion candles that engulf the entire previous
range AND close beyond it are overextended → fade them.

VARIANTS TESTED:
  A. OBR-PURE          Raw signal, no filters
  B. OBR-TREND         + EMA(10) > EMA(30) trend filter (only fade AGAINST trend)
                        Wait, actually: only take longs in uptrend, shorts in downtrend
                        (trend-aligned reversal = "buy the dip")
  C. OBR-RSI           + RSI(14) extreme filter (oversold for longs, overbought for shorts)
  D. OBR-STACK         + EMA trend + RSI extreme (triple confluence)
  E. OBR-NEXTBAR       Entry only if next bar confirms (close in reversal direction)

TP Levels: 1.0R, 1.5R, 2.0R, 2.5R
SL: Outside bar's extreme (OB low for longs, OB high for shorts) — natural, adaptive
Risk: 2%  |  Fee: 0.04R  |  Max 2 concurrent  |  $50 start
Session-faithful  |  Bybit (128 pairs) + Binance (58 pairs)

Breakeven WR with 0.04R fee:
  1.0R → 52.0%    1.5R → 41.6%    2.0R → 34.7%    2.5R → 29.7%
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
#  INDICATORS
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
        if d > 0: ag += d
        else: al -= d
    ag /= period; al /= period
    out.append(100.0 if al < 1e-15 else 100.0 - 100.0 / (1.0 + ag / al))
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g, lo = max(d, 0), max(-d, 0)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + lo) / period
        out.append(100.0 if al < 1e-15 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def precompute(pair_data: Dict[str, List[Candle]]) -> Dict[str, dict]:
    inds = {}
    for pair, candles in pair_data.items():
        closes = [c.c for c in candles]
        inds[pair] = {
            "ema10": _ema(closes, 10),
            "ema30": _ema(closes, 30),
            "rsi14": _rsi(closes, 14),
        }
    return inds


# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class OC:
    name: str = ""
    variant: str = "pure"       # pure | trend | rsi | stack | nextbar
    tp_r: float = 1.0
    risk_pct: float = 0.02
    fee_r: float = 0.04
    max_concurrent: int = 2
    start_equity: float = 50.0
    rsi_low: float = 35.0       # oversold threshold for longs
    rsi_high: float = 65.0      # overbought threshold for shorts


# ═══════════════════════════════════════════════════════════════════
#  TRADE MANAGEMENT  (fixed TP + SL)
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
#  OBR SIGNAL DETECTION  (mirrors notebook logic exactly)
# ═══════════════════════════════════════════════════════════════════

def detect_outside_bar(prev: Candle, curr: Candle):
    """
    Detect outside bar signal per the SimpleCandleStrategy notebook.
    Returns:
        2 = bearish OB (enter long reversal)
        1 = bullish OB (enter short reversal)
        0 = no signal
    """
    # LONG signal (signal=2): bearish OB
    # c0: bearish candle (open > close)
    # c1: High > prev High
    # c2: Low < prev Low
    # c3: Close < prev Low  (extreme close beyond prev range)
    if (curr.o > curr.c
            and curr.h > prev.h
            and curr.l < prev.l
            and curr.c < prev.l):
        return 2

    # SHORT signal (signal=1): bullish OB
    # c0: bullish candle (open < close)
    # c1: Low < prev Low
    # c2: High > prev High
    # c3: Close > prev High  (extreme close beyond prev range)
    if (curr.o < curr.c
            and curr.l < prev.l
            and curr.h > prev.h
            and curr.c > prev.h):
        return 1

    return 0


# ═══════════════════════════════════════════════════════════════════
#  ENTRY LOGIC  (variant-specific filtering)
# ═══════════════════════════════════════════════════════════════════

def check_obr_entry(cfg: OC, pair: str, idx: int,
                    candles: List[Candle], inds: dict, time_idx: dict,
                    pair_data, ct: datetime):
    """
    Check for OBR entry.
    Returns (direction, entry_price, stop_loss) or None.

    Entry is at the NEXT candle's open (idx+1) after the OB is detected (idx).
    SL at the OB's extreme (low for longs, high for shorts).
    """
    if idx < 1:
        return None
    # We need to look at candle idx-1 (prev) and idx (potential OB)
    # But we want entry at idx+1's open
    # So at candle idx, we detect the OB from candles idx-1 and idx
    # Then at candle idx+1 we enter at its open

    # Actually, let's detect OB on current candle and enter at NEXT candle
    # But in our session walk, we process candles sequentially.
    # So: at each candle, check if PREVIOUS candle was an OB, and enter at current open.

    prev_prev = candles[idx - 1] if idx >= 2 else None
    prev = candles[idx - 1]  # the candle we want to check as OB
    curr = candles[idx]      # current candle = we enter at curr.o

    # Need the candle before prev to detect OB on prev
    if idx < 2:
        return None

    ob_mother = candles[idx - 2]  # the candle before the OB
    ob_candle = candles[idx - 1]  # the potential outside bar

    signal = detect_outside_bar(ob_mother, ob_candle)
    if signal == 0:
        return None

    # Entry at current candle's open
    entry_price = curr.o
    if entry_price <= 0:
        return None

    if signal == 2:  # Bearish OB → LONG reversal
        direction = "long"
        sl = ob_candle.l  # SL at OB's low (if it goes lower, reversal failed)
        risk = entry_price - sl

        # ── Variant filters ──
        if cfg.variant in ("trend", "stack"):
            ef = inds["ema10"]
            es = inds["ema30"]
            if idx >= len(ef) or ef[idx] is None or es[idx] is None:
                return None
            if ef[idx] <= es[idx]:  # only long in uptrend
                return None

        if cfg.variant in ("rsi", "stack"):
            rsi = inds["rsi14"]
            if idx >= len(rsi) or rsi[idx] is None:
                return None
            if rsi[idx] > cfg.rsi_low:  # not oversold enough
                return None

        if cfg.variant == "nextbar":
            # Current candle must close above its open (bullish confirmation)
            if curr.c <= curr.o:
                return None

    elif signal == 1:  # Bullish OB → SHORT reversal
        direction = "short"
        sl = ob_candle.h  # SL at OB's high
        risk = sl - entry_price

        if cfg.variant in ("trend", "stack"):
            ef = inds["ema10"]
            es = inds["ema30"]
            if idx >= len(ef) or ef[idx] is None or es[idx] is None:
                return None
            if ef[idx] >= es[idx]:  # only short in downtrend
                return None

        if cfg.variant in ("rsi", "stack"):
            rsi = inds["rsi14"]
            if idx >= len(rsi) or rsi[idx] is None:
                return None
            if rsi[idx] < cfg.rsi_high:  # not overbought enough
                return None

        if cfg.variant == "nextbar":
            if curr.c >= curr.o:
                return None
    else:
        return None

    if risk <= 0 or risk / entry_price < 0.0005:
        return None

    return (direction, entry_price, sl)


# ═══════════════════════════════════════════════════════════════════
#  SESSION-FAITHFUL SIMULATION
# ═══════════════════════════════════════════════════════════════════

def simulate(pair_data, time_idx, indicators, cfg: OC) -> dict:
    all_dts = []
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

                    ci_idx = time_idx.get(p, {}).get(ct)
                    if ci_idx is None:
                        continue
                    pair_candles = pair_data[p]
                    pair_inds = indicators.get(p)
                    if pair_inds is None:
                        continue

                    entry = check_obr_entry(
                        cfg, p, ci_idx, pair_candles, pair_inds,
                        time_idx, pair_data, ct)
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


def _stats(closed, final_eq, peak_eq, max_dd, ss_stats, exits, dir_stats, cfg):
    r_vals = [t.r_multiple for t in closed if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers  = [r for r in r_vals if r <= 0]

    x10 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed):
        if t.r_multiple is None: continue
        eq += t.dollar_risk * t.r_multiple
        eq = max(eq, 0.01)
        if x10 is None and eq >= cfg.start_equity * 10:
            x10 = i + 1

    mc = cur = 0
    for r in r_vals:
        if r <= 0: cur += 1; mc = max(mc, cur)
        else: cur = 0

    monthly: Dict[str, List[float]] = {}
    for t in closed:
        if t.r_multiple is not None:
            mk = t.entry_time.strftime("%Y-%m")
            monthly.setdefault(mk, []).append(t.r_multiple)

    pair_s: Dict[str, dict] = {}
    for t in closed:
        if t.r_multiple is None: continue
        p = t.pair.replace("/USDT:USDT", "")
        if p not in pair_s:
            pair_s[p] = {"trades": 0, "wins": 0, "total_r": 0.0}
        pair_s[p]["trades"] += 1
        pair_s[p]["total_r"] += t.r_multiple
        if t.r_multiple > 0: pair_s[p]["wins"] += 1

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


def be_wr(tp_r: float) -> float:
    return 1.04 / ((tp_r - 0.04) + 1.04)


LABELS = {
    "pure": "OBR-PURE",
    "trend": "OBR-TREND",
    "rsi": "OBR-RSI",
    "stack": "OBR-STACK",
    "nextbar": "OBR-NEXTBAR",
}


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    t0 = time_mod.time()
    random.seed(42)
    w = 100

    print("=" * w)
    print("  OUTSIDE BAR REVERSAL (OBR) LAB")
    print("  From SimpleCandleStrategy notebook -> adapted for crypto futures")
    print("  5 variants  x  4 TP levels  x  2 exchanges = 40 sims")
    print("  Session-faithful  |  $50 start  |  2% risk  |  Max 2 concurrent")
    print()
    print("  Pattern: Outside Bar that engulfs AND closes beyond prev range")
    print("           -> Enter OPPOSITE direction (fade the exhaustion)")
    print("  SL: OB's extreme  |  Entry: next candle's open")
    print()
    print("  Breakeven:  1.0R=52.0%  1.5R=41.6%  2.0R=34.7%  2.5R=29.7%")
    print("=" * w)

    # ── Load data ──
    print("\n  Loading Bybit data...")
    bybit_data = load_all_pairs()
    print(f"  Bybit: {len(bybit_data)} pairs ({sum(len(c) for c in bybit_data.values()):,} candles)")

    print("  Loading Binance data...")
    binance_data = load_binance_pairs()
    print(f"  Binance: {len(binance_data)} pairs ({sum(len(c) for c in binance_data.values()):,} candles)")

    print("  Computing indicators...")
    ind_by = precompute(bybit_data)
    ind_bn = precompute(binance_data)
    ti_by  = build_time_index(bybit_data)
    ti_bn  = build_time_index(binance_data)
    print("  Done.\n")

    variants = ["pure", "trend", "rsi", "stack", "nextbar"]
    tps = [1.0, 1.5, 2.0, 2.5]
    exchanges = [
        ("Bybit",   bybit_data, ti_by, ind_by),
        ("Binance", binance_data, ti_bn, ind_bn),
    ]

    configs = []
    for var in variants:
        for tp in tps:
            for ex_n, pd, ti, ind in exchanges:
                name = f"{LABELS[var]}-{tp}R-{ex_n}"
                cfg = OC(name=name, variant=var, tp_r=tp)
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
    #  SECTION 1: ALL RESULTS
    # ══════════════════════════════════════════════════════════════

    sr = sorted(results, key=lambda r: (-r["total_r"], -r["wr"]))

    print("=" * w)
    print("  SECTION 1: ALL RESULTS (sorted by Total R)")
    print("=" * w)

    hdr = (f"  {'Strategy':<30s}  {'#':>5s}  {'WR':>5s}  {'BE':>5s}"
           f"  {'AvgR':>7s}  {'TotR':>7s}  {'MaxDD':>6s}  {'CL':>3s}"
           f"  {'x10':>5s}  {'Final$':>9s}  {'Exits':>12s}")
    print(hdr)
    print(f"  {'-'*30}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*3}  {'-'*5}  {'-'*9}  {'-'*12}")

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
    #  SECTION 2: BYBIT vs BINANCE  (side-by-side per variant-TP)
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 2: BYBIT vs BINANCE  (same config, side-by-side)")
    print("  Edge? = WR above breakeven on BOTH exchanges")
    print(f"{'=' * w}\n")

    pairs_map: Dict[str, Dict[str, dict]] = {}
    for r in results:
        c = r["cfg"]
        key = f"{LABELS[c.variant]}-{c.tp_r}R"
        ex = "Bybit" if "Bybit" in c.name else "Binance"
        pairs_map.setdefault(key, {})[ex] = r

    hdr2 = (f"  {'Strategy':<20s}  {'BE':>5s}  "
            f"{'By #':>5s}  {'By WR':>6s}  {'By TR':>7s}  {'By DD':>6s}  "
            f"{'Bn #':>5s}  {'Bn WR':>6s}  {'Bn TR':>7s}  {'Bn DD':>6s}  "
            f"{'Gap':>5s}  {'Edge?':>6s}")
    print(hdr2)
    print(f"  {'-'*20}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*6}")

    edge_configs = []
    for key in sorted(pairs_map.keys()):
        pm = pairs_map[key]
        tp = float(key.rsplit("-", 1)[-1].replace("R", ""))
        bw = be_wr(tp)
        by = pm.get("Bybit", _empty(OC()))
        bn = pm.get("Binance", _empty(OC()))
        gap = by["wr"] - bn["wr"]
        both = (by["wr"] > bw and bn["wr"] > bw
                and by["trades"] >= 20 and bn["trades"] >= 20)
        if both:
            edge_configs.append((key, by, bn, bw))
        flag = "YES" if both else "no"
        print(f"  {key:<20s}  {bw*100:4.1f}%  "
              f"{by['trades']:5d}  {by['wr']*100:5.1f}%  {by['total_r']:+7.1f}  {by['max_dd']*100:5.1f}%  "
              f"{bn['trades']:5d}  {bn['wr']*100:5.1f}%  {bn['total_r']:+7.1f}  {bn['max_dd']*100:5.1f}%  "
              f"{gap*100:+4.1f}%  {flag:>6s}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: TOP 5 DETAILED
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 3: TOP 5 DETAILED  (by Total R)")
    print(f"{'=' * w}")

    for rank, r in enumerate(sr[:5], 1):
        c = r["cfg"]
        bw = be_wr(c.tp_r)
        flag = " >>> ABOVE BREAKEVEN <<<" if r["wr"] > bw and r["trades"] >= 20 else ""
        print(f"\n  #{rank}: {c.name}{flag}")
        print(f"  {'~'*60}")
        print(f"    Trades: {r['trades']}  |  WR: {r['wr']*100:.1f}% (BE: {bw*100:.1f}%)"
              f"  |  Avg R: {r['avg_r']:+.4f}  |  Total R: {r['total_r']:+.1f}")
        if r["trades"] > 0 and r["avg_loss"] != 0:
            pr = abs(r["avg_win"]) / abs(r["avg_loss"])
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
                flag_m = " !!!" if mtr < -5 else ""
                print(f"      {mk}: {len(rs):3d}t  WR={mwr*100:4.0f}%  R={mtr:+7.1f}{flag_m}")

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
    #  SECTION 4: MONTE CARLO
    # ══════════════════════════════════════════════════════════════

    profitable = [r for r in sr if r["total_r"] > 0 and r["trades"] >= 20]

    print(f"\n{'=' * w}")
    print("  SECTION 4: MONTE CARLO STRESS TEST (2000 trials)")
    print(f"{'=' * w}")

    if not profitable:
        print("\n  No profitable strategies with 20+ trades -- skipping Monte Carlo.")
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
    #  SECTION 5: VERDICT
    # ══════════════════════════════════════════════════════════════

    print(f"\n{'=' * w}")
    print("  SECTION 5: VERDICT")
    print(f"{'=' * w}")

    if edge_configs:
        print(f"\n  >>> EDGE ON BOTH EXCHANGES <<<")
        for key, by, bn, bw in edge_configs:
            print(f"\n  {key}:")
            print(f"    Breakeven WR: {bw*100:.1f}%")
            print(f"    Bybit:   {by['trades']}t  WR={by['wr']*100:.1f}%  TotR={by['total_r']:+.1f}  DD={by['max_dd']*100:.1f}%")
            print(f"    Binance: {bn['trades']}t  WR={bn['wr']*100:.1f}%  TotR={bn['total_r']:+.1f}  DD={bn['max_dd']*100:.1f}%")

    # Best Binance
    bnce = sorted([r for r in results if "Binance" in r["cfg"].name],
                  key=lambda r: -r["total_r"])
    print(f"\n  VARIANT RANKING (Binance OOS, best TP each):")
    seen = set()
    for r in bnce:
        v = r["cfg"].variant
        if v in seen: continue
        seen.add(v)
        c = r["cfg"]
        bw = be_wr(c.tp_r)
        flag = " <<<EDGE>>>" if r["wr"] > bw and r["trades"] >= 20 else ""
        print(f"    {LABELS[v]:<14s}  {r['trades']:4d}t  WR={r['wr']*100:4.1f}% (BE:{bw*100:.0f}%)"
              f"  TotR={r['total_r']:+7.1f}  DD={r['max_dd']*100:4.1f}%"
              f"  ({c.name}){flag}")

    # Best Bybit
    bybt = sorted([r for r in results if "Bybit" in r["cfg"].name],
                  key=lambda r: -r["total_r"])
    print(f"\n  VARIANT RANKING (Bybit, best TP each):")
    seen = set()
    for r in bybt:
        v = r["cfg"].variant
        if v in seen: continue
        seen.add(v)
        c = r["cfg"]
        bw = be_wr(c.tp_r)
        flag = " <<<EDGE>>>" if r["wr"] > bw and r["trades"] >= 20 else ""
        print(f"    {LABELS[v]:<14s}  {r['trades']:4d}t  WR={r['wr']*100:4.1f}% (BE:{bw*100:.0f}%)"
              f"  TotR={r['total_r']:+7.1f}  DD={r['max_dd']*100:4.1f}%"
              f"  ({c.name}){flag}")

    elapsed = time_mod.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)\n")


if __name__ == "__main__":
    main()
