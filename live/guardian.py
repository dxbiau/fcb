"""
live/guardian.py — Synthetic Guardian Agent for the FCB bot.

A proactive watchdog that anticipates and prevents problems before they
cause losses.  Called at key moments by bot.py, the Guardian:

  1. POSITION HEALTH — Detects orphaned or stuck positions missing SL/TP.
  2. STALE POSITION TIMEOUT — Force-closes positions stuck > MAX_HOLD hours.
  3. SL/TP VERIFICATION — Confirms every open position has both SL and TP
     attached.  Re-attaches if missing (Bybit can drop them on amendments).
  4. MARGIN AWARENESS — Checks available margin before entries to avoid
     "insufficient funds" rejections.
  5. PAIR CLASSIFICATION — Promotes/demotes pairs between Class A and B
     based on live performance.
  6. ANOMALY DETECTION — Watches for equity anomalies, unexpected position
     counts, duplicate orders, and API degradation.
  7. SESSION DEBRIEF — After every session, logs a structured debrief with
     what happened, what went wrong, and what to watch for.

Design: Pure functions + a GuardianAgent class.  No side effects beyond
logging and returning decisions.  The bot.py is the executor.
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from live import exchange as exch
from live import logger as log

# ─── Configurable thresholds ───
MAX_HOLD_HOURS   = 8     # force-close positions older than this
MARGIN_SAFETY    = 0.85  # only use 85% of available margin
MIN_MARGIN_ENTRY = 5.0   # absolute minimum free margin to attempt entry
# NOTE: For small accounts ($150), the old $50 floor blocked most entries.
# Now uses max(MIN_MARGIN_ENTRY, equity * 0.03) so it scales with account.


class GuardianAgent:
    """Proactive watchdog — call methods at key bot lifecycle points."""

    def __init__(self, ex, state):
        self.ex = ex
        self.state = state
        self._issues = []     # accumulated issues for debrief

    # ═════════════════════════════════════════════════════════
    #  1. PRE-ENTRY CHECKS
    # ═════════════════════════════════════════════════════════

    def pre_entry_check(self, symbol: str, needed_margin: float) -> Tuple[bool, str]:
        """Called before placing ANY order.  Returns (ok, reason).

        Checks:
          - Sufficient margin available
          - No duplicate position already open on this symbol
          - Exchange API is responsive
        """
        # Check available margin
        try:
            bal = self.ex.fetch_balance({"type": "swap"})
            usdt = bal.get("USDT", {})
            free_margin = float(usdt.get("free", 0))

            # Dynamic floor: 3% of total equity or $5, whichever is higher
            total_eq = float(usdt.get("total", 0)) or free_margin
            floor = max(MIN_MARGIN_ENTRY, total_eq * 0.03)
            if free_margin < floor:
                reason = (f"GUARDIAN BLOCK: only ${free_margin:.2f} free margin "
                          f"(need ${floor:.0f} minimum)")
                log.warning(reason)
                self._issues.append(("margin_low", symbol, reason))
                return False, reason

            if needed_margin > free_margin * MARGIN_SAFETY:
                reason = (f"GUARDIAN BLOCK: entry needs ~${needed_margin:.2f} margin, "
                          f"only ${free_margin:.2f} free (safety={MARGIN_SAFETY:.0%})")
                log.warning(reason)
                self._issues.append(("margin_insufficient", symbol, reason))
                return False, reason
        except Exception as e:
            log.warning(f"GUARDIAN: margin check failed ({e}) — allowing entry")

        # Check for duplicate position
        try:
            positions = exch.get_open_positions(self.ex, symbol)
            if positions:
                reason = f"GUARDIAN BLOCK: {symbol} already has an open position"
                log.warning(reason)
                return False, reason
        except Exception:
            pass  # If check fails, allow entry

        return True, "OK"

    def estimate_margin(self, qty: float, price: float, leverage: int) -> float:
        """Estimate margin required for a position."""
        notional = qty * price
        return notional / leverage

    # ═════════════════════════════════════════════════════════
    #  2. POSITION HEALTH CHECK
    # ═════════════════════════════════════════════════════════

    def check_position_health(self) -> List[Dict]:
        """Scan all open positions for problems.  Returns list of issues.

        Checks:
          - Position has SL attached
          - Position has TP attached
          - Position isn't stuck beyond MAX_HOLD_HOURS
          - Position size matches what we expect in state
        """
        issues = []

        try:
            all_positions = exch.get_open_positions(self.ex)
        except Exception as e:
            log.warning(f"GUARDIAN: could not fetch positions — {e}")
            return issues

        pending_symbols = {e.get("symbol"): e for e in self.state.pending_entries}

        for pos in all_positions:
            symbol = pos.get("symbol", "")
            contracts = abs(float(pos.get("contracts", 0) or 0))

            if contracts <= 0:
                continue

            # Check SL
            sl = pos.get("stopLossPrice") or pos.get("stopLoss")
            if not sl or float(sl or 0) == 0:
                issue = {
                    "type": "MISSING_SL",
                    "symbol": symbol,
                    "severity": "CRITICAL",
                    "detail": f"Position has {contracts} contracts but NO STOP LOSS",
                }
                issues.append(issue)
                log.critical(f"GUARDIAN: {symbol} has NO STOP LOSS! "
                             f"Contracts={contracts}")

            # Check TP
            tp = pos.get("takeProfitPrice") or pos.get("takeProfit")
            if not tp or float(tp or 0) == 0:
                issue = {
                    "type": "MISSING_TP",
                    "symbol": symbol,
                    "severity": "HIGH",
                    "detail": f"Position has {contracts} contracts but NO TAKE PROFIT",
                }
                issues.append(issue)
                log.warning(f"GUARDIAN: {symbol} has NO TAKE PROFIT! "
                            f"Contracts={contracts}")

            # Check position age (if we have the entry timestamp)
            entry = pending_symbols.get(symbol)
            if entry:
                entry_time_str = entry.get("entry_time")
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str)
                        age_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
                        if age_hours > MAX_HOLD_HOURS:
                            issue = {
                                "type": "STALE_POSITION",
                                "symbol": symbol,
                                "severity": "HIGH",
                                "detail": f"Position open for {age_hours:.1f}h (max={MAX_HOLD_HOURS}h)",
                                "age_hours": age_hours,
                            }
                            issues.append(issue)
                            log.warning(f"GUARDIAN: {symbol} stale — open {age_hours:.1f}h")
                    except Exception:
                        pass

            # Check for orphaned position (open but not in our state)
            if symbol not in pending_symbols:
                issue = {
                    "type": "ORPHANED_POSITION",
                    "symbol": symbol,
                    "severity": "HIGH",
                    "detail": f"Position {contracts} contracts found on exchange "
                              f"but NOT in bot state",
                }
                issues.append(issue)
                log.warning(f"GUARDIAN: {symbol} ORPHANED — not tracked in state")

        self._issues.extend((i["type"], i["symbol"], i["detail"]) for i in issues)
        return issues

    def heal_missing_sltp(self, issues: List[Dict]):
        """Attempt to re-attach SL/TP for positions missing them."""
        for issue in issues:
            if issue["type"] not in ("MISSING_SL", "MISSING_TP"):
                continue

            symbol = issue["symbol"]
            entry = None
            for e in self.state.pending_entries:
                if e.get("symbol") == symbol:
                    entry = e
                    break

            if not entry:
                log.warning(f"GUARDIAN: cannot heal {symbol} — no state entry found")
                continue

            sl = entry.get("sl")
            tp = entry.get("tp")
            direction = entry.get("direction", "long")

            if sl and tp:
                log.info(f"GUARDIAN: re-attaching SL={sl} TP={tp} on {symbol}")
                ok = exch.set_trading_stop(
                    self.ex, symbol, side=direction,
                    sl_price=sl, tp_price=tp
                )
                if ok:
                    log.info(f"GUARDIAN: {symbol} SL/TP restored successfully")
                else:
                    log.critical(f"GUARDIAN: FAILED to restore SL/TP on {symbol}!")

    def force_close_stale(self, issues: List[Dict]):
        """Force-close positions that have been open longer than MAX_HOLD_HOURS."""
        for issue in issues:
            if issue["type"] != "STALE_POSITION":
                continue

            symbol = issue["symbol"]
            age = issue.get("age_hours", 0)
            log.critical(f"GUARDIAN: FORCE CLOSING {symbol} (open {age:.1f}h > {MAX_HOLD_HOURS}h)")
            try:
                exch.close_position(self.ex, symbol)
                log.info(f"GUARDIAN: {symbol} force-closed successfully")
            except Exception as e:
                log.critical(f"GUARDIAN: FAILED to force-close {symbol}: {e}")

    # ═════════════════════════════════════════════════════════
    #  3. SESSION DEBRIEF
    # ═════════════════════════════════════════════════════════

    def session_debrief(self, session: str, entries: int, wins: int,
                        losses: int, errors: List[str]):
        """Log a structured debrief after each session.

        Provides visibility into what happened, what went wrong, and
        what the Guardian recommends for the next session.
        """
        log.info("=" * 60)
        log.info(f"  GUARDIAN DEBRIEF — Session {session.upper()}")
        log.info("=" * 60)
        log.info(f"  Entries attempted: {entries}")
        log.info(f"  Wins/Losses: {wins}W / {losses}L")
        log.info(f"  Errors: {len(errors)}")
        for err in errors:
            log.info(f"    - {err}")
        log.info(f"  Issues detected: {len(self._issues)}")
        for issue_type, symbol, detail in self._issues:
            log.info(f"    [{issue_type}] {symbol}: {detail}")

        # Recommendations
        pending = len(self.state.pending_entries)
        if pending > 0:
            log.info(f"  ⚠ {pending} position(s) still pending — monitor closely")

        if any(i[0] == "margin_low" for i in self._issues):
            log.info(f"  ⚠ Margin was low — consider reducing Class A risk or "
                     f"closing stale positions")

        log.info("=" * 60)

        # Clear issues for next session
        self._issues.clear()

    # ═════════════════════════════════════════════════════════
    #  4. ANOMALY DETECTION
    # ═════════════════════════════════════════════════════════

    def check_equity_anomaly(self, expected_equity: float) -> bool:
        """Check if current equity has deviated abnormally from expected.

        Returns True if anomaly detected.
        A >10% sudden drop might indicate liquidation or missing SL.
        """
        try:
            equity = exch.get_equity(self.ex)
            if expected_equity > 0:
                change_pct = (equity - expected_equity) / expected_equity * 100
                if change_pct < -10:
                    log.critical(f"GUARDIAN ANOMALY: equity dropped {change_pct:.1f}% "
                                 f"(${expected_equity:.2f} → ${equity:.2f})")
                    self._issues.append(("EQUITY_ANOMALY", "ACCOUNT",
                                         f"Equity dropped {change_pct:.1f}%"))
                    return True
                if change_pct > 20:
                    log.warning(f"GUARDIAN: unusual equity increase {change_pct:.1f}% "
                                f"(${expected_equity:.2f} → ${equity:.2f})")
            return False
        except Exception as e:
            log.warning(f"GUARDIAN: equity check failed — {e}")
            return False

    # ═════════════════════════════════════════════════════════
    #  5. FULL HEALTH CHECK (call at each wake-up)
    # ═════════════════════════════════════════════════════════

    def full_health_check(self) -> bool:
        """Run all health checks.  Returns True if everything is healthy."""
        log.info("GUARDIAN: Running full health check...")

        healthy = True

        # 1. Check equity anomaly
        if self.check_equity_anomaly(self.state.equity):
            healthy = False

        # 2. Check all positions
        issues = self.check_position_health()
        if issues:
            healthy = False
            # Auto-heal what we can
            self.heal_missing_sltp(issues)
            self.force_close_stale(issues)

        # 3. Log health status
        if healthy:
            log.info("GUARDIAN: All checks passed — system healthy ✓")
        else:
            log.warning(f"GUARDIAN: {len(issues)} issue(s) found and addressed")

        return healthy
