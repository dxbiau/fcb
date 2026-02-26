"""
v13pro/journal.py -- Rich trade journal with timestamps for post-trade research.

Records EVERYTHING about every trade for cross-matching analysis:
  - Entry signal details (strategy, tf, conviction score, key levels)
  - Fill timestamps (order sent, filled, confirmed)
  - Guardian actions (tier moves, trail activations, rejection checks)
  - Exit details (reason, price, slippage, aftermath tracking request)
  - Post-exit price tracking (what price did 1m/5m/15m/1h after exit)
  - Aftermath analysis (was TP premature? was SL correct? runaway?)

Formats:
  - JSONL journal: v13pro/logs/journal_YYYY-MM-DD.jsonl (machine-readable)
  - Human-readable summary: v13pro/logs/journal_YYYY-MM-DD.txt

This data feeds the learner/skill self-tuning AND enables manual review.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from v13pro import config as cfg
from v13pro import logger as log

_JOURNAL_DIR = os.path.join(cfg.LOG_DIR, "journal")


def _ensure_dir():
    os.makedirs(_JOURNAL_DIR, exist_ok=True)


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_ts():
    return int(time.time() * 1000)


def _write_jsonl(record: dict):
    """Append record to today's JSONL journal."""
    _ensure_dir()
    path = os.path.join(_JOURNAL_DIR, f"journal_{_today()}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _write_human(text: str):
    """Append human-readable line to today's text journal."""
    _ensure_dir()
    path = os.path.join(_JOURNAL_DIR, f"journal_{_today()}.txt")
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")


# ══════════════════════════════════════════════════════════════
#  SIGNAL JOURNAL — records every signal seen (even non-traded)
# ══════════════════════════════════════════════════════════════

def log_signal(symbol: str, tf: str, strategy: str, side: str,
               entry: float, stop_dist: float, passed: bool,
               conviction: float = 0, grade: str = "",
               rejection_reason: str = ""):
    """Log a detected signal (traded or not)."""
    record = {
        "event": "signal",
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "symbol": symbol,
        "tf": tf,
        "strategy": strategy,
        "side": side,
        "entry": entry,
        "stop_dist": stop_dist,
        "passed": passed,
        "conviction": conviction,
        "grade": grade,
        "rejection_reason": rejection_reason,
    }
    _write_jsonl(record)
    status = "PASS" if passed else f"SKIP({rejection_reason})"
    _write_human(f"SIG {symbol} {tf} {strategy} {side} "
                 f"entry={entry:.6f} conv={conviction:.0f}{grade} {status}")


# ══════════════════════════════════════════════════════════════
#  ENTRY JOURNAL — everything about a filled trade entry
# ══════════════════════════════════════════════════════════════

def log_entry(symbol: str, side: str, strategy: str, tf: str,
              exit_mode: str, entry_price: float, sl_price: float,
              tp_price: float, qty: float, risk_usd: float,
              leverage: int, order_id: str,
              conviction: float = 0, grade: str = "",
              skill_breakdown: dict = None,
              equity: float = 0, dd_pct: float = 0,
              session: str = "", order_type: str = "market",
              fill_price: float = 0, slippage_bps: float = 0,
              sentiment: dict = None,
              orderflow: dict = None):
    """Full entry journal record."""
    record = {
        "event": "entry",
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "tf": tf,
        "exit_mode": exit_mode,
        "entry_price": entry_price,
        "fill_price": fill_price or entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "qty": qty,
        "risk_usd": risk_usd,
        "leverage": leverage,
        "order_id": order_id,
        "order_type": order_type,
        "slippage_bps": slippage_bps,
        "conviction": conviction,
        "grade": grade,
        "skill_breakdown": skill_breakdown or {},
        "equity_at_entry": equity,
        "dd_pct_at_entry": dd_pct,
        "session": session,
        "sentiment": sentiment or {},
        "orderflow": orderflow or {},
    }
    _write_jsonl(record)
    sent_tag = ""
    if sentiment:
        sent_tag = f" mkt={sentiment.get('bias','?')}({sentiment.get('arrows','')})"
    of_tag = ""
    if orderflow and orderflow.get("spread_bps"):
        of_tag = (f" spread={orderflow['spread_bps']:.1f}bps"
                  f" imb={orderflow.get('imbalance',0):+.2f}"
                  f" [{orderflow.get('quality','?')}]")
    _write_human(
        f"ENTRY {side.upper()} {symbol} [{strategy}/{tf}] "
        f"entry={fill_price or entry_price:.6f} SL={sl_price:.6f} "
        f"TP={tp_price:.6f} qty={qty} risk=${risk_usd:.2f} "
        f"lev={leverage}x conv={conviction:.0f}{grade} "
        f"exit={exit_mode} eq=${equity:.2f}{sent_tag}{of_tag}"
    )


# ══════════════════════════════════════════════════════════════
#  GUARDIAN JOURNAL — every SL move, trail event, rejection check
# ══════════════════════════════════════════════════════════════

def log_guardian_action(symbol: str, action: str,
                        current_r: float = 0, peak_r: float = 0,
                        old_sl: float = 0, new_sl: float = 0,
                        detail: str = ""):
    """Log a guardian action (tier move, trail, rejection check)."""
    record = {
        "event": "guardian",
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "symbol": symbol,
        "action": action,   # tier_move, trail_activate, trail_move, rejection_check, rejection_exit
        "current_r": current_r,
        "peak_r": peak_r,
        "old_sl": old_sl,
        "new_sl": new_sl,
        "detail": detail,
    }
    _write_jsonl(record)
    _write_human(f"GUARD {symbol} {action} R={current_r:.2f} "
                 f"peak={peak_r:.2f} SL {old_sl:.6f}->{new_sl:.6f} {detail}")


# ══════════════════════════════════════════════════════════════
#  EXIT JOURNAL — complete exit record with aftermath request
# ══════════════════════════════════════════════════════════════

def log_exit(symbol: str, side: str, strategy: str, tf: str,
             exit_mode: str, entry_price: float, exit_price: float,
             pnl_r: float, pnl_usd: float, reason: str,
             duration_minutes: float = 0,
             peak_r: float = 0, trough_r: float = 0,
             sl_moves: int = 0, trail_active: bool = False,
             conviction: float = 0, grade: str = "",
             equity_after: float = 0, fees_usd: float = 0,
             sentiment_entry: dict = None, sentiment_exit: dict = None):
    """Full exit journal record. Auto-schedules aftermath tracking."""
    record = {
        "event": "exit",
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "tf": tf,
        "exit_mode": exit_mode,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_r": pnl_r,
        "pnl_usd": pnl_usd,
        "reason": reason,  # tp, sl, trail, rejection, timeout, manual
        "duration_min": duration_minutes,
        "peak_r": peak_r,
        "trough_r": trough_r,
        "sl_moves": sl_moves,
        "trail_active": trail_active,
        "conviction": conviction,
        "grade": grade,
        "equity_after": equity_after,
        "fees_usd": fees_usd,
        "sentiment_at_entry": sentiment_entry or {},
        "sentiment_at_exit": sentiment_exit or {},
        # Aftermath checkpoints to track
        "aftermath_requested": True,
        "aftermath_checkpoints_min": [1, 5, 15, 60],
    }
    _write_jsonl(record)

    emoji = "+" if pnl_r > 0 else ""
    sent_tag = ""
    if sentiment_entry:
        sent_tag = f" mkt_entry={sentiment_entry.get('bias','?')}"
    if sentiment_exit:
        sent_tag += f" mkt_exit={sentiment_exit.get('bias','?')}"
    _write_human(
        f"EXIT {symbol} [{strategy}/{tf}] {reason.upper()} "
        f"{emoji}{pnl_r:.2f}R (${pnl_usd:+.2f}) "
        f"entry={entry_price:.6f} exit={exit_price:.6f} "
        f"peak={peak_r:.2f}R dur={duration_minutes:.0f}m "
        f"conv={conviction:.0f}{grade} eq=${equity_after:.2f}{sent_tag}"
    )


# ══════════════════════════════════════════════════════════════
#  AFTERMATH JOURNAL — what happened AFTER we exited
# ══════════════════════════════════════════════════════════════

def log_aftermath(symbol: str, side: str, exit_price: float,
                  exit_ts_ms: int, reason: str,
                  checkpoints: List[Dict[str, Any]]):
    """
    Log post-exit price movement.
    
    checkpoints: [{"minutes": 1, "price": ..., "move_pct": ..., "move_r": ...}, ...]
    
    This reveals:
      - TP exits: did price continue running? (premature TP)
      - SL exits: did price recover? (unnecessary SL)
      - Trail exits: how much was left on the table?
    """
    # Classify aftermath
    max_favor = 0.0
    max_against = 0.0
    for cp in checkpoints:
        move = cp.get("move_r", 0)
        if side == "long":
            if move > 0:
                max_favor = max(max_favor, move)
            else:
                max_against = min(max_against, move)
        else:
            if move < 0:
                max_favor = max(max_favor, abs(move))
            else:
                max_against = min(max_against, -move)

    if reason == "tp" and max_favor > 0.5:
        verdict = "PREMATURE_TP"  # price kept going in our favor
    elif reason == "sl" and max_favor > 1.0:
        verdict = "UNNECESSARY_SL"  # price recovered significantly
    elif reason == "trail" and max_favor > 0.3:
        verdict = "EARLY_TRAIL"  # trail triggered too early
    elif reason == "sl" and max_against < -0.5:
        verdict = "CORRECT_SL"  # price kept going against us
    elif reason == "tp" and max_against < -0.3:
        verdict = "CORRECT_TP"  # price reversed after our TP
    else:
        verdict = "NEUTRAL"

    record = {
        "event": "aftermath",
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "symbol": symbol,
        "side": side,
        "exit_price": exit_price,
        "exit_ts_ms": exit_ts_ms,
        "exit_reason": reason,
        "checkpoints": checkpoints,
        "max_favor_r": max_favor,
        "max_against_r": max_against,
        "verdict": verdict,
    }
    _write_jsonl(record)
    _write_human(
        f"AFTER {symbol} {reason} -> {verdict} "
        f"favor={max_favor:+.2f}R against={max_against:+.2f}R "
        f"checks={len(checkpoints)}"
    )

    return verdict


# ══════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════

def log_daily_summary(equity: float, peak_equity: float,
                      trades: int, wins: int, losses: int,
                      pnl_r: float, pnl_usd: float,
                      best_trade: dict = None, worst_trade: dict = None,
                      skill_stats: dict = None, learner_insights: dict = None):
    """End-of-day summary for research."""
    record = {
        "event": "daily_summary",
        "ts": _now_iso(),
        "date": _today(),
        "equity": equity,
        "peak_equity": peak_equity,
        "dd_pct": (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "wr_pct": wins / trades * 100 if trades > 0 else 0,
        "pnl_r": pnl_r,
        "pnl_usd": pnl_usd,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "skill_stats": skill_stats or {},
        "learner_insights": learner_insights or {},
    }
    _write_jsonl(record)

    wr = wins / trades * 100 if trades > 0 else 0
    dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
    _write_human(
        f"=== DAILY SUMMARY {_today()} ===\n"
        f"  Equity: ${equity:.2f} (peak ${peak_equity:.2f}, DD {dd:.1f}%)\n"
        f"  Trades: {trades} (W:{wins} L:{losses} WR:{wr:.0f}%)\n"
        f"  PnL: {pnl_r:+.2f}R (${pnl_usd:+.2f})\n"
        f"{'=' * 40}"
    )


# ══════════════════════════════════════════════════════════════
#  SESSION/MILESTONE EVENTS
# ══════════════════════════════════════════════════════════════

def log_event(event_type: str, detail: dict = None, message: str = ""):
    """Generic event logger for milestones, errors, state changes."""
    record = {
        "event": event_type,
        "ts": _now_iso(),
        "ts_ms": _now_ts(),
        "message": message,
        **(detail or {}),
    }
    _write_jsonl(record)
    if message:
        _write_human(f"EVENT[{event_type}] {message}")


# ══════════════════════════════════════════════════════════════
#  JOURNAL READER (for analysis scripts)
# ══════════════════════════════════════════════════════════════

def read_journal(date: str = None, event_types: List[str] = None) -> List[dict]:
    """Read journal entries for analysis."""
    if date is None:
        date = _today()
    path = os.path.join(_JOURNAL_DIR, f"journal_{date}.jsonl")
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if event_types is None or entry.get("event") in event_types:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def read_all_exits(days: int = 30) -> List[dict]:
    """Read all exit records from last N days."""
    from datetime import timedelta
    exits = []
    for i in range(days):
        d = datetime.now(timezone.utc) - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        exits.extend(read_journal(date_str, ["exit"]))
    return exits


def read_all_aftermath(days: int = 30) -> List[dict]:
    """Read all aftermath records for cross-matching research."""
    from datetime import timedelta
    results = []
    for i in range(days):
        d = datetime.now(timezone.utc) - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        results.extend(read_journal(date_str, ["aftermath"]))
    return results
