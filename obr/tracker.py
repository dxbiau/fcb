"""
obr/tracker.py -- Growth tracker for OBR bot.

Tracks progress from $50 -> $500 (x10).
Session snapshots, pace alerts, ASCII dashboard.
Persists to obr/tracker.json.
"""

import json
import os
import time
from datetime import datetime, timezone

from obr import config as cfg


class OBRTracker:
    """Track equity growth toward $500 target."""

    def __init__(self):
        self._path = cfg.TRACKER_FILE
        self._data = self._load()

    # ----------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "start_equity": cfg.START_EQUITY,
            "target_equity": cfg.TARGET_EQUITY,
            "target_days": cfg.TARGET_DAYS,
            "first_session_ts": None,
            "sessions": [],
            "daily_snapshots": [],
            "peak_equity": cfg.START_EQUITY,
            "last_date": None,
        }

    def _save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        if os.path.exists(self._path):
            os.replace(tmp, self._path)
        else:
            os.rename(tmp, self._path)

    # ----------------------------------------------------------
    # Session recording
    # ----------------------------------------------------------

    def record_session(self, equity: float, session: str,
                       trades: int, wins: int, losses: int,
                       r_total: float):
        """Record a completed session."""
        now = datetime.now(timezone.utc)
        ts = now.isoformat()

        if self._data["first_session_ts"] is None:
            self._data["first_session_ts"] = ts

        # Update peak
        if equity > self._data["peak_equity"]:
            self._data["peak_equity"] = equity

        # Session record
        self._data["sessions"].append({
            "ts": ts,
            "session": session,
            "equity": round(equity, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "r": round(r_total, 4),
        })

        # Daily snapshot (one per day)
        today = now.strftime("%Y-%m-%d")
        if self._data["last_date"] != today:
            self._data["daily_snapshots"].append({
                "date": today,
                "equity": round(equity, 2),
            })
            self._data["last_date"] = today
        else:
            # Update today's snapshot
            if self._data["daily_snapshots"]:
                self._data["daily_snapshots"][-1]["equity"] = round(equity, 2)

        # Keep last 200 sessions
        if len(self._data["sessions"]) > 200:
            self._data["sessions"] = self._data["sessions"][-200:]

        self._save()

    # ----------------------------------------------------------
    # Metrics
    # ----------------------------------------------------------

    def _days_elapsed(self) -> float:
        first = self._data.get("first_session_ts")
        if not first:
            return 0.0
        try:
            start = datetime.fromisoformat(first)
        except (ValueError, TypeError):
            return 0.0
        now = datetime.now(timezone.utc)
        return max(0, (now - start).total_seconds() / 86400)

    def _growth_pct(self, equity: float) -> float:
        start = self._data.get("start_equity") or cfg.START_EQUITY
        if start <= 0:
            return 0.0
        return (equity - start) / start * 100

    def _target_pct(self, equity: float) -> float:
        start = self._data.get("start_equity") or cfg.START_EQUITY
        target = self._data.get("target_equity") or cfg.TARGET_EQUITY
        total_needed = target - start
        if total_needed <= 0:
            return 100.0
        made = equity - start
        return max(0, min(100, made / total_needed * 100))

    def _required_daily_pct(self, equity: float) -> float:
        target = self._data.get("target_equity") or cfg.TARGET_EQUITY
        target_days = self._data.get("target_days") or cfg.TARGET_DAYS
        days_elapsed = self._days_elapsed()
        days_left = max(1, target_days - days_elapsed)
        if equity <= 0:
            return 999
        # What daily growth rate needed to reach target in days_left
        # equity * (1 + r)^days_left = target
        ratio = target / equity
        if ratio <= 1:
            return 0
        r = ratio ** (1 / days_left) - 1
        return r * 100

    def _pace_status(self, equity: float) -> str:
        """On pace / behind / ahead."""
        target = self._data.get("target_equity") or cfg.TARGET_EQUITY
        if equity >= target:
            return "TARGET REACHED"
        days = self._days_elapsed()
        target_days = self._data.get("target_days") or cfg.TARGET_DAYS
        if days <= 0:
            return "Day 0"
        start = self._data.get("start_equity") or cfg.START_EQUITY
        expected_frac = days / target_days
        expected_eq = start + (target - start) * expected_frac
        if equity >= expected_eq * 1.1:
            return "AHEAD"
        elif equity >= expected_eq * 0.9:
            return "ON PACE"
        else:
            return "BEHIND"

    # ----------------------------------------------------------
    # Dashboard
    # ----------------------------------------------------------

    def get_dashboard(self, equity: float) -> str:
        """Aesthetic ASCII dashboard with ANSI colors + emojis."""
        start = self._data.get("start_equity") or cfg.START_EQUITY
        target = self._data.get("target_equity") or cfg.TARGET_EQUITY
        peak = self._data.get("peak_equity") or equity
        if equity > peak:
            peak = equity

        days = self._days_elapsed()
        target_days = self._data.get("target_days") or cfg.TARGET_DAYS
        target_pct = self._target_pct(equity)
        growth = self._growth_pct(equity)
        pace = self._pace_status(equity)
        req_daily = self._required_daily_pct(equity)

        # Mod 4: Get current phase info
        try:
            phase_target, phase_cap, phase_label = cfg.get_current_phase(equity)
        except Exception:
            phase_target, phase_cap, phase_label = target, 15.0, "default"

        sessions = self._data.get("sessions", [])
        total_trades = sum(s.get("trades", 0) for s in sessions)
        total_wins = sum(s.get("wins", 0) for s in sessions)
        total_losses = sum(s.get("losses", 0) for s in sessions)
        total_r = sum(s.get("r", 0) for s in sessions)
        wr = (total_wins / max(1, total_wins + total_losses) * 100) if sessions else 0

        dd = ((peak - equity) / peak * 100) if peak > 0 else 0

        # Progress bar with colored segments
        bar_len = 30
        filled = int(target_pct / 100 * bar_len)
        filled = max(0, min(bar_len, filled))
        bar = "\033[92m" + "\u2588" * filled + "\033[2m" + "\u2591" * (bar_len - filled) + "\033[0m"

        # Color codes
        R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
        GR = "\033[92m"; RD = "\033[91m"; YL = "\033[93m"
        CY = "\033[96m"; MG = "\033[95m"; WH = "\033[97m"

        # Pace color
        if "AHEAD" in pace:
            pc = GR
        elif "ON PACE" in pace:
            pc = CY
        else:
            pc = YL

        # Growth / DD color
        gc = GR if growth >= 0 else RD
        dc = YL if dd < 10 else RD

        lines = [
            "",
            f"{MG}\u256d{'─' * 44}\u256e{R}",
            f"{MG}\u2502{R}  \U0001f3c6 {B}{WH}OBR Growth Tracker{R}{' ' * 18}{MG}\u2502{R}",
            f"{MG}\u251c{'─' * 44}\u2524{R}",
            f"{MG}\u2502{R}  \U0001f4b8 {D}Start:{R} {WH}${start:.0f}{R}   "
            f"\U0001f3af {D}Target:{R} {B}{GR}${target:.0f}{R}   "
            f"{D}x{target/max(start,0.01):.0f}{R}  {MG}\u2502{R}",
            f"{MG}\u2502{R}  \U0001f48e {D}Equity:{R} {GR}${equity:.2f}{R}   "
            f"\u2b50 {D}Peak:{R} {CY}${peak:.2f}{R}{' ' * 6}{MG}\u2502{R}",
            f"{MG}\u2502{R}  \U0001f4c8 {D}Growth:{R} {gc}{growth:+.1f}%{R}   "
            f"\U0001f4c9 {D}DD:{R} {dc}{dd:.1f}%{R}{' ' * 15}{MG}\u2502{R}",
            f"{MG}\u2502{R}  \u23f0 {D}Day{R} {WH}{days:.1f}{R}{D}/{target_days}{R}   "
            f"\U0001f3c3 {D}Pace:{R} {pc}{pace}{R}{' ' * 10}{MG}\u2502{R}",
            f"{MG}\u2502{R}  \U0001f525 {D}Need:{R} {YL}{req_daily:.1f}%/day{R}{' ' * 24}{MG}\u2502{R}",
            f"{MG}\u2502{R}  \U0001f3c1 {D}Phase:{R} {CY}{phase_label}{R}{' ' * max(1, 28 - len(phase_label))}{MG}\u2502{R}",
            f"{MG}\u251c{'─' * 44}\u2524{R}",
            f"{MG}\u2502{R}  [{bar}] {B}{WH}{target_pct:.1f}%{R}  {MG}\u2502{R}",
            f"{MG}\u251c{'─' * 44}\u2524{R}",
            f"{MG}\u2502{R}  \U0001f4ca {D}Trades:{R} {WH}{total_trades}{R}  "
            f"\u2705 {D}W:{R}{GR}{total_wins}{R}  "
            f"\u274c {D}L:{R}{RD}{total_losses}{R}  "
            f"{D}WR:{R}{GR if wr >= 50 else YL}{wr:.0f}%{R}{' ' * 2}{MG}\u2502{R}",
            f"{MG}\u2502{R}  \u26a1 {D}Total R:{R} "
            f"{GR if total_r >= 0 else RD}{total_r:+.1f}{R}{' ' * 27}{MG}\u2502{R}",
            f"{MG}\u2570{'─' * 44}\u256f{R}",
            "",
        ]
        return "\n".join(lines)

    def recent_sessions(self, n: int = 5) -> str:
        """Last N session summaries."""
        sessions = self._data.get("sessions", [])[-n:]
        if not sessions:
            return "No sessions recorded yet."
        lines = ["Recent sessions:"]
        for s in sessions:
            lines.append(f"  {s['ts'][:16]} {s['session']:>7} | "
                         f"${s['equity']:>8.2f} | "
                         f"T:{s['trades']} W:{s['wins']} L:{s['losses']} "
                         f"R:{s['r']:+.2f}")
        return "\n".join(lines)
