"""
live/growth_tracker.py — x10 Growth Tracking Module

Tracks daily equity progress toward $2,000 target.
Provides real-time x10 probability estimates and pace alerts.

Called by the bot at session boundaries and after each trade.
"""

import json
import os
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

log = logging.getLogger("fcb.growth")

# ─── Target ───
START_EQUITY = 200.0
TARGET_EQUITY = 2000.0
TARGET_DAYS = 10
TRACKER_FILE = "live/growth_state.json"


def _load_state() -> dict:
    """Load growth tracking state."""
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "start_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "start_equity": START_EQUITY,
        "target_equity": TARGET_EQUITY,
        "target_days": TARGET_DAYS,
        "daily_snapshots": [],  # [{date, equity, trades, wins, losses, r_total}]
        "session_log": [],      # [{timestamp, session, equity, trades, r_total}]
    }


def _save_state(state: dict):
    """Persist growth state."""
    with open(TRACKER_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def record_session_end(equity: float, session: str, trades: int,
                       wins: int, losses: int, r_total: float):
    """Called at end of each session to snapshot progress."""
    state = _load_state()
    now = datetime.now(timezone.utc)

    state["session_log"].append({
        "timestamp": now.isoformat(),
        "session": session,
        "equity": round(equity, 2),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "r_total": round(r_total, 3),
    })

    # Update daily snapshot
    today = now.strftime("%Y-%m-%d")
    daily = state["daily_snapshots"]
    existing = next((d for d in daily if d["date"] == today), None)
    if existing:
        existing["equity"] = round(equity, 2)
        existing["trades"] += trades
        existing["wins"] += wins
        existing["losses"] += losses
        existing["r_total"] = round(existing["r_total"] + r_total, 3)
    else:
        daily.append({
            "date": today,
            "equity": round(equity, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "r_total": round(r_total, 3),
        })

    _save_state(state)
    return _compute_dashboard(state, equity)


def get_dashboard(equity: float) -> str:
    """Get current growth dashboard string for logging."""
    state = _load_state()
    return _compute_dashboard(state, equity)


def _compute_dashboard(state: dict, current_equity: float) -> str:
    """Compute the growth dashboard report."""
    start_eq = state.get("start_equity", START_EQUITY)
    target_eq = state.get("target_equity", TARGET_EQUITY)
    target_days = state.get("target_days", TARGET_DAYS)
    start_date_str = state.get("start_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        start_date = datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    days_elapsed = max((now - start_date).total_seconds() / 86400, 0.01)
    days_remaining = max(target_days - days_elapsed, 0)

    # Current progress
    current_multiple = current_equity / start_eq if start_eq > 0 else 0
    target_multiple = target_eq / start_eq
    progress_pct = min((current_equity - start_eq) / (target_eq - start_eq) * 100, 100) if target_eq > start_eq else 0

    # Required daily growth to hit target from HERE
    if days_remaining > 0 and current_equity > 0:
        required_daily = (target_eq / current_equity) ** (1 / days_remaining) - 1
    else:
        required_daily = float("inf") if current_equity < target_eq else 0

    # Actual daily growth rate
    if days_elapsed > 0.5:  # at least half a day
        actual_daily = (current_equity / start_eq) ** (1 / days_elapsed) - 1
    else:
        actual_daily = 0

    # Projected day to hit target at current pace
    if actual_daily > 0:
        days_to_target = math.log(target_eq / current_equity) / math.log(1 + actual_daily)
        eta_date = now + timedelta(days=days_to_target)
        eta_str = eta_date.strftime("%b %d")
    else:
        days_to_target = float("inf")
        eta_str = "never (growth needed!)"

    # Pace indicator
    if actual_daily >= required_daily * 1.2:
        pace = "AHEAD"
        pace_icon = "[+++]"
    elif actual_daily >= required_daily * 0.8:
        pace = "ON TRACK"
        pace_icon = "[===]"
    elif actual_daily >= required_daily * 0.5:
        pace = "BEHIND"
        pace_icon = "[---]"
    else:
        pace = "CRITICAL"
        pace_icon = "[!!!]"

    # Session stats
    daily = state.get("daily_snapshots", [])
    total_trades = sum(d.get("trades", 0) for d in daily)
    total_wins = sum(d.get("wins", 0) for d in daily)
    total_r = sum(d.get("r_total", 0) for d in daily)
    trades_per_day = total_trades / max(days_elapsed, 0.5)

    # Build report
    lines = [
        "=" * 60,
        f"  x10 GROWTH TRACKER  {pace_icon} {pace}",
        "=" * 60,
        f"  Equity:   ${current_equity:,.2f}  (x{current_multiple:.2f} of start)",
        f"  Target:   ${target_eq:,.0f}  ({progress_pct:.1f}% of the way)",
        f"  Day:      {days_elapsed:.1f} of {target_days}  ({days_remaining:.1f} remaining)",
        "-" * 60,
        f"  Required: {required_daily:.1%}/day from here",
        f"  Actual:   {actual_daily:.1%}/day so far",
        f"  ETA:      {eta_str}",
        "-" * 60,
        f"  Trades:   {total_trades} total  ({trades_per_day:.1f}/day)",
        f"  Wins:     {total_wins}  ({total_wins/max(total_trades,1)*100:.0f}% WR)",
        f"  Total R:  {total_r:+.2f}R",
        "=" * 60,
    ]

    # Daily breakdown if we have data
    if daily:
        lines.append("  DAILY BREAKDOWN:")
        for d in daily[-5:]:  # show last 5 days
            dt = d["trades"]
            dw = d["wins"]
            deq = d["equity"]
            dr = d.get("r_total", 0)
            lines.append(f"    {d['date']}  ${deq:>8.2f}  {dt}t {dw}W  {dr:+.2f}R")
        lines.append("=" * 60)

    return "\n".join(lines)


def log_dashboard(equity: float):
    """Log the growth dashboard."""
    dashboard = get_dashboard(equity)
    for line in dashboard.split("\n"):
        log.info(line)


def check_pace_alert(equity: float) -> Optional[str]:
    """Check if we need a pace alert. Returns alert message or None."""
    state = _load_state()
    start_eq = state.get("start_equity", START_EQUITY)
    target_eq = state.get("target_equity", TARGET_EQUITY)
    target_days = state.get("target_days", TARGET_DAYS)
    start_date_str = state.get("start_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    days_elapsed = max((now - start_date).total_seconds() / 86400, 0.01)
    days_remaining = max(target_days - days_elapsed, 0)

    if days_remaining <= 0:
        if equity >= target_eq:
            return "x10 TARGET HIT! Current: ${:.2f}".format(equity)
        else:
            return "x10 DEADLINE PASSED. Final: ${:.2f} ({:.1f}% of target)".format(
                equity, equity / target_eq * 100)

    # Required growth from current position
    if current_equity := equity:
        required_daily = (target_eq / current_equity) ** (1 / days_remaining) - 1
        if required_daily > 0.50:
            return ("PACE CRITICAL: Need {:.0%}/day — consider increasing risk or trades. "
                    "Current equity ${:.2f}, need ${:.0f} in {:.1f} days.").format(
                required_daily, equity, target_eq, days_remaining)
        elif required_daily > 0.35:
            return "PACE WARNING: Need {:.0%}/day to hit target. Push for more trades.".format(
                required_daily)

    return None


def init_tracker(start_equity: float = START_EQUITY,
                 target_equity: float = TARGET_EQUITY,
                 target_days: int = TARGET_DAYS):
    """Initialize or reset the growth tracker."""
    state = {
        "start_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "start_equity": round(start_equity, 2),
        "target_equity": round(target_equity, 2),
        "target_days": target_days,
        "daily_snapshots": [],
        "session_log": [],
    }
    _save_state(state)
    log.info(f"Growth tracker initialized: ${start_equity:.2f} -> ${target_equity:.0f} in {target_days} days")
    return state
