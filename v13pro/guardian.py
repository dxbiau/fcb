"""
v13pro/guardian.py -- Async position guardian.

Monitors open positions and manages:
  1. Progressive SL tiers (move SL as profit grows)
  2. Trailing stop (activate at R threshold, trail behind peak)
  3. 1m rejection / engulfing exit (protects profits from reversals)
  4. Funding rate monitor (close positions with extreme adverse funding)
  5. Resolves closed positions via Bybit closedPnl endpoint

Fully async — all exchange calls use v13pro.exchange async wrappers.
Runs as an asyncio.Task inside the main bot event loop.
"""

import asyncio
import time
from typing import Callable, Dict, Optional

from v13pro import config as cfg
from v13pro import logger as log
from v13pro import journal
from v13pro.ws_data import WSDataEngine

# Type alias for callback
PositionClosedCallback = Callable  # async def(symbol, pnl_r, pnl_usd, reason, exit_price)


class Guardian:
    """Async position guardian."""

    def __init__(self, exchange, state, ws_data: WSDataEngine,
                 on_position_closed: Optional[PositionClosedCallback] = None):
        self._ex = exchange          # ccxt.pro async exchange
        self._state = state          # BotState
        self._ws = ws_data           # WSDataEngine (for 1m candles)
        self._on_closed = on_position_closed
        self._running = False
        self._tracked: Dict[str, dict] = {}
        self._resolving: set = set()      # symbols currently being resolved (prevent spam)
        self._task: Optional[asyncio.Task] = None
        self._last_funding_check = 0.0    # timestamp of last funding rate sweep
        self._burst = None                # BurstEngine reference (set by bot.py)

    def set_burst_engine(self, burst_engine):
        """Inject burst engine reference for partial TP decisions."""
        self._burst = burst_engine

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="guardian")
        log.info("Guardian started (async)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Guardian stopped")

    def track_position(self, symbol: str, direction: str, entry_price: float,
                       stop_loss: float, risk_per_unit: float, dollar_risk: float,
                       exit_mode: str = "fix1.5",
                       exit_params: dict = None):
        """Register a new position for guardian monitoring."""
        # Per-trade trail overrides (e.g. trl_tight: activate 1R, trail 0.3R)
        ep = exit_params or {}
        self._tracked[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "current_sl": stop_loss,
            "risk_per_unit": risk_per_unit,
            "dollar_risk": dollar_risk,
            "exit_mode": exit_mode,
            "peak_r": 0.0,
            "trail_active": False,
            "tier_idx": -1,
            "polls": 0,
            "_last_rej_ts": 0,
            # Per-trade trail params (override config defaults if present)
            "_trail_activation_r": ep.get("trail_activation_r"),
            "_trail_distance_r": ep.get("trail_distance_r"),
            # Timestamp when tracking started — used to prevent premature
            # resolution of limit orders that haven't filled yet
            "_tracked_at": time.time(),
            "_is_limit": False,  # set True by bot for maker entries
            # Burst engine partial TP — set True after partial close fires
            "_burst_partial_taken": False,
        }
        log.debug(f"Guardian tracking {symbol} {direction} "
                  f"entry={entry_price} sl={stop_loss} exit={exit_mode}")

    def untrack_position(self, symbol: str):
        self._tracked.pop(symbol, None)

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    @property
    def tracked_symbols(self):
        return list(self._tracked.keys())

    # ── Main Loop ─────────────────────────────────────────────

    async def _loop(self):
        while self._running:
            try:
                if self._tracked:
                    await self._poll_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.log_exception("Guardian poll", e)
            await asyncio.sleep(cfg.GUARDIAN_POLL_SECS)

    async def _poll_all(self):
        """Batch-fetch all positions, then check each tracked one."""
        try:
            from v13pro.exchange import get_open_positions
            all_positions = await get_open_positions(self._ex)
        except Exception as e:
            log.log_exception("Guardian batch fetch", e)
            return

        # Index by (symbol, side)
        pos_map = {}
        for p in all_positions:
            sym = p.get("symbol", "")
            side = p.get("side", "").lower()
            pos_map[(sym, side)] = p

        for symbol in list(self._tracked.keys()):
            try:
                await self._poll_one(symbol, pos_map)
            except Exception as e:
                log.log_exception(f"Guardian {symbol}", e)

        # Periodic funding rate check
        now = time.time()
        if now - self._last_funding_check >= cfg.FUNDING_CHECK_INTERVAL:
            self._last_funding_check = now
            await self._check_funding_rates()

    # Minimum grace period (seconds) before guardian will resolve a
    # "position gone" — prevents false resolution caused by API propagation
    # delays after a limit order fills.  _monitor_limit_fill clears _is_limit
    # quickly, so relying on _is_limit alone is insufficient.
    MIN_GRACE_SECS = 45

    async def _poll_one(self, symbol: str, pos_map: dict):
        info = self._tracked.get(symbol)
        if not info:
            return

        info["polls"] += 1

        # Look up from batch fetch
        pos = pos_map.get((symbol, info["direction"]))

        if pos is None:
            # Position closed externally (SL/TP hit)
            if symbol in self._resolving:
                return  # already resolving, don't spam

            age = time.time() - info.get("_tracked_at", 0)

            # Hard minimum grace — ALWAYS wait at least MIN_GRACE_SECS after
            # tracking started (or after limit fill reset _tracked_at).
            # This prevents ghost exits from API propagation delays.
            if age < self.MIN_GRACE_SECS:
                if info["polls"] <= 3:
                    log.debug(f"Guardian: {symbol} no position yet "
                              f"(grace, age={age:.0f}s/{self.MIN_GRACE_SECS}s)")
                return

            # Extended grace for limit orders that haven't filled yet
            if info.get("_is_limit") and age < cfg.MAKER_ENTRY_TIMEOUT_SEC + 10:
                if info["polls"] <= 3:
                    log.debug(f"Guardian: {symbol} no position yet "
                              f"(limit pending, age={age:.0f}s)")
                return  # let _monitor_limit_fill handle it

            self._resolving.add(symbol)
            log.info(f"Guardian: {symbol} position gone -- resolving "
                     f"(age={age:.0f}s, is_limit={info.get('_is_limit')})")
            await self._resolve_closed(symbol, info)
            return

        # Current price
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

        # ── Progressive SL Tiers ──
        from v13pro.exchange import set_trading_stop
        for ti, (trigger_r, new_sl_r, label) in enumerate(cfg.PROFIT_TIERS):
            if ti <= info["tier_idx"]:
                continue
            if current_r >= trigger_r:
                if info["direction"] == "long":
                    new_sl = entry + new_sl_r * rpu
                else:
                    new_sl = entry - new_sl_r * rpu

                if _is_better_sl(info["direction"], new_sl, info["current_sl"]):
                    result = await set_trading_stop(
                        self._ex, symbol, info["direction"], sl_price=new_sl)
                    if result == "CLOSED":
                        await self._resolve_closed(symbol, info)
                        return
                    if result:
                        log.info(f"Guardian {symbol}: {label} SL -> {new_sl:.6f} "
                                 f"(R={current_r:.2f}, peak={info['peak_r']:.2f})")
                        journal.log_guardian_action(
                            symbol, "tier_move",
                            current_r=current_r, peak_r=info["peak_r"],
                            old_sl=info["current_sl"], new_sl=new_sl,
                            detail=label)
                        info["current_sl"] = new_sl
                        info["tier_idx"] = ti

        # ── Burst Partial TP (lock gains before edge decay) ──
        if await self._check_burst_partial_tp(symbol, info, current_r):
            log.info(f"Guardian {symbol}: BURST partial TP fired at R={current_r:.2f}")

        # ── 1m Rejection Exit ──
        if await self._check_1m_rejection(symbol, info, current_r):
            log.info(f"Guardian {symbol}: 1m REJECTION exit R={current_r:.2f}")
            journal.log_guardian_action(
                symbol, "rejection_exit",
                current_r=current_r, peak_r=info["peak_r"],
                old_sl=info["current_sl"], new_sl=0,
                detail="1m_rejection_pattern")
            try:
                from v13pro.exchange import close_position
                await close_position(self._ex, symbol)
            except Exception as e:
                log.log_exception(f"Guardian rejection close {symbol}", e)
            else:
                await self._resolve_closed(symbol, info)
                return

        # ── Trailing Stop ──
        # Use per-trade trail params if available, else fall back to config
        trail_act_r = info.get("_trail_activation_r") or cfg.TRAIL_ACTIVATION_R
        trail_dist_r = info.get("_trail_distance_r") or cfg.TRAIL_DISTANCE_R

        if cfg.TRAIL_ENABLED and current_r >= trail_act_r:
            if not info["trail_active"]:
                info["trail_active"] = True
                log.info(f"Guardian {symbol}: Trail ON at R={current_r:.2f} "
                         f"(act={trail_act_r}, dist={trail_dist_r})")
                journal.log_guardian_action(
                    symbol, "trail_activate",
                    current_r=current_r, peak_r=info["peak_r"],
                    old_sl=info["current_sl"], new_sl=info["current_sl"],
                    detail=f"activated_at_R={current_r:.2f}_act={trail_act_r}_dist={trail_dist_r}")

            trail_r = info["peak_r"] - trail_dist_r
            if info["direction"] == "long":
                trail_sl = entry + trail_r * rpu
            else:
                trail_sl = entry - trail_r * rpu

            if _is_better_sl(info["direction"], trail_sl, info["current_sl"]):
                sl_move_r = abs(trail_sl - info["current_sl"]) / rpu
                if sl_move_r < cfg.TRAIL_MIN_MOVE_R:
                    return  # throttle tiny moves

                result = await set_trading_stop(
                    self._ex, symbol, info["direction"], sl_price=trail_sl)
                if result == "CLOSED":
                    await self._resolve_closed(symbol, info)
                    return
                if result:
                    log.debug(f"Guardian {symbol}: Trail SL -> {trail_sl:.6f} "
                              f"(peak_r={info['peak_r']:.2f})")
                    journal.log_guardian_action(
                        symbol, "trail_move",
                        current_r=current_r, peak_r=info["peak_r"],
                        old_sl=info["current_sl"], new_sl=trail_sl,
                        detail=f"trail_dist={trail_dist_r}")
                    info["current_sl"] = trail_sl

    # ── 1m Rejection Detection ────────────────────────────────

    async def _check_1m_rejection(self, symbol: str, info: dict,
                                   current_r: float) -> bool:
        """Check last 1m candle for reversal patterns."""
        if not cfg.REJECTION_EXIT_ENABLED:
            return False
        if current_r < cfg.REJECTION_MIN_PROFIT_R:
            return False

        # Get 1m candles from WS buffer (no REST call!)
        candles = await self._ws.get_1m_candles(symbol, n=2)
        if not candles or len(candles) < 2:
            return False

        c = candles[-1]
        prev = candles[-2]

        if c["ts"] == info.get("_last_rej_ts", 0):
            return False
        info["_last_rej_ts"] = c["ts"]

        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        candle_range = h - l
        if candle_range <= 0:
            return False

        mid = (h + l) / 2
        if mid <= 0 or candle_range / mid < cfg.REJECTION_MIN_RANGE_PCT:
            return False

        body = abs(cl - o)
        upper_wick = h - max(o, cl)
        lower_wick = min(o, cl) - l
        body_ratio = body / candle_range
        direction = info["direction"]

        # Pattern 1: Rejection candle
        if direction == "long":
            wick_ratio = upper_wick / candle_range
            if (wick_ratio >= cfg.REJECTION_WICK_RATIO
                    and body_ratio <= cfg.REJECTION_BODY_MAX_RATIO
                    and cl < o):
                log.info(f"  {symbol}: 1m REJECTION (wick={wick_ratio:.0%})")
                return True
        else:
            wick_ratio = lower_wick / candle_range
            if (wick_ratio >= cfg.REJECTION_WICK_RATIO
                    and body_ratio <= cfg.REJECTION_BODY_MAX_RATIO
                    and cl > o):
                log.info(f"  {symbol}: 1m REJECTION (wick={wick_ratio:.0%})")
                return True

        # Pattern 2: Engulfing
        prev_top = max(prev["open"], prev["close"])
        prev_bot = min(prev["open"], prev["close"])
        curr_top = max(o, cl)
        curr_bot = min(o, cl)

        engulfs = (curr_top > prev_top and curr_bot < prev_bot
                   and body_ratio >= cfg.REJECTION_ENGULF_BODY_RATIO)

        if direction == "long" and cl < o and engulfs:
            log.info(f"  {symbol}: 1m BEARISH ENGULFING")
            return True
        if direction == "short" and cl > o and engulfs:
            log.info(f"  {symbol}: 1m BULLISH ENGULFING")
            return True

        return False

    # ── Burst Partial TP (gain locking) ─────────────────────

    async def _check_burst_partial_tp(self, symbol: str, info: dict,
                                       current_r: float) -> bool:
        """Take partial profit during BURST windows to lock gains before decay.

        Fires once per position when:
          1. Burst engine exists and state == "BURST"
          2. current_r >= BURST_PARTIAL_TP_R
          3. Haven't already taken partial on this position
        Closes BURST_PARTIAL_TP_PCT of the position via reduceOnly market order.
        """
        if not cfg.BURST_PARTIAL_TP_ENABLED:
            return False
        if not self._burst:
            return False
        if info.get("_burst_partial_taken"):
            return False
        if current_r < cfg.BURST_PARTIAL_TP_R:
            return False

        # Only fire during active BURST state
        try:
            state = self._burst.burst_state
        except Exception:
            return False
        if state != "BURST":
            return False

        # Execute partial close
        try:
            from v13pro.exchange import partial_close_position
            qty_closed = await partial_close_position(
                self._ex, symbol, fraction=cfg.BURST_PARTIAL_TP_PCT)
            if qty_closed > 0:
                info["_burst_partial_taken"] = True
                short_sym = symbol.replace("/USDT:USDT", "")
                log.info(f"Guardian {short_sym}: 🔒 BURST partial TP "
                         f"{cfg.BURST_PARTIAL_TP_PCT*100:.0f}% at R={current_r:.2f} "
                         f"(qty={qty_closed})")
                journal.log_guardian_action(
                    symbol, "burst_partial_tp",
                    current_r=current_r, peak_r=info["peak_r"],
                    old_sl=info["current_sl"], new_sl=info["current_sl"],
                    detail=f"partial_{cfg.BURST_PARTIAL_TP_PCT*100:.0f}pct_at_R={current_r:.2f}")
                return True
        except Exception as e:
            log.warning(f"Guardian burst partial TP {symbol}: {e}")

        return False

    # ── Funding Rate Monitor ─────────────────────────────────

    async def _check_funding_rates(self):
        """Check funding rates on all tracked positions.

        If a position's funding rate is extremely adverse (> EXIT threshold),
        force-close it to prevent silent equity drain.
        Log a warning for moderately adverse rates.
        """
        if not self._tracked:
            return

        from v13pro.exchange import fetch_funding_rate, close_position

        for symbol in list(self._tracked.keys()):
            info = self._tracked.get(symbol)
            if not info:
                continue

            try:
                rate_pct = await fetch_funding_rate(self._ex, symbol)
            except Exception:
                continue

            direction = info["direction"]
            # Positive rate = longs pay; negative rate = shorts pay
            if direction == "long":
                cost_pct = rate_pct       # positive = paying
            else:
                cost_pct = -rate_pct      # negative funding = shorts paying

            # Store for dashboard visibility
            info["funding_rate"] = rate_pct
            info["funding_cost"] = cost_pct

            short_sym = symbol.replace("/USDT:USDT", "")

            if cost_pct >= cfg.FUNDING_RATE_EXIT_PCT:
                # EXTREME adverse funding — force close
                log.warning(
                    f"Guardian {short_sym}: FUNDING EXIT — rate {rate_pct:+.4f}% "
                    f"costs {cost_pct:.4f}% per 8h for {direction}")
                journal.log_guardian_action(
                    symbol, "funding_exit",
                    current_r=info.get("peak_r", 0), peak_r=info.get("peak_r", 0),
                    old_sl=info.get("current_sl", 0), new_sl=0,
                    detail=f"funding_rate={rate_pct:+.4f}%")
                try:
                    await close_position(self._ex, symbol)
                except Exception as e:
                    log.warning(f"Guardian funding close {symbol}: {e}")
                else:
                    info["_funding_exit"] = True
                    await self._resolve_closed(symbol, info)
                continue

            if cost_pct >= cfg.FUNDING_RATE_MAX_PCT:
                # Moderately adverse — just warn
                log.info(
                    f"  Guardian {short_sym}: ⚠ funding {rate_pct:+.4f}% "
                    f"({cost_pct:.4f}% cost/8h for {direction})")

            await asyncio.sleep(0.05)  # rate limit courtesy

    # ── Resolve Closed Position ───────────────────────────────

    async def _resolve_closed(self, symbol: str, info: dict):
        """Resolve a closed position using Bybit closedPnl endpoint."""
        entry = info["entry_price"]
        rpu = info["risk_per_unit"]
        dr = info["dollar_risk"]
        tracked_at_ms = int(info.get("_tracked_at", 0) * 1000)
        age_secs = time.time() - info.get("_tracked_at", 0)

        await asyncio.sleep(2.5)  # wait for settlement (increased from 1.5)

        try:
            from v13pro.exchange import fetch_closed_pnl
            records = await fetch_closed_pnl(self._ex, symbol, limit=10)

            # ---------- strict match: entry price + timestamp ----------
            best = None
            for r in records:
                rec_entry = float(r.get("avgEntryPrice", 0) or 0)
                rec_ts = int(r.get("createdTime", 0) or 0)

                # Skip records that predate our trade
                if tracked_at_ms > 0 and rec_ts > 0 and rec_ts < tracked_at_ms - 5000:
                    log.debug(f"Guardian {symbol}: skip stale closedPnl "
                              f"rec_ts={rec_ts} < tracked_at={tracked_at_ms}")
                    continue

                # Entry price must match within 0.5%
                if rec_entry > 0 and abs(rec_entry - entry) / entry < 0.005:
                    best = r
                    break

            # ---------- NO loose fallback ----------
            # Removed: the old "use most recent record" fallback was the main
            # source of ghost exits.  If entry price doesn't match, don't
            # use that record.

            if best is None:
                # No matching record found
                if age_secs < 180:
                    # Position is young — maybe Bybit hasn't settled yet.
                    # Retry on next poll instead of giving up.
                    log.info(f"Guardian {symbol}: no closedPnl match yet "
                             f"(age={age_secs:.0f}s) — will retry")
                    self._resolving.discard(symbol)
                    return

                # Position is old enough — truly gone with no record.
                # Likely an unfilled limit order or manual close.
                log.warning(f"Guardian {symbol}: no matching closedPnl after "
                            f"{age_secs:.0f}s — untracking (possible unfilled order)")
                self._tracked.pop(symbol, None)
                self._resolving.discard(symbol)
                return

            # ---------- matched record — report outcome ----------
            pnl = float(best.get("closedPnl", 0) or 0)
            exit_price = float(best.get("avgExitPrice", 0) or 0)
            pnl_r = pnl / dr if dr > 0 else 0
            reason = self._infer_reason(info, pnl)
            log.debug(f"Guardian {symbol}: closedPnl match — "
                      f"pnl={pnl:.4f} exit={exit_price} reason={reason}")
            try:
                if self._on_closed:
                    await self._on_closed(symbol, pnl_r, pnl, reason, exit_price)
            except Exception as cb_err:
                log.warning(f"Guardian callback {symbol}: {cb_err}")
            finally:
                self._tracked.pop(symbol, None)
                self._resolving.discard(symbol)
            return

        except Exception as e:
            log.warning(f"Guardian resolve {symbol}: {e}")

        # Fallback estimate — only used when API call itself fails
        if age_secs < 120:
            # Don't estimate for young positions — retry on next poll
            log.info(f"Guardian {symbol}: API error, will retry (age={age_secs:.0f}s)")
            self._resolving.discard(symbol)
            return

        exit_mode = info.get("exit_mode", "fix1.5")
        tp_r_str = exit_mode.replace("fix", "").replace("trl", "").replace("_tight", "")
        try:
            tp_r = float(tp_r_str) if tp_r_str else 1.5
        except ValueError:
            tp_r = 1.5
        if info["peak_r"] >= tp_r * 0.8:
            est_r = tp_r - 0.04
            reason = "tp"
        else:
            est_r = -1.04
            reason = "sl"
        pnl = est_r * dr
        try:
            if self._on_closed:
                await self._on_closed(symbol, est_r, pnl, reason, 0)
        except Exception as cb_err:
            log.warning(f"Guardian fallback callback {symbol}: {cb_err}")
        finally:
            self._tracked.pop(symbol, None)
            self._resolving.discard(symbol)

    @staticmethod
    def _infer_reason(info: dict, pnl: float) -> str:
        if info.get("_funding_exit"):
            return "funding"
        if info.get("trail_active"):
            return "trail"
        if pnl > 0:
            return "tp"
        return "sl"


def _is_better_sl(direction: str, new_sl: float, current_sl: float) -> bool:
    if direction == "long":
        return new_sl > current_sl
    return new_sl < current_sl
