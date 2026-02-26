"""
obr/fcb_bot.py -- Multi-strategy FCB v13 portfolio bot.

Runs the validated v13 portfolio (50 combos, 12 strategies, 3 TFs)
alongside or instead of the NTS single-strategy bot.

Architecture:
  1. Load combo registry (pair/TF/strategy/exit combos)
  2. Wait for shortest-TF candle close (15m)
  3. At each 15m close, also check if 30m / 1H candles just closed
  4. For each TF that closed: scan all pairs, run registered strategies
  5. Match signals against combos, pick best per pair
  6. Execute with correct exit mode (fixed TP or trailing)
  7. Guardian manages progressive SL + exit per-position

Key differences from NTS bot:
  - Multiple strategies per pair (not just NTS)
  - Multiple timeframes (15m, 30m, 1H)
  - Per-combo exit modes (fix1.2→fix3.0, trl1.5→trl2.0)
  - Full-maker fee execution (limit TP orders)
"""

import time
import sys
import os
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from obr import config as cfg
from obr import logger as log
from obr import exchange as ex_mod
from obr.strategies import (
    scan_last_bar, atr as compute_atr, Signal, STRATEGY_REGISTRY,
    generate_ensemble_signals,
)
from obr.combo_registry import ComboRegistry, EXIT_PARAMS
from obr.state import BotState
from obr.guardian import Guardian
from obr.tracker import OBRTracker
from obr import trade_logger as tlog


# ═══════════════════════════════════════════════════════
#  Timeframe helpers
# ═══════════════════════════════════════════════════════

def _tf_minutes(tf: str) -> int:
    """Convert timeframe string to minutes."""
    if tf.endswith("m"):
        return int(tf[:-1])
    elif tf.lower().endswith("h"):
        return int(tf[:-1]) * 60
    return 15  # default


def _tf_to_ccxt(tf: str) -> str:
    """Normalise TF string for ccxt (e.g. '1H' -> '1h')."""
    return tf.lower()


def _is_tf_boundary(tf: str, now: datetime) -> bool:
    """Check if `now` is within 30s after a TF candle close boundary."""
    mins = _tf_minutes(tf)
    total_minute = now.hour * 60 + now.minute
    return (total_minute % mins) == 0 and now.second < 30


# ═══════════════════════════════════════════════════════
#  FCB Bot
# ═══════════════════════════════════════════════════════

class FCBBot:
    """Multi-strategy portfolio bot for FCB v13 deployment."""

    # Minimum candles needed per strategy (includes indicator warmup)
    # SMA200 needs 200, EMA55 needs 55, etc. — 210 is safe for all.
    LOOKBACK = 210

    def __init__(self, combo_file: str = None, auto_start: bool = False):
        self._ex = None
        self._state = BotState()
        self._guardian: Optional[Guardian] = None
        self._tracker = OBRTracker()
        self._registry = ComboRegistry(combo_file)
        self._market_info: Dict[str, dict] = {}
        self._valid_pairs: List[str] = []
        self._day_trades = 0

        # Candle cache: (pair, tf) -> list of OHLCV dicts
        self._candle_cache: Dict[Tuple[str, str], List[dict]] = {}

        # Per-position metadata: symbol -> {combo, exit_params, atr_at_entry, ...}
        self._position_meta: Dict[str, dict] = {}

        if auto_start:
            self.run()

    # ----------------------------------------------------------
    #  Startup
    # ----------------------------------------------------------

    def _connect(self) -> float:
        """Connect to exchange and verify."""
        from obr.logger import C
        log.info("")
        log.header("FCB v13 Multi-Strategy Bot", "🚀")
        log.info(f"  📊 {C.DIM}Portfolio:{C.RESET} {C.BOLD}{C.BCYAN}"
                 f"{self._registry.n_combos} combos{C.RESET}")
        log.info(f"  🔢 {C.DIM}Strategies:{C.RESET} "
                 f"{C.BWHITE}{len(self._registry.all_strategies)}{C.RESET}")
        log.info(f"  ⏱️  {C.DIM}Timeframes:{C.RESET} "
                 f"{C.BWHITE}{sorted(self._registry.all_tfs)}{C.RESET}")
        log.info(f"  💹 {C.DIM}Pairs:{C.RESET} "
                 f"{C.BWHITE}{len(self._registry.all_pairs)}{C.RESET}")

        self._ex = ex_mod.create_exchange()
        equity = ex_mod.get_equity(self._ex)
        self._state.update_equity(equity)

        risk = cfg.get_risk_pct(equity)
        lev = cfg.get_leverage(equity)

        log.info(f"  💰 {C.DIM}Risk:{C.RESET} {C.BGREEN}{risk*100:.1f}%{C.RESET}  "
                 f"⚡ {C.DIM}Leverage:{C.RESET} {C.BYELLOW}{lev}x{C.RESET}")
        log.info(f"  🔗 {C.DIM}Connected to Bybit{C.RESET}  "
                 f"💎 {C.DIM}Equity:{C.RESET} {C.BOLD}{C.BGREEN}${equity:.2f}{C.RESET}")
        log.info(f"  💸 {C.DIM}Fee model:{C.RESET} "
                 f"{C.BCYAN}{'FULL MAKER' if cfg.MAKER_TP_ENABLED else 'CURRENT'}{C.RESET}")
        log.divider()
        return equity

    def _setup_pairs(self):
        """Validate all portfolio pairs against exchange and setup leverage."""
        log.info("Setting up pairs...")
        all_pairs = sorted(self._registry.all_pairs)
        valid = []

        for pair in all_pairs:
            try:
                info = ex_mod.get_market_info(self._ex, pair)
                self._market_info[pair] = info
                lev = cfg.get_leverage(self._state.equity)
                ex_mod.set_leverage(self._ex, pair, lev)
                ex_mod.set_margin_mode(self._ex, pair, "cross")
                valid.append(pair)
            except Exception as e:
                short = pair.split('/')[0]
                log.warning(f"  {short}: skip ({e})")

        self._valid_pairs = valid
        log.info(f"  ✅ {len(valid)}/{len(all_pairs)} pairs active")

    # ----------------------------------------------------------
    #  Candle fetching + OHLCV array conversion
    # ----------------------------------------------------------

    def _fetch_candles_raw(self, pair: str, tf: str) -> List[dict]:
        """Fetch closed candles for a pair+TF, return as list of dicts."""
        try:
            raw = ex_mod.fetch_latest_candles(
                self._ex, pair, self.LOOKBACK, timeframe=_tf_to_ccxt(tf)
            )
            return raw
        except Exception as e:
            log.debug(f"  Fetch {pair} {tf}: {e}")
            return []

    @staticmethod
    def _to_arrays(candles: List[dict]) -> Optional[Tuple]:
        """Convert candle dicts to numpy arrays (o, h, l, c, v)."""
        if len(candles) < 20:
            return None
        o = np.array([c["open"] for c in candles], dtype=float)
        h = np.array([c["high"] for c in candles], dtype=float)
        l = np.array([c["low"] for c in candles], dtype=float)
        c = np.array([c["close"] for c in candles], dtype=float)
        v = np.array([c["volume"] for c in candles], dtype=float)
        return o, h, l, c, v

    # ----------------------------------------------------------
    #  Signal scanning
    # ----------------------------------------------------------

    def _scan_tf(self, tf: str) -> List[Tuple[Signal, dict]]:
        """
        Scan all pairs for a given timeframe. Returns list of
        (signal, combo_dict) tuples ready for execution.
        """
        pairs = self._registry.get_pairs_for_tf(tf)
        valid_pairs = [p for p in pairs if p in self._valid_pairs]

        results = []

        for pair in valid_pairs:
            # Check cooldown, position limit, and pair availability
            session = cfg.current_session_name(
                datetime.now(timezone.utc).hour)
            _max_conc = cfg.get_max_concurrent(self._state.equity)
            if not self._state.can_trade(pair, session,
                                          max_concurrent=_max_conc):
                continue

            # Fetch candles
            candles = self._fetch_candles_raw(pair, tf)
            if not candles:
                continue

            arrays = self._to_arrays(candles)
            if arrays is None:
                continue

            o, h, l, c, v = arrays

            # Get strategies needed for this pair+TF
            combos = self._registry.get_combos(pair, tf)
            if not combos:
                continue

            # Separate base-strategy combos from ensemble combos
            base_combos = [cb for cb in combos
                           if cb["strat"] not in ("ENS2", "ENS3")]
            ens_combos = [cb for cb in combos
                          if cb["strat"] in ("ENS2", "ENS3")]

            # For base combos, only run needed strategies
            strat_names = list(set(cb["strat"] for cb in base_combos))
            # For ensemble combos, run all 12 base strategies
            if ens_combos:
                strat_names = list(STRATEGY_REGISTRY.keys())

            # Scan last bar
            signals = scan_last_bar(o, h, l, c, v,
                                     strategies=strat_names,
                                     maker_fees=cfg.MAKER_TP_ENABLED)

            if not signals and not ens_combos:
                continue

            # Pre-compute ATR for this pair
            atr_arr = compute_atr(h, l, c, 14)
            atr_now = (float(atr_arr[-1]) if not np.isnan(atr_arr[-1])
                       else float(signals[0].stop_dist) if signals else 1.0)

            # Match base signals to base combos
            for sig in signals:
                matching = [cb for cb in base_combos
                            if cb["strat"] == sig.strategy]
                if matching:
                    best_combo = max(matching,
                                     key=lambda x: x.get("val_wr", 0)
                                     * x.get("val_pf", 0))
                    sig.pair = pair
                    sig.tf = tf
                    best_combo["_atr_at_signal"] = atr_now
                    results.append((sig, best_combo))

            # Check ensemble combos (ENS2/ENS3)
            for ens_cb in ens_combos:
                min_agree = int(ens_cb["strat"][-1])  # ENS2->2, ENS3->3
                ens_sigs = generate_ensemble_signals(signals, min_agree)
                for esig in ens_sigs:
                    esig.pair = pair
                    esig.tf = tf
                    ens_cb_copy = dict(ens_cb)
                    ens_cb_copy["_atr_at_signal"] = atr_now
                    results.append((esig, ens_cb_copy))

            time.sleep(0.3)  # rate limit courtesy

        return results

    # ----------------------------------------------------------
    #  Trade execution
    # ----------------------------------------------------------

    def _execute_signal(self, sig: Signal, combo: dict) -> bool:
        """Execute a signal based on its combo's exit mode."""
        pair = sig.pair
        side = "buy" if sig.side == "long" else "sell"
        entry_price = sig.entry
        stop_dist = sig.stop_dist

        # Compute SL price
        if sig.side == "long":
            sl_price = entry_price - stop_dist
        else:
            sl_price = entry_price + stop_dist

        # Exit mode determines TP
        exit_mode = combo.get("exit", "fix2.0")
        exit_params = self._registry.get_exit_params(exit_mode)
        atr_val = combo.get("_atr_at_signal", stop_dist)

        if exit_params["type"] == "fixed":
            tp_r = exit_params["tp_r"]
            if sig.side == "long":
                tp_price = entry_price + stop_dist * tp_r
            else:
                tp_price = entry_price - stop_dist * tp_r
            # For fixed exits, set exchange TP at actual target
            exchange_tp = tp_price
        else:
            # Trailing: set exchange TP far out, guardian manages trail
            tp_r = 10.0  # placeholder (guardian manages actual exit)
            if sig.side == "long":
                tp_price = entry_price + stop_dist * 10.0
            else:
                tp_price = entry_price - stop_dist * 10.0
            exchange_tp = tp_price

        # Risk sizing (same x1000 curves as NTS)
        equity = self._state.equity
        base_risk = cfg.get_risk_pct(equity)
        dd_mult = cfg.get_drawdown_multiplier(equity, self._state.peak_equity)
        risk_pct = min(base_risk * dd_mult, cfg.MAX_RISK_PCT)
        leverage = cfg.get_leverage(equity)

        dollar_risk = equity * risk_pct
        if stop_dist <= 0:
            return False

        # Position size = dollar_risk / stop_distance (in price units)
        qty = dollar_risk / stop_dist

        # Apply leverage-based margin cap
        avail = ex_mod.get_available_balance(self._ex)
        margin_needed = (qty * entry_price) / leverage
        if margin_needed > avail * 0.95:
            qty = (avail * 0.95 * leverage) / entry_price
            dollar_risk = qty * stop_dist

        if dollar_risk < 1.0:
            return False

        # Round to exchange precision
        try:
            qty = ex_mod.round_qty(self._ex, pair, qty)
            sl_price = ex_mod.round_price(self._ex, pair, sl_price)
            exchange_tp = ex_mod.round_price(self._ex, pair, exchange_tp)
        except Exception as e:
            log.warning(f"  {pair}: precision error: {e}")
            return False

        # Validate SL/TP direction
        if side == "buy":
            if exchange_tp <= entry_price or sl_price >= entry_price:
                return False
        else:
            if exchange_tp >= entry_price or sl_price <= entry_price:
                return False

        # Set leverage
        try:
            ex_mod.set_leverage(self._ex, pair, leverage)
        except Exception:
            pass

        # Place order
        from obr.logger import C
        short = pair.split('/')[0]
        log.info(f"  ⚡ {C.BOLD}FCB ENTRY{C.RESET}: "
                 f"{'📈' if sig.side == 'long' else '📉'} "
                 f"{C.BWHITE}{short}{C.RESET} "
                 f"{sig.strategy} @ {sig.tf} | "
                 f"exit={exit_mode} risk=${dollar_risk:.2f}")

        try:
            if cfg.MAKER_ENTRY_ENABLED:
                limit_price = ex_mod.round_price(self._ex, pair, entry_price)
                order = ex_mod.place_limit_order(
                    self._ex, pair, side, qty, limit_price,
                    sl_price, exchange_tp
                )
                if not order:
                    return False

                order_id = order.get("id", "")
                fill_deadline = time.time() + cfg.MAKER_ENTRY_TIMEOUT_SEC
                avg_price = 0.0
                filled = False

                while time.time() < fill_deadline:
                    time.sleep(3)
                    try:
                        status = ex_mod.fetch_order(self._ex, pair, order_id)
                        if status and status.get("status") == "closed":
                            avg_price = float(
                                status.get("average")
                                or status.get("price")
                                or limit_price
                            )
                            filled = True
                            break
                        elif status and status.get("status") == "canceled":
                            return False
                    except Exception:
                        pass

                if not filled:
                    ex_mod.cancel_order(self._ex, pair, order_id)
                    log.info(f"  {short}: Limit unfilled, cancelled")
                    return False
            else:
                order = ex_mod.place_market_order(
                    self._ex, pair, side, qty, sl_price, exchange_tp
                )
                if not order:
                    return False
                avg_price = float(
                    order.get("average")
                    or order.get("price")
                    or entry_price
                )

            # Record in state
            session = cfg.current_session_name(
                datetime.now(timezone.utc).hour)
            entry_data = {
                "direction": sig.side,
                "entry_price": avg_price,
                "stop_loss": float(sl_price),
                "take_profit": float(exchange_tp),
                "exchange_tp": float(exchange_tp),
                "risk_per_unit": stop_dist,
                "dollar_risk": dollar_risk,
                "position_size": float(qty),
                "order_id": order.get("id", ""),
                "ob_high": 0.0,   # no OB candle in FCB
                "ob_low": 0.0,
                "ob_open": 0.0,
                "ob_close": 0.0,
            }
            self._state.record_entry(pair, session, entry_data)

            # Store position metadata for guardian customisation
            self._position_meta[pair] = {
                "combo": combo,
                "exit_mode": exit_mode,
                "exit_params": exit_params,
                "strategy": sig.strategy,
                "tf": sig.tf,
                "atr_at_entry": atr_val,
                "stop_dist": stop_dist,
            }

            # Register with guardian
            self._guardian.track_position(
                symbol=pair,
                direction=sig.side,
                entry_price=avg_price,
                stop_loss=float(sl_price),
                risk_per_unit=stop_dist,
                dollar_risk=dollar_risk,
            )

            log.position_opened(
                pair, sig.side, avg_price, sl_price,
                float(exchange_tp), qty, dollar_risk
            )

            tlog.log_entry(
                symbol=pair, direction=sig.side,
                entry_price=avg_price, stop_loss=float(sl_price),
                take_profit=float(exchange_tp), qty=float(qty),
                dollar_risk=dollar_risk, risk_per_unit=stop_dist,
                session=session, order_id=order.get("id", ""),
                ob_high=0, ob_low=0, ob_open=0, ob_close=0,
            )

            return True

        except Exception as e:
            log.error(f"  {short}: Order failed: {e}")
            return False

    # ----------------------------------------------------------
    #  Position closed callback
    # ----------------------------------------------------------

    def _on_position_closed(self, pair: str, pnl_r: float,
                             pnl_usd: float, reason: str):
        """Called by Guardian when a position is closed."""
        self._position_meta.pop(pair, None)
        from obr.logger import C
        short = pair.split('/')[0]
        color = C.BGREEN if pnl_r >= 0 else C.BRED
        log.info(f"  {'✅' if pnl_r >= 0 else '❌'} "
                 f"{C.BOLD}{short}{C.RESET}: "
                 f"{color}{pnl_r:+.2f}R{C.RESET} "
                 f"(${pnl_usd:+.2f}) [{reason}]")

        # Update equity
        try:
            eq = ex_mod.get_equity(self._ex)
            self._state.update_equity(eq)
        except Exception:
            pass

    # ----------------------------------------------------------
    #  Candle-close waiting
    # ----------------------------------------------------------

    def _wait_for_next_15m(self):
        """Wait until the next 15m candle close + 5s safety margin."""
        now = datetime.now(timezone.utc)
        minute = now.minute
        next_boundary = ((minute // 15) + 1) * 15
        if next_boundary >= 60:
            target = (now + timedelta(hours=1)).replace(
                minute=0, second=5, microsecond=0)
        else:
            target = now.replace(minute=next_boundary, second=5,
                                  microsecond=0)

        wait = (target - now).total_seconds()
        if wait > 0:
            log.debug(f"Waiting {wait:.0f}s for 15m candle close...")
            time.sleep(wait)

    def _tfs_that_just_closed(self) -> List[str]:
        """
        Return which TFs just had a candle close.
        Call this right after the 15m boundary.
        """
        now = datetime.now(timezone.utc)
        total_minute = now.hour * 60 + now.minute
        closed = []

        for tf in sorted(self._registry.all_tfs):
            mins = _tf_minutes(tf)
            if total_minute % mins == 0:
                closed.append(tf)

        return closed if closed else ["15m"]  # 15m always closes

    # ----------------------------------------------------------
    #  Main loop
    # ----------------------------------------------------------

    def run(self):
        """Main FCB bot loop -- multi-TF continuous scanning."""
        equity = self._connect()
        self._setup_pairs()

        # Start guardian
        self._guardian = Guardian(
            exchange=self._ex,
            state=self._state,
            on_position_closed=self._on_position_closed,
        )
        self._guardian.start()

        from obr.logger import C

        # Summary breakdown by TF
        for tf in sorted(self._registry.all_tfs):
            pairs = self._registry.get_pairs_for_tf(tf)
            active = [p for p in pairs if p in self._valid_pairs]
            log.info(f"  ⏱️  {tf}: {len(active)} pairs active")

        log.banner_box([
            f"🌊  FCB v13 MULTI-STRATEGY MODE",
            f"💎  Equity: ${equity:.2f}",
            f"📊  {self._registry.n_combos} combos | "
            f"{len(self._valid_pairs)} pairs",
            f"⏱️   TFs: {sorted(self._registry.all_tfs)}",
            f"💸  Fee model: "
            f"{'FULL MAKER' if cfg.MAKER_TP_ENABLED else 'CURRENT'}",
            f"🚀  Scanning every 15m candle...",
        ], color=C.BGREEN)

        try:
            while True:
                self._state.check_new_day()

                # Reset day counter
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if not hasattr(self, '_current_day') or \
                        self._current_day != today:
                    self._current_day = today
                    self._day_trades = 0

                # Phase-aware daily growth cap
                _eq = self._state.equity
                _pt, _pc, _pl = cfg.get_current_phase(_eq)
                day_growth = self._state.daily_growth_pct
                if _pc > 0 and day_growth >= _pc:
                    log.info(f"  🔥 DAILY CAP: {day_growth:.1f}% "
                             f"(cap={_pc:.0f}%) -- pausing")
                    self._wait_for_next_15m()
                    try:
                        eq = ex_mod.get_equity(self._ex)
                        self._state.update_equity(eq)
                    except Exception:
                        pass
                    continue

                # Wait for next 15m close
                self._wait_for_next_15m()

                # Equity floor check
                try:
                    equity = ex_mod.get_equity(self._ex)
                    self._state.update_equity(equity)
                except Exception:
                    equity = self._state.equity

                peak = self._state.peak_equity
                if peak > 0 and equity / peak < cfg.EQUITY_FLOOR_PCT:
                    log.critical(
                        f"🛑 EQUITY FLOOR: ${equity:.2f} / ${peak:.2f} = "
                        f"{equity/peak*100:.1f}%")
                    time.sleep(300)
                    continue

                # Determine which TFs just closed
                closed_tfs = self._tfs_that_just_closed()
                session = cfg.current_session_name(
                    datetime.now(timezone.utc).hour)

                log.debug(f"🔍 Scanning TFs: {closed_tfs} "
                           f"({len(self._valid_pairs)} pairs)")

                signals_found = 0
                traded_this_cycle: Set[str] = set()

                for tf in closed_tfs:
                    results = self._scan_tf(tf)

                    for sig, combo in results:
                        pair = sig.pair
                        if pair in traded_this_cycle:
                            continue

                        _max_conc = cfg.get_max_concurrent(equity)
                        if self._state.pending_count >= _max_conc:
                            break

                        success = self._execute_signal(sig, combo)
                        if success:
                            traded_this_cycle.add(pair)
                            signals_found += 1
                            self._day_trades += 1

                if signals_found > 0:
                    log.info(
                        f"  ⚡ Scan: {signals_found} entries, "
                        f"{self._day_trades} today, "
                        f"growth: {self._state.daily_growth_pct:+.1f}%"
                    )

                # Heartbeat
                log.heartbeat(equity, self._state.pending_count, session)

        except KeyboardInterrupt:
            log.info("\n👋 FCB Bot stopped by user")
        except Exception as e:
            log.critical(f"💀 Fatal: {e}")
            log.log_exception("fcb_main", e)
        finally:
            if self._guardian:
                self._guardian.stop()


# ═══════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    bot = FCBBot(auto_start=True)
