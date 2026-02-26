"""
obr/logger.py -- Aesthetic logging for OBR bot.

Three output channels:
  1. Console  -- INFO+, ANSI colored with emojis
  2. File     -- DEBUG+, daily rotation (obr/logs/bot_YYYYMMDD.log)
  3. Audit    -- trades + orders only (obr/logs/audit_YYYYMMDD.log)
"""

import os
import sys
import logging
import time
import functools
import traceback
from typing import Any, Callable
from datetime import datetime, timezone
from obr.config import LOG_DIR

os.makedirs(LOG_DIR, exist_ok=True)

# Enable ANSI escape processing on Windows 10+
if sys.platform == "win32":
    os.system("")


# ══════════════════════════════════════════════════════════════════
#  ANSI Color Palette
# ══════════════════════════════════════════════════════════════════

class C:
    """ANSI color codes."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    # Foreground
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"

    # Bright
    BRED    = "\033[91m"
    BGREEN  = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE   = "\033[94m"
    BMAGENTA= "\033[95m"
    BCYAN   = "\033[96m"
    BWHITE  = "\033[97m"

    # Background
    BG_RED  = "\033[41m"


# ══════════════════════════════════════════════════════════════════
#  Custom colored console formatter
# ══════════════════════════════════════════════════════════════════

class _ColorFormatter(logging.Formatter):
    """ANSI-colored formatter for console output."""

    LEVEL_MAP = {
        logging.DEBUG:    (C.DIM + C.CYAN,    "    "),
        logging.INFO:     (C.BWHITE,          "    "),
        logging.WARNING:  (C.BYELLOW,         " ⚠️  "),
        logging.ERROR:    (C.BRED,            " ❌ "),
        logging.CRITICAL: (C.BOLD + C.BG_RED + C.WHITE, " 💀 "),
    }

    def format(self, record: logging.LogRecord) -> str:
        style, icon = self.LEVEL_MAP.get(record.levelno, (C.WHITE, "    "))
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        msg = record.getMessage()
        return (f"{C.DIM}{C.CYAN}{ts}{C.RESET}"
                f"{icon}"
                f"{C.DIM}│{C.RESET} {msg}")


# ══════════════════════════════════════════════════════════════════
#  Logger setup
# Force UTF-8 stdout (needed when piped through supervisor on Windows)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════

_logger = logging.getLogger("obr")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

# Console (colored) -- use explicit UTF-8 stream
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_ColorFormatter())
_logger.addHandler(_ch)

# File (plain text, no ANSI)
_today = datetime.now(timezone.utc).strftime("%Y%m%d")
_fh = logging.FileHandler(os.path.join(LOG_DIR, f"bot_{_today}.log"),
                           encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s"))
_logger.addHandler(_fh)

# Audit
_ah = logging.FileHandler(os.path.join(LOG_DIR, f"audit_{_today}.log"),
                           encoding="utf-8")
_ah.setLevel(logging.INFO)
_ah.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
_audit = logging.getLogger("obr.audit")
_audit.setLevel(logging.INFO)
_audit.addHandler(_ah)
_audit.propagate = False


# ══════════════════════════════════════════════════════════════════
#  Convenience functions
# ══════════════════════════════════════════════════════════════════

def debug(msg: str) -> None:
    _logger.debug(msg)

def info(msg: str) -> None:
    _logger.info(msg)

def warning(msg: str) -> None:
    _logger.warning(msg)

def error(msg: str) -> None:
    _logger.error(msg)

def critical(msg: str) -> None:
    _logger.critical(msg)

def audit(event: str, **kwargs: object) -> None:
    parts = [f"[{event}]"] + [f"{k}={v}" for k, v in kwargs.items()]
    _audit.info(" | ".join(parts))


# ══════════════════════════════════════════════════════════════════
#  Styled display helpers
# ══════════════════════════════════════════════════════════════════

def header(title: str, emoji: str = "💎", width: int = 56) -> None:
    """Print a prominent section header."""
    border = f"{C.BMAGENTA}{'━' * width}{C.RESET}"
    pad = max(1, (width - len(title) - 4) // 2)
    info(border)
    info(f"{C.BMAGENTA}┃{C.RESET}{' ' * pad}{emoji} "
         f"{C.BOLD}{C.BWHITE}{title}{C.RESET}"
         f"{' ' * max(1, width - pad - len(title) - 4)}"
         f"{C.BMAGENTA}┃{C.RESET}")
    info(border)


def divider(width: int = 56) -> None:
    info(f"{C.DIM}{'─' * width}{C.RESET}")


def banner_box(lines: list, color: str = C.BCYAN) -> None:
    """Print a box with styled border."""
    width = max(len(line) for line in lines) + 4
    info(f"{color}╔{'═' * width}╗{C.RESET}")
    for line in lines:
        padded = line + " " * (width - len(line) - 2)
        info(f"{color}║{C.RESET} {C.BWHITE}{padded}{C.RESET}{color}║{C.RESET}")
    info(f"{color}╚{'═' * width}╝{C.RESET}")


def kv(label: str, value: str, emoji: str = "", indent: int = 5) -> None:
    """Print a key=value pair with styling."""
    pre = " " * indent
    e = f"{emoji} " if emoji else ""
    info(f"{pre}{e}{C.DIM}{label}={C.RESET}{C.BWHITE}{value}{C.RESET}")


# ══════════════════════════════════════════════════════════════════
#  API timing decorator
# ══════════════════════════════════════════════════════════════════

def _categorize_error(e: Exception) -> str:
    msg = str(e).lower()
    if "rate" in msg or "429" in msg:
        return "RATE_LIMIT"
    if "auth" in msg or "key" in msg or "signature" in msg:
        return "AUTH"
    if "insufficient" in msg or "balance" in msg:
        return "INSUFFICIENT_BALANCE"
    if "not found" in msg or "not exist" in msg:
        return "NOT_FOUND"
    if "timeout" in msg or "timed out" in msg:
        return "TRANSIENT"
    return "EXCHANGE"


def timed_api(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for exchange API calls -- timing, retry on rate limit, error logging."""
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        fname = func.__name__
        max_retries = 3
        backoff = 1.0  # seconds

        for attempt in range(max_retries + 1):
            t0 = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = (time.time() - t0) * 1000
                debug(f"⚡ {C.DIM}API {fname} "
                      f"{C.GREEN}OK{C.RESET}{C.DIM} ({elapsed:.0f}ms){C.RESET}")
                return result
            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                cat = _categorize_error(e)

                # Retry on rate limit (up to max_retries)
                if cat == "RATE_LIMIT" and attempt < max_retries:
                    wait = backoff * (2 ** attempt)  # 1s, 2s, 4s
                    debug(f"⏳ {C.DIM}API {fname} rate-limited, "
                          f"retry {attempt + 1}/{max_retries} in {wait:.0f}s{C.RESET}")
                    time.sleep(wait)
                    continue

                # Timeouts and network errors are transient — log as debug
                if cat in ("EXCHANGE", "TRANSIENT"):
                    debug(f"⚠️  {C.DIM}API {fname} {cat} "
                          f"({elapsed:.0f}ms): {e}{C.RESET}")
                else:
                    error(f"❌ API {fname} {C.BRED}FAILED{C.RESET} "
                          f"[{cat}] ({elapsed:.0f}ms): {e}")
                audit("API_ERROR", func=fname, category=cat, error=str(e)[:200])
                raise
    return wrapper


# ══════════════════════════════════════════════════════════════════
#  Trade lifecycle loggers
# ══════════════════════════════════════════════════════════════════

def order_placed(symbol: str, side: str, order_type: str, qty: float,
                 **kwargs: object) -> None:
    audit("ORDER", symbol=symbol, side=side, type=order_type,
          qty=str(qty), **{k: str(v) for k, v in kwargs.items()})


def order_cancelled(symbol: str, order_id: str, reason: str = "") -> None:
    audit("CANCEL", symbol=symbol, order_id=order_id, reason=reason)


def position_opened(symbol: str, direction: str, entry: float, sl: float,
                    tp: float, qty: float, risk_usd: float) -> None:
    short = symbol.split("/")[0]
    arrow = "📈" if direction == "long" else "📉"
    dc    = C.BGREEN if direction == "long" else C.BRED

    info("")
    info(f"  {arrow} {C.BOLD}{dc}OPEN {direction.upper()}{C.RESET} "
         f"{C.BOLD}{C.BWHITE}{short}{C.RESET} "
         f"{C.DIM}@{C.RESET} {C.BCYAN}{entry}{C.RESET}")
    info(f"     🛡️  SL={C.BRED}{sl}{C.RESET}  "
         f"🎯 TP={C.BGREEN}{tp}{C.RESET}  "
         f"💎 Qty={C.BYELLOW}{qty}{C.RESET}  "
         f"💰 Risk={C.BMAGENTA}${risk_usd:.2f}{C.RESET}")
    info("")

    audit("POSITION_OPEN", symbol=symbol, dir=direction, entry=str(entry),
          sl=str(sl), tp=str(tp), qty=str(qty), risk=f"{risk_usd:.2f}")


def position_closed(symbol: str, direction: str, entry: float,
                    exit_price: float, pnl_r: float, pnl_usd: float,
                    reason: str) -> None:
    short = symbol.split("/")[0]

    if pnl_r > 0:
        icon, tc, tag = "🏆", C.BGREEN, "WIN"
    else:
        icon, tc, tag = "💔", C.BRED, "LOSS"

    info("")
    info(f"  {icon} {C.BOLD}{tc}CLOSE [{tag}]{C.RESET} "
         f"{C.BOLD}{C.BWHITE}{short}{C.RESET} "
         f"{C.DIM}{direction.upper()}{C.RESET}")
    info(f"     Entry={C.BCYAN}{entry}{C.RESET}  "
         f"Exit={C.BCYAN}{exit_price}{C.RESET}")
    info(f"     {C.BOLD}R={tc}{pnl_r:+.2f}{C.RESET}  "
         f"PnL={tc}${pnl_usd:+.2f}{C.RESET}  "
         f"{C.DIM}({reason}){C.RESET}")
    info("")

    audit("POSITION_CLOSE", symbol=symbol, dir=direction,
          entry=str(entry), exit=str(exit_price),
          pnl_r=f"{pnl_r:.2f}", pnl_usd=f"{pnl_usd:.2f}", reason=reason)


# ══════════════════════════════════════════════════════════════════
#  Heartbeat
# ══════════════════════════════════════════════════════════════════

_last_heartbeat: float = 0

def heartbeat(equity: float, pending: int, session: str) -> None:
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < 300:
        return
    _last_heartbeat = now

    ec = C.BGREEN if equity >= 50 else C.BYELLOW
    pc = C.BGREEN if pending > 0 else C.DIM

    info(f"💓 {C.DIM}HEARTBEAT{C.RESET} │ "
         f"Equity={ec}${equity:.2f}{C.RESET} │ "
         f"Open={pc}{pending}{C.RESET} │ "
         f"Session={C.BCYAN}{session}{C.RESET}")

    audit("HEARTBEAT", equity=f"{equity:.2f}", open=str(pending),
          session=session)


# ══════════════════════════════════════════════════════════════════
#  Exception helper
# ══════════════════════════════════════════════════════════════════

def log_exception(context: str, e: Exception, level: str = "error") -> None:
    cat = _categorize_error(e)
    msg = f"💀 [{cat}] {context}: {e}"
    getattr(_logger, level)(msg)
    debug(f"Traceback:\n{traceback.format_exc()}")
