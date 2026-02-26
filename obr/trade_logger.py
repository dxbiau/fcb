"""
obr/trade_logger.py -- Structured JSONL trade logger for OBR bot.

Every event (entry, exit, guardian update, session open/close, heartbeat)
is appended to a .jsonl file for post-session analysis.
"""

import json
import os
from datetime import datetime, timezone

from obr import config as cfg


TRADE_LOG_DIR = cfg.LOG_DIR


def _ensure_dir():
    os.makedirs(TRADE_LOG_DIR, exist_ok=True)


def _log_path() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(TRADE_LOG_DIR, f"events_{today}.jsonl")


def _append(event: dict):
    _ensure_dir()
    event["ts"] = datetime.now(timezone.utc).isoformat()
    path = _log_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


# ----------------------------------------------------------
#  Event writers
# ----------------------------------------------------------

def log_entry(symbol: str, direction: str, entry_price: float,
              stop_loss: float, take_profit: float, qty: float,
              dollar_risk: float, risk_per_unit: float,
              session: str, order_id: str = "",
              ob_high: float = 0, ob_low: float = 0,
              ob_open: float = 0, ob_close: float = 0):
    _append({
        "event": "ENTRY",
        "symbol": symbol,
        "direction": direction,
        "entry": entry_price,
        "sl": stop_loss,
        "tp": take_profit,
        "qty": qty,
        "dollar_risk": round(dollar_risk, 4),
        "risk_per_unit": round(risk_per_unit, 6),
        "session": session,
        "order_id": order_id,
        "ob_high": ob_high,
        "ob_low": ob_low,
        "ob_open": ob_open,
        "ob_close": ob_close,
    })


def log_exit(symbol: str, direction: str, entry_price: float,
             exit_price: float, pnl_r: float, pnl_usd: float,
             reason: str, session: str = ""):
    _append({
        "event": "EXIT",
        "symbol": symbol,
        "direction": direction,
        "entry": entry_price,
        "exit": exit_price,
        "pnl_r": round(pnl_r, 4),
        "pnl_usd": round(pnl_usd, 4),
        "reason": reason,
        "session": session,
    })


def log_guardian_update(symbol: str, action: str,
                        old_sl: float = 0, new_sl: float = 0,
                        current_r: float = 0, detail: str = ""):
    _append({
        "event": "GUARDIAN",
        "symbol": symbol,
        "action": action,
        "old_sl": round(old_sl, 6),
        "new_sl": round(new_sl, 6),
        "current_r": round(current_r, 4),
        "detail": detail,
    })


def log_trail_activate(symbol: str, current_r: float,
                       trail_sl: float, price: float):
    _append({
        "event": "TRAIL_ACTIVATE",
        "symbol": symbol,
        "current_r": round(current_r, 4),
        "trail_sl": round(trail_sl, 6),
        "price": price,
    })


def log_heartbeat(equity: float, open_positions: int,
                  session: str = ""):
    _append({
        "event": "HEARTBEAT",
        "equity": round(equity, 2),
        "open": open_positions,
        "session": session,
    })


def log_error(context: str, message: str, symbol: str = ""):
    _append({
        "event": "ERROR",
        "context": context,
        "message": str(message)[:500],
        "symbol": symbol,
    })


# ----------------------------------------------------------
#  Readers (for analysis)
# ----------------------------------------------------------

def read_events(date_str: str = "") -> list:
    """Read events from a specific date's log (default: today)."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(TRADE_LOG_DIR, f"events_{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def trades_today() -> list:
    """Get today's ENTRY + EXIT events."""
    events = read_events()
    return [e for e in events if e.get("event") in ("ENTRY", "EXIT")]


def daily_summary() -> dict:
    """Quick stats from today's events."""
    events = read_events()
    entries = [e for e in events if e.get("event") == "ENTRY"]
    exits = [e for e in events if e.get("event") == "EXIT"]
    wins = sum(1 for e in exits if e.get("pnl_r", 0) > 0)
    losses = sum(1 for e in exits if e.get("pnl_r", 0) <= 0)
    total_r = sum(e.get("pnl_r", 0) for e in exits)
    total_pnl = sum(e.get("pnl_usd", 0) for e in exits)
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "entries": len(entries),
        "exits": len(exits),
        "wins": wins,
        "losses": losses,
        "wr": wins / max(1, wins + losses) * 100,
        "total_r": round(total_r, 4),
        "total_pnl": round(total_pnl, 4),
    }
