"""
obr/state.py -- Persistent state for OBR bot.

Tracks:
  - Equity, daily P&L, 24/7 trade limits
  - Open positions with entry details
  - Trade history (last 200)
  - Per-pair statistics and cooldowns
  - Daily counters and growth cap

Atomic JSON persistence with thread-safe locking.
"""

import os
import json
import threading
from datetime import datetime, timezone
from obr.config import (
    STATE_FILE, MAX_TRADES_DAY,
    MAX_CONCURRENT_POSITIONS, START_EQUITY,
    DAILY_GROWTH_CAP_PCT, PAIR_COOLDOWN_MINUTES,
    PAIR_LOSS_COOLDOWN_COUNT, PAIR_LOSS_COOLDOWN_HOURS,
)
from obr import logger as log


class BotState:
    """Thread-safe persistent bot state."""

    def __init__(self, state_file: str = STATE_FILE):
        self._file = state_file
        self._lock = threading.Lock()
        self._state = self._load()

    # ----------------------------------------------------------
    #  Persistence
    # ----------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                log.info(f"State loaded from {self._file}")
                return state
            except Exception as e:
                log.warning(f"Failed to load state: {e} -- starting fresh")
        return self._default_state()

    def _save(self):
        tmp = self._file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._file) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
            os.replace(tmp, self._file)
        except Exception as e:
            log.error(f"Failed to save state: {e}")

    def _default_state(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {
            "date": today,
            "equity": START_EQUITY,
            "peak_equity": START_EQUITY,
            "day_start_equity": START_EQUITY,

            # Counters
            "total_trades": 0,
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl_r": 0.0,
            "total_pnl_usd": 0.0,

            # Daily
            "entries_today": 0,
            "wins_today": 0,
            "losses_today": 0,
            "pnl_today_r": 0.0,
            "pnl_today_usd": 0.0,

            # Session tracking
            "daily_counts": {},       # {pair: count_today}
            "pair_last_trade": {},    # {pair: iso_timestamp}

            # Active positions
            "pending_entries": [],    # list of dicts with entry details

            # History
            "trade_history": [],      # last 200 trades

            # Mod 8: Withdrawal milestones already alerted
            "milestones_alerted": [],  # list of equity levels already triggered
        }

    # ----------------------------------------------------------
    #  Day rollover
    # ----------------------------------------------------------

    def check_new_day(self):
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._state["date"] != today:
                log.info(f"Day rollover: {self._state['date']} -> {today}")
                self._state["date"] = today
                self._state["day_start_equity"] = self._state["equity"]
                self._state["entries_today"] = 0
                self._state["wins_today"] = 0
                self._state["losses_today"] = 0
                self._state["pnl_today_r"] = 0.0
                self._state["pnl_today_usd"] = 0.0
                self._state["daily_counts"] = {}
                self._state["pair_last_trade"] = {}
                self._save()

    # ----------------------------------------------------------
    #  Trade limits
    # ----------------------------------------------------------

    def can_trade(self, pair: str, session: str = "",
                 max_concurrent: int = 0, daily_cap: float = 0.0) -> bool:
        """Check if we can open a new trade for this pair (24/7 mode).

        Args:
            max_concurrent: Dynamic max positions (Mod 10). 0 = use config default.
            daily_cap: Dynamic daily growth cap % (Mod 4). 0 = use config default.
        """
        with self._lock:
            # Max concurrent (Mod 10: dynamic, fallback to config)
            _max = max_concurrent if max_concurrent > 0 else MAX_CONCURRENT_POSITIONS
            if len(self._state["pending_entries"]) >= _max:
                return False

            # Already trading this pair
            for p in self._state["pending_entries"]:
                if p.get("symbol") == pair:
                    return False

            # Daily growth cap (Mod 4: phase-aware, fallback to config)
            _cap = daily_cap if daily_cap > 0 else DAILY_GROWTH_CAP_PCT
            day_start = self._state.get("day_start_equity", START_EQUITY)
            if day_start > 0 and _cap > 0:
                current = self._state["equity"]
                day_growth = (current - day_start) / day_start * 100
                if day_growth >= _cap:
                    return False

            # Pair cooldown -- can't re-trade same pair within N minutes
            last_entry_time = None
            for h in reversed(self._state.get("trade_history", [])):
                if h.get("pair") == pair:
                    last_entry_time = h.get("entry_time")
                    break
            if not last_entry_time:
                # Also check daily_traded timestamps
                ts = self._state.get("pair_last_trade", {}).get(pair)
                if ts:
                    last_entry_time = ts
            if last_entry_time:
                try:
                    last_dt = datetime.fromisoformat(last_entry_time)
                    now = datetime.now(timezone.utc)
                    minutes_since = (now - last_dt).total_seconds() / 60
                    if minutes_since < PAIR_COOLDOWN_MINUTES:
                        return False
                except Exception:
                    pass

            # Daily per-pair limit (max 5 per pair per day)
            daily = self._state["daily_counts"].get(pair, 0)
            if daily >= 5:
                return False

            # Consecutive loss cooldown -- after N losses, pause pair for M hours
            consec = self._state.get("pair_consec_losses", {}).get(pair, {})
            streak = consec.get("streak", 0)
            if streak >= PAIR_LOSS_COOLDOWN_COUNT:
                last_loss_time = consec.get("last_loss_time")
                if last_loss_time:
                    try:
                        last_dt = datetime.fromisoformat(last_loss_time)
                        now = datetime.now(timezone.utc)
                        hours_since = (now - last_dt).total_seconds() / 3600
                        if hours_since < PAIR_LOSS_COOLDOWN_HOURS:
                            return False
                        else:
                            # Cooldown expired, reset streak
                            consec["streak"] = 0
                            self._state.get("pair_consec_losses", {})[pair] = consec
                    except Exception:
                        pass

            # Daily total
            if self._state["entries_today"] >= MAX_TRADES_DAY:
                return False

            return True

    # ----------------------------------------------------------
    #  Record entry
    # ----------------------------------------------------------

    def record_entry(self, pair: str, session: str, entry_data: dict):
        """Record a new entry."""
        with self._lock:
            # Add to pending
            entry_data["symbol"] = pair
            entry_data["session"] = session
            entry_data["entry_time"] = datetime.now(timezone.utc).isoformat()
            self._state["pending_entries"].append(entry_data)

            # Update daily counts
            self._state["daily_counts"][pair] = \
                self._state["daily_counts"].get(pair, 0) + 1
            self._state["entries_today"] += 1

            # Track per-pair last trade time for cooldown
            if "pair_last_trade" not in self._state:
                self._state["pair_last_trade"] = {}
            self._state["pair_last_trade"][pair] = \
                datetime.now(timezone.utc).isoformat()

            self._state["total_trades"] += 1
            self._save()

    @property
    def daily_growth_pct(self) -> float:
        """Current day's growth percentage."""
        day_start = self._state.get("day_start_equity", START_EQUITY)
        if day_start <= 0:
            return 0.0
        return (self._state["equity"] - day_start) / day_start * 100

    @property
    def daily_capped(self) -> bool:
        """Whether daily growth cap has been hit."""
        return DAILY_GROWTH_CAP_PCT > 0 and self.daily_growth_pct >= DAILY_GROWTH_CAP_PCT

    # ----------------------------------------------------------
    #  Record outcome
    # ----------------------------------------------------------

    def record_outcome(self, pair: str, pnl_r: float, pnl_usd: float,
                       exit_reason: str, entry_data: dict = None):
        """Record trade result."""
        with self._lock:
            is_win = pnl_r > 0

            # Update totals
            if is_win:
                self._state["total_wins"] += 1
                self._state["wins_today"] += 1
            else:
                self._state["total_losses"] += 1
                self._state["losses_today"] += 1

            self._state["total_pnl_r"] += pnl_r
            self._state["total_pnl_usd"] += pnl_usd
            self._state["pnl_today_r"] += pnl_r
            self._state["pnl_today_usd"] += pnl_usd

            # Remove from pending
            self._state["pending_entries"] = [
                p for p in self._state["pending_entries"]
                if p.get("symbol") != pair
            ]

            # Track consecutive losses per pair
            if "pair_consec_losses" not in self._state:
                self._state["pair_consec_losses"] = {}
            if pair not in self._state["pair_consec_losses"]:
                self._state["pair_consec_losses"][pair] = {"streak": 0, "last_loss_time": None}
            consec = self._state["pair_consec_losses"][pair]
            if is_win:
                consec["streak"] = 0  # reset on win
            else:
                consec["streak"] = consec.get("streak", 0) + 1
                consec["last_loss_time"] = datetime.now(timezone.utc).isoformat()
            self._state["pair_consec_losses"][pair] = consec

            # Add to history
            record = {
                "pair": pair,
                "pnl_r": round(pnl_r, 4),
                "pnl_usd": round(pnl_usd, 2),
                "exit_reason": exit_reason,
                "time": datetime.now(timezone.utc).isoformat(),
            }
            if entry_data:
                record.update({
                    "direction": entry_data.get("direction", ""),
                    "entry_price": entry_data.get("entry_price", 0),
                    "stop_loss": entry_data.get("stop_loss", 0),
                })
            self._state["trade_history"].append(record)
            # Keep last 200
            if len(self._state["trade_history"]) > 200:
                self._state["trade_history"] = self._state["trade_history"][-200:]

            self._save()

    # ----------------------------------------------------------
    #  Equity
    # ----------------------------------------------------------

    def update_equity(self, equity: float):
        with self._lock:
            old_equity = self._state.get("equity", START_EQUITY)
            self._state["equity"] = equity
            if equity > self._state.get("peak_equity", 0):
                self._state["peak_equity"] = equity
            if not self._state.get("day_start_equity"):
                self._state["day_start_equity"] = equity

            # Detect deposit/withdrawal: if equity jumps by more than
            # what trading could produce (>30% in one check), reset
            # day_start to avoid false daily-cap trigger.
            day_start = self._state.get("day_start_equity", equity)
            if day_start > 0 and old_equity > 0:
                jump_pct = abs(equity - old_equity) / old_equity * 100
                # A single equity refresh can't legitimately move >30%
                # unless capital was added/removed externally
                if jump_pct > 30 and abs(equity - old_equity) > 5:
                    log.info(f"  💰 Deposit/withdrawal detected: "
                             f"${old_equity:.2f} → ${equity:.2f} "
                             f"-- resetting day baseline")
                    self._state["day_start_equity"] = equity

            self._save()

    @property
    def equity(self) -> float:
        return self._state.get("equity", START_EQUITY)

    @property
    def peak_equity(self) -> float:
        return self._state.get("peak_equity", START_EQUITY)

    @property
    def pending_count(self) -> int:
        return len(self._state.get("pending_entries", []))

    @property
    def pending_entries(self) -> list:
        return list(self._state.get("pending_entries", []))

    # ----------------------------------------------------------
    #  Summary
    # ----------------------------------------------------------

    def daily_summary(self) -> dict:
        with self._lock:
            s = self._state
            total = s["wins_today"] + s["losses_today"]
            wr = s["wins_today"] / total * 100 if total > 0 else 0
            return {
                "date": s["date"],
                "equity": s["equity"],
                "day_start": s["day_start_equity"],
                "entries": s["entries_today"],
                "wins": s["wins_today"],
                "losses": s["losses_today"],
                "wr": wr,
                "pnl_r": s["pnl_today_r"],
                "pnl_usd": s["pnl_today_usd"],
                "pending": len(s["pending_entries"]),
            }

    def lifetime_summary(self) -> dict:
        with self._lock:
            s = self._state
            total = s["total_wins"] + s["total_losses"]
            wr = s["total_wins"] / total * 100 if total > 0 else 0
            return {
                "total_trades": s["total_trades"],
                "wins": s["total_wins"],
                "losses": s["total_losses"],
                "wr": wr,
                "total_r": s["total_pnl_r"],
                "total_usd": s["total_pnl_usd"],
                "equity": s["equity"],
                "peak": s["peak_equity"],
                "dd": (s["peak_equity"] - s["equity"]) / s["peak_equity"] * 100
                      if s["peak_equity"] > 0 else 0,
            }

    def remove_pending(self, pair: str):
        """Remove a pair from pending entries (e.g. position externally closed)."""
        with self._lock:
            self._state["pending_entries"] = [
                p for p in self._state["pending_entries"]
                if p.get("symbol") != pair
            ]
            self._save()

    # ----------------------------------------------------------
    #  Mod 8: Withdrawal milestones
    # ----------------------------------------------------------

    def check_milestones(self, milestones: list) -> list:
        """
        Check if equity has crossed any withdrawal milestones.

        Args:
            milestones: list of (equity_level, withdraw_pct, label) tuples

        Returns:
            list of newly triggered milestones: [(level, pct, label), ...]
        """
        with self._lock:
            equity = self._state["equity"]
            alerted = self._state.get("milestones_alerted", [])
            newly_triggered = []
            for level, pct, label in milestones:
                if equity >= level and level not in alerted:
                    newly_triggered.append((level, pct, label))
                    alerted.append(level)
            if newly_triggered:
                self._state["milestones_alerted"] = alerted
                self._save()
            return newly_triggered
