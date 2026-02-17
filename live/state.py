"""
live/state.py — Persistent state tracking for the FCB bot.

Tracks:
  - Daily trade counts per pair (resets at 00:00 UTC)
  - Session trade flags (which pairs already traded this session)
  - Cumulative trade log
  - Last known equity
  - Daily PnL, wins, losses, equity growth
  - Pending entries (positions awaiting resolution)
  - Equity floor flag

State is saved to JSON after every mutation so the bot can resume after crash/restart.
"""

import os, json
from datetime import datetime, timezone
from typing import Dict, Set
from live.config import STATE_FILE, INITIAL_PAIR_CLASS, PROMOTE_WINS, DEMOTE_LOSSES, REHABILITATE_WINS
from live import logger as log


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class BotState:
    def __init__(self):
        self.date: str = _utc_today()
        # {pair: count} — trades taken today per pair (all sessions)
        self.daily_counts: Dict[str, int] = {}
        # {session: set(pair)} — pairs that already traded this session
        self.session_traded: Dict[str, list] = {}
        # Last known USDT equity
        self.equity: float = 0.0
        # Running totals
        self.total_trades: int = 0
        self.total_pnl_r: float = 0.0
        # Trade history (last 200 for state file size)
        self.trade_history: list = []

        # ── Daily tracking (resets each UTC day) ──
        self.day_start_equity: float = 0.0
        self.wins_today: int = 0
        self.losses_today: int = 0
        self.entries_today: int = 0
        self.pnl_today_usd: float = 0.0

        # ── Cumulative win/loss ──
        self.total_wins: int = 0
        self.total_losses: int = 0

        # ── Pending entries awaiting resolution ──
        self.pending_entries: list = []

        # ── Equity floor flag ──
        self.equity_floor_hit: bool = False

        # ── Pair Classification (A/B tier system) ──
        # {pair: {"class": "A"|"B", "consec_wins": N, "consec_losses": N,
        #         "live_wins": N, "live_losses": N, "promoted": bool, "demoted": bool}}
        self.pair_classes: Dict[str, dict] = {}

        self._load()

        # Initialize pair classes from config defaults if not yet set
        self._init_pair_classes()

    def _load(self):
        """Load state from disk."""
        if not os.path.exists(STATE_FILE):
            log.info("No state file — starting fresh")
            return

        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)

            self.date = data.get("date", _utc_today())
            self.daily_counts = data.get("daily_counts", {})
            self.session_traded = data.get("session_traded", {})
            self.equity = data.get("equity", 0.0)
            self.total_trades = data.get("total_trades", 0)
            self.total_pnl_r = data.get("total_pnl_r", 0.0)
            self.trade_history = data.get("trade_history", [])

            # Daily tracking
            self.day_start_equity = data.get("day_start_equity", 0.0)
            self.wins_today = data.get("wins_today", 0)
            self.losses_today = data.get("losses_today", 0)
            self.entries_today = data.get("entries_today", 0)
            self.pnl_today_usd = data.get("pnl_today_usd", 0.0)

            # Cumulative win/loss
            self.total_wins = data.get("total_wins", 0)
            self.total_losses = data.get("total_losses", 0)

            # Pending entries
            self.pending_entries = data.get("pending_entries", [])

            # Equity floor
            self.equity_floor_hit = data.get("equity_floor_hit", False)

            # Pair classifications
            self.pair_classes = data.get("pair_classes", {})

            # Reset if new day
            if self.date != _utc_today():
                log.info(f"New day detected ({self.date} → {_utc_today()}) — resetting daily counters")
                self.date = _utc_today()
                self.daily_counts = {}
                self.session_traded = {}
                self.day_start_equity = self.equity
                self.wins_today = 0
                self.losses_today = 0
                self.entries_today = 0
                self.pnl_today_usd = 0.0
                self._save()

            log.info(f"State loaded: {self.total_trades} total trades, equity=${self.equity:.2f}, "
                     f"pending={len(self.pending_entries)}")
        except Exception as e:
            log.error(f"Failed to load state: {e} — starting fresh")

    def _save(self):
        """Persist state to disk using atomic write.

        Writes to a temp file first, then renames.  If the process is killed
        mid-write the temp file is corrupted but the original state.json
        remains intact.  On next startup, _load() reads the uncorrupted file.
        """
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        data = {
            "date": self.date,
            "daily_counts": self.daily_counts,
            "session_traded": self.session_traded,
            "equity": self.equity,
            "total_trades": self.total_trades,
            "total_pnl_r": self.total_pnl_r,
            "trade_history": self.trade_history[-200:],
            "day_start_equity": self.day_start_equity,
            "wins_today": self.wins_today,
            "losses_today": self.losses_today,
            "entries_today": self.entries_today,
            "pnl_today_usd": self.pnl_today_usd,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "pending_entries": self.pending_entries,
            "equity_floor_hit": self.equity_floor_hit,
            "pair_classes": self.pair_classes,
            "last_updated": _utc_now_str(),
        }
        tmp_file = STATE_FILE + ".tmp"
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            # Atomic rename (on Windows, need to remove target first)
            if os.path.exists(STATE_FILE):
                os.replace(tmp_file, STATE_FILE)
            else:
                os.rename(tmp_file, STATE_FILE)
        except Exception as e:
            log.error(f"Failed to save state: {e}")
            # Clean up temp file if it exists
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except:
                    pass

    def check_new_day(self):
        """Call at start of each cycle — reset if new UTC day."""
        today = _utc_today()
        if self.date != today:
            log.info(f"Day rollover: {self.date} → {today}")
            self.date = today
            self.daily_counts = {}
            self.session_traded = {}
            self.day_start_equity = self.equity
            self.wins_today = 0
            self.losses_today = 0
            self.entries_today = 0
            self.pnl_today_usd = 0.0
            self._save()

    def can_trade(self, pair: str, session: str, max_session: int, max_day: int) -> bool:
        """Check if a trade is allowed for this pair in this session."""
        # Session limit
        traded = self.session_traded.get(session, [])
        if pair in traded:
            return False

        # Daily limit
        day_count = self.daily_counts.get(pair, 0)
        if day_count >= max_day:
            return False

        return True

    def record_entry(self, pair: str, session: str, entry_data: dict):
        """Record that an entry was placed (before outcome is known)."""
        self.check_new_day()

        if session not in self.session_traded:
            self.session_traded[session] = []
        if pair not in self.session_traded[session]:
            self.session_traded[session].append(pair)

        self.daily_counts[pair] = self.daily_counts.get(pair, 0) + 1
        self.entries_today += 1

        # Track pending position for outcome resolution
        self.pending_entries.append(entry_data)

        self._save()

        log.info(f"State: {pair}/{session} entry recorded | "
                 f"day_count={self.daily_counts[pair]} | "
                 f"entries_today={self.entries_today} | "
                 f"pending={len(self.pending_entries)}")

    def record_outcome(self, symbol: str, session: str, direction: str,
                       entry_price: float, close_price: float,
                       pnl_r: float, pnl_usd: float, is_win: bool):
        """Record a completed trade outcome (win or loss)."""
        if is_win:
            self.wins_today += 1
            self.total_wins += 1
        else:
            self.losses_today += 1
            self.total_losses += 1

        self.pnl_today_usd += pnl_usd
        self.total_pnl_r += pnl_r
        self.total_trades += 1

        trade_data = {
            "symbol": symbol, "session": session, "direction": direction,
            "entry_price": entry_price, "close_price": close_price,
            "pnl_r": round(pnl_r, 4), "pnl_usd": round(pnl_usd, 2),
            "outcome": "WIN" if is_win else "LOSS",
            "timestamp": _utc_now_str(),
        }
        self.trade_history.append(trade_data)
        self._save()

    def update_equity(self, equity: float):
        """Update current equity."""
        self.equity = equity
        # Set day-start equity if not yet set
        if self.day_start_equity <= 0:
            self.day_start_equity = equity
        self._save()

    def daily_summary(self) -> dict:
        """Return a dict summarising today's performance."""
        total_resolved = self.wins_today + self.losses_today
        pnl_pct = ((self.equity / self.day_start_equity - 1) * 100
                    if self.day_start_equity > 0 else 0.0)
        all_time_total = self.total_wins + self.total_losses
        return {
            "date": self.date,
            "start_equity": self.day_start_equity,
            "current_equity": self.equity,
            "pnl_usd": self.pnl_today_usd,
            "pnl_pct": pnl_pct,
            "entries_today": self.entries_today,
            "resolved_today": total_resolved,
            "wins": self.wins_today,
            "losses": self.losses_today,
            "win_rate": (self.wins_today / total_resolved * 100) if total_resolved > 0 else 0.0,
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_pnl_r": self.total_pnl_r,
            "all_time_wr": (self.total_wins / all_time_total * 100) if all_time_total > 0 else 0.0,
            "pending": len(self.pending_entries),
        }

    # ── Pair Classification System ──────────────────────────────────────

    def _init_pair_classes(self):
        """Seed pair_classes from INITIAL_PAIR_CLASS for any pair not yet tracked."""
        changed = False
        for pair, cls in INITIAL_PAIR_CLASS.items():
            if pair not in self.pair_classes:
                self.pair_classes[pair] = {
                    "class": cls,
                    "consec_wins": 0,
                    "consec_losses": 0,
                    "live_wins": 0,
                    "live_losses": 0,
                    "promoted": False,      # ever promoted B→A
                    "demoted": False,        # currently demoted A→B
                }
                changed = True
        if changed:
            self._save()
            log.info(f"Pair classes initialised: "
                     f"{sum(1 for p in self.pair_classes.values() if p['class']=='A')} A, "
                     f"{sum(1 for p in self.pair_classes.values() if p['class']=='B')} B")

    def get_pair_class(self, pair: str) -> str:
        """Return 'A' or 'B' for a pair. Defaults to 'B' for unknowns."""
        entry = self.pair_classes.get(pair)
        if entry:
            return entry["class"]
        # Unknown pair — treat as B and register
        self.pair_classes[pair] = {
            "class": "B", "consec_wins": 0, "consec_losses": 0,
            "live_wins": 0, "live_losses": 0, "promoted": False, "demoted": False,
        }
        self._save()
        return "B"

    def record_pair_outcome(self, pair: str, is_win: bool):
        """Update pair classification based on live outcome.

        Promotion:  B → A after PROMOTE_WINS consecutive wins
                    (or REHABILITATE_WINS if pair was demoted)
        Demotion:   A → B after DEMOTE_LOSSES consecutive losses
        """
        entry = self.pair_classes.get(pair)
        if entry is None:
            self.get_pair_class(pair)  # registers it
            entry = self.pair_classes[pair]

        old_class = entry["class"]

        if is_win:
            entry["consec_wins"] += 1
            entry["consec_losses"] = 0
            entry["live_wins"] += 1
        else:
            entry["consec_losses"] += 1
            entry["consec_wins"] = 0
            entry["live_losses"] += 1

        # ── Promotion check (B → A) ──
        if entry["class"] == "B":
            threshold = REHABILITATE_WINS if entry["demoted"] else PROMOTE_WINS
            if entry["consec_wins"] >= threshold:
                entry["class"] = "A"
                entry["promoted"] = True
                entry["demoted"] = False
                entry["consec_wins"] = 0
                log.info(f"⬆️  PROMOTED {pair} → Class A  "
                         f"(W/L: {entry['live_wins']}/{entry['live_losses']}, "
                         f"threshold={threshold})")

        # ── Demotion check (A → B) ──
        elif entry["class"] == "A":
            if entry["consec_losses"] >= DEMOTE_LOSSES:
                entry["class"] = "B"
                entry["demoted"] = True
                entry["consec_losses"] = 0
                log.info(f"⬇️  DEMOTED {pair} → Class B  "
                         f"(W/L: {entry['live_wins']}/{entry['live_losses']})")

        if entry["class"] != old_class:
            log.info(f"Pair class change: {pair} {old_class} → {entry['class']}")

        self._save()

    def pair_class_summary(self) -> str:
        """Return a formatted summary of current pair classifications."""
        a_pairs = [p for p, v in self.pair_classes.items() if v["class"] == "A"]
        b_pairs = [p for p, v in self.pair_classes.items() if v["class"] == "B"]
        lines = [
            f"Class A ({len(a_pairs)}): {', '.join(sorted(a_pairs)) or 'none'}",
            f"Class B ({len(b_pairs)}): {', '.join(sorted(b_pairs)) or 'none'}",
        ]
        # Show any near promotion/demotion
        for pair, v in self.pair_classes.items():
            if v["class"] == "B" and v["consec_wins"] >= 2:
                thresh = REHABILITATE_WINS if v["demoted"] else PROMOTE_WINS
                lines.append(f"  ↑ {pair}: {v['consec_wins']}/{thresh} wins toward promotion")
            elif v["class"] == "A" and v["consec_losses"] >= 2:
                lines.append(f"  ↓ {pair}: {v['consec_losses']}/{DEMOTE_LOSSES} losses toward demotion")
        return "\n".join(lines)
