"""
research/prove_dynamic_hybrid.py — Backtest proof for Dynamic Hybrid Engine

PURPOSE:
  Prove the new Dynamic Hybrid configuration (10x leverage, 8% risk, dynamic
  engine modulation) produces a viable x10 path before going live.

TESTS:
  1. OLD CONFIG:     12% risk, no dynamic engine (static)
  2. NEW CONFIG:     8% risk, no dynamic engine (static baseline)
  3. DYNAMIC HYBRID: 8% base risk + dynamic engine simulation
  4. DYNAMIC + HEAT: 8% base + heat management (cooldown after 3 losses)
  5. Drawdown stress test across all configs
  6. Monthly/session breakdown

Uses mega_sweep.py's backtest engine for trade generation.
Adds dynamic engine simulation on top of the R-multiples.

ZERO external deps — stdlib only.
"""

from __future__ import annotations

import sys
import os
import time
import math
import statistics
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime

# Add parent to path for imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.mega_sweep import (
    SweepConfig, Trade, Candle, PairResult,
    discover_data_files, load_csv, assign_sessions,
    run_fcb, compute_metrics,
)


# ═══════════════════════════════════════════════════
#  CONFIGURATION VARIANTS TO TEST
# ═══════════════════════════════════════════════════

# CANDIDATE_ALPHA = our proven best params from 28,544 backtests
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


# ═══════════════════════════════════════════════════
#  DYNAMIC ENGINE SIMULATOR (backtest version)
# ═══════════════════════════════════════════════════

@dataclass
class DynState:
    """Simulated dynamic engine state for backtesting."""
    consec_losses: int = 0
    consec_wins: int = 0
    session_r: float = 0.0
    session_trades: int = 0
    session_wins: int = 0
    session_losses: int = 0
    in_cooldown: bool = False
    cooldown_remaining: int = 0
    
    # Per-session breakout quality
    breakouts_taken: int = 0
    breakouts_won: int = 0
    
    # Bankroll
    phase: int = 1
    
    # Current session key for reset detection
    current_session: str = ""
    
    def reset_session(self, session_key: str):
        """Reset session-level counters."""
        self.session_r = 0.0
        self.session_trades = 0
        self.session_wins = 0
        self.session_losses = 0
        self.breakouts_taken = 0
        self.breakouts_won = 0
        self.current_session = session_key
        # Don't reset consec_losses/wins — they carry across sessions
    
    def record(self, r_val: float, won: bool):
        """Record a trade outcome."""
        self.session_trades += 1
        self.session_r += r_val
        self.breakouts_taken += 1
        
        if won:
            self.session_wins += 1
            self.breakouts_won += 1
            self.consec_wins += 1
            self.consec_losses = 0
        else:
            self.session_losses += 1
            self.consec_losses += 1
            self.consec_wins = 0
        
        # Heat management
        if self.in_cooldown:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining <= 0:
                self.in_cooldown = False
                self.cooldown_remaining = 0
        elif self.consec_losses >= 3:
            self.in_cooldown = True
            self.cooldown_remaining = 2


def dynamic_risk_multiplier(state: DynState, equity: float, start_eq: float,
                            direction: str = "long") -> Tuple[float, bool, str]:
    """
    Compute the dynamic risk multiplier for a trade.
    Returns (multiplier, should_take, reason).
    Simulates the full DynamicEngine.evaluate_entry() logic.
    """
    mult = 1.0
    reasons = []
    
    # 1. Bankroll phase
    if start_eq > 0:
        growth = (equity - start_eq) / start_eq
        if growth < 0.15:
            mult *= 0.85
            state.phase = 1
            reasons.append("SURV")
        elif growth < 0.50:
            mult *= 1.0
            state.phase = 2
            reasons.append("GROW")
        elif equity < start_eq * 10:
            mult *= 1.10
            state.phase = 3
            reasons.append("COMP")
        else:
            mult *= 0.80
            state.phase = 4
            reasons.append("CRUISE")
    else:
        mult *= 0.85
        state.phase = 1
    
    # 2. Heat management
    if state.in_cooldown:
        mult *= 0.50
        reasons.append("COOL")
    elif state.consec_losses >= 2:
        mult *= 0.85
        reasons.append(f"LSTRK{state.consec_losses}")
    
    # 3. Momentum boost
    if state.consec_wins >= 3 and state.session_r > 0:
        mult *= 1.15
        reasons.append("MBOOST")
    elif state.consec_wins >= 2 and state.session_wins > 0 and state.session_trades > 0:
        wr = state.session_wins / state.session_trades
        if wr >= 0.6:
            mult *= 1.05
            reasons.append("MPLUS")
    
    # 4. Market-wide breakout quality
    if state.breakouts_taken >= 3:
        fail_rate = 1.0 - (state.breakouts_won / state.breakouts_taken)
        if fail_rate >= 0.75:
            mult *= 0.65
            reasons.append("HOSTILE")
        elif fail_rate >= 0.60:
            mult *= 0.80
            reasons.append("WEAK_MKT")
        elif fail_rate <= 0.30:
            mult *= 1.10
            reasons.append("STRONG_MKT")
    
    # 5. Session halt check
    if state.session_trades >= 2 and state.session_r <= -3.0:
        return 0.0, False, "SESSION_HALT"
    
    # Clamp
    mult = max(0.35, min(1.30, mult))
    
    reason = "+".join(reasons) if reasons else "NORMAL"
    return mult, True, reason


# ═══════════════════════════════════════════════════
#  EQUITY SIMULATION VARIANTS
# ═══════════════════════════════════════════════════

def simulate_static(trades: List[Trade], start_eq: float, risk_pct: float,
                    label: str = "") -> Dict:
    """Static risk — same % every trade."""
    closed = sorted(
        [t for t in trades if t.is_closed and t.r_multiple is not None],
        key=lambda t: t.entry_time
    )
    
    equity = start_eq
    peak = start_eq
    max_dd_pct = 0.0
    max_dd_eq = 0.0
    equity_curve = [start_eq]
    dd_curve = [0.0]
    
    wins = losses = 0
    total_r = 0.0
    worst_streak = 0
    curr_lose_streak = 0
    
    for t in closed:
        r = t.r_multiple
        pnl_pct = risk_pct * r
        equity *= (1 + pnl_pct)
        equity = max(equity, 0.01)
        total_r += r
        
        if r > 0:
            wins += 1
            curr_lose_streak = 0
        else:
            losses += 1
            curr_lose_streak += 1
            worst_streak = max(worst_streak, curr_lose_streak)
        
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd)
        max_dd_eq = max(max_dd_eq, peak - equity)
        
        equity_curve.append(equity)
        dd_curve.append(dd)
    
    total = wins + losses
    wr = wins / total if total > 0 else 0
    avg_r = total_r / total if total > 0 else 0
    
    # Time to x10
    if avg_r > 0:
        g = 1 + risk_pct * avg_r
        if g > 1:
            trades_x10 = math.log(10) / math.log(g)
        else:
            trades_x10 = float('inf')
    else:
        trades_x10 = float('inf')
    
    return {
        "label": label,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "total_r": total_r,
        "avg_r": avg_r,
        "risk_pct": risk_pct,
        "start_eq": start_eq,
        "final_eq": equity,
        "peak_eq": peak,
        "max_dd_pct": max_dd_pct,
        "max_dd_usd": max_dd_eq,
        "worst_streak": worst_streak,
        "trades_x10": trades_x10,
        "growth_x": equity / start_eq if start_eq > 0 else 0,
        "equity_curve": equity_curve,
    }


def simulate_dynamic(trades: List[Trade], start_eq: float, base_risk: float,
                     label: str = "") -> Dict:
    """
    Dynamic risk — modulated by simulated DynamicEngine.
    Adapts risk based on consecutive wins/losses, session performance,
    bankroll phase, and market-wide breakout quality.
    """
    closed = sorted(
        [t for t in trades if t.is_closed and t.r_multiple is not None],
        key=lambda t: t.entry_time
    )
    
    equity = start_eq
    peak = start_eq
    max_dd_pct = 0.0
    max_dd_eq = 0.0
    equity_curve = [start_eq]
    
    state = DynState()
    wins = losses = 0
    total_r = 0.0
    skipped = 0
    worst_streak = 0
    curr_lose_streak = 0
    
    mults_used = []
    risk_pcts_used = []
    
    for t in closed:
        r = t.r_multiple
        session_key = f"{t.session_name}_{t.session_date}"
        
        # Reset session if new
        if session_key != state.current_session:
            state.reset_session(session_key)
        
        # Get dynamic multiplier
        dyn_mult, should_take, reason = dynamic_risk_multiplier(
            state, equity, start_eq, t.direction
        )
        
        if not should_take:
            skipped += 1
            # Still record outcome for state tracking (market-wide quality)
            state.record(r, r > 0)
            continue
        
        # Apply dynamic risk
        effective_risk = base_risk * dyn_mult
        effective_risk = max(0.01, min(0.15, effective_risk))  # hard floor/cap
        
        mults_used.append(dyn_mult)
        risk_pcts_used.append(effective_risk)
        
        pnl_pct = effective_risk * r
        equity *= (1 + pnl_pct)
        equity = max(equity, 0.01)
        total_r += r
        
        won = r > 0
        state.record(r, won)
        
        if won:
            wins += 1
            curr_lose_streak = 0
        else:
            losses += 1
            curr_lose_streak += 1
            worst_streak = max(worst_streak, curr_lose_streak)
        
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd)
        max_dd_eq = max(max_dd_eq, peak - equity)
        
        equity_curve.append(equity)
    
    total = wins + losses
    wr = wins / total if total > 0 else 0
    avg_r = total_r / total if total > 0 else 0
    avg_mult = statistics.mean(mults_used) if mults_used else 1.0
    avg_risk = statistics.mean(risk_pcts_used) if risk_pcts_used else base_risk
    
    # Time to x10
    if avg_r > 0:
        g = 1 + avg_risk * avg_r
        if g > 1:
            trades_x10 = math.log(10) / math.log(g)
        else:
            trades_x10 = float('inf')
    else:
        trades_x10 = float('inf')
    
    return {
        "label": label,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "total_r": total_r,
        "avg_r": avg_r,
        "risk_pct": base_risk,
        "avg_risk_actual": avg_risk,
        "avg_mult": avg_mult,
        "start_eq": start_eq,
        "final_eq": equity,
        "peak_eq": peak,
        "max_dd_pct": max_dd_pct,
        "max_dd_usd": max_dd_eq,
        "worst_streak": worst_streak,
        "skipped": skipped,
        "trades_x10": trades_x10,
        "growth_x": equity / start_eq if start_eq > 0 else 0,
        "equity_curve": equity_curve,
    }


# ═══════════════════════════════════════════════════
#  DRAWDOWN ANALYSIS
# ═══════════════════════════════════════════════════

def analyze_drawdowns(equity_curve: List[float]) -> List[Dict]:
    """Find all drawdowns sorted by severity."""
    drawdowns = []
    peak = equity_curve[0]
    dd_start = 0
    
    in_dd = False
    for i, eq in enumerate(equity_curve):
        if eq >= peak:
            if in_dd:
                # Drawdown ended
                dd_depth = (peak - min(equity_curve[dd_start:i+1])) / peak
                dd_len = i - dd_start
                drawdowns.append({
                    "start_idx": dd_start,
                    "end_idx": i,
                    "depth_pct": dd_depth,
                    "depth_usd": peak - min(equity_curve[dd_start:i+1]),
                    "length_trades": dd_len,
                    "peak_eq": peak,
                    "trough_eq": min(equity_curve[dd_start:i+1]),
                })
                in_dd = False
            peak = eq
            dd_start = i
        else:
            if not in_dd:
                in_dd = True
                dd_start = i - 1 if i > 0 else 0
    
    # Still in drawdown at end
    if in_dd:
        dd_depth = (peak - min(equity_curve[dd_start:])) / peak
        drawdowns.append({
            "start_idx": dd_start,
            "end_idx": len(equity_curve) - 1,
            "depth_pct": dd_depth,
            "depth_usd": peak - min(equity_curve[dd_start:]),
            "length_trades": len(equity_curve) - 1 - dd_start,
            "peak_eq": peak,
            "trough_eq": min(equity_curve[dd_start:]),
        })
    
    return sorted(drawdowns, key=lambda d: d["depth_pct"], reverse=True)


# ═══════════════════════════════════════════════════
#  SESSION BREAKDOWN
# ═══════════════════════════════════════════════════

def session_breakdown(trades: List[Trade]) -> Dict[str, Dict]:
    """Break down performance by session."""
    sessions: Dict[str, Dict] = {}
    
    for t in trades:
        if not t.is_closed or t.r_multiple is None:
            continue
        s = t.session_name
        if s not in sessions:
            sessions[s] = {"wins": 0, "losses": 0, "total_r": 0.0, "trades": 0}
        
        sessions[s]["trades"] += 1
        sessions[s]["total_r"] += t.r_multiple
        if t.r_multiple > 0:
            sessions[s]["wins"] += 1
        else:
            sessions[s]["losses"] += 1
    
    return sessions


# ═══════════════════════════════════════════════════
#  MAIN PROOF
# ═══════════════════════════════════════════════════

def print_result(r: Dict, indent: str = "  "):
    """Print a simulation result block."""
    wr_str = f"{r['wr']:.1%}"
    eq = r['final_eq']
    x = r['growth_x']
    dd = r['max_dd_pct']
    t2x10 = r['trades_x10']
    
    print(f"{indent}Trades: {r['trades']:,}  ({r['wins']}W / {r['losses']}L)  WR: {wr_str}")
    print(f"{indent}Total R: {r['total_r']:+.1f}  Avg R: {r['avg_r']:+.4f}")
    print(f"{indent}Risk/trade: {r['risk_pct']:.1%}", end="")
    if 'avg_risk_actual' in r:
        print(f" (avg actual: {r['avg_risk_actual']:.2%}, avg mult: {r['avg_mult']:.2f})", end="")
    print()
    print(f"{indent}Final equity: ${eq:,.2f}  ({x:.1f}x growth)")
    print(f"{indent}Max drawdown: {dd:.1%} (${r['max_dd_usd']:,.2f})")
    print(f"{indent}Worst losing streak: {r['worst_streak']}")
    if 'skipped' in r:
        print(f"{indent}Trades skipped (session halt): {r['skipped']}")
    if t2x10 < 99999:
        print(f"{indent}Projected trades to x10: {t2x10:.0f}")
        days = t2x10 / 8
        print(f"{indent}Projected days to x10 (8 trades/day): {days:.0f}")
    else:
        print(f"{indent}x10 projection: not viable (negative avg R)")


def main():
    t_start = time.time()
    start_equity = 150.0
    
    print("=" * 78)
    print("  DYNAMIC HYBRID ENGINE — BACKTEST PROOF")
    print("  Proving the x10 journey before going live")
    print("=" * 78)
    
    # ── Load data ──
    pair_files = discover_data_files()
    print(f"\n  Loading {len(pair_files)} pairs...")
    
    pair_data: Dict[str, List[Candle]] = {}
    total_candles = 0
    for pair, fpath in pair_files:
        candles = load_csv(fpath)
        assign_sessions(candles)
        pair_data[pair] = candles
        total_candles += len(candles)
        sys.stdout.write(f"\r    {len(pair_data)}/{len(pair_files)} pairs ({total_candles:,} candles)")
        sys.stdout.flush()
    
    load_time = time.time() - t_start
    print(f"\n    Loaded in {load_time:.1f}s\n")
    
    # ── Generate trades with CANDIDATE_ALPHA config ──
    print("  Running CANDIDATE_ALPHA backtest across all pairs...")
    all_trades: List[Trade] = []
    t0 = time.time()
    
    for i, (pair, candles) in enumerate(pair_data.items()):
        trades = run_fcb(pair, candles, CANDIDATE_ALPHA)
        all_trades.extend(trades)
        if (i + 1) % 20 == 0 or i + 1 == len(pair_data):
            sys.stdout.write(f"\r    {i+1}/{len(pair_data)} pairs, {len(all_trades):,} trades")
            sys.stdout.flush()
    
    bt_time = time.time() - t0
    closed = [t for t in all_trades if t.is_closed and t.r_multiple is not None]
    print(f"\n    {len(closed):,} closed trades in {bt_time:.1f}s\n")
    
    # ── Basic trade stats ──
    r_values = [t.r_multiple for t in closed]
    winners = [r for r in r_values if r > 0]
    losers = [r for r in r_values if r <= 0]
    avg_win = statistics.mean(winners) if winners else 0
    avg_loss = statistics.mean(losers) if losers else 0
    
    print("=" * 78)
    print("  TRADE QUALITY (CANDIDATE_ALPHA parameters)")
    print("=" * 78)
    print(f"  Total trades: {len(closed):,}")
    print(f"  Win rate:     {len(winners)/len(closed):.1%}")
    print(f"  Avg winner:   {avg_win:+.3f}R")
    print(f"  Avg loser:    {avg_loss:+.3f}R")
    print(f"  Payoff ratio: {avg_win/abs(avg_loss):.2f}x" if avg_loss < 0 else "")
    print(f"  Total R:      {sum(r_values):+.1f}")
    print(f"  Avg R/trade:  {statistics.mean(r_values):+.4f}")
    
    # Exit reasons
    exits: Dict[str, int] = {}
    for t in closed:
        exits[t.exit_reason or "unknown"] = exits.get(t.exit_reason or "unknown", 0) + 1
    print(f"  Exit reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(exits.items())))
    
    # ═══════════════════════════════════════════════════
    #  TEST 1: OLD CONFIG — 12% static risk
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 1: OLD CONFIG — 12% static risk (previous aggressive mode)")
    print(f"{'=' * 78}")
    
    old_result = simulate_static(all_trades, start_equity, 0.12, "OLD_12pct")
    print_result(old_result)
    
    # ═══════════════════════════════════════════════════
    #  TEST 2: NEW BASELINE — 8% static risk
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 2: NEW BASELINE — 8% static risk (conservative base)")
    print(f"{'=' * 78}")
    
    new_static = simulate_static(all_trades, start_equity, 0.08, "NEW_8pct")
    print_result(new_static)
    
    # ═══════════════════════════════════════════════════
    #  TEST 3: DYNAMIC HYBRID — 8% base + engine
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 3: DYNAMIC HYBRID — 8% base + dynamic engine")
    print(f"{'=' * 78}")
    
    dynamic_result = simulate_dynamic(all_trades, start_equity, 0.08, "DYNAMIC_8pct")
    print_result(dynamic_result)
    
    # ═══════════════════════════════════════════════════
    #  TEST 4: COMPARE 4% and 6% for safety reference
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  TEST 4: ADDITIONAL RISK LEVELS (static)")
    print(f"{'=' * 78}")
    
    for risk_label, risk_val in [("4%", 0.04), ("6%", 0.06), ("10%", 0.10)]:
        r = simulate_static(all_trades, start_equity, risk_val, f"STATIC_{risk_label}")
        print(f"\n  --- {risk_label} risk ---")
        print_result(r, "    ")
    
    # ═══════════════════════════════════════════════════
    #  COMPARISON TABLE
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  COMPARISON TABLE")
    print(f"{'=' * 78}")
    
    all_results = [
        simulate_static(all_trades, start_equity, 0.04, "Static 4%"),
        simulate_static(all_trades, start_equity, 0.06, "Static 6%"),
        new_static,
        simulate_static(all_trades, start_equity, 0.10, "Static 10%"),
        old_result,
        dynamic_result,
    ]
    
    hdr = f"  {'Config':<22s} {'Trades':>6s} {'WR':>6s} {'Final $':>12s} {'Growth':>8s} {'MaxDD':>8s} {'WorstL':>6s} {'x10 in':>8s}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    
    for r in all_results:
        t2x10 = f"{r['trades_x10']:.0f}t" if r['trades_x10'] < 99999 else "n/a"
        print(f"  {r['label']:<22s} {r['trades']:>6,d} {r['wr']:>5.1%} "
              f"${r['final_eq']:>11,.2f} {r['growth_x']:>7.1f}x {r['max_dd_pct']:>7.1%} "
              f"{r['worst_streak']:>6d} {t2x10:>8s}")
    
    # ═══════════════════════════════════════════════════
    #  DRAWDOWN STRESS TEST
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  DRAWDOWN STRESS TEST")
    print(f"{'=' * 78}")
    
    for r in [old_result, new_static, dynamic_result]:
        dds = analyze_drawdowns(r["equity_curve"])
        top3 = dds[:3] if len(dds) >= 3 else dds
        print(f"\n  {r['label']}:")
        for i, dd in enumerate(top3, 1):
            print(f"    #{i}: -{dd['depth_pct']:.1%} (${dd['peak_eq']:,.2f} -> ${dd['trough_eq']:,.2f}) "
                  f"over {dd['length_trades']} trades")
    
    # ═══════════════════════════════════════════════════
    #  SESSION BREAKDOWN
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  SESSION BREAKDOWN")
    print(f"{'=' * 78}")
    
    sb = session_breakdown(all_trades)
    for sess in ["asia", "london", "ny"]:
        if sess in sb:
            s = sb[sess]
            total = s["trades"]
            wr = s["wins"] / total if total > 0 else 0
            avg = s["total_r"] / total if total > 0 else 0
            print(f"  {sess:>7s}: {total:>5d} trades  WR={wr:>5.1%}  "
                  f"Total R={s['total_r']:>+8.1f}  Avg R={avg:>+.4f}")
    
    # ═══════════════════════════════════════════════════
    #  MONTHLY EQUITY SNAPSHOTS (dynamic 8%)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  EQUITY MILESTONES (Dynamic Hybrid 8%)")
    print(f"{'=' * 78}")
    
    curve = dynamic_result["equity_curve"]
    total_t = len(curve) - 1
    milestones = [100, 250, 500, 1000, 2000, 3000, 5000, total_t]
    
    print(f"  {'Trade #':>8s}  {'Equity':>12s}  {'Growth':>8s}  {'DD from peak':>14s}")
    print(f"  {'--------':>8s}  {'------':>12s}  {'------':>8s}  {'------------':>14s}")
    
    peak_so_far = start_equity
    for m in milestones:
        if m >= len(curve):
            m = len(curve) - 1
        eq = curve[m]
        if eq > peak_so_far:
            peak_so_far = eq
        dd = (peak_so_far - eq) / peak_so_far if peak_so_far > 0 else 0
        gx = eq / start_equity
        print(f"  {m:>8,d}  ${eq:>11,.2f}  {gx:>7.1f}x  {dd:>13.1%}")
    
    # ═══════════════════════════════════════════════════
    #  RUIN PROBABILITY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  RUIN ANALYSIS")
    print(f"{'=' * 78}")
    
    wr_actual = len(winners) / len(closed) if closed else 0.5
    loss_prob = 1 - wr_actual
    
    print(f"  Win rate: {wr_actual:.1%}")
    print(f"  Loss probability per trade: {loss_prob:.1%}")
    
    for streak in [5, 7, 10, 12, 15]:
        prob = loss_prob ** streak
        dd_at_8pct = 1 - (1 - 0.08 * abs(avg_loss)) ** streak
        dd_at_12pct = 1 - (1 - 0.12 * abs(avg_loss)) ** streak
        print(f"  P({streak} consecutive losses): {prob:.4%}  "
              f"DrawDown: {dd_at_8pct:.1%} @8%  |  {dd_at_12pct:.1%} @12%")
    
    # ═══════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print("  VERDICT")
    print(f"{'=' * 78}")
    
    # Criteria for "worth going live"
    criteria = []
    
    # 1. Positive expectancy
    if dynamic_result["avg_r"] > 0:
        criteria.append(("PASS", f"Positive expectancy: {dynamic_result['avg_r']:+.4f}R/trade"))
    else:
        criteria.append(("FAIL", f"Negative expectancy: {dynamic_result['avg_r']:+.4f}R/trade"))
    
    # 2. Win rate >= 40%
    if dynamic_result["wr"] >= 0.40:
        criteria.append(("PASS", f"Win rate {dynamic_result['wr']:.1%} >= 40%"))
    else:
        criteria.append(("FAIL", f"Win rate {dynamic_result['wr']:.1%} < 40%"))
    
    # 3. Max drawdown < 50%
    if dynamic_result["max_dd_pct"] < 0.50:
        criteria.append(("PASS", f"Max DD {dynamic_result['max_dd_pct']:.1%} < 50%"))
    else:
        criteria.append(("FAIL", f"Max DD {dynamic_result['max_dd_pct']:.1%} >= 50%"))
    
    # 4. x10 reachable in < 300 trades
    if dynamic_result["trades_x10"] < 300:
        criteria.append(("PASS", f"x10 in {dynamic_result['trades_x10']:.0f} trades (< 300)"))
    elif dynamic_result["trades_x10"] < 500:
        criteria.append(("WARN", f"x10 in {dynamic_result['trades_x10']:.0f} trades (slow but viable)"))
    else:
        criteria.append(("FAIL", f"x10 in {dynamic_result['trades_x10']:.0f} trades (too slow)"))
    
    # 5. Dynamic beats static 8%
    if dynamic_result["final_eq"] >= new_static["final_eq"]:
        ratio = dynamic_result["final_eq"] / new_static["final_eq"] if new_static["final_eq"] > 0 else 0
        criteria.append(("PASS", f"Dynamic ${dynamic_result['final_eq']:,.2f} >= Static ${new_static['final_eq']:,.2f} ({ratio:.2f}x)"))
    else:
        criteria.append(("WARN", f"Dynamic ${dynamic_result['final_eq']:,.2f} < Static ${new_static['final_eq']:,.2f}"))
    
    # 6. DD improvement over 12%
    if dynamic_result["max_dd_pct"] < old_result["max_dd_pct"]:
        improvement = old_result["max_dd_pct"] - dynamic_result["max_dd_pct"]
        criteria.append(("PASS", f"DD improved: {old_result['max_dd_pct']:.1%} -> {dynamic_result['max_dd_pct']:.1%} ({improvement:.1%} less)"))
    else:
        criteria.append(("WARN", f"DD not improved vs old: {dynamic_result['max_dd_pct']:.1%} vs {old_result['max_dd_pct']:.1%}"))
    
    passes = sum(1 for s, _ in criteria if s == "PASS")
    warns = sum(1 for s, _ in criteria if s == "WARN")
    fails = sum(1 for s, _ in criteria if s == "FAIL")
    
    for status, msg in criteria:
        icon = "[PASS]" if status == "PASS" else ("[WARN]" if status == "WARN" else "[FAIL]")
        print(f"  {icon} {msg}")
    
    print(f"\n  Score: {passes} PASS / {warns} WARN / {fails} FAIL")
    
    if fails == 0 and passes >= 4:
        print(f"\n  >>> VERDICT: READY FOR LIVE <<<")
        print(f"  The Dynamic Hybrid Engine at 8% base risk with 10x leverage")
        print(f"  produces a viable x10 path with controlled drawdowns.")
    elif fails <= 1 and passes >= 3:
        print(f"\n  >>> VERDICT: CONDITIONALLY READY <<<")
        print(f"  Edge exists but monitor closely in first 20 live trades.")
    else:
        print(f"\n  >>> VERDICT: NOT READY <<<")
        print(f"  Backtest does not support going live. Review parameters.")
    
    total_time = time.time() - t_start
    print(f"\n  Total time: {total_time:.1f}s")
    print()


if __name__ == "__main__":
    main()
