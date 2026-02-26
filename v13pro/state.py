"""
v13pro/state.py -- Thread-safe bot state with JSON persistence.

Adapted from obr/state.py for v13pro (self-contained).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from v13pro import config as cfg
from v13pro import logger as log


class BotState:
    def __init__(self):
        self._lock = threading.Lock()
        self._path = cfg.STATE_FILE
        self._state = self._load()
        self._adaptive = None  # AdaptiveParams (set via set_adaptive)

    def set_adaptive(self, adaptive):
        """Wire in adaptive engine for per-pair cooldowns."""
        self._adaptive = adaptive

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return self._default()

    def _default(self):
        return {
            "equity": cfg.START_EQUITY,
            "peak_equity": cfg.START_EQUITY,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "day_start_equity": cfg.START_EQUITY,
            "entries_today": 0,
            "wins_today": 0,
            "losses_today": 0,
            "pnl_today_r": 0.0,
            "pnl_today_usd": 0.0,
            "total_trades": 0,
            "total_wins": 0,
            "total_losses": 0,
            "pending_entries": [],
            "trade_history": [],
            "daily_counts": {},
            "pair_last_trade": {},
            "consecutive_losses": {},
            "milestones_alerted": [],
        }

    def _save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, default=str)
        if os.path.exists(self._path):
            os.replace(tmp, self._path)
        else:
            os.rename(tmp, self._path)

    @property
    def equity(self):
        with self._lock:
            return self._state.get("equity", cfg.START_EQUITY)

    @property
    def peak_equity(self):
        with self._lock:
            return self._state.get("peak_equity", cfg.START_EQUITY)

    @property
    def pending_entries(self):
        with self._lock:
            return list(self._state.get("pending_entries", []))

    @property
    def pending_count(self):
        with self._lock:
            return len(self._state.get("pending_entries", []))

    @property
    def daily_growth_pct(self):
        with self._lock:
            day_start = self._state.get("day_start_equity", cfg.START_EQUITY)
            current = self._state.get("equity", cfg.START_EQUITY)
            if day_start <= 0:
                return 0.0
            return (current - day_start) / day_start * 100

    @property
    def pnl_today_r(self):
        with self._lock:
            return self._state.get("pnl_today_r", 0.0)

    @property
    def pnl_today_usd(self):
        with self._lock:
            return self._state.get("pnl_today_usd", 0.0)

    @property
    def wins_today(self):
        with self._lock:
            return self._state.get("wins_today", 0)

    @property
    def losses_today(self):
        with self._lock:
            return self._state.get("losses_today", 0)

    @property
    def entries_today(self):
        with self._lock:
            return self._state.get("entries_today", 0)

    def update_equity(self, equity: float):
        with self._lock:
            self._state["equity"] = equity
            if equity > self._state.get("peak_equity", 0):
                self._state["peak_equity"] = equity
            self._save()

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
                # NOTE: pair_last_trade is NOT cleared — it must survive
                # day rollover so consecutive-loss cooldowns still work.
                # The 60-minute pair cooldown will expire naturally.
                self._save()

    def can_trade(self, pair: str, session: str = "",
                  max_concurrent: int = 0, daily_cap: float = 0.0) -> bool:
        with self._lock:
            _max = max_concurrent if max_concurrent > 0 else cfg.MAX_CONCURRENT_POSITIONS
            if len(self._state["pending_entries"]) >= _max:
                return False
            # Already trading
            for p in self._state["pending_entries"]:
                if p.get("symbol") == pair:
                    return False
            # Daily growth cap
            _cap = daily_cap if daily_cap > 0 else cfg.DAILY_GROWTH_CAP_PCT
            day_start = self._state.get("day_start_equity", cfg.START_EQUITY)
            if day_start > 0 and _cap > 0:
                growth = (self._state["equity"] - day_start) / day_start * 100
                if growth >= _cap:
                    return False
            # Pair cooldown (adaptive if available)
            cooldown_mins = cfg.PAIR_COOLDOWN_MINUTES
            if self._adaptive:
                try:
                    cooldown_mins = self._adaptive.pair_cooldown(pair)
                except Exception:
                    pass
            ts = self._state.get("pair_last_trade", {}).get(pair)
            if ts:
                try:
                    last = datetime.fromisoformat(str(ts))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    diff = (datetime.now(timezone.utc) - last).total_seconds() / 60
                    if diff < cooldown_mins:
                        return False
                except Exception:
                    pass
            # Consecutive losses — escalating cooldown
            consec = self._state.get("consecutive_losses", {}).get(pair, 0)
            if consec >= cfg.PAIR_LOSS_COOLDOWN_COUNT:
                cooldown_hours = cfg.get_loss_streak_cooldown_hours(consec)
                ts2 = self._state.get("pair_last_trade", {}).get(pair)
                if ts2:
                    try:
                        last2 = datetime.fromisoformat(str(ts2))
                        if last2.tzinfo is None:
                            last2 = last2.replace(tzinfo=timezone.utc)
                        hours = (datetime.now(timezone.utc) - last2).total_seconds() / 3600
                        if hours < cooldown_hours:
                            return False
                    except Exception:
                        pass
                else:
                    # No timestamp (legacy state) — stamp NOW to start cooldown
                    self._state.setdefault("pair_last_trade", {})[pair] = \
                        datetime.now(timezone.utc).isoformat()
                    self._save()
                    return False
            # Daily count
            if self._state.get("entries_today", 0) >= cfg.MAX_TRADES_DAY:
                return False
            return True

    def can_trade_hunter(self, pair: str) -> bool:
        """Risk check for hunter scalps.

        Bypasses the portfolio max_concurrent check but DOES enforce:
        - Not already trading this symbol
        - Pair cooldown (60 min)
        - Consecutive loss cooldown (escalating)
        - Daily count
        - Daily growth cap
        """
        with self._lock:
            # Already trading this exact symbol?
            for p in self._state["pending_entries"]:
                if p.get("symbol") == pair:
                    return False
            # Pair cooldown (adaptive if available)
            cooldown_mins = cfg.PAIR_COOLDOWN_MINUTES
            if self._adaptive:
                try:
                    cooldown_mins = self._adaptive.pair_cooldown(pair)
                except Exception:
                    pass
            ts = self._state.get("pair_last_trade", {}).get(pair)
            if ts:
                try:
                    last = datetime.fromisoformat(str(ts))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    diff = (datetime.now(timezone.utc) - last).total_seconds() / 60
                    if diff < cooldown_mins:
                        return False
                except Exception:
                    pass
            # Consecutive losses — escalating cooldown (CRITICAL)
            consec = self._state.get("consecutive_losses", {}).get(pair, 0)
            if consec >= cfg.PAIR_LOSS_COOLDOWN_COUNT:
                cooldown_hours = cfg.get_loss_streak_cooldown_hours(consec)
                ts2 = self._state.get("pair_last_trade", {}).get(pair)
                if ts2:
                    try:
                        last2 = datetime.fromisoformat(str(ts2))
                        if last2.tzinfo is None:
                            last2 = last2.replace(tzinfo=timezone.utc)
                        hours = (datetime.now(timezone.utc) - last2).total_seconds() / 3600
                        if hours < cooldown_hours:
                            return False
                    except Exception:
                        pass
                else:
                    # No timestamp (legacy state) — stamp NOW to start cooldown
                    self._state.setdefault("pair_last_trade", {})[pair] = \
                        datetime.now(timezone.utc).isoformat()
                    self._save()
                    return False
            # Daily count
            if self._state.get("entries_today", 0) >= cfg.MAX_TRADES_DAY:
                return False
            # Daily growth cap (still respect this)
            _cap = cfg.DAILY_GROWTH_CAP_PCT
            day_start = self._state.get("day_start_equity", cfg.START_EQUITY)
            if day_start > 0 and _cap > 0:
                growth = (self._state["equity"] - day_start) / day_start * 100
                if growth >= _cap:
                    return False
            return True

    def record_entry(self, symbol: str, session: str, entry_data: dict):
        with self._lock:
            entry_data["symbol"] = symbol
            entry_data["session"] = session
            entry_data["entry_time"] = datetime.now(timezone.utc).isoformat()
            self._state["pending_entries"].append(entry_data)
            self._state["entries_today"] = self._state.get("entries_today", 0) + 1
            self._state["pair_last_trade"][symbol] = datetime.now(timezone.utc).isoformat()
            self._save()

    def get_consecutive_losses(self, pair: str) -> int:
        """Return consecutive loss count for a pair (thread-safe)."""
        with self._lock:
            return self._state.get("consecutive_losses", {}).get(pair, 0)

    def record_outcome(self, symbol: str, pnl_r: float, pnl_usd: float,
                       reason: str, entry_data=None):
        with self._lock:
            # Remove from pending
            self._state["pending_entries"] = [
                p for p in self._state["pending_entries"]
                if p.get("symbol") != symbol
            ]
            # Update counters
            self._state["total_trades"] = self._state.get("total_trades", 0) + 1
            self._state["pnl_today_r"] = self._state.get("pnl_today_r", 0) + pnl_r
            self._state["pnl_today_usd"] = self._state.get("pnl_today_usd", 0) + pnl_usd
            if pnl_r > 0:
                self._state["wins_today"] = self._state.get("wins_today", 0) + 1
                self._state["total_wins"] = self._state.get("total_wins", 0) + 1
                self._state.setdefault("consecutive_losses", {})[symbol] = 0
            else:
                self._state["losses_today"] = self._state.get("losses_today", 0) + 1
                self._state["total_losses"] = self._state.get("total_losses", 0) + 1
                cl = self._state.setdefault("consecutive_losses", {})
                cl[symbol] = cl.get(symbol, 0) + 1
            # History
            self._state.setdefault("trade_history", []).append({
                "pair": symbol, "pnl_r": pnl_r, "pnl_usd": pnl_usd,
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            if len(self._state["trade_history"]) > 500:
                self._state["trade_history"] = self._state["trade_history"][-500:]
            self._save()
