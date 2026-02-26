"""
v13pro/thesis.py  —  Thesis Builder / Logger

Silently runs in the background capturing which PAIR + STRATEGY + SIDE
combinations win. Over time this builds a knowledge base of:

  "DOGE works best with BB_FADE/15m on longs — 12 wins, 3 losses, 80% WR"

Use cases:
  1. Find which strategy fits each pair best
  2. Discover pair <-> strategy correlations
  3. Build confidence in promoting combos from shadow to live
  4. Find new pairs that match winning patterns

Data persisted to thesis_state.json and thesis_log.jsonl.
Reads shadow outcomes in real-time (hooks into ShadowTrader._finalize).
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

_STATE_FILE = os.path.join(cfg.BASE_DIR, "thesis_state.json")
_LOG_FILE = os.path.join(cfg.LOG_DIR, "thesis_log.jsonl")

# How often to print summary to bot log (seconds)
SUMMARY_INTERVAL = 1800  # every 30 min


class ThesisLogger:
    """Background thesis builder — tracks pair/strategy/side win rates."""

    def __init__(self):
        # Core data: (pair, strategy, tf, side) → {wins, losses, streak, pnl_r}
        self._combos: Dict[Tuple[str, str, str, str], dict] = {}
        self._total_recorded = 0
        self._last_summary = 0.0
        self._load_state()

    def _load_state(self):
        """Load persisted thesis state."""
        if os.path.exists(_STATE_FILE):
            try:
                with open(_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data.get("combos", []):
                    key = (entry["pair"], entry["strategy"],
                           entry["tf"], entry["side"])
                    self._combos[key] = {
                        "wins": entry["wins"],
                        "losses": entry["losses"],
                        "total_r": entry.get("total_r", 0.0),
                        "streak": entry.get("streak", 0),
                        "best_r": entry.get("best_r", 0.0),
                        "worst_r": entry.get("worst_r", 0.0),
                        "last_ts": entry.get("last_ts", 0),
                    }
                self._total_recorded = data.get("total_recorded", 0)
                n = len(self._combos)
                if n:
                    log.info(f"Thesis: loaded {n} pair/strategy combos "
                             f"({self._total_recorded} total outcomes)")
            except Exception as e:
                _log.warning(f"Thesis: load error: {e}")

    def _save_state(self):
        """Persist thesis state to disk."""
        try:
            combos_list = []
            for (pair, strat, tf, side), d in self._combos.items():
                combos_list.append({
                    "pair": pair,
                    "strategy": strat,
                    "tf": tf,
                    "side": side,
                    **d,
                })
            data = {
                "total_recorded": self._total_recorded,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "combos": combos_list,
            }
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            _log.warning(f"Thesis: save error: {e}")

    def _log_event(self, event: dict):
        """Append to thesis log JSONL."""
        try:
            os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass

    def record_outcome(self, record: dict):
        """
        Record a shadow outcome into the thesis.
        Called from ShadowTrader._finalize() for every completed shadow trade.

        Args:
            record: shadow_outcome dict with symbol, strategy, tf, side, pnl_r, etc.
        """
        pnl_r = record.get("pnl_r")
        if pnl_r is None:
            return

        pair = record.get("symbol", "?")
        strat = record.get("strategy", "?")
        tf = record.get("tf", "?")
        side = record.get("side", "?").lower()
        grade = record.get("grade", "?")
        conviction = record.get("conviction", 0)
        passed = record.get("passed", False)
        is_win = pnl_r > 0

        key = (pair, strat, tf, side)

        if key not in self._combos:
            self._combos[key] = {
                "wins": 0, "losses": 0, "total_r": 0.0,
                "streak": 0, "best_r": 0.0, "worst_r": 0.0,
                "last_ts": 0,
            }

        d = self._combos[key]
        if is_win:
            d["wins"] += 1
            d["streak"] = max(0, d["streak"]) + 1
        else:
            d["losses"] += 1
            d["streak"] = min(0, d["streak"]) - 1

        d["total_r"] += pnl_r
        d["best_r"] = max(d["best_r"], pnl_r)
        d["worst_r"] = min(d["worst_r"], pnl_r)
        d["last_ts"] = int(time.time())

        self._total_recorded += 1

        # Log the event
        self._log_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "pair": pair,
            "strategy": strat,
            "tf": tf,
            "side": side,
            "pnl_r": round(pnl_r, 3),
            "win": is_win,
            "grade": grade,
            "conviction": round(conviction, 1),
            "passed": passed,
            "cumulative_wins": d["wins"],
            "cumulative_losses": d["losses"],
            "cumulative_wr": round(d["wins"] / (d["wins"] + d["losses"]) * 100, 1),
            "streak": d["streak"],
        })

        # Periodic save (every 10 outcomes)
        if self._total_recorded % 10 == 0:
            self._save_state()

    def maybe_print_summary(self):
        """Print top performers to log if enough time has passed."""
        now = time.time()
        if now - self._last_summary < SUMMARY_INTERVAL:
            return
        self._last_summary = now

        if not self._combos:
            return

        # Find top combos by wins (min 5 trades)
        eligible = []
        for key, d in self._combos.items():
            total = d["wins"] + d["losses"]
            if total < 5:
                continue
            wr = d["wins"] / total * 100
            exp = d["total_r"] / total
            eligible.append((key, d, wr, exp, total))

        if not eligible:
            return

        # Sort by ExpR
        eligible.sort(key=lambda x: x[3], reverse=True)

        # Top 5 performers
        top = eligible[:5]
        log.info(f"Thesis top combos ({len(eligible)} tracked, "
                 f"{self._total_recorded} total outcomes):")
        for (pair, strat, tf, side), d, wr, exp, total in top:
            # Clean pair name
            short_pair = pair.replace("/USDT:USDT", "").replace("USDT", "")
            log.info(f"  {short_pair:<10} {strat}/{tf:<4} {side:<5} "
                     f"W={d['wins']:>3} L={d['losses']:>3} "
                     f"WR={wr:.0f}% ExpR={exp:+.2f}")

    def get_best_strategy(self, pair: str, side: str = "long",
                          min_trades: int = 10) -> Optional[dict]:
        """
        Get the best strategy for a specific pair and side.
        Returns dict with strategy, tf, wins, losses, wr, exp_r or None.
        """
        best = None
        best_exp = -999
        for (p, strat, tf, s), d in self._combos.items():
            if p != pair or s != side:
                continue
            total = d["wins"] + d["losses"]
            if total < min_trades:
                continue
            exp = d["total_r"] / total
            if exp > best_exp:
                best_exp = exp
                best = {
                    "strategy": strat,
                    "tf": tf,
                    "wins": d["wins"],
                    "losses": d["losses"],
                    "wr": d["wins"] / total * 100,
                    "exp_r": exp,
                    "total_trades": total,
                    "streak": d["streak"],
                }
        return best

    def get_pair_affinity(self, strategy: str, tf: str,
                          side: str = "long",
                          min_trades: int = 5) -> list:
        """
        Find which pairs work best with a given strategy/tf/side.
        Returns sorted list of (pair, stats) tuples.
        """
        results = []
        for (pair, strat, _tf, s), d in self._combos.items():
            if strat != strategy or _tf != tf or s != side:
                continue
            total = d["wins"] + d["losses"]
            if total < min_trades:
                continue
            exp = d["total_r"] / total
            results.append((pair, {
                "wins": d["wins"],
                "losses": d["losses"],
                "wr": d["wins"] / total * 100,
                "exp_r": exp,
                "total_trades": total,
            }))
        results.sort(key=lambda x: x[1]["exp_r"], reverse=True)
        return results

    def save(self):
        """Force save state."""
        self._save_state()


def print_report():
    """CLI: Print full thesis report."""
    t = ThesisLogger()

    print("\n" + "=" * 75)
    print("  THESIS REPORT — Pair x Strategy x Side Win Rates")
    print("=" * 75)

    if not t._combos:
        print("  No data yet. Run the bot with shadow trader to collect.")
        return

    # Build sorted list
    rows = []
    for (pair, strat, tf, side), d in t._combos.items():
        total = d["wins"] + d["losses"]
        if total < 3:
            continue
        wr = d["wins"] / total * 100
        exp = d["total_r"] / total
        short_pair = pair.replace("/USDT:USDT", "").replace("USDT", "")
        rows.append((short_pair, strat, tf, side, d["wins"], d["losses"],
                      total, wr, exp, d["streak"]))

    # Sort by ExpR descending
    rows.sort(key=lambda x: x[8], reverse=True)

    print(f"\n  {'Pair':<12} {'Strategy/TF':<16} {'Side':<5} "
          f"{'W':>3} {'L':>3} {'N':>4} {'WR%':>5} {'ExpR':>6} {'Strk':>4}")
    print("  " + "-" * 70)

    for pair, strat, tf, side, w, l, n, wr, exp, streak in rows:
        s_mark = ""
        if streak >= 3:
            s_mark = " 🔥"
        elif streak <= -3:
            s_mark = " ❄️"
        print(f"  {pair:<12} {strat}/{tf:<10} {side:<5} "
              f"{w:>3} {l:>3} {n:>4} {wr:>4.0f}% {exp:>+5.2f} {streak:>+3}{s_mark}")

    # Summary stats
    total_w = sum(d["wins"] for d in t._combos.values())
    total_l = sum(d["losses"] for d in t._combos.values())
    total_n = total_w + total_l
    overall_wr = total_w / total_n * 100 if total_n > 0 else 0
    print(f"\n  Grand total: {total_n} outcomes | {total_w}W / {total_l}L | WR={overall_wr:.1f}%")


if __name__ == "__main__":
    print_report()
