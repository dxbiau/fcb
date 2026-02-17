"""
live/trades.py — Trade logging to CSV.

Every entry and exit is appended to live/trades.csv for audit trail.
"""

import os, csv
from datetime import datetime, timezone
from live.config import TRADE_LOG
from live import logger as log


HEADER = [
    "timestamp_utc", "symbol", "session", "direction", "action",
    "price", "qty", "sl", "tp", "risk_per_unit", "fee_r",
    "order_id", "equity_before", "equity_after", "notes",
]


def _ensure_file():
    """Create CSV with header if it doesn't exist."""
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)


def log_entry(
    symbol: str, session: str, direction: str,
    price: float, qty: float, sl: float, tp: float,
    risk_per_unit: float, fee_r: float,
    order_id: str, equity: float, notes: str = "",
):
    """Log a trade entry."""
    _ensure_file()
    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        symbol, session, direction, "ENTRY",
        f"{price:.8f}", f"{qty:.6f}", f"{sl:.8f}", f"{tp:.8f}",
        f"{risk_per_unit:.8f}", f"{fee_r:.4f}",
        order_id, f"{equity:.2f}", "", notes,
    ]
    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow(row)
    log.info(f"TRADE LOG: ENTRY {direction} {qty:.4f} {symbol} @ {price:.6f}")


def log_exit(
    symbol: str, session: str, direction: str,
    price: float, qty: float,
    order_id: str, equity_after: float, notes: str = "",
):
    """Log a trade exit."""
    _ensure_file()
    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        symbol, session, direction, "EXIT",
        f"{price:.8f}", f"{qty:.6f}", "", "",
        "", "", order_id, "", f"{equity_after:.2f}", notes,
    ]
    with open(TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow(row)
    log.info(f"TRADE LOG: EXIT {direction} {qty:.4f} {symbol} @ {price:.6f}")
