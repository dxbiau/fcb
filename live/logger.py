"""
live/logger.py — Enhanced structured logging for the FCB live bot.

Three outputs:
  1. Console — coloured, human-readable (INFO+)
  2. File    — timestamped, rotated daily in live/logs/ (DEBUG+)
  3. Audit   — separate file for trades/orders only (live/logs/audit_YYYYMMDD.log)

Features:
  - Order lifecycle tracking (placed → filled/cancelled/error)
  - Exchange API call logging with timing
  - Error categorisation (transient vs critical)
  - Session boundary markers
  - Performance metrics per session
"""

import os, sys, logging, time, functools, json, traceback
from datetime import datetime, timezone
from live.config import LOG_DIR

os.makedirs(LOG_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════
#  ROOT LOGGER
# ═══════════════════════════════════════════════════════════
_logger = logging.getLogger("fcb_bot")
_logger.setLevel(logging.DEBUG)

# Prevent duplicate handlers on re-import
if not _logger.handlers:
    # ── Console handler ──
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%H:%M:%S",
    ))
    _logger.addHandler(_ch)

    # ── File handler (daily rotation) ──
    _log_path = os.path.join(LOG_DIR, f"bot_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_fh)

# ═══════════════════════════════════════════════════════════
#  AUDIT LOGGER (separate file for trades + orders only)
# ═══════════════════════════════════════════════════════════
_audit = logging.getLogger("fcb_audit")
_audit.setLevel(logging.DEBUG)
if not _audit.handlers:
    _audit_path = os.path.join(LOG_DIR, f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log")
    _ah = logging.FileHandler(_audit_path, encoding="utf-8")
    _ah.setLevel(logging.DEBUG)
    _ah.setFormatter(logging.Formatter(
        "%(asctime)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _audit.addHandler(_ah)


# ═══════════════════════════════════════════════════════════
#  CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════
def debug(msg: str):    _logger.debug(msg)
def info(msg: str):     _logger.info(msg)
def warning(msg: str):  _logger.warning(msg)
def error(msg: str):    _logger.error(msg)
def critical(msg: str): _logger.critical(msg)


# ═══════════════════════════════════════════════════════════
#  AUDIT LOG (trades, orders, position changes)
# ═══════════════════════════════════════════════════════════
def audit(event: str, **kwargs):
    """Write to the audit log (separate from main log).

    Usage:
      audit("ORDER_PLACED", symbol="BTC/USDT:USDT", side="buy", qty=0.01)
      audit("ORDER_FILLED", order_id="abc123", fill_price=50123.5)
    """
    parts = [f"{k}={v}" for k, v in kwargs.items()]
    line = f"[{event}] " + " | ".join(parts)
    _audit.info(line)
    _logger.debug(f"AUDIT: {line}")


# ═══════════════════════════════════════════════════════════
#  API CALL LOGGER (wraps exchange calls with timing)
# ═══════════════════════════════════════════════════════════
def log_api_call(func_name: str, **kwargs):
    """Log the start of an API call."""
    _logger.debug(f"API_CALL: {func_name}({', '.join(f'{k}={v}' for k, v in kwargs.items())})")


def log_api_result(func_name: str, elapsed_ms: float, result_summary: str = ""):
    """Log the result and timing of an API call."""
    _logger.debug(f"API_DONE: {func_name} [{elapsed_ms:.0f}ms] {result_summary}")


def log_api_error(func_name: str, elapsed_ms: float, error: Exception):
    """Log an API error with categorisation."""
    err_str = str(error)
    category = _categorize_error(error)
    _logger.error(f"API_ERR: {func_name} [{elapsed_ms:.0f}ms] [{category}] {err_str}")
    _audit.info(f"[API_ERROR] func={func_name} | category={category} | error={err_str}")


def _categorize_error(e: Exception) -> str:
    """Categorise an error as TRANSIENT, AUTH, RATE_LIMIT, or CRITICAL."""
    msg = str(e).lower()

    if isinstance(e, (ConnectionError, TimeoutError)):
        return "TRANSIENT"
    if "rate limit" in msg or "too many" in msg or "429" in msg:
        return "RATE_LIMIT"
    if "api key" in msg or "invalid" in msg and "key" in msg or "auth" in msg:
        return "AUTH"
    if "not modified" in msg or "already" in msg:
        return "IDEMPOTENT"
    if "insufficient" in msg or "balance" in msg:
        return "INSUFFICIENT_BALANCE"
    if "not found" in msg or "does not exist" in msg:
        return "NOT_FOUND"
    if isinstance(e, ccxt.NetworkError):
        return "TRANSIENT"
    if isinstance(e, ccxt.ExchangeError):
        return "EXCHANGE"
    return "UNKNOWN"


# ═══════════════════════════════════════════════════════════
#  TIMED API WRAPPER DECORATOR
# ═══════════════════════════════════════════════════════════
def timed_api(func):
    """Decorator: logs API call timing, args, result/error."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        fname = func.__name__
        # Build arg summary (skip 'ex' which is always first)
        arg_strs = []
        for i, a in enumerate(args[1:], 1):  # skip exchange object
            if isinstance(a, (str, int, float, bool)):
                arg_strs.append(str(a))
        for k, v in kwargs.items():
            if isinstance(v, (str, int, float, bool)):
                arg_strs.append(f"{k}={v}")
        call_summary = ", ".join(arg_strs[:5])  # limit to 5 args

        _logger.debug(f"API→ {fname}({call_summary})")
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = (time.time() - t0) * 1000
            # Summarise result
            if isinstance(result, dict):
                rid = result.get("id", "")
                rstatus = result.get("status", "")
                summary = f"id={rid} status={rstatus}" if rid else f"keys={list(result.keys())[:5]}"
            elif isinstance(result, list):
                summary = f"len={len(result)}"
            elif isinstance(result, (int, float)):
                summary = str(result)
            else:
                summary = type(result).__name__
            _logger.debug(f"API← {fname} [{elapsed:.0f}ms] {summary}")
            return result
        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            cat = _categorize_error(e)
            _logger.error(f"API✗ {fname} [{elapsed:.0f}ms] [{cat}] {e}")
            _audit.info(f"[API_ERROR] func={fname} | cat={cat} | args={call_summary} | err={e}")
            raise
    return wrapper


# ═══════════════════════════════════════════════════════════
#  SESSION BOUNDARY LOGGING
# ═══════════════════════════════════════════════════════════
def session_start(session: str, equity: float, pair_count: int):
    """Log session start marker."""
    _logger.info("━" * 70)
    _logger.info(f"  SESSION {session.upper()} — START")
    _logger.info(f"  Equity: ${equity:.2f} | Pairs: {pair_count}")
    _logger.info(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    _logger.info("━" * 70)
    _audit.info(f"[SESSION_START] session={session} | equity={equity:.2f} | pairs={pair_count}")


def session_end(session: str, equity: float, entries: int, wins: int, losses: int):
    """Log session end marker."""
    _logger.info("─" * 70)
    _logger.info(f"  SESSION {session.upper()} — END")
    _logger.info(f"  Equity: ${equity:.2f} | Entries: {entries} | W/L: {wins}/{losses}")
    _logger.info("─" * 70)
    _audit.info(f"[SESSION_END] session={session} | equity={equity:.2f} | "
                f"entries={entries} | wins={wins} | losses={losses}")


# ═══════════════════════════════════════════════════════════
#  ORDER / POSITION LIFECYCLE LOGGING
# ═══════════════════════════════════════════════════════════
def order_placed(symbol: str, side: str, order_type: str, qty: float,
                 price: float = None, sl: float = None, tp: float = None,
                 order_id: str = "", notes: str = ""):
    """Log order placement to audit trail."""
    audit("ORDER_PLACED",
          symbol=symbol, side=side, type=order_type,
          qty=f"{qty:.6f}", price=f"{price:.6f}" if price else "market",
          sl=f"{sl:.6f}" if sl else "—", tp=f"{tp:.6f}" if tp else "—",
          order_id=order_id, notes=notes)


def order_filled(symbol: str, order_id: str, fill_price: float, qty: float):
    """Log order fill."""
    audit("ORDER_FILLED",
          symbol=symbol, order_id=order_id,
          fill_price=f"{fill_price:.6f}", qty=f"{qty:.6f}")


def order_cancelled(symbol: str, order_id: str, reason: str = ""):
    """Log order cancellation."""
    audit("ORDER_CANCELLED",
          symbol=symbol, order_id=order_id, reason=reason)


def position_opened(symbol: str, direction: str, entry: float, qty: float,
                    sl: float, tp: float, risk_usd: float):
    """Log position open."""
    audit("POSITION_OPENED",
          symbol=symbol, direction=direction,
          entry=f"{entry:.6f}", qty=f"{qty:.6f}",
          sl=f"{sl:.6f}", tp=f"{tp:.6f}",
          risk_usd=f"{risk_usd:.2f}")


def position_closed(symbol: str, direction: str, entry: float, close: float,
                    pnl_r: float, pnl_usd: float, outcome: str):
    """Log position close."""
    audit("POSITION_CLOSED",
          symbol=symbol, direction=direction,
          entry=f"{entry:.6f}", close=f"{close:.6f}",
          pnl_r=f"{pnl_r:+.3f}", pnl_usd=f"{pnl_usd:+.2f}",
          outcome=outcome)


def scale_in_event(symbol: str, event: str, **kwargs):
    """Log scale-in lifecycle events."""
    audit(f"SCALE_{event}", symbol=symbol, **{k: str(v) for k, v in kwargs.items()})


# ═══════════════════════════════════════════════════════════
#  ERROR CLASSIFICATION LOGGING
# ═══════════════════════════════════════════════════════════
def log_exception(context: str, e: Exception, level: str = "error"):
    """Log an exception with full traceback to debug, short message at chosen level."""
    cat = _categorize_error(e)
    short = f"[{cat}] {context}: {e}"
    if level == "warning":
        _logger.warning(short)
    elif level == "critical":
        _logger.critical(short)
    else:
        _logger.error(short)
    _logger.debug(f"TRACEBACK for '{context}':\n{traceback.format_exc()}")
    _audit.info(f"[EXCEPTION] context={context} | category={cat} | error={e}")


# ═══════════════════════════════════════════════════════════
#  HEARTBEAT (periodic health check log)
# ═══════════════════════════════════════════════════════════
_last_heartbeat = 0

def heartbeat(equity: float, pending: int, session: str = ""):
    """Log periodic heartbeat (max once per 5 minutes)."""
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < 300:
        return
    _last_heartbeat = now
    _logger.info(f"♥ Heartbeat | session={session} | equity=${equity:.2f} | "
                 f"pending={pending} | "
                 f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")


# ═══════════════════════════════════════════════════════════
#  IMPORT GUARD (for when ccxt is used in error categorisation)
# ═══════════════════════════════════════════════════════════
try:
    import ccxt as _ccxt_mod
    ccxt = _ccxt_mod
except ImportError:
    ccxt = None  # categorize_error will skip ccxt-specific checks
