"""
live/session_reviewer.py — Post-Session Review Agent

Automatically called after each trading session closes.
Analyses session performance vs historical patterns,
generates actionable insights, and persists everything.

Integration point in bot.py:
    from live.session_reviewer import review_session
    # After session close:
    review_session(session, session_exits, equity, logger)

Outputs:
  - Structured log lines prefixed [JOURNAL]
  - Persistent reviews in live/logs/session_reviews.jsonl
  - Periodic full reports in live/logs/reports/
"""

import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from live import logger as log
from live.config import LOG_DIR
from live.journal import (
    analyze_all, failure_dynamics, generate_report,
    load_exits, save_analysis, save_report,
    tp_optimisation_analysis,
)

REVIEW_JSONL = os.path.join(LOG_DIR, "session_reviews.jsonl")

# Milestones at which to auto-generate full reports
REPORT_MILESTONES = {25, 50, 75, 100, 150, 200, 300, 500, 750, 1000}


# ═══════════════════════════════════════════════════════════
#  MAIN REVIEW FUNCTION
# ═══════════════════════════════════════════════════════════

def review_session(
    session: str,
    session_exits: List[Dict],
    equity: float = 0.0,
) -> Dict[str, Any]:
    """
    Review a completed session.

    Called automatically from bot.py after all positions are closed.

    Args:
        session: Session name (asia/london/ny)
        session_exits: EXIT events from this session (from trades.jsonl)
        equity: Current equity

    Returns:
        Review dict with grades, patterns, and recommendations.
    """
    all_trades = load_exits()
    n_total = len(all_trades)
    n_session = len(session_exits)

    review = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "equity": round(equity, 2),
        "n_session": n_session,
        "n_total": n_total,
    }

    if n_session == 0:
        review["verdict"] = "NO_TRADES"
        log.info(f"[JOURNAL] {session.upper()}: No trades this session")
        _persist(review)
        return review

    # ── Session Stats ──
    wins = [t for t in session_exits if t["pnl_r"] > 0]
    losses = [t for t in session_exits if t["pnl_r"] <= 0]
    total_r = sum(t["pnl_r"] for t in session_exits)

    review.update({
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / n_session * 100, 1),
        "total_r": round(total_r, 3),
        "avg_r": round(total_r / n_session, 3),
    })

    # ── Per-Trade Grades ──
    trade_reviews = []
    for t in session_exits:
        sym = t.get("sym", "?").replace("/USDT:USDT", "")
        grade = _grade_trade(t)
        notes = _trade_notes(t, all_trades)
        trade_reviews.append({
            "sym": sym,
            "dir": t.get("dir", "?"),
            "pnl_r": round(t["pnl_r"], 3),
            "g_tier": t.get("g_tier", -1),
            "fc_rng": round(t.get("fc_rng", 0), 4),
            "slip_r": round(t.get("slip_r", 0), 3),
            "dur_s": t.get("dur_s", 0),
            "grade": grade,
            "notes": notes,
        })
    review["trades"] = trade_reviews

    # ── Session Patterns ──
    review["patterns"] = _detect_patterns(session_exits, all_trades, session)

    # ── Recommendations ──
    review["recommendations"] = _recommendations(review, all_trades)

    # ── Verdict ──
    if total_r >= 3.0:
        verdict = "OUTSTANDING"
    elif total_r >= 1.5:
        verdict = "EXCELLENT"
    elif total_r >= 0.5:
        verdict = "GOOD"
    elif total_r >= -0.5:
        verdict = "NEUTRAL"
    elif total_r >= -2.0:
        verdict = "POOR"
    else:
        verdict = "CRITICAL"
    review["verdict"] = verdict

    # ── Log ──
    _log_review(review)

    # ── Persist ──
    _persist(review)

    # ── Auto-report at milestones ──
    if n_total in REPORT_MILESTONES:
        log.info(f"[JOURNAL] MILESTONE: {n_total} trades reached — generating full report")
        try:
            report = generate_report()
            path = save_report(report)
            save_analysis()
            log.info(f"[JOURNAL] Report saved: {path}")
        except Exception as exc:
            log.warning(f"[JOURNAL] Report generation failed: {exc}")

    return review


# ═══════════════════════════════════════════════════════════
#  SESSION EXTRACTION HELPER
# ═══════════════════════════════════════════════════════════

def get_session_exits(session: str, date_str: str = None) -> List[Dict]:
    """
    Extract EXIT events for a specific session from trades.jsonl.
    If date_str is given (YYYY-MM-DD), filter to that date.
    """
    all_exits = load_exits()
    filtered = [t for t in all_exits if t.get("ses") == session]
    if date_str:
        filtered = [t for t in filtered if t.get("ts", "").startswith(date_str)]
    return filtered


# ═══════════════════════════════════════════════════════════
#  TRADE GRADING
# ═══════════════════════════════════════════════════════════

def _grade_trade(trade: Dict) -> str:
    """
    Grade a trade A+ through F.

    A+ : Outstanding runner (>= +2.0R)
    A  : Clean winner with guardian T3+   (>= +1.5R)
    B+ : Profitable, decent progression   (>= +0.5R, T2+)
    B  : Marginally profitable            (> 0)
    C  : Small loss, acceptable           (> -0.5R)
    C- : Lost but showed progress         (g_tier >= 1)
    D  : Loss, minimal momentum           (-0.5R to -1.0R)
    F  : Full loss, no momentum           (<= -1.0R, g_tier <= 0)
    """
    r = trade["pnl_r"]
    g = trade.get("g_tier", -1)

    if r >= 2.0:
        return "A+"
    if r >= 1.5 and g >= 3:
        return "A"
    if r >= 0.5 and g >= 2:
        return "B+"
    if r > 0:
        return "B"
    if r > -0.5:
        return "C"
    if g >= 1:
        return "C-"
    if r > -1.0:
        return "D"
    return "F"


# ═══════════════════════════════════════════════════════════
#  TRADE NOTES
# ═══════════════════════════════════════════════════════════

def _trade_notes(trade: Dict, all_trades: List[Dict]) -> List[str]:
    """Generate contextual notes for a single trade."""
    notes = []
    r = trade["pnl_r"]
    g = trade.get("g_tier", -1)
    dur = trade.get("dur_s", 0)
    slip = trade.get("slip_r", 0)
    sym = trade.get("sym", "")

    # Guardian momentum analysis
    if g >= 3 and r >= 1.5:
        notes.append("Strong momentum carry — ideal trade profile")
    elif g <= -1 and r < -0.5:
        notes.append("Never gained momentum — immediate reversal after entry")
    elif g == 0 and r < 0:
        notes.append("Minimal favourable movement before reversal")

    # Speed analysis
    if dur < 300 and r < -0.5:
        notes.append(f"Flash stop in {dur:.0f}s — likely fakeout breakout")
    elif dur > 5000 and r < -1.0:
        notes.append(f"Slow bleed over {dur/60:.0f}min — held losing position too long")

    # Slippage
    if slip > 0.7:
        notes.append(f"Heavy slip ({slip:.2f}R) — poor fill vs expected entry")
    elif slip < 0.1 and r > 0:
        notes.append(f"Clean entry ({slip:.2f}R slip) — good fill quality")

    # Fee impact
    fee_r = trade.get("fee_r", 0)
    if fee_r > 0.08 and r > 0 and r < 0.5:
        notes.append(f"High fees ({fee_r:.3f}R) ate into a marginal win")

    # Repeat offender
    pair_hist = [t for t in all_trades if t.get("sym") == sym]
    pair_losses = sum(1 for t in pair_hist if t["pnl_r"] <= 0)
    if len(pair_hist) >= 2 and pair_losses >= 2:
        clean = sym.replace("/USDT:USDT", "")
        notes.append(f"REPEAT LOSER: {clean} ({pair_losses}L in {len(pair_hist)} trades)")

    return notes


# ═══════════════════════════════════════════════════════════
#  PATTERN DETECTION
# ═══════════════════════════════════════════════════════════

def _detect_patterns(
    session_exits: List[Dict],
    all_trades: List[Dict],
    session: str,
) -> List[str]:
    """Detect patterns in this session's trades."""
    patterns = []
    losers = [t for t in session_exits if t["pnl_r"] <= 0]
    winners = [t for t in session_exits if t["pnl_r"] > 0]

    # All losses same direction
    if len(losers) >= 2:
        dirs = [t.get("dir") for t in losers]
        if len(set(dirs)) == 1:
            patterns.append(
                f"All {len(losers)} losses were {dirs[0]}s — possible directional bias"
            )

    # All losers never progressed
    if len(losers) >= 2:
        tiers = [t.get("g_tier", 0) for t in losers]
        if all(g <= 0 for g in tiers):
            patterns.append(
                "All losers had g_tier <= 0 — market likely choppy/ranging"
            )

    # Flash stops
    flash = [t for t in losers if t.get("dur_s", 0) < 300]
    if len(flash) >= 2:
        patterns.append(
            f"{len(flash)} flash stops in session — fakeout breakout regime"
        )

    # WR comparison to historical
    if len(all_trades) >= 10:
        hist_wr = sum(1 for t in all_trades if t["pnl_r"] > 0) / len(all_trades)
        sess_wr = len(winners) / len(session_exits) if session_exits else 0
        if sess_wr > hist_wr + 0.20:
            patterns.append(
                f"Session WR ({sess_wr*100:.0f}%) well above average ({hist_wr*100:.0f}%)"
            )
        elif sess_wr < hist_wr - 0.20:
            patterns.append(
                f"Session WR ({sess_wr*100:.0f}%) below average ({hist_wr*100:.0f}%) — review entries"
            )

    # Best/worst trade this session
    if session_exits:
        best = max(session_exits, key=lambda t: t["pnl_r"])
        worst = min(session_exits, key=lambda t: t["pnl_r"])
        if best["pnl_r"] >= 2.0:
            bsym = best.get("sym", "").replace("/USDT:USDT", "")
            patterns.append(f"RUNNER: {bsym} at +{best['pnl_r']:.3f}R — study this setup")
        if worst["pnl_r"] <= -1.5:
            wsym = worst.get("sym", "").replace("/USDT:USDT", "")
            patterns.append(f"HEAVY LOSS: {wsym} at {worst['pnl_r']:.3f}R — review why taken")

    return patterns


# ═══════════════════════════════════════════════════════════
#  RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════

def _recommendations(review: Dict, all_trades: List[Dict]) -> List[str]:
    """Generate actionable recommendations."""
    recs = []
    n = review["n_total"]

    # Sample size
    if n < 30:
        recs.append(
            f"PATIENCE: {n}/30 trades — keep trading and collecting data. "
            f"Do NOT change config parameters yet."
        )
    elif n < 50:
        recs.append(
            f"EMERGING: {n}/50 trades — patterns forming. "
            f"Small tweaks OK, avoid major restructuring."
        )
    elif n >= 50:
        recs.append(
            f"ACTIONABLE: {n} trades — run `python -m live.journal --save` "
            f"for full optimisation report."
        )

    # Guardian tier signal
    no_progress = [t for t in all_trades if t.get("g_tier", 0) <= -1]
    if len(no_progress) >= 5:
        np_wr = sum(1 for t in no_progress if t["pnl_r"] > 0) / len(no_progress)
        np_avg = statistics.mean([t["pnl_r"] for t in no_progress])
        if np_wr < 0.15:
            recs.append(
                f"STRONG SIGNAL: T-1 trades win only {np_wr*100:.0f}% "
                f"(avg {np_avg:+.3f}R, n={len(no_progress)}). "
                f"Consider faster initial SL tightening or micro-filter enhancement."
            )

    # Session-level warning
    session = review["session"]
    ses_hist = [t for t in all_trades if t.get("ses") == session]
    if len(ses_hist) >= 5:
        ses_avg = statistics.mean([t["pnl_r"] for t in ses_hist])
        if ses_avg < -0.3:
            recs.append(
                f"WARNING: {session} averaging {ses_avg:+.3f}R/trade over "
                f"{len(ses_hist)} trades. Consider tighter entry filters."
            )

    # TP consideration (at n >= 30)
    if n >= 30:
        exits_with_r = [t for t in all_trades if t.get("exit_r", 0) > 0]
        if exits_with_r:
            above_2r = sum(1 for t in exits_with_r if t["exit_r"] >= 2.0)
            pct = above_2r / len(exits_with_r) * 100
            if pct >= 25:
                recs.append(
                    f"TP OPPORTUNITY: {pct:.0f}% of exits reach >=2.0R. "
                    f"Consider hybrid TP: keep 1.5R base + trail for T3 runners."
                )

    return recs


# ═══════════════════════════════════════════════════════════
#  OUTPUT
# ═══════════════════════════════════════════════════════════

def _log_review(review: Dict):
    """Log review summary to main bot logger."""
    ses = review["session"].upper()
    v = review.get("verdict", "?")
    n = review.get("n_session", 0)
    r = review.get("total_r", 0)
    wr = review.get("wr", 0)
    n_total = review.get("n_total", 0)

    log.info(f"[JOURNAL] ═══ {ses} SESSION REVIEW ═══")
    log.info(
        f"[JOURNAL] Verdict: {v} | {n} trades | "
        f"{r:+.3f}R | {wr:.0f}% WR | cum. n={n_total}"
    )

    for t in review.get("trades", []):
        log.info(
            f"[JOURNAL]   {t['sym']:<15} {t['dir']:<5} "
            f"{t['pnl_r']:>+7.3f}R [{t['grade']}] "
            f"g_tier={t['g_tier']} dur={t['dur_s']:.0f}s"
        )
        for note in t.get("notes", []):
            log.info(f"[JOURNAL]     → {note}")

    for pattern in review.get("patterns", []):
        log.info(f"[JOURNAL] PATTERN: {pattern}")

    for rec in review.get("recommendations", []):
        log.info(f"[JOURNAL] REC: {rec}")

    log.info(f"[JOURNAL] ═══ END REVIEW ═══")


def _persist(review: Dict):
    """Append review to session_reviews.jsonl."""
    os.makedirs(os.path.dirname(REVIEW_JSONL), exist_ok=True)
    with open(REVIEW_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(review, separators=(",", ":")) + "\n")
