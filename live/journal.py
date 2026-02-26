"""
live/journal.py — Trade Journal & Pattern Analyzer (Learning Agent Core)

Automated learning agent that:
1. Reads all trade data from trades.jsonl
2. Computes per-trade enriched metrics
3. Identifies statistically significant patterns
4. Tracks running edge per dimension
5. Generates actionable reports

Statistical significance thresholds:
    n >= 30  : marginal signal (patterns suggestive)
    n >= 50  : moderate confidence (safe to optimise)
    n >= 100 : strong confidence (per-dimension tuning)
    n >= 200 : per-pair-per-session analysis viable

Usage (standalone):
    python -m live.journal          # Print report to stdout
    python -m live.journal --save   # Save report to live/logs/reports/

Usage (from bot):
    from live.journal import analyze_all, generate_report
    analysis = analyze_all()
    print(generate_report())
"""

import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from live.config import LOG_DIR

TRADE_JSONL = os.path.join(LOG_DIR, "trades.jsonl")
JOURNAL_JSONL = os.path.join(LOG_DIR, "journal.jsonl")
REPORTS_DIR = os.path.join(LOG_DIR, "reports")


# ═══════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════

def load_events(event_type: str = None) -> List[Dict]:
    """Load events from trades.jsonl, optionally filtered by type."""
    events = []
    if not os.path.exists(TRADE_JSONL):
        return events
    with open(TRADE_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event_type is None or event.get("e") == event_type:
                    events.append(event)
            except json.JSONDecodeError:
                continue
    return events


def load_exits() -> List[Dict]:
    """Load all EXIT events."""
    return load_events("EXIT")


def load_entries() -> List[Dict]:
    """Load all ENTRY events."""
    return load_events("ENTRY")


def load_skips() -> List[Dict]:
    """Load all SKIP events for counterfactual analysis."""
    return load_events("SKIP")


def load_session_close_events() -> List[Dict]:
    """Load SESSION_CLOSE events for session-level analysis."""
    return load_events("SESSION_CLOSE")


def match_entry_to_exit(entries: List[Dict], exits: List[Dict]) -> List[Dict]:
    """
    Match ENTRY events to EXIT events by symbol + entry timestamp.
    Returns enriched EXIT dicts with entry context merged in.
    """
    # Build lookup: sym → list of entries (sorted by time)
    entry_lookup = defaultdict(list)
    for ent in entries:
        entry_lookup[ent.get("sym", "")].append(ent)

    enriched = []
    for ex in exits:
        sym = ex.get("sym", "")
        ent_ts = ex.get("ent_ts", "")
        # Find matching entry
        matched = None
        for ent in entry_lookup.get(sym, []):
            if ent.get("ts", "")[:19] == ent_ts[:19]:  # match to second
                matched = ent
                break
        merged = dict(ex)
        if matched:
            # Carry over entry-only fields
            for key in ("fc_o", "fc_c", "fc_h", "fc_l", "fc_vol",
                        "c2_o", "c2_h", "c2_l", "c2_cl", "c2_br", "c2_vol",
                        "bid", "ask", "spread", "ent_today",
                        "cw", "cl", "lw", "ll",
                        "btc_price", "btc_change_pct", "sim_breakouts"):
                if key in matched:
                    merged[f"_ent_{key}"] = matched[key]
        enriched.append(merged)
    return enriched


# ═══════════════════════════════════════════════════════════
#  CLASSIFICATION BINS
# ═══════════════════════════════════════════════════════════

def _bin_fc_range(fc_rng: float) -> str:
    """Bin first-candle range by percentile."""
    if fc_rng < 0.003:
        return "tight(<0.3%)"
    elif fc_rng < 0.006:
        return "normal(0.3-0.6%)"
    elif fc_rng < 0.010:
        return "wide(0.6-1.0%)"
    else:
        return "very_wide(>1.0%)"


def _bin_slip(slip_r: float) -> str:
    if slip_r < 0.15:
        return "clean(<0.15)"
    elif slip_r < 0.40:
        return "normal(0.15-0.40)"
    elif slip_r < 0.70:
        return "heavy(0.40-0.70)"
    else:
        return "extreme(>0.70)"


def _bin_duration(dur_s: float) -> str:
    if dur_s < 300:
        return "flash(<5m)"
    elif dur_s < 900:
        return "quick(5-15m)"
    elif dur_s < 1800:
        return "normal(15-30m)"
    elif dur_s < 3600:
        return "patient(30-60m)"
    else:
        return "marathon(>60m)"


def _bin_guardian_tier(g_tier: int) -> str:
    if g_tier <= -1:
        return "no_progress(T-1)"
    elif g_tier == 0:
        return "minimal(T0)"
    elif g_tier == 1:
        return "breakeven(T1)"
    elif g_tier == 2:
        return "lock_profit(T2)"
    else:
        return "runner(T3+)"


def _bin_fee_r(fee_r: float) -> str:
    if fee_r < 0.04:
        return "low(<0.04)"
    elif fee_r < 0.08:
        return "normal(0.04-0.08)"
    else:
        return "high(>0.08)"


# ═══════════════════════════════════════════════════════════
#  EDGE COMPUTATION
# ═══════════════════════════════════════════════════════════

def compute_edge_by_field(
    trades: List[Dict],
    field: str,
    bin_fn=None,
) -> Dict[str, Dict]:
    """
    Compute edge (expectancy) per bucket.

    If bin_fn is provided, it's called on the field value.
    If field is None and bin_fn takes the whole trade dict, use that.
    """
    buckets: Dict[str, List[float]] = defaultdict(list)

    for t in trades:
        if field:
            val = t.get(field, None)
            if val is None:
                continue
            bucket = bin_fn(val) if bin_fn else str(val)
        else:
            bucket = bin_fn(t)
        buckets[bucket].append(t["pnl_r"])

    result = {}
    for bucket, r_values in sorted(buckets.items()):
        n = len(r_values)
        wins_r = [r for r in r_values if r > 0]
        losses_r = [r for r in r_values if r <= 0]
        total_r = sum(r_values)
        gross_win = sum(wins_r) if wins_r else 0
        gross_loss = abs(sum(losses_r)) if losses_r else 0.001

        result[bucket] = {
            "n": n,
            "wins": len(wins_r),
            "losses": len(losses_r),
            "wr": round(len(wins_r) / n * 100, 1) if n else 0,
            "avg_r": round(total_r / n, 3) if n else 0,
            "total_r": round(total_r, 3),
            "avg_win": round(statistics.mean(wins_r), 3) if wins_r else 0,
            "avg_loss": round(statistics.mean(losses_r), 3) if losses_r else 0,
            "pf": round(gross_win / gross_loss, 2),
            "sig": n >= 30,
        }
    return result


# ═══════════════════════════════════════════════════════════
#  TP OPTIMISATION ANALYSIS
# ═══════════════════════════════════════════════════════════

def tp_optimisation_analysis(trades: List[Dict]) -> Dict[str, Any]:
    """
    Analyse what happens at different TP levels using peak_r data.

    For each candidate TP (1.0, 1.5, 2.0, 2.5, 3.0):
      - How many trades WOULD have hit that TP (peak_r >= TP)?
      - Estimated expectancy at that TP level
    """
    tp_levels = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
    results = {}

    for tp in tp_levels:
        # Trades where peak_r reached at least TP → would have won
        # Use exit_r as proxy: if exit_r >= tp, definitely would hit
        # If peak_r isn't reliably tracked, use exit_r for winners
        would_hit = 0
        simulated_r = []

        for t in trades:
            exit_r = t.get("exit_r", 0)
            pnl_r = t["pnl_r"]

            if exit_r >= tp:
                # This trade reached TP level → simulate win at this TP (minus fees)
                fee_r = t.get("fee_r", 0.05)
                simulated_r.append(tp - fee_r)
                would_hit += 1
            else:
                # This trade did NOT reach the candidate TP → same loss
                simulated_r.append(pnl_r)

        if not simulated_r:
            continue

        total = sum(simulated_r)
        n = len(simulated_r)
        wins = sum(1 for r in simulated_r if r > 0)

        results[str(tp)] = {
            "tp_r": tp,
            "would_hit": would_hit,
            "hit_pct": round(would_hit / n * 100, 1),
            "sim_wr": round(wins / n * 100, 1),
            "sim_avg_r": round(total / n, 3),
            "sim_total_r": round(total, 3),
            "sim_pf": round(
                sum(r for r in simulated_r if r > 0)
                / max(abs(sum(r for r in simulated_r if r < 0)), 0.001),
                2,
            ),
        }

    return results


# ═══════════════════════════════════════════════════════════
#  FAILURE DYNAMICS ANALYSIS
# ═══════════════════════════════════════════════════════════

def failure_dynamics(trades: List[Dict]) -> Dict[str, Any]:
    """
    Deep analysis of what makes trades fail.

    Examines: speed of failure (flash stops), guardian progression,
    FC range, slippage, second-attempt failures (same pair twice),
    directional clustering.
    """
    losers = [t for t in trades if t["pnl_r"] <= 0]
    if not losers:
        return {"n": 0}

    # Flash stops: lost within 5 minutes
    flash = [t for t in losers if t.get("dur_s", 0) < 300]
    # Slow bleeds: lost after 30+ minutes
    slow_bleed = [t for t in losers if t.get("dur_s", 0) > 1800]
    # Immediate reversals: g_tier never progressed
    immediate_rev = [t for t in losers if t.get("g_tier", 0) <= -1]
    # High slip losses
    high_slip_loss = [t for t in losers if t.get("slip_r", 0) > 0.5]
    # Same pair, multiple losses
    pair_counter = Counter(t.get("sym", "") for t in losers)
    repeat_losers = {p: c for p, c in pair_counter.items() if c >= 2}

    # R-distribution of losses
    loss_r = [t["pnl_r"] for t in losers]

    return {
        "n_losers": len(losers),
        "avg_loss_r": round(statistics.mean(loss_r), 3),
        "median_loss_r": round(statistics.median(loss_r), 3),
        "flash_stops": {
            "count": len(flash),
            "pct": round(len(flash) / len(losers) * 100, 1),
            "avg_r": round(statistics.mean([t["pnl_r"] for t in flash]), 3) if flash else 0,
            "pairs": [t.get("sym", "").replace("/USDT:USDT", "") for t in flash],
        },
        "slow_bleeds": {
            "count": len(slow_bleed),
            "pct": round(len(slow_bleed) / len(losers) * 100, 1),
            "avg_r": round(statistics.mean([t["pnl_r"] for t in slow_bleed]), 3) if slow_bleed else 0,
            "pairs": [t.get("sym", "").replace("/USDT:USDT", "") for t in slow_bleed],
        },
        "immediate_reversals": {
            "count": len(immediate_rev),
            "pct": round(len(immediate_rev) / len(losers) * 100, 1),
            "description": "Price never moved in our favour — pure fakeout breakouts",
        },
        "high_slip_losses": {
            "count": len(high_slip_loss),
            "pct": round(len(high_slip_loss) / len(losers) * 100, 1),
            "description": "Entered with >0.5R slippage — already behind before trade starts",
        },
        "repeat_loser_pairs": {
            k.replace("/USDT:USDT", ""): v for k, v in repeat_losers.items()
        },
    }


# ═══════════════════════════════════════════════════════════
#  FULL ANALYSIS
# ═══════════════════════════════════════════════════════════

def analyze_all(trades: List[Dict] = None) -> Dict[str, Any]:
    """Run complete pattern analysis across all dimensions."""
    if trades is None:
        trades = load_exits()

    if not trades:
        return {"error": "No trades to analyze", "n": 0}

    n = len(trades)
    wins = [t for t in trades if t["pnl_r"] > 0]
    losses = [t for t in trades if t["pnl_r"] <= 0]
    total_r = sum(t["pnl_r"] for t in trades)

    analysis = {
        "n": n,
        "generated": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1),
            "total_r": round(total_r, 3),
            "avg_r": round(total_r / n, 3),
            "avg_win_r": round(
                statistics.mean([t["pnl_r"] for t in wins]), 3
            ) if wins else 0,
            "avg_loss_r": round(
                statistics.mean([t["pnl_r"] for t in losses]), 3
            ) if losses else 0,
            "best_trade": round(max(t["pnl_r"] for t in trades), 3),
            "worst_trade": round(min(t["pnl_r"] for t in trades), 3),
            "stat_power": (
                "STRONG" if n >= 100 else
                "MODERATE" if n >= 50 else
                "MARGINAL" if n >= 30 else
                f"INSUFFICIENT ({n}/30 min)"
            ),
        },

        # ── Edge per dimension ──
        "by_session": compute_edge_by_field(
            trades, None, lambda t: t.get("ses", "?")),
        "by_direction": compute_edge_by_field(
            trades, None, lambda t: t.get("dir", "?")),
        "by_class": compute_edge_by_field(
            trades, None, lambda t: t.get("cls", "?")),
        "by_guardian_tier": compute_edge_by_field(
            trades, "g_tier", _bin_guardian_tier),
        "by_fc_range": compute_edge_by_field(
            trades, "fc_rng", _bin_fc_range),
        "by_slip": compute_edge_by_field(
            trades, "slip_r", _bin_slip),
        "by_duration": compute_edge_by_field(
            trades, "dur_s", _bin_duration),
        "by_dow": compute_edge_by_field(
            trades, "dow", lambda v: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][int(v)] if 0 <= int(v) <= 6 else f"dow_{v}"),
        "by_fee_r": compute_edge_by_field(
            trades, "fee_r", _bin_fee_r),

        # ── Per-pair breakdown ──
        "by_pair": _per_pair_breakdown(trades),

        # ── TP Optimisation ──
        "tp_analysis": tp_optimisation_analysis(trades),

        # ── Failure Dynamics ──
        "failure_dynamics": failure_dynamics(trades),
    }

    # ── WINNER DNA (>= +1.5R) ──
    big_wins = [t for t in trades if t["pnl_r"] >= 1.5]
    if big_wins:
        analysis["winner_dna"] = _compute_dna(big_wins, "+1.5R winners")

    # ── LOSER DNA (<= -1.0R) ──
    big_losses = [t for t in trades if t["pnl_r"] <= -1.0]
    if big_losses:
        analysis["loser_dna"] = _compute_dna(big_losses, "-1.0R losers")

    # ── Actionable Insights ──
    analysis["insights"] = _generate_insights(analysis, trades)

    # ── Statistical Power ──
    analysis["stat_power"] = {
        "current_n": n,
        "need_for_marginal": max(0, 30 - n),
        "need_for_moderate": max(0, 50 - n),
        "need_for_strong": max(0, 100 - n),
        "est_sessions_to_50": max(0, math.ceil((50 - n) / 3)),
        "est_days_to_50": max(0, math.ceil((50 - n) / 9)),
    }

    return analysis


def _per_pair_breakdown(trades: List[Dict]) -> Dict[str, Dict]:
    """Compute per-pair statistics."""
    pair_buckets: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        sym = t.get("sym", "?").replace("/USDT:USDT", "")
        pair_buckets[sym].append(t["pnl_r"])

    result = {}
    for pair, r_values in sorted(pair_buckets.items()):
        n = len(r_values)
        total = sum(r_values)
        w = sum(1 for r in r_values if r > 0)
        result[pair] = {
            "n": n,
            "wins": w,
            "losses": n - w,
            "total_r": round(total, 3),
            "avg_r": round(total / n, 3) if n else 0,
        }
    return result


def _compute_dna(trades: List[Dict], label: str) -> Dict[str, Any]:
    """Compute DNA profile for a subset of trades."""
    sessions = Counter(t.get("ses", "?") for t in trades)
    directions = Counter(t.get("dir", "?") for t in trades)
    classes = Counter(t.get("cls", "?") for t in trades)
    pairs = [t.get("sym", "?").replace("/USDT:USDT", "") for t in trades]

    return {
        "label": label,
        "count": len(trades),
        "avg_r": round(statistics.mean([t["pnl_r"] for t in trades]), 3),
        "avg_fc_rng": round(statistics.mean(
            [t.get("fc_rng", 0) for t in trades]), 4),
        "avg_slip_r": round(statistics.mean(
            [t.get("slip_r", 0) for t in trades]), 3),
        "avg_dur_s": round(statistics.mean(
            [t.get("dur_s", 0) for t in trades]), 0),
        "avg_g_tier": round(statistics.mean(
            [t.get("g_tier", 0) for t in trades]), 1),
        "avg_fee_r": round(statistics.mean(
            [t.get("fee_r", 0) for t in trades]), 3),
        "sessions": dict(sessions),
        "directions": dict(directions),
        "classes": dict(classes),
        "pairs": pairs,
    }


def _generate_insights(analysis: Dict, trades: List[Dict]) -> List[str]:
    """Generate human-readable insights from analysis."""
    insights = []
    n = analysis["n"]

    # 1. Guardian tier is the strongest signal
    gt = analysis.get("by_guardian_tier", {})
    runner = gt.get("runner(T3+)", {})
    no_prog = gt.get("no_progress(T-1)", {})
    if runner.get("n", 0) > 0 and no_prog.get("n", 0) > 0:
        insights.append(
            f"GUARDIAN TIER is the STRONGEST predictor: "
            f"T3+ = {runner['wr']}% WR (+{runner['avg_r']}R avg, n={runner['n']}) "
            f"vs T-1 = {no_prog['wr']}% WR ({no_prog['avg_r']}R avg, n={no_prog['n']})"
        )

    # 2. Session edge
    by_ses = analysis.get("by_session", {})
    if len(by_ses) >= 2:
        best = max(by_ses.items(), key=lambda x: x[1]["avg_r"])
        worst = min(by_ses.items(), key=lambda x: x[1]["avg_r"])
        if best[0] != worst[0]:
            insights.append(
                f"Session edge: {best[0]} averages {best[1]['avg_r']:+.3f}R "
                f"({best[1]['wr']}% WR, n={best[1]['n']}) — "
                f"{worst[0]} averages {worst[1]['avg_r']:+.3f}R (n={worst[1]['n']})"
            )

    # 3. Direction bias
    by_dir = analysis.get("by_direction", {})
    if "short" in by_dir and "long" in by_dir:
        s, l = by_dir["short"], by_dir["long"]
        better = "short" if s["avg_r"] > l["avg_r"] else "long"
        worse = "long" if better == "short" else "short"
        insights.append(
            f"Direction: {better}s +{by_dir[better]['avg_r']}R avg "
            f"({by_dir[better]['wr']}% WR, n={by_dir[better]['n']}) vs "
            f"{worse}s {by_dir[worse]['avg_r']}R avg (n={by_dir[worse]['n']})"
        )

    # 4. TP optimisation hint
    tp = analysis.get("tp_analysis", {})
    current_tp = tp.get("1.5", {})
    higher_tp = tp.get("2.0", {})
    if current_tp and higher_tp:
        if higher_tp.get("sim_avg_r", 0) > current_tp.get("sim_avg_r", 0):
            insights.append(
                f"TP INCREASE OPPORTUNITY: 2.0R TP would yield "
                f"+{higher_tp['sim_avg_r']}R avg vs current +{current_tp['sim_avg_r']}R"
            )
        else:
            insights.append(
                f"TP at 1.5R is OPTIMAL for now: 2.0R TP drops to "
                f"{higher_tp['sim_avg_r']}R avg ({higher_tp['hit_pct']}% hit rate)"
            )

    # 5. Failure patterns
    fd = analysis.get("failure_dynamics", {})
    flash = fd.get("flash_stops", {})
    if flash.get("count", 0) >= 2:
        insights.append(
            f"FLASH STOPS: {flash['count']} trades ({flash['pct']}% of losses) "
            f"stopped within 5 minutes — pure fakeout breakouts"
        )
    imm = fd.get("immediate_reversals", {})
    if imm.get("count", 0) > 0:
        insights.append(
            f"IMMEDIATE REVERSALS: {imm['count']} trades ({imm['pct']}% of losses) "
            f"never gained momentum (g_tier=-1)"
        )

    # 6. Toxic pairs
    by_pair = analysis.get("by_pair", {})
    toxic = [(p, d) for p, d in by_pair.items()
             if d["n"] >= 2 and d["total_r"] < -1.5]
    if toxic:
        names = ", ".join(f"{p} ({d['total_r']:+.1f}R/{d['n']})" for p, d in toxic)
        insights.append(f"TOXIC PAIRS: {names}")

    # 7. Sample size warning
    if n < 30:
        insights.append(
            f"SAMPLE SIZE: Only {n} trades. Patterns are SUGGESTIVE not proven. "
            f"Need {30 - n} more for marginal significance, "
            f"{50 - n} more for moderate confidence."
        )

    return insights


# ═══════════════════════════════════════════════════════════
#  REPORT GENERATION
# ═══════════════════════════════════════════════════════════

def generate_report(trades: List[Dict] = None) -> str:
    """Generate a complete human-readable journal report."""
    analysis = analyze_all(trades)

    if "error" in analysis:
        return f"No trades to analyze: {analysis['error']}"

    lines = []
    s = analysis["summary"]

    lines.append("=" * 72)
    lines.append(f"  TRADE JOURNAL REPORT — {analysis['generated'][:10]}")
    lines.append(f"  {s['total_trades']} trades | Power: {s['stat_power']}")
    lines.append("=" * 72)

    # ── Summary ──
    lines.append(f"\n{'─' * 50}")
    lines.append("  PERFORMANCE SUMMARY")
    lines.append(f"{'─' * 50}")
    lines.append(f"  Total R:      {s['total_r']:+.3f}")
    lines.append(f"  Win Rate:     {s['win_rate']}% ({s['wins']}W / {s['losses']}L)")
    lines.append(f"  Avg R/trade:  {s['avg_r']:+.3f}")
    lines.append(f"  Avg Win:      {s['avg_win_r']:+.3f}R")
    lines.append(f"  Avg Loss:     {s['avg_loss_r']:+.3f}R")
    lines.append(f"  Best/Worst:   {s['best_trade']:+.3f} / {s['worst_trade']:+.3f}")

    # ── Winner DNA ──
    if "winner_dna" in analysis:
        w = analysis["winner_dna"]
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  WINNER DNA ({w['label']}: {w['count']} trades)")
        lines.append(f"{'─' * 50}")
        lines.append(f"  Avg R:        {w['avg_r']:+.3f}")
        lines.append(f"  Avg FC rng:   {w['avg_fc_rng']:.4f} ({w['avg_fc_rng']*100:.2f}%)")
        lines.append(f"  Avg Slip:     {w['avg_slip_r']:.3f}")
        lines.append(f"  Avg Duration: {w['avg_dur_s']:.0f}s ({w['avg_dur_s']/60:.0f}min)")
        lines.append(f"  Avg G-Tier:   {w['avg_g_tier']:.1f}")
        lines.append(f"  Avg Fee R:    {w['avg_fee_r']:.3f}")
        lines.append(f"  Sessions:     {w['sessions']}")
        lines.append(f"  Directions:   {w['directions']}")
        lines.append(f"  Classes:      {w['classes']}")
        lines.append(f"  Pairs:        {', '.join(w['pairs'])}")

    # ── Loser DNA ──
    if "loser_dna" in analysis:
        l = analysis["loser_dna"]
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  LOSER DNA ({l['label']}: {l['count']} trades)")
        lines.append(f"{'─' * 50}")
        lines.append(f"  Avg R:        {l['avg_r']:+.3f}")
        lines.append(f"  Avg FC rng:   {l['avg_fc_rng']:.4f} ({l['avg_fc_rng']*100:.2f}%)")
        lines.append(f"  Avg Slip:     {l['avg_slip_r']:.3f}")
        lines.append(f"  Avg Duration: {l['avg_dur_s']:.0f}s ({l['avg_dur_s']/60:.0f}min)")
        lines.append(f"  Avg G-Tier:   {l['avg_g_tier']:.1f}")
        lines.append(f"  Avg Fee R:    {l['avg_fee_r']:.3f}")
        lines.append(f"  Sessions:     {l['sessions']}")
        lines.append(f"  Directions:   {l['directions']}")
        lines.append(f"  Pairs:        {', '.join(l['pairs'])}")

    # ── Edge by dimension ──
    for label, key in [
        ("GUARDIAN TIER", "by_guardian_tier"),
        ("SESSION", "by_session"),
        ("DIRECTION", "by_direction"),
        ("FC RANGE", "by_fc_range"),
        ("SLIPPAGE", "by_slip"),
        ("DURATION", "by_duration"),
        ("DAY OF WEEK", "by_dow"),
        ("PAIR CLASS", "by_class"),
        ("FEE IMPACT", "by_fee_r"),
    ]:
        dim = analysis.get(key, {})
        if not dim:
            continue
        lines.append(f"\n{'─' * 50}")
        lines.append(f"  EDGE BY {label}")
        lines.append(f"{'─' * 50}")
        hdr = f"  {'Bucket':<22} {'N':>4} {'WR':>6} {'AvgR':>7} {'TotR':>7} {'PF':>6}"
        lines.append(hdr)
        for bucket, st in dim.items():
            sig = " *" if st["sig"] else ""
            lines.append(
                f"  {bucket:<22} {st['n']:>4} {st['wr']:>5.1f}% "
                f"{st['avg_r']:>+7.3f} {st['total_r']:>+7.3f} {st['pf']:>6.2f}{sig}"
            )

    # ── TP Optimisation ──
    tp = analysis.get("tp_analysis", {})
    if tp:
        lines.append(f"\n{'─' * 50}")
        lines.append("  TP OPTIMISATION SIMULATION")
        lines.append(f"{'─' * 50}")
        lines.append(f"  {'TP_R':>5} {'Hit%':>6} {'WR':>6} {'AvgR':>7} {'TotR':>8} {'PF':>6}")
        for tp_r, st in sorted(tp.items(), key=lambda x: float(x[0])):
            marker = " ◄" if st["tp_r"] == 1.5 else ""
            lines.append(
                f"  {st['tp_r']:>5.2f} {st['hit_pct']:>5.1f}% "
                f"{st['sim_wr']:>5.1f}% {st['sim_avg_r']:>+7.3f} "
                f"{st['sim_total_r']:>+8.3f} {st['sim_pf']:>6.2f}{marker}"
            )

    # ── Failure Dynamics ──
    fd = analysis.get("failure_dynamics", {})
    if fd.get("n_losers", 0) > 0:
        lines.append(f"\n{'─' * 50}")
        lines.append("  FAILURE DYNAMICS")
        lines.append(f"{'─' * 50}")
        lines.append(f"  Total losers:           {fd['n_losers']}")
        lines.append(f"  Avg loss R:             {fd['avg_loss_r']:+.3f}")
        lines.append(f"  Median loss R:          {fd['median_loss_r']:+.3f}")
        fl = fd.get("flash_stops", {})
        lines.append(f"  Flash stops (<5m):      {fl.get('count', 0)} "
                     f"({fl.get('pct', 0):.0f}%) avg {fl.get('avg_r', 0):+.3f}R")
        if fl.get("pairs"):
            lines.append(f"    Pairs: {', '.join(fl['pairs'])}")
        sb = fd.get("slow_bleeds", {})
        lines.append(f"  Slow bleeds (>30m):     {sb.get('count', 0)} "
                     f"({sb.get('pct', 0):.0f}%) avg {sb.get('avg_r', 0):+.3f}R")
        if sb.get("pairs"):
            lines.append(f"    Pairs: {', '.join(sb['pairs'])}")
        ir = fd.get("immediate_reversals", {})
        lines.append(f"  Immediate reversals:    {ir.get('count', 0)} ({ir.get('pct', 0):.0f}%)")
        hs = fd.get("high_slip_losses", {})
        lines.append(f"  High-slip losses:       {hs.get('count', 0)} ({hs.get('pct', 0):.0f}%)")
        rp = fd.get("repeat_loser_pairs", {})
        if rp:
            lines.append(f"  Repeat loser pairs:     {rp}")

    # ── Per-pair ──
    by_pair = analysis.get("by_pair", {})
    if by_pair:
        lines.append(f"\n{'─' * 50}")
        lines.append("  PER-PAIR BREAKDOWN")
        lines.append(f"{'─' * 50}")
        lines.append(f"  {'Pair':<16} {'N':>3} {'W':>3} {'L':>3} {'TotR':>7} {'AvgR':>7}")
        for pair, st in sorted(by_pair.items(), key=lambda x: x[1]["total_r"], reverse=True):
            lines.append(
                f"  {pair:<16} {st['n']:>3} {st['wins']:>3} {st['losses']:>3} "
                f"{st['total_r']:>+7.3f} {st['avg_r']:>+7.3f}"
            )

    # ── Insights ──
    lines.append(f"\n{'─' * 50}")
    lines.append("  KEY INSIGHTS")
    lines.append(f"{'─' * 50}")
    for i, insight in enumerate(analysis.get("insights", []), 1):
        lines.append(f"  {i}. {insight}")

    # ── Stat Power ──
    sp = analysis.get("stat_power", {})
    lines.append(f"\n{'─' * 50}")
    lines.append("  STATISTICAL POWER")
    lines.append(f"{'─' * 50}")
    lines.append(f"  Current trades:         {sp['current_n']}")
    lines.append(f"  Need (marginal n=30):   {sp['need_for_marginal']} more")
    lines.append(f"  Need (moderate n=50):   {sp['need_for_moderate']} more")
    lines.append(f"  Need (strong n=100):    {sp['need_for_strong']} more")
    lines.append(f"  Est. days to n=50:      ~{sp['est_days_to_50']}")
    lines.append(f"\n{'=' * 72}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  PERSISTENCE
# ═══════════════════════════════════════════════════════════

def save_analysis(analysis: Dict = None) -> str:
    """Save analysis to journal.jsonl. Returns file path."""
    if analysis is None:
        analysis = analyze_all()
    os.makedirs(os.path.dirname(JOURNAL_JSONL), exist_ok=True)
    with open(JOURNAL_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(analysis, separators=(",", ":")) + "\n")
    return JOURNAL_JSONL


def save_report(report: str = None) -> str:
    """Save report to timestamped file. Returns file path."""
    if report is None:
        report = generate_report()
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORTS_DIR, f"journal_{ts}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path


# ═══════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    report = generate_report()
    print(report)
    if "--save" in sys.argv:
        path = save_report(report)
        print(f"\nSaved to: {path}")
        analysis = analyze_all()
        save_analysis(analysis)
        print(f"Analysis appended to: {JOURNAL_JSONL}")
