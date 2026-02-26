"""
v13pro/trade_logger.py -- JSONL trade event logger.
"""
import json
import os
from datetime import datetime, timezone
from v13pro import config as cfg

def _ensure():
    os.makedirs(cfg.LOG_DIR, exist_ok=True)

def _path():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(cfg.LOG_DIR, f"events_{today}.jsonl")

def _append(evt):
    _ensure()
    evt["ts"] = datetime.now(timezone.utc).isoformat()
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(evt, default=str) + "\n")

def log_entry(symbol, direction, entry_price, stop_loss, take_profit,
              qty, dollar_risk, risk_per_unit, session, order_id="",
              strategy="", tf="", exit_mode="", **kw):
    _append({"event": "ENTRY", "symbol": symbol, "direction": direction,
             "entry": entry_price, "sl": stop_loss, "tp": take_profit,
             "qty": qty, "dollar_risk": round(dollar_risk, 4),
             "risk_per_unit": round(risk_per_unit, 6), "session": session,
             "order_id": order_id, "strategy": strategy, "tf": tf,
             "exit_mode": exit_mode, **kw})

def log_exit(symbol, direction, entry_price, exit_price, pnl_r, pnl_usd,
             reason, session="", **kw):
    _append({"event": "EXIT", "symbol": symbol, "direction": direction,
             "entry": entry_price, "exit": exit_price,
             "pnl_r": round(pnl_r, 4), "pnl_usd": round(pnl_usd, 4),
             "reason": reason, "session": session, **kw})

def log_guardian(symbol, action, old_sl=0, new_sl=0, current_r=0, detail=""):
    _append({"event": "GUARDIAN", "symbol": symbol, "action": action,
             "old_sl": round(old_sl, 6), "new_sl": round(new_sl, 6),
             "current_r": round(current_r, 4), "detail": detail})

def log_signal(symbol, strategy, tf, side, entry, sl, tp, exit_mode, dry=False):
    _append({"event": "SIGNAL", "symbol": symbol, "strategy": strategy,
             "tf": tf, "side": side, "entry": entry, "sl": sl, "tp": tp,
             "exit_mode": exit_mode, "dry_run": dry})
