"""
research/strategy_lab.py — Alternative Strategy Lab

Finds the BEST strategy for $50 → $500 (x10) with LOW DD and HIGH WR.

Tests 8 approaches with the SAME data & session-faithful engine:

  1. FCB-TP1R     Same FCB entry (C2 breakout + C3 retest), fixed TP at 1.0R
  2. FCB-TP1.5R   Same FCB entry, fixed TP at 1.5R
  3. FCB-TP2R     Same FCB entry, fixed TP at 2.0R
  4. FCB-BE+TP    FCB entry, BE move at 0.5R, TP at 1.5R
  5. QUICK-75     FCB entry NO retest, grab 0.75R fast
  6. FADE-1R      Fade failed breakouts, TP 1.0R
  7. FADE-1.5R    Fade failed breakouts, TP 1.5R
  8. FCB-TRAIL    Current trailing strategy (baseline)

All with concentrated pair universe (top 20 + top 30) at risk 2-4%.
Starting equity: $50.
"""

from __future__ import annotations
import sys, time, statistics, random
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research.session_sim import (
    load_all_pairs, build_time_index, get_candle_at, get_candle_after,
    Candle, OpenTrade, SESSIONS, SESSION_ORDER, monte_carlo,
)

# ═══════════════════════════════════════════════
#  TOP PAIRS from pair_solo.py (ranked by total R)
# ═══════════════════════════════════════════════
PAIR_PRIORITY = [
    "MYX/USDT:USDT", "SEI/USDT:USDT", "HEMI/USDT:USDT", "WLFI/USDT:USDT",
    "KITE/USDT:USDT", "DYDX/USDT:USDT", "ENA/USDT:USDT", "INIT/USDT:USDT",
    "PENGU/USDT:USDT", "US/USDT:USDT", "FHE/USDT:USDT", "ARB/USDT:USDT",
    "RENDER/USDT:USDT", "SOMI/USDT:USDT", "COAI/USDT:USDT", "FIL/USDT:USDT",
    "WIF/USDT:USDT", "MON/USDT:USDT", "UNI/USDT:USDT", "ZKP/USDT:USDT",
    "APE/USDT:USDT", "VIRTUAL/USDT:USDT", "STRK/USDT:USDT", "UAI/USDT:USDT",
    "ORCA/USDT:USDT", "APEX/USDT:USDT", "JTO/USDT:USDT", "TAO/USDT:USDT",
    "FLUID/USDT:USDT", "GALA/USDT:USDT",
]
TOP_20_SET = set(PAIR_PRIORITY[:20])
TOP_30_SET = set(PAIR_PRIORITY[:30])


# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════

@dataclass
class LabConfig:
    name: str = ""
    entry_mode: str = "breakout"    # "breakout" or "fade"
    exit_mode: str = "fixed_tp"     # "fixed_tp" or "trail"
    tp_r: float = 1.0               # Fixed TP in R-multiples
    be_trigger_r: float = 0.0       # Move SL to BE at this R (0 = disabled)
    require_retest: bool = True
    risk_pct: float = 0.03
    max_concurrent: int = 2
    fc_counter: bool = True
    trail_activation_r: float = 0.95
    trail_distance_r: float = 0.15
    trail_max_r: float = 10.0
    min_c2_body: float = 0.50
    vol_ratio_long: float = 1.0
    vol_ratio_short: float = 0.25
    min_range_pct: float = 0.003
    fee_per_trade_r: float = 0.04
    breakout_window: int = 12       # 60min / 5min
    max_per_session: int = 1
    max_per_day: int = 6
    start_equity: float = 50.0


# ═══════════════════════════════════════════════
#  TRADE MANAGEMENT
# ═══════════════════════════════════════════════

def _manage_fixed_tp(candle: Candle, trade: OpenTrade, cfg: LabConfig):
    """Fixed TP + optional breakeven move."""
    h, l = candle.h, candle.l
    risk = trade.risk_per_unit

    if trade.direction == "long":
        # SL check first (conservative: use low)
        if l <= trade.stop_loss:
            reason = "be" if (cfg.be_trigger_r > 0 and trade.trail_active) else "sl"
            trade.close(trade.stop_loss, candle.dt, reason)
            return

        current_r = (h - trade.entry_price) / risk if risk > 0 else 0

        # BE trigger
        if cfg.be_trigger_r > 0 and not trade.trail_active:
            if current_r >= cfg.be_trigger_r:
                trade.trail_active = True
                trade.stop_loss = trade.entry_price

        # TP check
        tp_price = trade.entry_price + cfg.tp_r * risk
        if h >= tp_price:
            trade.close(tp_price, candle.dt, "tp")
            return

    else:  # SHORT
        if h >= trade.stop_loss:
            reason = "be" if (cfg.be_trigger_r > 0 and trade.trail_active) else "sl"
            trade.close(trade.stop_loss, candle.dt, reason)
            return

        current_r = (trade.entry_price - l) / risk if risk > 0 else 0

        if cfg.be_trigger_r > 0 and not trade.trail_active:
            if current_r >= cfg.be_trigger_r:
                trade.trail_active = True
                trade.stop_loss = trade.entry_price

        tp_price = trade.entry_price - cfg.tp_r * risk
        if l <= tp_price:
            trade.close(tp_price, candle.dt, "tp")
            return


def _manage_trail(candle: Candle, trade: OpenTrade, cfg: LabConfig):
    """Trailing stop management (identical to session_sim)."""
    h, l = candle.h, candle.l
    risk = trade.risk_per_unit

    if trade.direction == "long":
        current_r = (h - trade.entry_price) / risk if risk > 0 else 0

        if l <= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return

        if trade.trail_active:
            if h > (trade.peak_price or 0):
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop = h - cfg.trail_distance_r * risk
            if current_r >= cfg.trail_max_r:
                trade.close(trade.entry_price + cfg.trail_max_r * risk, candle.dt, "max_r")
                return
            if trade.trail_stop and l <= trade.trail_stop:
                trade.close(trade.trail_stop, candle.dt, "trail")
                return
        else:
            if current_r >= cfg.trail_activation_r:
                trade.trail_active = True
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop = h - cfg.trail_distance_r * risk
                trade.stop_loss = trade.entry_price

        if current_r > trade.peak_r:
            trade.peak_r = current_r

    else:  # SHORT
        current_r = (trade.entry_price - l) / risk if risk > 0 else 0

        if h >= trade.stop_loss:
            trade.close(trade.stop_loss, candle.dt, "sl")
            return

        if trade.trail_active:
            if l < (trade.peak_price or float('inf')):
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop = l + cfg.trail_distance_r * risk
            if current_r >= cfg.trail_max_r:
                trade.close(trade.entry_price - cfg.trail_max_r * risk, candle.dt, "max_r")
                return
            if trade.trail_stop and h >= trade.trail_stop:
                trade.close(trade.trail_stop, candle.dt, "trail")
                return
        else:
            if current_r >= cfg.trail_activation_r:
                trade.trail_active = True
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop = l + cfg.trail_distance_r * risk
                trade.stop_loss = trade.entry_price

        if current_r > trade.peak_r:
            trade.peak_r = current_r


# ═══════════════════════════════════════════════
#  SIMULATION ENGINE
# ═══════════════════════════════════════════════

def simulate_lab(pair_data: Dict[str, List[Candle]], cfg: LabConfig) -> dict:
    """
    Session-faithful simulation supporting multiple strategy modes.

    Walks every day x 3 sessions x every 5-min candle, exactly like the live bot.
    Pairs are checked in PRIORITY ORDER (best pairs get slots first).
    """
    time_idx = build_time_index(pair_data)

    all_dts = []
    for candles in pair_data.values():
        if candles:
            all_dts.extend([candles[0].dt, candles[-1].dt])
    if not all_dts:
        return _empty_result(cfg)

    start_date = min(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = max(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)

    # Build pair priority list for this universe
    pair_order = []
    for p in PAIR_PRIORITY:
        if p in pair_data:
            pair_order.append(p)
    # Add any remaining pairs not in priority list (alphabetical)
    for p in sorted(pair_data.keys()):
        if p not in set(pair_order):
            pair_order.append(p)

    # State
    equity = cfg.start_equity
    peak_eq = cfg.start_equity
    max_dd = 0.0
    open_positions: Dict[str, OpenTrade] = {}
    closed_trades: List[OpenTrade] = []

    session_entries: Dict[str, Set[str]] = {}
    daily_counts: Dict[str, int] = {}

    session_stats: Dict[str, dict] = {
        s: {"trades": 0, "wins": 0, "losses": 0, "total_r": 0.0}
        for s in SESSION_ORDER
    }
    exit_reasons: Dict[str, int] = {}

    # Walk day by day
    current_day = start_date
    while current_day <= end_date:
        session_entries.clear()
        daily_counts.clear()

        for sess_name in SESSION_ORDER:
            sh, eh = SESSIONS[sess_name]
            session_entries[sess_name] = set()

            sess_start = current_day.replace(
                hour=sh, minute=0, second=0, microsecond=0)
            sess_end = current_day.replace(
                hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=eh)

            # ── Step 1: Capture FCs ──
            fc_time = sess_start
            pair_fcs: Dict[str, Candle] = {}
            for pair in pair_order:
                fc = get_candle_at(pair, fc_time, pair_data, time_idx)
                if fc is None:
                    continue
                mid = (fc.h + fc.l) / 2
                if mid <= 0:
                    continue
                if (fc.h - fc.l) / mid < cfg.min_range_pct:
                    continue
                pair_fcs[pair] = fc

            # ── Step 2: Walk candles ──
            n_candles = (eh - sh) * 12
            for c_idx in range(n_candles):
                ct = sess_start + timedelta(minutes=c_idx * 5)

                # Manage open trades
                for pair in list(open_positions.keys()):
                    trade = open_positions[pair]
                    candle = get_candle_at(pair, ct, pair_data, time_idx)
                    if candle is None:
                        continue

                    if cfg.exit_mode == "trail":
                        _manage_trail(candle, trade, cfg)
                    else:
                        _manage_fixed_tp(candle, trade, cfg)

                    if not trade.is_open:
                        if cfg.fee_per_trade_r > 0:
                            trade.r_multiple -= cfg.fee_per_trade_r
                        pnl = trade.dollar_risk * trade.r_multiple
                        equity += pnl
                        equity = max(equity, 0.01)
                        if equity > peak_eq:
                            peak_eq = equity
                        dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
                        max_dd = max(max_dd, dd)
                        closed_trades.append(trade)
                        del open_positions[pair]

                        # Track stats
                        session_stats[sess_name]["trades"] += 1
                        session_stats[sess_name]["total_r"] += trade.r_multiple
                        if trade.r_multiple > 0:
                            session_stats[sess_name]["wins"] += 1
                        else:
                            session_stats[sess_name]["losses"] += 1
                        er = trade.exit_reason or "unknown"
                        exit_reasons[er] = exit_reasons.get(er, 0) + 1

                # ── Step 3: Look for entries ──
                if c_idx < 1 or c_idx >= cfg.breakout_window:
                    continue

                # Iterate pairs in PRIORITY ORDER
                for pair in pair_order:
                    if pair not in pair_fcs:
                        continue
                    fc = pair_fcs[pair]

                    # can_trade checks
                    if pair in session_entries[sess_name]:
                        continue
                    if daily_counts.get(pair, 0) >= cfg.max_per_day:
                        continue
                    if pair in open_positions:
                        continue
                    if len(open_positions) >= cfg.max_concurrent:
                        break  # no slots

                    # Get C2 candle
                    c2 = get_candle_at(pair, ct, pair_data, time_idx)
                    if c2 is None:
                        continue

                    # Detect breakout direction
                    bo_dir = None
                    if c2.c > fc.h:
                        bo_dir = "long"
                    elif c2.c < fc.l:
                        bo_dir = "short"
                    if bo_dir is None:
                        continue

                    # ── Shared filters (both modes) ──
                    if cfg.min_c2_body > 0 and c2.body_ratio < cfg.min_c2_body:
                        continue
                    vr = c2.v / fc.v if fc.v > 0 else 1.0
                    if bo_dir == "long" and cfg.vol_ratio_long > 0 and vr < cfg.vol_ratio_long:
                        continue
                    if bo_dir == "short" and cfg.vol_ratio_short > 0 and vr < cfg.vol_ratio_short:
                        continue

                    # ══════════════════════════════════
                    #  ENTRY: BREAKOUT MODE
                    # ══════════════════════════════════
                    if cfg.entry_mode == "breakout":
                        # FC counter filter
                        if cfg.fc_counter:
                            if bo_dir == "long" and fc.candle_dir > 0:
                                continue
                            if bo_dir == "short" and fc.candle_dir < 0:
                                continue

                        if cfg.require_retest:
                            c3 = get_candle_after(
                                pair, ct, pair_data, time_idx, offset=1)
                            if c3 is None:
                                continue
                            if bo_dir == "long":
                                if not (c3.l <= fc.h and c3.c > fc.h):
                                    continue
                            else:
                                if not (c3.h >= fc.l and c3.c < fc.l):
                                    continue
                            entry_price = c3.c
                            entry_time = c3.dt
                        else:
                            entry_price = c2.c
                            entry_time = c2.dt

                        direction = bo_dir
                        sl = (fc.h + fc.l) / 2
                        risk_per_unit = abs(entry_price - sl)

                    # ══════════════════════════════════
                    #  ENTRY: FADE MODE
                    # ══════════════════════════════════
                    elif cfg.entry_mode == "fade":
                        c3 = get_candle_after(
                            pair, ct, pair_data, time_idx, offset=1)
                        if c3 is None:
                            continue

                        fc_range = fc.h - fc.l

                        if bo_dir == "long":
                            # Breakout was long; check if C3 fails
                            # (closes back below FC high)
                            if c3.c >= fc.h:
                                continue  # breakout held, no fade
                            # FADE → SHORT
                            direction = "short"
                            entry_price = c3.c
                            entry_time = c3.dt
                            sl = max(c2.h, c3.h)
                            risk_per_unit = sl - entry_price
                        else:
                            # Breakout was short; check if C3 fails
                            if c3.c <= fc.l:
                                continue
                            # FADE → LONG
                            direction = "long"
                            entry_price = c3.c
                            entry_time = c3.dt
                            sl = min(c2.l, c3.l)
                            risk_per_unit = entry_price - sl

                        # Sanity: skip if risk is absurd relative to FC range
                        if risk_per_unit > fc_range * 2:
                            continue
                    else:
                        continue

                    # ── Risk validation ──
                    if risk_per_unit <= 0:
                        continue
                    if risk_per_unit / entry_price < 0.0005:
                        continue  # risk too tiny = degenerate
                    if len(open_positions) >= cfg.max_concurrent:
                        break
                    if equity < 2.0:
                        break

                    # ── ENTER ──
                    dollar_risk = equity * cfg.risk_pct
                    trade = OpenTrade(
                        pair=pair, session=sess_name, direction=direction,
                        entry_price=entry_price, entry_time=entry_time,
                        stop_loss=sl, risk_per_unit=risk_per_unit,
                        dollar_risk=dollar_risk, entry_equity=equity,
                        range_high=fc.h, range_low=fc.l,
                        range_mid=(fc.h + fc.l) / 2,
                    )
                    open_positions[pair] = trade
                    session_entries[sess_name].add(pair)
                    daily_counts[pair] = daily_counts.get(pair, 0) + 1

        current_day += timedelta(days=1)

    # ── Close remaining open trades ──
    for pair, trade in list(open_positions.items()):
        if pair in pair_data and pair_data[pair]:
            last = pair_data[pair][-1]
            trade.close(last.c, last.dt, "data_end")
            if cfg.fee_per_trade_r > 0:
                trade.r_multiple -= cfg.fee_per_trade_r
            pnl = trade.dollar_risk * trade.r_multiple
            equity += pnl
            equity = max(equity, 0.01)
            closed_trades.append(trade)

    return _compute_stats(
        closed_trades, equity, peak_eq, max_dd,
        session_stats, exit_reasons, cfg)


# ═══════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════

def _compute_stats(closed_trades, final_eq, peak_eq, max_dd,
                   session_stats, exit_reasons, cfg) -> dict:
    r_vals = [t.r_multiple for t in closed_trades if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers = [r for r in r_vals if r <= 0]
    scratches = sum(1 for r in r_vals if -0.10 <= r <= 0.10)

    # x10 milestone
    x10 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed_trades):
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
    for t in closed_trades:
        if t.r_multiple is None:
            continue
        mk = t.entry_time.strftime("%Y-%m")
        monthly.setdefault(mk, []).append(t.r_multiple)

    # Pair stats
    pair_stats: Dict[str, dict] = {}
    for t in closed_trades:
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
        "max_consec_loss": max_consec,
        "scratches": scratches,
        "r_vals": r_vals,
        "monthly": monthly,
        "session_stats": session_stats,
        "pair_stats": pair_stats,
        "exit_reasons": exit_reasons,
        "closed_trades": closed_trades,
        "config": cfg,
    }


def _empty_result(cfg):
    return {
        "trades": 0, "wr": 0, "avg_r": 0, "total_r": 0, "max_dd": 0,
        "final_eq": cfg.start_equity, "x10": None, "r_vals": [],
        "monthly": {}, "config": cfg, "scratches": 0,
        "exit_reasons": {}, "session_stats": {}, "pair_stats": {},
        "closed_trades": [], "max_consec_loss": 0,
    }


# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════

def main():
    t0 = time.time()
    random.seed(42)

    print("=" * 90)
    print("  STRATEGY LAB — Finding Higher WR & Lower DD")
    print("  Start: $50  |  Session-faithful  |  Priority pair ordering")
    print("  Goal: x10 ($500) with DD < 50% and WR > 45%")
    print("=" * 90)

    # ── Load data ──
    print("\n  Loading data...")
    all_pairs = load_all_pairs()
    top20 = {p: c for p, c in all_pairs.items() if p in TOP_20_SET}
    top30 = {p: c for p, c in all_pairs.items() if p in TOP_30_SET}
    total_c = sum(len(c) for c in all_pairs.values())
    print(f"  {len(all_pairs)} total pairs ({total_c:,} candles)")
    print(f"  Top-20: {len(top20)} pairs  |  Top-30: {len(top30)} pairs")

    # ── Define strategies ──
    # (name, entry_mode, exit_mode, tp_r, be_trigger, require_retest, fc_counter)
    strategies = [
        ("FCB-TP1R",    "breakout", "fixed_tp", 1.0,  0.0, True,  True),
        ("FCB-TP1.5R",  "breakout", "fixed_tp", 1.5,  0.0, True,  True),
        ("FCB-TP2R",    "breakout", "fixed_tp", 2.0,  0.0, True,  True),
        ("FCB-BE+TP",   "breakout", "fixed_tp", 1.5,  0.5, True,  True),
        ("QUICK-75",    "breakout", "fixed_tp", 0.75, 0.0, False, True),
        ("FADE-1R",     "fade",     "fixed_tp", 1.0,  0.0, False, False),
        ("FADE-1.5R",   "fade",     "fixed_tp", 1.5,  0.0, False, False),
        ("FCB-TRAIL*",  "breakout", "trail",    10.0, 0.0, True,  True),
    ]

    risks = [0.02, 0.03, 0.04]
    universes = [("T20", top20), ("T30", top30)]

    total = len(strategies) * len(risks) * len(universes)
    print(f"\n  Running {total} simulations...\n")

    results = []
    n = 0
    for s_name, entry, exit_m, tp, be, retest, fcc in strategies:
        for risk in risks:
            for u_name, u_data in universes:
                n += 1
                label = f"{s_name} {risk:.0%} {u_name}"
                sys.stdout.write(f"\r  {n:>3d}/{total}: {label:<35s}")
                sys.stdout.flush()

                cfg = LabConfig(
                    name=label,
                    entry_mode=entry,
                    exit_mode=exit_m,
                    tp_r=tp,
                    be_trigger_r=be,
                    require_retest=retest,
                    risk_pct=risk,
                    fc_counter=fcc,
                )
                res = simulate_lab(u_data, cfg)
                results.append(res)

    elapsed_sim = time.time() - t0
    print(f"\r  Done: {total} simulations in {elapsed_sim:.0f}s.              \n")

    # ═══════════════════════════════════════════════════
    #  SECTION 1: FULL COMPARISON TABLE
    # ═══════════════════════════════════════════════════
    print("=" * 100)
    print("  SECTION 1: ALL STRATEGIES COMPARED")
    print("  Sorted by Win Rate (highest first), then lowest DD")
    print("=" * 100)

    results.sort(key=lambda r: (-r["wr"], r["max_dd"]))

    hdr = (f"  {'Strategy':>25s}  {'#':>4s}  {'WR':>5s}  {'AvgR':>7s}  "
           f"{'TotR':>6s}  {'MaxDD':>6s}  {'ConsL':>5s}  "
           f"{'x10':>5s}  {'Final$':>9s}")
    div = (f"  {'-'*25}  {'-'*4}  {'-'*5}  {'-'*7}  "
           f"{'-'*6}  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*9}")
    print(f"\n{hdr}")
    print(div)

    for res in results:
        cfg = res["config"]
        x10s = f"{res['x10']}t" if res.get("x10") else "-"
        flag = ""
        if res["wr"] >= 0.50 and res["max_dd"] <= 0.50:
            flag = " <<<"
        elif res["wr"] >= 0.45 and res["max_dd"] <= 0.40:
            flag = " <<"
        elif res["wr"] >= 0.40 and res["max_dd"] <= 0.50:
            flag = " <"
        print(f"  {cfg.name:>25s}  {res['trades']:>4d}  {res['wr']:>4.1%}  "
              f"{res['avg_r']:>+.4f}  {res['total_r']:>+5.1f}  "
              f"{res['max_dd']:>5.1%}  {res.get('max_consec_loss',0):>5d}  "
              f"{x10s:>5s}  ${res['final_eq']:>8,.0f}{flag}")

    # ═══════════════════════════════════════════════════
    #  SECTION 2: BEST HIGH-WR LOW-DD STRATEGIES
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 2: TOP PERFORMERS (WR > 40% AND DD < 60%, min 50 trades)")
    print("=" * 100)

    filtered = [r for r in results
                if r["wr"] >= 0.40 and r["max_dd"] < 0.60 and r["trades"] >= 50]
    filtered.sort(key=lambda r: (-r["wr"], r["max_dd"]))

    if not filtered:
        # Relax filter
        filtered = [r for r in results if r["trades"] >= 30]
        filtered.sort(key=lambda r: (-r["wr"], r["max_dd"]))
        print("  (Relaxed filter: showing best by WR with min 30 trades)")

    for i, res in enumerate(filtered[:8]):
        cfg = res["config"]
        print(f"\n  #{i+1}: {cfg.name}")
        print(f"    Trades: {res['trades']:>4d}  |  WR: {res['wr']:.1%}  |  "
              f"Avg R: {res['avg_r']:+.4f}  |  Total R: {res['total_r']:+.1f}")
        print(f"    Avg Win: {res.get('avg_win',0):+.3f}  |  "
              f"Avg Loss: {res.get('avg_loss',0):+.3f}  |  "
              f"DD: {res['max_dd']:.1%}  |  Consec: {res['max_consec_loss']}")
        print(f"    Final: ${res['final_eq']:,.2f}  |  "
              f"x10: {'trade #' + str(res['x10']) if res.get('x10') else 'not yet'}")
        if res.get("scratches", 0) > 0:
            print(f"    Scratches (BE exits): {res['scratches']}")

        # Exit reasons
        if res.get("exit_reasons"):
            parts = []
            for reason in ["tp", "sl", "be", "trail", "max_r", "data_end"]:
                count = res["exit_reasons"].get(reason, 0)
                if count > 0:
                    parts.append(f"{reason}={count}")
            if parts:
                print(f"    Exits: {', '.join(parts)}")

        # Monthly breakdown
        if res.get("monthly"):
            print(f"    Monthly:")
            eq = cfg.start_equity
            for mk in sorted(res["monthly"].keys()):
                rv = res["monthly"][mk]
                w = sum(1 for r in rv if r > 0)
                wr = w / len(rv) if rv else 0
                tot = sum(rv)
                s_eq = eq
                for r in rv:
                    eq += eq * cfg.risk_pct * r
                    eq = max(eq, 0.01)
                ret = (eq - s_eq) / s_eq if s_eq > 0 else 0
                print(f"      {mk}: {len(rv):>3d}t  WR={wr:>4.0%}  "
                      f"R={tot:>+6.1f}  "
                      f"${s_eq:>7.0f} -> ${eq:>7.0f}  ({ret:>+5.0%})")

        # Top/bottom pairs
        if res.get("pair_stats"):
            ps = sorted(res["pair_stats"].items(),
                        key=lambda x: x[1]["total_r"], reverse=True)
            prof = sum(1 for _, s in ps if s["total_r"] > 0)
            print(f"    Pairs: {prof}/{len(ps)} profitable")
            top3 = ps[:3]
            bot3 = ps[-3:]
            top_str = ", ".join(
                f"{p} {s['wins']}/{s['trades']} R={s['total_r']:+.1f}"
                for p, s in top3)
            bot_str = ", ".join(
                f"{p} {s['wins']}/{s['trades']} R={s['total_r']:+.1f}"
                for p, s in bot3)
            print(f"      Best:  {top_str}")
            print(f"      Worst: {bot_str}")

    # ═══════════════════════════════════════════════════
    #  SECTION 3: x10 ACHIEVERS
    # ═══════════════════════════════════════════════════
    x10_list = [r for r in results if r.get("x10")]
    print(f"\n{'=' * 100}")
    if x10_list:
        print(f"  SECTION 3: STRATEGIES THAT REACH x10 ($50 -> $500)")
        print("=" * 100)
        x10_list.sort(key=lambda r: r["max_dd"])
        print(f"\n  {'Strategy':>25s}  {'WR':>5s}  {'DD':>6s}  "
              f"{'x10 at':>6s}  {'Final$':>9s}  {'Consec':>6s}")
        print(f"  {'-'*25}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*9}  {'-'*6}")
        for res in x10_list:
            cfg = res["config"]
            print(f"  {cfg.name:>25s}  {res['wr']:>4.1%}  "
                  f"{res['max_dd']:>5.1%}  {res['x10']:>5d}t  "
                  f"${res['final_eq']:>8,.0f}  {res['max_consec_loss']:>6d}")
    else:
        print(f"  SECTION 3: x10 PATHS")
        print("=" * 100)
        print(f"\n  No strategy reached x10 from $50 in 6 months.")
        print(f"  Closest approaches (by final equity):")
        by_eq = sorted(results, key=lambda r: -r["final_eq"])
        for res in by_eq[:5]:
            cfg = res["config"]
            mult = res["final_eq"] / cfg.start_equity
            print(f"    {cfg.name:>25s}  ${cfg.start_equity:.0f} -> "
                  f"${res['final_eq']:,.0f} (x{mult:.1f})  "
                  f"WR={res['wr']:.1%}  DD={res['max_dd']:.1%}")

    # ═══════════════════════════════════════════════════
    #  SECTION 4: MONTE CARLO (top 3)
    # ═══════════════════════════════════════════════════
    mc_candidates = filtered[:3] if len(filtered) >= 3 else (
        sorted(results, key=lambda r: -r["wr"])[:3])

    print(f"\n{'=' * 100}")
    print("  SECTION 4: MONTE CARLO (2000 trials) — Top 3 by WR")
    print("  Answer: what DD should you PLAN FOR?")
    print("=" * 100)

    for res in mc_candidates:
        cfg = res["config"]
        r_vals = res["r_vals"]
        closed = res.get("closed_trades", [])
        d_risks = [t.dollar_risk for t in closed if t.r_multiple is not None]
        if not r_vals or not d_risks:
            continue

        print(f"\n  {cfg.name}  ({len(r_vals)} trades, WR={res['wr']:.1%})")
        mc = monte_carlo(r_vals, d_risks, 2000, cfg.start_equity)
        print(f"    Median DD:   {mc['median_dd']:>6.1%}")
        print(f"    75th pctl:   {mc['p75_dd']:>6.1%}")
        print(f"    95th pctl:   {mc['p95_dd']:>6.1%}  <- PLAN FOR THIS")
        print(f"    Bust (<$5):  {mc['bust_pct']:>6.1%}")
        print(f"    x10 chance:  {mc['x10_pct']:>6.1%}")
        if mc.get("x10_median"):
            print(f"    x10 median:  trade #{mc['x10_median']}")
        print(f"    Median eq:   ${mc['median_final']:>,.0f}")
        print(f"    10th pctl:   ${mc['p10_final']:>,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 5: STRATEGY TYPE HEAD-TO-HEAD
    #  Average across all risk/universe combos
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 5: STRATEGY TYPE COMPARISON (averaged across configs)")
    print("=" * 100)

    strat_types = {}
    for res in results:
        stype = res["config"].name.split()[0]  # e.g. "FCB-TP1R"
        if stype not in strat_types:
            strat_types[stype] = []
        strat_types[stype].append(res)

    print(f"\n  {'Strategy':>14s}  {'AvgWR':>6s}  {'AvgDD':>6s}  "
          f"{'AvgR':>7s}  {'AvgTotR':>8s}  {'MaxFinal':>10s}  {'AvgConsec':>9s}")
    print(f"  {'-'*14}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*9}")

    type_rows = []
    for stype, res_list in sorted(strat_types.items()):
        avg_wr = statistics.mean(r["wr"] for r in res_list)
        avg_dd = statistics.mean(r["max_dd"] for r in res_list)
        avg_r = statistics.mean(r["avg_r"] for r in res_list)
        avg_tot = statistics.mean(r["total_r"] for r in res_list)
        max_eq = max(r["final_eq"] for r in res_list)
        avg_cl = statistics.mean(r.get("max_consec_loss", 0) for r in res_list)
        type_rows.append((stype, avg_wr, avg_dd, avg_r, avg_tot, max_eq, avg_cl))

    type_rows.sort(key=lambda x: -x[1])
    for stype, wr, dd, ar, tot, meq, cl in type_rows:
        flag = " <<<" if wr >= 0.50 and dd <= 0.40 else (
               " <<" if wr >= 0.45 else "")
        print(f"  {stype:>14s}  {wr:>5.1%}  {dd:>5.1%}  "
              f"{ar:>+.4f}  {tot:>+7.1f}  ${meq:>9,.0f}  {cl:>9.1f}{flag}")

    # ═══════════════════════════════════════════════════
    #  SECTION 6: LOSS STREAK COMPARISON
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 6: WHAT LOSING FEELS LIKE")
    print("  Worst losing streak per strategy type at 3% risk")
    print("=" * 100)

    for res in results:
        cfg = res["config"]
        if cfg.risk_pct != 0.03:
            continue
        if "T20" not in cfg.name:
            continue
        r_vals = res["r_vals"]
        if not r_vals:
            continue

        # Find worst streak
        streaks = []
        cur_streak: List[float] = []
        for r in r_vals:
            if r <= 0:
                cur_streak.append(r)
            else:
                if cur_streak:
                    streaks.append(list(cur_streak))
                    cur_streak = []
        if cur_streak:
            streaks.append(cur_streak)

        if not streaks:
            continue
        streaks.sort(key=lambda s: -len(s))
        worst = streaks[0]
        mult = 1.0
        for r in worst:
            mult *= (1 + cfg.risk_pct * r)
        eq_drop = (1 - mult) * 100

        print(f"  {cfg.name:>25s}  WR={res['wr']:.1%}  "
              f"Worst streak: {len(worst)} losses  "
              f"R={sum(worst):+.1f}  equity -{eq_drop:.1f}%")

    # ═══════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  VERDICT")
    print("=" * 100)

    # Best overall: highest WR with DD < 50%
    viable = [r for r in results if r["max_dd"] < 0.50 and r["trades"] >= 50]
    if not viable:
        viable = [r for r in results if r["trades"] >= 30]
    viable.sort(key=lambda r: (-r["wr"], r["max_dd"]))

    if viable:
        best = viable[0]
        cfg = best["config"]
        mult = best["final_eq"] / cfg.start_equity
        print(f"\n  BEST HIGH-WR LOW-DD:")
        print(f"    Strategy:    {cfg.name}")
        print(f"    Entry:       {cfg.entry_mode} "
              f"({'+ retest' if cfg.require_retest else 'no retest'})")
        print(f"    Exit:        {cfg.exit_mode} "
              f"(TP={cfg.tp_r}R"
              f"{f', BE@{cfg.be_trigger_r}R' if cfg.be_trigger_r > 0 else ''})")
        print(f"    Risk:        {cfg.risk_pct:.0%}")
        print(f"    ──────────────────")
        print(f"    Win Rate:    {best['wr']:.1%}")
        print(f"    Avg R:       {best['avg_r']:+.4f}")
        print(f"    Max DD:      {best['max_dd']:.1%}")
        print(f"    Consec Loss: {best['max_consec_loss']}")
        print(f"    $50 -> ${best['final_eq']:,.2f} (x{mult:.1f}) in 6 months")

    # Best x10
    if x10_list:
        x10_list.sort(key=lambda r: (r["max_dd"], -r["wr"]))
        best_x10 = x10_list[0]
        cfg_x10 = best_x10["config"]
        print(f"\n  LOWEST-DD x10 PATH:")
        print(f"    Strategy:    {cfg_x10.name}")
        print(f"    WR: {best_x10['wr']:.1%}  |  DD: {best_x10['max_dd']:.1%}  |  "
              f"x10 at trade #{best_x10['x10']}")
    else:
        # Show time-to-x10 estimate
        if viable:
            best = viable[0]
            r_per_trade = best["avg_r"]
            risk = best["config"].risk_pct
            if r_per_trade > 0:
                # x10 means equity grows 10x. With compound: (1 + risk*avg_r)^n = 10
                growth_per_trade = 1 + risk * r_per_trade
                if growth_per_trade > 1:
                    trades_to_x10 = math.log(10) / math.log(growth_per_trade)
                    trades_per_month = best["trades"] / 6  # 6 months data
                    months_to_x10 = trades_to_x10 / trades_per_month if trades_per_month > 0 else 999
                    print(f"\n  x10 ESTIMATE (compound math):")
                    print(f"    Growth/trade: {growth_per_trade:.5f}")
                    print(f"    Trades to x10: ~{trades_to_x10:.0f}")
                    print(f"    At ~{trades_per_month:.0f} trades/month: "
                          f"~{months_to_x10:.1f} months to x10")

    elapsed = time.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print()


import math

if __name__ == "__main__":
    main()
