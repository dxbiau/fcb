"""
obr/guardian.py -- Position guardian for OBR bot.

Runs as a daemon thread, polling open positions every N seconds.
Implements:
  1. Progressive SL tiers (move SL up as profit grows)
  2. Trailing stop (activate at 1.0R, trail 0.3R behind peak)
  3. Real TP management (exchange TP is set far out; guardian closes at actual TP)
"""

import time
import threading
from datetime import datetime, timezone
from typing import Dict, Optional
from obr.config import (
    PROFIT_TIERS, TRAIL_ENABLED, TRAIL_ACTIVATION_R, TRAIL_DISTANCE_R,
    TRAIL_MIN_MOVE_R, TP_R, GUARDIAN_POLL_SECS,
    REJECTION_EXIT_ENABLED, REJECTION_MIN_PROFIT_R,
    REJECTION_WICK_RATIO, REJECTION_BODY_MAX_RATIO,
    REJECTION_MIN_RANGE_PCT, REJECTION_ENGULF_BODY_RATIO,
)
from obr import exchange as ex_mod
from obr import logger as log


class Guardian:
    """
    Position guardian daemon.

    Monitors open positions and:
      - Moves SL through profit tiers
      - Activates trailing stop at threshold
      - Closes position when trail is hit or TP reached
    """

    def __init__(self, exchange, state, on_position_closed=None):
        self._ex = exchange
        self._state = state
        self._on_closed = on_position_closed  # callback(pair, pnl_r, pnl_usd, reason)
        self._running = False
        self._thread = None
        self._tracked: Dict[str, dict] = {}  # symbol -> tracking state

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Guardian started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Guardian stopped")

    def track_position(self, symbol: str, direction: str, entry_price: float,
                       stop_loss: float, risk_per_unit: float, dollar_risk: float):
        """Register a new position for guardian monitoring."""
        self._tracked[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "current_sl": stop_loss,
            "risk_per_unit": risk_per_unit,
            "dollar_risk": dollar_risk,
            "peak_r": 0.0,
            "trail_active": False,
            "tier_idx": -1,
            "polls": 0,
        }
        log.debug(f"Guardian tracking {symbol} {direction} entry={entry_price} sl={stop_loss}")

    def untrack_position(self, symbol: str):
        """Stop tracking a position (already closed externally)."""
        self._tracked.pop(symbol, None)

    def _run(self):
        while self._running:
            try:
                self._poll_all()
            except Exception as e:
                log.log_exception("Guardian poll", e)
            time.sleep(GUARDIAN_POLL_SECS)

    def _poll_all(self):
        if not self._tracked:
            return

        # --- Batch fetch ALL open positions in ONE API call ---
        try:
            all_positions = ex_mod.get_open_positions(self._ex, symbol=None)
        except Exception as e:
            log.log_exception("Guardian batch fetch", e)
            return

        # Index by symbol+side for fast lookup
        pos_map = {}
        for p in all_positions:
            sym = p.get("symbol", "")
            side = p.get("side", "").lower()
            pos_map[(sym, side)] = p

        for symbol in list(self._tracked.keys()):
            try:
                self._poll_one(symbol, pos_map)
            except Exception as e:
                log.log_exception(f"Guardian {symbol}", e)

    def _poll_one(self, symbol: str, pos_map: dict):
        info = self._tracked.get(symbol)
        if not info:
            return

        info["polls"] += 1

        # Look up position from batch-fetched map (no extra API call)
        pos = pos_map.get((symbol, info["direction"]))

        if pos is None:
            # Position closed externally (SL/TP hit on exchange)
            log.info(f"Guardian: {symbol} position gone -- resolving")
            self._resolve_closed(symbol, info)
            return

        # Get current price
        current_price = float(pos.get("markPrice", 0) or
                              pos.get("lastPrice", 0) or 0)
        if current_price <= 0:
            return

        # Calculate current R
        entry = info["entry_price"]
        rpu = info["risk_per_unit"]
        if info["direction"] == "long":
            current_r = (current_price - entry) / rpu
        else:
            current_r = (entry - current_price) / rpu

        # Update peak
        if current_r > info["peak_r"]:
            info["peak_r"] = current_r

        # Progressive SL tiers
        for ti, (trigger_r, new_sl_r, label) in enumerate(PROFIT_TIERS):
            if ti <= info["tier_idx"]:
                continue
            if current_r >= trigger_r:
                if info["direction"] == "long":
                    new_sl = entry + new_sl_r * rpu
                else:
                    new_sl = entry - new_sl_r * rpu

                if self._is_better_sl(info["direction"], new_sl, info["current_sl"]):
                    result = ex_mod.set_trading_stop(
                        self._ex, symbol, info["direction"], sl_price=new_sl)
                    if result == "CLOSED":
                        self._resolve_closed(symbol, info)
                        return
                    if result:
                        log.info(f"Guardian {symbol}: {label} SL -> {new_sl} "
                                 f"(R={current_r:.2f}, peak={info['peak_r']:.2f})")
                        info["current_sl"] = new_sl
                        info["tier_idx"] = ti

        # ---- 1m rejection / reversal check ----
        if self._check_1m_rejection(symbol, info, current_r):
            log.info(f"Guardian {symbol}: 1m REJECTION exit at R={current_r:.2f} "
                     f"(peak={info['peak_r']:.2f})")
            try:
                ex_mod.close_position(self._ex, symbol)
            except Exception as e:
                log.log_exception(f"Guardian rejection close {symbol}", e)
                # Fall through — tiers/trail still protect
            else:
                self._resolve_closed(symbol, info)
                return

        # Trailing stop
        if TRAIL_ENABLED and current_r >= TRAIL_ACTIVATION_R:
            if not info["trail_active"]:
                info["trail_active"] = True
                log.info(f"Guardian {symbol}: Trail activated at R={current_r:.2f}")

            trail_r = info["peak_r"] - TRAIL_DISTANCE_R
            if info["direction"] == "long":
                trail_sl = entry + trail_r * rpu
            else:
                trail_sl = entry - trail_r * rpu

            if self._is_better_sl(info["direction"], trail_sl, info["current_sl"]):
                # --- Throttle: only send API call if SL moves >= TRAIL_MIN_MOVE_R ---
                sl_move_r = abs(trail_sl - info["current_sl"]) / rpu
                if sl_move_r < TRAIL_MIN_MOVE_R:
                    return  # skip tiny update, wait for bigger move

                result = ex_mod.set_trading_stop(
                    self._ex, symbol, info["direction"], sl_price=trail_sl)
                if result == "CLOSED":
                    self._resolve_closed(symbol, info)
                    return
                if result:
                    log.debug(f"Guardian {symbol}: Trail SL -> {trail_sl:.6f} "
                              f"(peak_r={info['peak_r']:.2f})")
                    info["current_sl"] = trail_sl

    # ------------------------------------------------------------------
    #  1-minute rejection / reversal detection
    # ------------------------------------------------------------------

    def _check_1m_rejection(self, symbol: str, info: dict, current_r: float) -> bool:
        """Check last closed 1m candle for rejection/reversal against position.

        Returns True if a clear reversal signal is detected and the position
        should be closed immediately to protect profits.

        Checks two patterns:
          1. Rejection candle: big wick against direction + small body
          2. Engulfing candle: body engulfs previous candle body, closes against direction
        """
        if not REJECTION_EXIT_ENABLED:
            return False

        # Only check when we're in meaningful profit
        if current_r < REJECTION_MIN_PROFIT_R:
            return False

        # --- Fetch last 2 closed 1m candles ---
        try:
            candles = ex_mod.fetch_latest_candles(
                self._ex, symbol, n=2, timeframe="1m")
        except Exception:
            return False

        if not candles or len(candles) < 2:
            return False

        c = candles[-1]   # most recent closed 1m candle
        prev = candles[-2]

        # Don't re-analyze the same candle
        last_ts = info.get("_last_rej_ts", 0)
        if c["ts"] == last_ts:
            return False
        info["_last_rej_ts"] = c["ts"]

        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        candle_range = h - l
        if candle_range <= 0:
            return False

        # Ignore tiny noise candles
        mid_price = (h + l) / 2
        if mid_price <= 0 or candle_range / mid_price < REJECTION_MIN_RANGE_PCT:
            return False

        body = abs(cl - o)
        upper_wick = h - max(o, cl)
        lower_wick = min(o, cl) - l
        body_ratio = body / candle_range
        direction = info["direction"]

        # ---- Pattern 1: Rejection candle (big wick + small body) ----
        if direction == "long":
            # Bearish rejection: huge upper wick, close near low
            wick_ratio = upper_wick / candle_range
            if (wick_ratio >= REJECTION_WICK_RATIO
                    and body_ratio <= REJECTION_BODY_MAX_RATIO
                    and cl < o):  # bearish close
                log.info(f"  {symbol}: 1m REJECTION candle detected "
                         f"(wick={wick_ratio:.0%}, body={body_ratio:.0%})")
                return True
        else:  # short
            # Bullish rejection: huge lower wick, close near high
            wick_ratio = lower_wick / candle_range
            if (wick_ratio >= REJECTION_WICK_RATIO
                    and body_ratio <= REJECTION_BODY_MAX_RATIO
                    and cl > o):  # bullish close
                log.info(f"  {symbol}: 1m REJECTION candle detected "
                         f"(wick={wick_ratio:.0%}, body={body_ratio:.0%})")
                return True

        # ---- Pattern 2: Engulfing candle (body engulfs prev, against direction) ----
        prev_body_top = max(prev["open"], prev["close"])
        prev_body_bot = min(prev["open"], prev["close"])
        curr_body_top = max(o, cl)
        curr_body_bot = min(o, cl)

        engulfs = (curr_body_top > prev_body_top and
                   curr_body_bot < prev_body_bot and
                   body_ratio >= REJECTION_ENGULF_BODY_RATIO)

        if direction == "long" and cl < o and engulfs:
            log.info(f"  {symbol}: 1m BEARISH ENGULFING detected "
                     f"(body covers {prev_body_bot:.6f}-{prev_body_top:.6f})")
            return True

        if direction == "short" and cl > o and engulfs:
            log.info(f"  {symbol}: 1m BULLISH ENGULFING detected "
                     f"(body covers {prev_body_bot:.6f}-{prev_body_top:.6f})")
            return True

        return False

    def _is_better_sl(self, direction: str, new_sl: float, current_sl: float) -> bool:
        if direction == "long":
            return new_sl > current_sl
        else:
            return new_sl < current_sl

    def _resolve_closed(self, symbol: str, info: dict):
        """Handle position that was closed by exchange SL/TP."""
        entry = info["entry_price"]
        rpu = info["risk_per_unit"]
        dr = info["dollar_risk"]

        # Use Bybit's closed PnL endpoint for REAL trade results
        try:
            import time as _time
            _time.sleep(1.5)  # small delay for settlement to propagate
            records = ex_mod.fetch_closed_pnl(self._ex, symbol, limit=3)

            # Find the matching record (most recent with close to our entry price)
            best = None
            for r in records:
                rec_entry = float(r.get("avgEntryPrice", 0) or 0)
                # Match: entry price within 0.5% of our recorded entry
                if rec_entry > 0 and abs(rec_entry - entry) / entry < 0.005:
                    best = r
                    break

            if best is None and records:
                # Fallback: take most recent record
                best = records[0]

            if best:
                pnl = float(best.get("closedPnl", 0) or 0)
                exit_price = float(best.get("avgExitPrice", 0) or 0)
                pnl_r = pnl / dr if dr > 0 else 0
                reason = "trail" if info.get("trail_active") else ("tp" if pnl > 0 else "sl")
                if self._on_closed:
                    self._on_closed(symbol, pnl_r, pnl, reason, exit_price)
                self._tracked.pop(symbol, None)
                return

        except Exception as e:
            log.warning(f"Guardian resolve {symbol} closedPnl: {e}")

        # Fallback: estimate from peak_r
        if info["peak_r"] >= TP_R * 0.8:
            est_r = TP_R - 0.04
            reason = "tp"
        else:
            est_r = -1.04
            reason = "sl"
        pnl = est_r * dr
        if self._on_closed:
            self._on_closed(symbol, est_r, pnl, reason, 0)

        self._tracked.pop(symbol, None)
