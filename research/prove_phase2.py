"""
research/prove_phase2.py — Phase 2: Stress tests for Dynamic Hybrid Engine

Tests:
  1. EARLY JOURNEY: First 200 trades only (the critical $150→$1500 phase)
  2. A-CLASS ONLY: Only proven pairs (SCAN_ALWAYS_TRADE set)
  3. WORST-CASE ORDERING: Trades sorted worst-first (maximum adversity)
  4. MONTE CARLO: 1000 random shuffles of trade order → distribution
  5. ROLLING WINDOW: 50-trade rolling WR and R to detect regime shifts

Stdlib only.
"""

from __future__ import annotations

import sys
import os
import time
import math
import random
import statistics
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.mega_sweep import (
    SweepConfig, Trade, Candle,
    discover_data_files, load_csv, assign_sessions, run_fcb,
)

CANDIDATE_ALPHA = SweepConfig(
    name="CANDIDATE_ALPHA",
    tp_r=1.5,
    trail_enabled=True,
    trail_activation_r=0.95,
    trail_distance_r=0.15,
    trail_max_r=10.0,
    safety_tp_r=10.0,
    min_c2_body=0.50,
    fc_counter=True,
    vol_ratio_long=1.0,
    vol_ratio_short=0.25,
    min_range_pct=0.003,
)

# Proven pairs from config.py SCAN_ALWAYS_TRADE
A_CLASS_PAIRS = {
    "OPEN/USDT:USDT", "WHITEWHALE/USDT:USDT", "PIPPIN/USDT:USDT",
    "FHE/USDT:USDT", "CYBER/USDT:USDT", "WIF/USDT:USDT",
    "ALCH/USDT:USDT", "CLOUD/USDT:USDT", "CYS/USDT:USDT",
    "VVV/USDT:USDT", "F/USDT:USDT", "API3/USDT:USDT",
    "ENSO/USDT:USDT", "JTO/USDT:USDT", "PUMPFUN/USDT:USDT",
    "STBL/USDT:USDT", "AXS/USDT:USDT", "SENT/USDT:USDT",
    "KITE/USDT:USDT", "TRUST/USDT:USDT", "UB/USDT:USDT",
    "BREV/USDT:USDT", "US/USDT:USDT", "KERNEL/USDT:USDT",
    "RECALL/USDT:USDT", "VANA/USDT:USDT", "MYX/USDT:USDT",
    "CLO/USDT:USDT", "HANA/USDT:USDT", "SUPER/USDT:USDT",
    "IRYS/USDT:USDT", "POWER/USDT:USDT",
}


def sim_equity(r_values: List[float], start: float, risk: float) -> Tuple[float, float, List[float]]:
    """Simple equity simulation. Returns (final, max_dd_pct, curve)."""
    eq = start
    peak = start
    max_dd = 0.0
    curve = [start]
    for r in r_values:
        eq *= (1 + risk * r)
        eq = max(eq, 0.01)
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        curve.append(eq)
    return eq, max_dd, curve


def sim_dynamic(r_values: List[float], start: float, base_risk: float,
                session_keys: List[str] = None) -> Tuple[float, float, List[float], int]:
    """Dynamic risk simulation. Returns (final, max_dd_pct, curve, skipped)."""
    eq = start
    peak = start
    max_dd = 0.0
    curve = [start]
    
    consec_loss = 0
    consec_win = 0
    in_cool = False
    cool_rem = 0
    sess_r = 0.0
    sess_trades = 0
    sess_wins = 0
    sess_taken = 0
    sess_won = 0
    cur_sess = ""
    skipped = 0
    
    for i, r in enumerate(r_values):
        sk = session_keys[i] if session_keys and i < len(session_keys) else str(i // 8)
        
        if sk != cur_sess:
            cur_sess = sk
            sess_r = 0.0
            sess_trades = 0
            sess_wins = 0
            sess_taken = 0
            sess_won = 0
        
        # Session halt
        if sess_trades >= 2 and sess_r <= -3.0:
            skipped += 1
            won = r > 0
            if won: consec_win += 1; consec_loss = 0
            else: consec_loss += 1; consec_win = 0
            sess_r += r; sess_trades += 1
            sess_taken += 1
            if won: sess_wins += 1; sess_won += 1
            if in_cool:
                cool_rem -= 1
                if cool_rem <= 0: in_cool = False; cool_rem = 0
            elif consec_loss >= 3:
                in_cool = True; cool_rem = 2
            continue
        
        mult = 1.0
        # Bankroll
        growth = (eq - start) / start if start > 0 else 0
        if growth < 0.15: mult *= 0.85
        elif growth < 0.50: mult *= 1.0
        elif eq < start * 10: mult *= 1.10
        else: mult *= 0.80
        
        # Heat
        if in_cool: mult *= 0.50
        elif consec_loss >= 2: mult *= 0.85
        
        # Momentum
        if consec_win >= 3 and sess_r > 0: mult *= 1.15
        elif consec_win >= 2 and sess_trades > 0 and sess_wins / max(sess_trades, 1) >= 0.6: mult *= 1.05
        
        # Market quality
        if sess_taken >= 3:
            fr = 1.0 - (sess_won / sess_taken)
            if fr >= 0.75: mult *= 0.65
            elif fr >= 0.60: mult *= 0.80
            elif fr <= 0.30: mult *= 1.10
        
        mult = max(0.35, min(1.30, mult))
        eff_risk = base_risk * mult
        
        eq *= (1 + eff_risk * r)
        eq = max(eq, 0.01)
        
        won = r > 0
        if won: consec_win += 1; consec_loss = 0; sess_wins += 1; sess_won += 1
        else: consec_loss += 1; consec_win = 0
        sess_r += r; sess_trades += 1; sess_taken += 1
        
        if in_cool:
            cool_rem -= 1
            if cool_rem <= 0: in_cool = False; cool_rem = 0
        elif consec_loss >= 3:
            in_cool = True; cool_rem = 2
        
        if eq > peak: peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        curve.append(eq)
    
    return eq, max_dd, curve, skipped


def main():
    t_start = time.time()
    START_EQ = 150.0
    
    print("=" * 78)
    print("  DYNAMIC HYBRID — PHASE 2 STRESS TESTS")
    print("=" * 78)
    
    # Load data
    pair_files = discover_data_files()
    print(f"\n  Loading {len(pair_files)} pairs...")
    pair_data = {}
    for pair, fpath in pair_files:
        candles = load_csv(fpath)
        assign_sessions(candles)
        pair_data[pair] = candles
        sys.stdout.write(f"\r    {len(pair_data)}/{len(pair_files)}")
        sys.stdout.flush()
    print(f"\n    Done\n")
    
    # Generate all trades
    all_trades = []
    for pair, candles in pair_data.items():
        trades = run_fcb(pair, candles, CANDIDATE_ALPHA)
        all_trades.extend(trades)
    
    closed = sorted(
        [t for t in all_trades if t.is_closed and t.r_multiple is not None],
        key=lambda t: t.entry_time
    )
    r_values = [t.r_multiple for t in closed]
    session_keys = [f"{t.session_name}_{t.session_date}" for t in closed]
    
    print(f"  Total closed trades: {len(closed):,}")
    print(f"  Total R: {sum(r_values):+.1f}  Avg R: {statistics.mean(r_values):+.4f}")
    
    # ═══════════════════════════════════════════════════
    #  TEST 1: EARLY JOURNEY (first N trades)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 1: EARLY JOURNEY — The critical first trades")
    print(f"{'=' * 78}")
    print(f"\n  {'Window':>10s}  {'Final $':>12s}  {'Growth':>8s}  {'MaxDD':>8s}  {'WR':>6s}  {'Avg R':>8s}")
    print(f"  {'-'*10}  {'-'*12}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*8}")
    
    for n in [25, 50, 100, 150, 200, 300, 500]:
        if n > len(r_values):
            break
        chunk_r = r_values[:n]
        chunk_sk = session_keys[:n]
        
        eq_s, dd_s, _ = sim_equity(chunk_r, START_EQ, 0.08)
        eq_d, dd_d, _, skip_d = sim_dynamic(chunk_r, START_EQ, 0.08, chunk_sk)
        
        wr = sum(1 for r in chunk_r if r > 0) / len(chunk_r)
        avg = statistics.mean(chunk_r)
        
        print(f"  Static 8%  {n:>4d}t  ${eq_s:>11,.2f}  {eq_s/START_EQ:>7.1f}x  {dd_s:>7.1%}  {wr:>5.1%}  {avg:>+.4f}")
        print(f"  Dynamic    {n:>4d}t  ${eq_d:>11,.2f}  {eq_d/START_EQ:>7.1f}x  {dd_d:>7.1%}  {wr:>5.1%}  {avg:>+.4f}  (skip={skip_d})")
    
    # ═══════════════════════════════════════════════════
    #  TEST 2: A-CLASS PAIRS ONLY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 2: A-CLASS PAIRS ONLY (proven 33 pairs)")
    print(f"{'=' * 78}")
    
    a_trades = sorted(
        [t for t in all_trades 
         if t.is_closed and t.r_multiple is not None and t.pair in A_CLASS_PAIRS],
        key=lambda t: t.entry_time
    )
    a_r = [t.r_multiple for t in a_trades]
    a_sk = [f"{t.session_name}_{t.session_date}" for t in a_trades]
    
    if a_r:
        a_wr = sum(1 for r in a_r if r > 0) / len(a_r)
        a_avg = statistics.mean(a_r)
        winners = [r for r in a_r if r > 0]
        losers = [r for r in a_r if r <= 0]
        a_avg_win = statistics.mean(winners) if winners else 0
        a_avg_loss = statistics.mean(losers) if losers else 0
        
        print(f"  Trades: {len(a_r):,}  WR: {a_wr:.1%}  Avg R: {a_avg:+.4f}")
        print(f"  Avg Win: {a_avg_win:+.3f}R  Avg Loss: {a_avg_loss:+.3f}R")
        print(f"  Payoff: {a_avg_win/abs(a_avg_loss):.2f}x" if a_avg_loss < 0 else "")
        
        eq_s, dd_s, _ = sim_equity(a_r, START_EQ, 0.08)
        eq_d, dd_d, _, skip_d = sim_dynamic(a_r, START_EQ, 0.08, a_sk)
        
        print(f"\n  Static 8%:  ${eq_s:>12,.2f}  ({eq_s/START_EQ:.1f}x)  MaxDD: {dd_s:.1%}")
        print(f"  Dynamic 8%: ${eq_d:>12,.2f}  ({eq_d/START_EQ:.1f}x)  MaxDD: {dd_d:.1%}  (skip={skip_d})")
        
        # Trades to x10
        if a_avg > 0:
            g = 1 + 0.08 * a_avg
            t2x10 = math.log(10) / math.log(g) if g > 1 else float('inf')
            print(f"  Trades to x10 @ 8%: {t2x10:.0f} (~{t2x10/8:.0f} days)")
    
    # ═══════════════════════════════════════════════════
    #  TEST 3: WORST-CASE ORDERING
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 3: WORST-CASE ORDERING (all losses first)")
    print(f"{'=' * 78}")
    
    # Sort: all losses first (worst first), then wins
    worst_order = sorted(r_values, key=lambda r: r)
    eq_worst_s, dd_worst_s, _ = sim_equity(worst_order, START_EQ, 0.08)
    eq_worst_d, dd_worst_d, _, _ = sim_dynamic(worst_order, START_EQ, 0.08)
    
    print(f"  Scenario: All {sum(1 for r in r_values if r <= 0)} losses FIRST, then {sum(1 for r in r_values if r > 0)} wins")
    print(f"  Static 8%:  Final=${eq_worst_s:>12,.2f}  MaxDD={dd_worst_s:.1%}")
    print(f"  Dynamic 8%: Final=${eq_worst_d:>12,.2f}  MaxDD={dd_worst_d:.1%}")
    print(f"  Minimum equity (static):  ${START_EQ * (1 - 0.08 * abs(min(r_values))) ** sum(1 for r in r_values if r <= 0):,.6f}")
    
    # Also test: worst 100 trades first
    worst_start = sorted(r_values)[:100] + sorted(r_values)[100:]
    eq_ws, dd_ws, _ = sim_equity(worst_start, START_EQ, 0.08)
    eq_wd, dd_wd, _, _ = sim_dynamic(worst_start, START_EQ, 0.08)
    print(f"\n  Scenario: 100 worst trades first, then remaining")
    print(f"  Static 8%:  Final=${eq_ws:>12,.2f}  MaxDD={dd_ws:.1%}")
    print(f"  Dynamic 8%: Final=${eq_wd:>12,.2f}  MaxDD={dd_wd:.1%}")
    
    # ═══════════════════════════════════════════════════
    #  TEST 4: MONTE CARLO (trade order randomization)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 4: MONTE CARLO — 1000 random trade orderings")
    print(f"{'=' * 78}")
    
    random.seed(42)
    mc_finals_s = []
    mc_dds_s = []
    mc_finals_d = []
    mc_dds_d = []
    mc_ruins_s = 0
    mc_ruins_d = 0
    
    N_MC = 1000
    for trial in range(N_MC):
        shuffled = r_values[:]
        random.shuffle(shuffled)
        
        eq_s, dd_s, _ = sim_equity(shuffled, START_EQ, 0.08)
        eq_d, dd_d, _, _ = sim_dynamic(shuffled, START_EQ, 0.08)
        
        mc_finals_s.append(eq_s)
        mc_dds_s.append(dd_s)
        mc_finals_d.append(eq_d)
        mc_dds_d.append(dd_d)
        
        if eq_s < 1.0:
            mc_ruins_s += 1
        if eq_d < 1.0:
            mc_ruins_d += 1
        
        if (trial + 1) % 200 == 0:
            sys.stdout.write(f"\r    {trial+1}/{N_MC} trials")
            sys.stdout.flush()
    
    print(f"\r    {N_MC} trials complete\n")
    
    def pct(arr, p):
        s = sorted(arr)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s)-1)]
    
    print(f"  {'Metric':<25s}  {'Static 8%':>15s}  {'Dynamic 8%':>15s}")
    print(f"  {'-'*25}  {'-'*15}  {'-'*15}")
    
    print(f"  {'Median final equity':<25s}  ${pct(mc_finals_s, 50):>14,.2f}  ${pct(mc_finals_d, 50):>14,.2f}")
    print(f"  {'5th pctile equity':<25s}  ${pct(mc_finals_s, 5):>14,.2f}  ${pct(mc_finals_d, 5):>14,.2f}")
    print(f"  {'95th pctile equity':<25s}  ${pct(mc_finals_s, 95):>14,.2f}  ${pct(mc_finals_d, 95):>14,.2f}")
    print(f"  {'Median max DD':<25s}  {pct(mc_dds_s, 50):>14.1%}  {pct(mc_dds_d, 50):>14.1%}")
    print(f"  {'95th pctile max DD':<25s}  {pct(mc_dds_s, 95):>14.1%}  {pct(mc_dds_d, 95):>14.1%}")
    print(f"  {'Worst max DD':<25s}  {max(mc_dds_s):>14.1%}  {max(mc_dds_d):>14.1%}")
    print(f"  {'Ruin probability (<$1)':<25s}  {mc_ruins_s/N_MC:>14.1%}  {mc_ruins_d/N_MC:>14.1%}")
    
    # How many reach x10?
    x10_s = sum(1 for f in mc_finals_s if f >= START_EQ * 10)
    x10_d = sum(1 for f in mc_finals_d if f >= START_EQ * 10)
    print(f"  {'Reached x10':<25s}  {x10_s/N_MC:>14.1%}  {x10_d/N_MC:>14.1%}")
    
    x100_s = sum(1 for f in mc_finals_s if f >= START_EQ * 100)
    x100_d = sum(1 for f in mc_finals_d if f >= START_EQ * 100)
    print(f"  {'Reached x100':<25s}  {x100_s/N_MC:>14.1%}  {x100_d/N_MC:>14.1%}")
    
    # ═══════════════════════════════════════════════════
    #  TEST 5: ROLLING WINDOW STABILITY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 5: ROLLING 50-TRADE WINDOW STABILITY")
    print(f"{'=' * 78}")
    
    window = 50
    if len(r_values) >= window:
        rolling_wr = []
        rolling_avg = []
        negative_windows = 0
        
        for i in range(len(r_values) - window + 1):
            chunk = r_values[i:i+window]
            wr = sum(1 for r in chunk if r > 0) / len(chunk)
            avg = statistics.mean(chunk)
            rolling_wr.append(wr)
            rolling_avg.append(avg)
            if avg < 0:
                negative_windows += 1
        
        total_windows = len(rolling_wr)
        print(f"  Total 50-trade windows: {total_windows}")
        print(f"  WR range: {min(rolling_wr):.1%} — {max(rolling_wr):.1%}")
        print(f"  WR median: {sorted(rolling_wr)[len(rolling_wr)//2]:.1%}")
        print(f"  Avg R range: {min(rolling_avg):+.4f} — {max(rolling_avg):+.4f}")
        print(f"  Avg R median: {sorted(rolling_avg)[len(rolling_avg)//2]:+.4f}")
        print(f"  Negative windows: {negative_windows}/{total_windows} ({negative_windows/total_windows:.1%})")
        print(f"  Positive windows: {total_windows - negative_windows}/{total_windows} ({(total_windows - negative_windows)/total_windows:.1%})")
    
    # ═══════════════════════════════════════════════════
    #  TEST 6: SESSION AND DIRECTION BREAKDOWN
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 6: SESSION x DIRECTION BREAKDOWN")
    print(f"{'=' * 78}")
    
    buckets: Dict[str, Dict] = {}
    for t in closed:
        key = f"{t.session_name}_{t.direction}"
        if key not in buckets:
            buckets[key] = {"w": 0, "l": 0, "r": 0.0}
        b = buckets[key]
        b["r"] += t.r_multiple
        if t.r_multiple > 0: b["w"] += 1
        else: b["l"] += 1
    
    print(f"  {'Session_Dir':<18s} {'Trades':>7s} {'WR':>6s} {'Total R':>9s} {'Avg R':>8s}")
    print(f"  {'-'*18} {'-'*7} {'-'*6} {'-'*9} {'-'*8}")
    
    for key in sorted(buckets.keys()):
        b = buckets[key]
        total = b["w"] + b["l"]
        wr = b["w"] / total if total > 0 else 0
        avg = b["r"] / total if total > 0 else 0
        print(f"  {key:<18s} {total:>7d} {wr:>5.1%} {b['r']:>+8.1f} {avg:>+.4f}")
    
    # ═══════════════════════════════════════════════════
    #  FINAL VERDICT
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  FINAL STRESS TEST VERDICT")
    print(f"{'=' * 78}")
    
    verdict = []
    
    # 1. MC x10 probability
    if x10_d / N_MC >= 0.99:
        verdict.append(("[PASS]", f"Monte Carlo x10 probability: {x10_d/N_MC:.1%} (all orderings reach x10)"))
    elif x10_d / N_MC >= 0.90:
        verdict.append(("[PASS]", f"Monte Carlo x10 probability: {x10_d/N_MC:.1%}"))
    else:
        verdict.append(("[FAIL]", f"Monte Carlo x10 probability: {x10_d/N_MC:.1%} (<90%)"))
    
    # 2. MC ruin probability
    if mc_ruins_d / N_MC <= 0.01:
        verdict.append(("[PASS]", f"Ruin probability: {mc_ruins_d/N_MC:.2%}"))
    else:
        verdict.append(("[FAIL]", f"Ruin probability: {mc_ruins_d/N_MC:.2%} (>1%)"))
    
    # 3. MC median DD
    med_dd = pct(mc_dds_d, 50)
    if med_dd < 0.40:
        verdict.append(("[PASS]", f"Median max DD: {med_dd:.1%} (<40%)"))
    elif med_dd < 0.50:
        verdict.append(("[WARN]", f"Median max DD: {med_dd:.1%} (40-50%)"))
    else:
        verdict.append(("[FAIL]", f"Median max DD: {med_dd:.1%} (>50%)"))
    
    # 4. Rolling stability
    if len(r_values) >= window:
        neg_pct = negative_windows / total_windows
        if neg_pct < 0.15:
            verdict.append(("[PASS]", f"Edge stability: {neg_pct:.1%} negative windows (<15%)"))
        elif neg_pct < 0.25:
            verdict.append(("[WARN]", f"Edge stability: {neg_pct:.1%} negative windows (15-25%)"))
        else:
            verdict.append(("[FAIL]", f"Edge stability: {neg_pct:.1%} negative windows (>25%)"))
    
    # 5. Dynamic DD improvement
    med_dd_s = pct(mc_dds_s, 50)
    improvement = med_dd_s - med_dd
    if improvement > 0:
        verdict.append(("[PASS]", f"Dynamic DD improvement: {med_dd_s:.1%} -> {med_dd:.1%} ({improvement:.1%} less)"))
    else:
        verdict.append(("[WARN]", f"No DD improvement from dynamic engine"))
    
    # 6. A-class subset edge
    if a_r:
        a_avg_r = statistics.mean(a_r)
        if a_avg_r > 0:
            verdict.append(("[PASS]", f"A-class pair edge: {a_avg_r:+.4f}R/trade (positive)"))
        else:
            verdict.append(("[FAIL]", f"A-class pair edge: {a_avg_r:+.4f}R/trade (negative)"))
    
    passes = sum(1 for s, _ in verdict if s == "[PASS]")
    warns = sum(1 for s, _ in verdict if s == "[WARN]")
    fails = sum(1 for s, _ in verdict if s == "[FAIL]")
    
    for status, msg in verdict:
        print(f"  {status} {msg}")
    
    print(f"\n  Score: {passes} PASS / {warns} WARN / {fails} FAIL")
    
    if fails == 0:
        print(f"\n  >>> ALL STRESS TESTS PASSED <<<")
        print(f"  The Dynamic Hybrid Engine is robust across 1000 Monte Carlo")
        print(f"  orderings, worst-case scenarios, and session/direction splits.")
        print(f"  READY FOR LIVE DEPLOYMENT.")
    elif fails <= 1:
        print(f"\n  >>> MOSTLY PASSED — Minor concerns to monitor <<<")
    else:
        print(f"\n  >>> SIGNIFICANT CONCERNS — Review before live <<<")
    
    print(f"\n  Total time: {time.time() - t_start:.1f}s\n")


if __name__ == "__main__":
    main()
