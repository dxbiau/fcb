"""
obr/bot.py -- Outside Bar Reversal live trading bot (24/7 continuous mode).

Main loop:
  1. Connect to Bybit, verify balance
  2. Setup leverage + margin on all pairs
  3. Continuous 24/7 scanning:
     - Every candle close: scan all 15 pairs for OBR signals
     - On signal: compute trade, place market order with SL/TP
     - Guardian daemon manages progressive SL + trailing stop
     - Daily growth cap: stop new entries once equity hits target pct
  4. Never sleeps between sessions -- monitors market around the clock
"""

import time
import sys
import os
import csv
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from obr import config as cfg
from obr import logger as log
from obr import exchange as ex_mod
from obr.strategy_nts import (
    CandleData, OBRSignal, TradeSignal,
    scan_for_signal, compute_trade,
    compute_1h_trend, check_trend_alignment, check_volume_spike,
)
from obr.state import BotState
from obr.guardian import Guardian
from obr.tracker import OBRTracker
from obr import trade_logger as tlog
from obr.pair_hunter import PairHunter
from obr.ws_cache import WSCandleCache
from obr.skill import PerformanceSkill
from obr.regime import RegimeCache


class OBRBot:
    """Outside Bar Reversal live trading bot."""

    def __init__(self, auto_start: bool = False):
        self._ex = None
        self._state = BotState()
        self._guardian = None
        self._tracker = OBRTracker()
        self._market_info: Dict[str, dict] = {}
        self._valid_pairs: List[str] = []
        self._hunter: Optional[PairHunter] = None
        self._ws_cache: Optional[WSCandleCache] = None
        self._skill = PerformanceSkill()
        self._regime_cache = RegimeCache()
        self._day_trades = 0
        self._last_scan_count = 0

        if auto_start:
            self.run()

    # ----------------------------------------------------------
    #  Startup
    # ----------------------------------------------------------

    def _connect(self):
        """Connect to exchange and verify."""
        from obr.logger import C
        log.info("")
        log.header("OBR Bot  --  Outside Bar Reversal", "🚀")
        log.info(f"  🕯️  {C.DIM}Strategy:{C.RESET} Fade extreme outside bars (contrarian)")
        log.info(f"  📊 {C.DIM}Signal TF:{C.RESET} {C.BOLD}{C.BCYAN}{cfg.SIGNAL_TIMEFRAME}{C.RESET}  "
                 f"⏱️  {C.DIM}Exec TF:{C.RESET} {C.BWHITE}{cfg.TIMEFRAME}{C.RESET}")

        # x1000 mode: show dynamic curves instead of hardcoded values
        self._ex = ex_mod.create_exchange()
        equity = ex_mod.get_equity(self._ex)
        self._state.update_equity(equity)

        risk = cfg.get_risk_pct(equity)
        lev = cfg.get_leverage(equity)
        dd_mult = cfg.get_drawdown_multiplier(equity, self._state.peak_equity)
        phase_target, phase_cap, phase_label = cfg.get_current_phase(equity)
        max_conc = cfg.get_max_concurrent(equity)

        log.info(f"  💰 {C.DIM}Risk:{C.RESET} {C.BGREEN}{risk*100:.1f}%{C.RESET} "
                 f"{C.DIM}(curve){C.RESET}  "
                 f"⚡ {C.DIM}Leverage:{C.RESET} {C.BYELLOW}{lev}x{C.RESET} "
                 f"{C.DIM}(curve){C.RESET}  "
                 f"🎯 {C.DIM}TP:{C.RESET} {C.BCYAN}dynamic{C.RESET}")
        log.info(f"  🌊 {C.DIM}Mode:{C.RESET} {C.BOLD}{C.BGREEN}24/7 x1000{C.RESET}  "
                 f"📊 {C.DIM}Phase cap:{C.RESET} {C.BYELLOW}{phase_cap:.0f}%/day{C.RESET}  "
                 f"{C.DIM}DD mult:{C.RESET} {C.BCYAN}{dd_mult:.2f}{C.RESET}")
        log.info(f"  🔒 {C.DIM}Max conc:{C.RESET} {C.BWHITE}{max_conc}{C.RESET} "
                 f"{C.DIM}(curve){C.RESET}  "
                 f"⏰ {C.DIM}Cooldown:{C.RESET} {C.BWHITE}{cfg.PAIR_COOLDOWN_MINUTES}m{C.RESET}  "
                 f"🚫 {C.DIM}Loss pause:{C.RESET} {C.BRED}{cfg.PAIR_LOSS_COOLDOWN_COUNT}L→{cfg.PAIR_LOSS_COOLDOWN_HOURS}h{C.RESET}")
        log.info(f"  🏁 {C.DIM}Phase:{C.RESET} {C.BOLD}{C.BWHITE}{phase_label}{C.RESET}")
        log.divider()

        log.info(f"  🔗 {C.DIM}Connected to Bybit{C.RESET}  "
                 f"💎 {C.DIM}Equity:{C.RESET} {C.BOLD}{C.BGREEN}${equity:.2f}{C.RESET}")

        if equity < 5.0:
            log.critical(f"🛑 Equity too low: ${equity:.2f} -- aborting")
            sys.exit(1)

        return equity

    def _setup_pairs(self):
        """Setup leverage and margin for all pairs, filter invalid ones."""
        pairs = cfg.all_pairs()
        valid = []
        # Use dynamic leverage based on current equity (Mod 2)
        _leverage = cfg.get_leverage(self._state.equity)

        for pair in pairs:
            try:
                if pair not in self._ex.markets:
                    log.debug(f"  {pair} -- not on exchange, skipping")
                    continue

                ex_mod.set_leverage(self._ex, pair, _leverage)
                ex_mod.set_margin_mode(self._ex, pair, "isolated")

                info = ex_mod.get_market_info(self._ex, pair)
                self._market_info[pair] = info
                valid.append(pair)
                time.sleep(0.1)  # rate limit courtesy

            except ValueError as e:
                log.info(f"  {pair} -- excluded: {e}")
            except Exception as e:
                log.warning(f"  {pair} -- setup failed: {e}")

        self._valid_pairs = valid
        from obr.logger import C
        log.info(f"  ✅ {C.BGREEN}{len(valid)}{C.RESET}{C.DIM}/{len(pairs)} pairs ready{C.RESET}")

    def _startup_report(self, equity: float):
        """Print startup summary."""
        from obr.logger import C
        lt = self._state.lifetime_summary()
        wr_c = C.BGREEN if lt['wr'] >= 50 else C.BYELLOW if lt['wr'] >= 40 else C.BRED
        r_c = C.BGREEN if lt['total_r'] >= 0 else C.BRED
        log.info("")
        log.info(f"  📊 {C.BOLD}{C.BWHITE}LIFETIME{C.RESET}  "
                 f"{C.DIM}Trades:{C.RESET}{C.BWHITE}{lt['total_trades']}{C.RESET}  "
                 f"{C.DIM}WR:{C.RESET}{wr_c}{lt['wr']:.1f}%{C.RESET}  "
                 f"{C.DIM}R:{C.RESET}{r_c}{lt['total_r']:+.2f}{C.RESET}  "
                 f"{C.DIM}Eq:{C.RESET}{C.BGREEN}${equity:.2f}{C.RESET}  "
                 f"{C.DIM}Peak:{C.RESET}{C.BCYAN}${lt['peak']:.2f}{C.RESET}  "
                 f"{C.DIM}DD:{C.RESET}{C.BYELLOW}{lt['dd']:.1f}%{C.RESET}")

        # Show tracker dashboard
        dash = self._tracker.get_dashboard(equity)
        if dash:
            log.info(dash)

    # ----------------------------------------------------------
    #  Restore guardian positions after restart
    # ----------------------------------------------------------

    def _restore_guardian_positions(self):
        """
        Re-register any open positions with the Guardian after a restart.
        Exchange-side SL/TP protect positions during downtime, but the Guardian
        needs to re-track them for progressive SL and trailing stops.
        """
        pending = self._state.pending_entries
        if not pending:
            return

        from obr.logger import C
        log.info(f"  🔄 {C.BOLD}{C.BYELLOW}Restoring {len(pending)} "
                 f"position(s) to Guardian{C.RESET}")

        restored = 0
        orphaned = []

        for entry in pending:
            symbol = entry.get("symbol", "")
            direction = entry.get("direction", "")
            if not symbol or not direction:
                continue

            # Verify position still exists on exchange
            try:
                positions = ex_mod.get_open_positions(self._ex, symbol)
                found = False
                for p in positions:
                    side = p.get("side", "").lower()
                    contracts = abs(float(p.get("contracts", 0) or 0))
                    if side == direction and contracts > 0:
                        found = True
                        break

                if not found:
                    log.info(f"  ⚠️  {C.DIM}{symbol} -- position gone "
                             f"(SL/TP hit during downtime){C.RESET}")
                    orphaned.append(symbol)
                    continue

                # Re-register with guardian
                self._guardian.track_position(
                    symbol=symbol,
                    direction=direction,
                    entry_price=float(entry.get("entry_price", 0)),
                    stop_loss=float(entry.get("stop_loss", 0)),
                    risk_per_unit=float(entry.get("risk_per_unit", 0)),
                    dollar_risk=float(entry.get("dollar_risk", 0)),
                )
                restored += 1
                short = symbol.split("/")[0]
                dc = C.BGREEN if direction == "long" else C.BRED
                log.info(f"    ✅ {dc}{direction.upper()}{C.RESET} "
                         f"{C.BWHITE}{short}{C.RESET} "
                         f"{C.DIM}@ {entry.get('entry_price', '?')}{C.RESET}")

                time.sleep(0.15)  # rate limit

            except Exception as e:
                log.warning(f"  ⚠️  {symbol}: restore check failed: {e}")

        # Clean up orphaned entries (position closed during downtime)
        for sym in orphaned:
            # Record as loss (conservative — exchange SL was the last defense)
            entry_data = None
            for p in pending:
                if p.get("symbol") == sym:
                    entry_data = p
                    break
            dollar_risk = float(entry_data.get("dollar_risk", 0)) if entry_data else 0
            self._state.record_outcome(
                sym, pnl_r=-1.04, pnl_usd=-(dollar_risk * 1.04),
                exit_reason="sl_during_restart", entry_data=entry_data)
            log.info(f"    💔 {C.DIM}{sym}: recorded as SL during restart{C.RESET}")

        if restored > 0:
            log.info(f"  🛡️  {C.BGREEN}{restored} position(s) restored to Guardian{C.RESET}")

    # ----------------------------------------------------------
    #  Session management
    # ----------------------------------------------------------

    def _current_session(self) -> Optional[str]:
        """Which session is active right now."""
        hour = datetime.now(timezone.utc).hour
        return cfg.current_session_name(hour)

    def _wait_for_candle_close(self):
        """Wait until the next SIGNAL timeframe candle close + 5s safety margin.

        The 5-second margin ensures Bybit has finalised the closed candle
        before we fetch, preventing us from acting on mid-formation pins.
        """
        # We wait on the SIGNAL timeframe (5m), not the execution timeframe (1m)
        tf = cfg.SIGNAL_TIMEFRAME
        if tf.endswith("m"):
            interval = int(tf[:-1])
        elif tf.endswith("h"):
            interval = int(tf[:-1]) * 60
        else:
            interval = 5  # fallback to 5 minutes

        now = datetime.now(timezone.utc)
        minute = now.minute
        next_boundary = ((minute // interval) + 1) * interval
        if next_boundary >= 60:
            target = (now + timedelta(hours=1)).replace(
                minute=0, second=5, microsecond=0)
        else:
            target = now.replace(minute=next_boundary, second=5, microsecond=0)

        wait = (target - now).total_seconds()
        if wait > 0:
            log.debug(f"Waiting {wait:.0f}s for {tf} candle close...")
            time.sleep(wait)

    # ----------------------------------------------------------
    #  Candle fetching
    # ----------------------------------------------------------

    def _fetch_candles(self, symbol: str, n: int = 5,
                        timeframe: str = None) -> List[CandleData]:
        """Fetch last N closed candles as CandleData objects.
        Uses WebSocket cache first, falls back to REST API."""
        tf = timeframe or cfg.SIGNAL_TIMEFRAME

        # Try WebSocket cache first (zero API calls)
        if self._ws_cache and tf == cfg.SIGNAL_TIMEFRAME:
            ws_raw = self._ws_cache.get_candles(symbol, n)
            if ws_raw:
                candles = []
                for c in ws_raw:
                    candles.append(CandleData(
                        timestamp=str(c["ts"]),
                        open=c["open"], high=c["high"],
                        low=c["low"], close=c["close"],
                        volume=c.get("volume", 0),
                    ))
                return candles

        # Fallback to REST API
        try:
            raw = ex_mod.fetch_latest_candles(self._ex, symbol, n, timeframe=tf)
            candles = []
            for c in raw:
                candles.append(CandleData(
                    timestamp=str(c["ts"]),
                    open=c["open"],
                    high=c["high"],
                    low=c["low"],
                    close=c["close"],
                    volume=c["volume"],
                ))
            return candles
        except Exception as e:
            log.debug(f"Fetch candles {symbol}: {e}")
            return []

    # ----------------------------------------------------------
    #  Signal scanning
    # ----------------------------------------------------------

    def _scan_pair(self, symbol: str, session: str) -> Optional[TradeSignal]:
        """Scan a single pair for OBR signal and compute trade if found."""
        # Mod 4+10: pass dynamic max_concurrent and phase daily cap
        _eq = self._state.equity
        _phase_target, _phase_cap, _phase_label = cfg.get_current_phase(_eq)
        _max_conc = cfg.get_max_concurrent(_eq)
        if not self._state.can_trade(symbol, session, max_concurrent=_max_conc,
                                     daily_cap=_phase_cap):
            return None

        candles = self._fetch_candles(symbol, cfg.LOOKBACK_CANDLES,
                                       timeframe=cfg.SIGNAL_TIMEFRAME)
        if len(candles) < 3:
            return None

        # Skip flat candles (H == L)
        for c in candles[-3:]:
            if c.high == c.low:
                return None

        signal = scan_for_signal(symbol, candles)
        if signal is None:
            return None

        # ── HTF TREND ALIGNMENT FILTER ──
        # Fetch enough 5m candles to derive 1H SMA (50 * 12 = 600 bars)
        if cfg.HTF_TREND_ENABLED:
            htf_candles = self._fetch_candles(symbol, cfg.HTF_CANDLES_NEEDED,
                                               timeframe=cfg.SIGNAL_TIMEFRAME)
            if len(htf_candles) >= 24:
                trend = compute_1h_trend(htf_candles, cfg.HTF_SMA_PERIOD)
                short_name = symbol.split('/')[0]
                if not check_trend_alignment(signal.signal_type, trend):
                    trend_label = {1: "BULL", -1: "BEAR", 0: "NEUTRAL"}.get(trend, "?")
                    log.debug(f"  {short_name}: HTF trend {trend_label} rejects "
                              f"{'LONG' if signal.signal_type == 2 else 'SHORT'}")
                    return None

        # ── VOLUME SPIKE FILTER ──
        # Need enough candles for volume lookback + the OB candle
        if cfg.VOLUME_FILTER_ENABLED:
            vol_candles = self._fetch_candles(symbol,
                                              cfg.VOLUME_LOOKBACK + 5,
                                              timeframe=cfg.SIGNAL_TIMEFRAME)
            if len(vol_candles) >= cfg.VOLUME_LOOKBACK + 3:
                if not check_volume_spike(vol_candles, ob_index=-2,
                                          lookback=cfg.VOLUME_LOOKBACK,
                                          threshold=cfg.VOLUME_SPIKE_THRESHOLD):
                    short_name = symbol.split('/')[0]
                    log.debug(f"  {short_name}: Volume spike filter rejected "
                              f"(vol < {cfg.VOLUME_SPIKE_THRESHOLD}x avg{cfg.VOLUME_LOOKBACK})")
                    return None

        # Get current price for entry
        try:
            ticker = ex_mod.get_ticker(self._ex, symbol)
            current_price = float(ticker.get("last", 0) or 0)
        except Exception:
            current_price = candles[-1].close

        if current_price <= 0:
            return None

        # ── Conviction scoring (PerformanceSkill) ──
        candle_dicts = [{"ts": c.timestamp, "open": c.open, "high": c.high,
                         "low": c.low, "close": c.close, "volume": c.volume}
                        for c in candles]
        ob_d = candle_dicts[-2] if len(candle_dicts) >= 3 else candle_dicts[-1]
        prev_d = candle_dicts[-3] if len(candle_dicts) >= 3 else candle_dicts[0]
        confirm_d = candle_dicts[-1]

        # Fetch deeper history for key-level detection (30 candles, WS first)
        deep_candles = self._fetch_candles(symbol, 30, timeframe=cfg.SIGNAL_TIMEFRAME)
        if len(deep_candles) >= 10:
            candle_dicts_deep = [{"ts": c.timestamp, "open": c.open, "high": c.high,
                                  "low": c.low, "close": c.close, "volume": c.volume}
                                 for c in deep_candles]
        else:
            candle_dicts_deep = candle_dicts

        # Quick fee_r estimate for scoring
        sl_price = signal.stop_loss_price
        rpu = abs(current_price - sl_price)
        est_fee_r = (cfg.FEE_RATE * 2 * current_price) / rpu if rpu > 0 else 9.0

        # Mod 9: Compute x1000 context for Bayesian features
        _eq = self._state.equity
        _regime = self._regime_cache.get(symbol)
        _, _, _phase_label = cfg.get_current_phase(_eq)
        _dd_mult = cfg.get_drawdown_multiplier(_eq, self._state.peak_equity)
        _dd_zone = ("normal" if _dd_mult >= 0.9 else "caution" if _dd_mult >= 0.5
                    else "defensive" if _dd_mult >= 0.25 else "emergency")

        skill_result = self._skill.evaluate(
            ob_candle=ob_d, prev_candle=prev_d, confirm_candle=confirm_d,
            direction=signal.direction, fee_r=est_fee_r,
            current_price=current_price, candles=candle_dicts_deep,
            symbol=symbol,
            market_regime=_regime, equity_phase=_phase_label,
            drawdown_zone=_dd_zone,
        )

        from obr.logger import C
        short_name = symbol.split('/')[0]
        grade = skill_result['grade']
        score = skill_result['score']

        if not skill_result["pass"]:
            log.debug(f"  {short_name}: Skill REJECT {grade} ({score:.0f}) "
                      f"< {skill_result['min_conviction']:.0f}")
            return None

        bayes_adj = skill_result.get('bayes_adjustment', 0)
        adj_label = f" bayes={bayes_adj:+.0f}" if bayes_adj != 0 else ""
        log.info(f"  🧠 {C.BOLD}{short_name}{C.RESET} conviction "
                 f"{C.BCYAN}{grade}{C.RESET} ({score:.0f}/100) "
                 f"{C.DIM}min={skill_result['min_conviction']:.0f}{adj_label}{C.RESET}")

        # Get market info for precision
        info = self._market_info.get(symbol, {})
        price_prec = int(info.get("price_precision", 4))
        qty_prec = int(info.get("amount_precision", 3))
        min_qty = info.get("min_qty", 0.001) or 0.001
        min_notional = info.get("min_notional", 5.0) or 5.0

        equity = self._state.equity
        avail_bal = ex_mod.get_available_balance(self._ex)

        # ── x1000 RISK CHAIN ──
        # Mod 1: Dynamic risk curve
        base_risk = cfg.get_risk_pct(equity)
        # Mod 3: Conviction multiplier
        conv_mult = cfg.get_conviction_mult(grade)
        # Mod 6: Drawdown throttle
        dd_mult = cfg.get_drawdown_multiplier(equity, self._state.peak_equity)
        # Final risk with absolute cap
        risk_pct = min(base_risk * conv_mult * dd_mult, cfg.MAX_RISK_PCT)
        # Mod 2: Dynamic leverage
        leverage = cfg.get_leverage(equity)
        # Mod 10: Dynamic max concurrent
        max_concurrent = cfg.get_max_concurrent(equity)
        # Mod 5: Regime detection (cached per-symbol)
        regime = self._regime_cache.get(symbol)
        # Mod 7: Dynamic TP (conviction + regime adjusted)
        pair_tp = cfg.get_dynamic_tp(cfg.get_pair_tp(symbol), grade, regime)

        log.info(f"  ⚙️  RISK_CHAIN: {short_name} risk={risk_pct*100:.1f}% "
                 f"(base={base_risk*100:.0f}% ×conv={conv_mult:.2f} "
                 f"×dd={dd_mult:.2f}) lev={leverage}x tp={pair_tp:.2f}R "
                 f"regime={regime}")

        trade = compute_trade(
            signal=signal,
            current_price=current_price,
            equity=equity,
            risk_pct=risk_pct,
            price_precision=price_prec,
            qty_precision=qty_prec,
            min_qty=min_qty,
            min_notional=min_notional,
            tp_r=pair_tp,
            fixed_risk_usd=cfg.FIXED_RISK_USD,
            max_positions=max_concurrent,
            leverage=leverage,
            available_balance=avail_bal,
        )

        if trade is None:
            log.debug(f"  {symbol}: Signal found but trade invalid "
                      f"(risk too small, margin cap, or below min notional)")
            return None

        # Log when margin cap reduced the target risk
        target_risk = equity * risk_pct
        if cfg.FIXED_RISK_USD > 0:
            target_risk = cfg.FIXED_RISK_USD
        if trade.dollar_risk < target_risk * 0.95:
            log.info(f"  ⚠️  {symbol}: Margin-capped risk "
                     f"${trade.dollar_risk:.2f} (target ${target_risk:.2f})")

        return trade

    # ----------------------------------------------------------
    #  Trade execution
    # ----------------------------------------------------------

    def _execute_trade(self, trade: TradeSignal, session: str) -> bool:
        """Execute a trade on the exchange."""
        symbol = trade.symbol
        side = "buy" if trade.direction == "long" else "sell"

        # Mod 2: Set dynamic leverage before each trade
        try:
            _lev = cfg.get_leverage(self._state.equity)
            ex_mod.set_leverage(self._ex, symbol, _lev)
        except Exception:
            pass  # fallback: leverage from last setup

        # Round to exchange precision
        try:
            qty = ex_mod.round_qty(self._ex, symbol, trade.position_size)
            sl = ex_mod.round_price(self._ex, symbol, trade.stop_loss)
            tp = ex_mod.round_price(self._ex, symbol, trade.take_profit)
            # Set exchange TP far out (guardian manages actual TP)
            exchange_tp = ex_mod.round_price(self._ex, symbol, trade.exchange_tp)
        except Exception as e:
            log.warning(f"  {symbol}: precision error: {e}")
            return False

        # Validate TP/SL direction (exchange rejects wrong-side TP)
        if side == "buy":
            if exchange_tp <= trade.entry_price:
                log.warning(f"  {symbol}: exchange_tp {exchange_tp} <= entry {trade.entry_price} for BUY, skipping")
                return False
            if sl >= trade.entry_price:
                log.warning(f"  {symbol}: SL {sl} >= entry {trade.entry_price} for BUY, skipping")
                return False
        else:
            if exchange_tp >= trade.entry_price:
                log.warning(f"  {symbol}: exchange_tp {exchange_tp} >= entry {trade.entry_price} for SELL, skipping")
                return False
            if sl <= trade.entry_price:
                log.warning(f"  {symbol}: SL {sl} <= entry {trade.entry_price} for SELL, skipping")
                return False

        try:
            if cfg.LIMIT_ENTRY_ENABLED:
                # LIMIT ORDER ENTRY: place at signal price for maker fees
                limit_price = ex_mod.round_price(self._ex, symbol, trade.entry_price)
                order = ex_mod.place_limit_order(
                    self._ex, symbol, side, qty, limit_price, sl, exchange_tp)
                
                if not order:
                    return False

                order_id = order.get("id", "")
                
                # Wait for fill with timeout
                fill_deadline = time.time() + cfg.LIMIT_ENTRY_TIMEOUT_SEC
                avg_price = 0.0
                filled = False
                
                while time.time() < fill_deadline:
                    time.sleep(3)  # check every 3 seconds
                    try:
                        status = ex_mod.fetch_order(self._ex, symbol, order_id)
                        if status and status.get("status") == "closed":
                            avg_price = float(status.get("average") or 
                                            status.get("price") or limit_price)
                            filled = True
                            break
                        elif status and status.get("status") == "canceled":
                            log.info(f"  {symbol}: Limit order was canceled")
                            return False
                    except Exception:
                        pass
                
                if not filled:
                    # Timeout — cancel unfilled order
                    ex_mod.cancel_order(self._ex, symbol, order_id)
                    log.info(f"  {symbol}: Limit order unfilled after "
                             f"{cfg.LIMIT_ENTRY_TIMEOUT_SEC}s, cancelled")
                    return False
            else:
                # MARKET ORDER: original logic
                order = ex_mod.place_market_order(
                    self._ex, symbol, side, qty, sl, exchange_tp)

                if not order:
                    return False

                avg_price = float(order.get("average") or order.get("price") or
                                  trade.entry_price)

            # Record entry in state
            entry_data = {
                "direction": trade.direction,
                "entry_price": avg_price,
                "stop_loss": float(sl),
                "take_profit": float(tp),
                "exchange_tp": float(exchange_tp),
                "risk_per_unit": trade.risk_per_unit,
                "dollar_risk": trade.dollar_risk,
                "position_size": float(qty),
                "order_id": order.get("id", ""),
                "ob_high": trade.ob_candle.high,
                "ob_low": trade.ob_candle.low,
                "ob_open": trade.ob_candle.open,
                "ob_close": trade.ob_candle.close,
            }
            self._state.record_entry(symbol, session, entry_data)

            # Register with guardian
            self._guardian.track_position(
                symbol=symbol,
                direction=trade.direction,
                entry_price=avg_price,
                stop_loss=float(sl),
                risk_per_unit=trade.risk_per_unit,
                dollar_risk=trade.dollar_risk,
            )

            log.position_opened(symbol, trade.direction, avg_price, sl,
                                trade.take_profit, qty, trade.dollar_risk)

            # Log to JSONL event stream
            tlog.log_entry(
                symbol=symbol, direction=trade.direction,
                entry_price=avg_price, stop_loss=float(sl),
                take_profit=trade.take_profit, qty=float(qty),
                dollar_risk=trade.dollar_risk,
                risk_per_unit=trade.risk_per_unit,
                session=session, order_id=order.get("id", ""),
                ob_high=trade.ob_candle.high, ob_low=trade.ob_candle.low,
                ob_open=trade.ob_candle.open, ob_close=trade.ob_candle.close,
            )

            # Log to CSV
            self._log_trade_csv(trade, avg_price, session)

            return True

        except Exception as e:
            log.error(f"  {symbol}: Order failed: {e}")
            return False

    def _log_trade_csv(self, trade: TradeSignal, fill_price: float, session: str):
        """Append entry to trades CSV."""
        os.makedirs(os.path.dirname(cfg.TRADE_LOG) or ".", exist_ok=True)
        exists = os.path.exists(cfg.TRADE_LOG)
        with open(cfg.TRADE_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow([
                    "time", "pair", "session", "direction", "entry", "sl", "tp",
                    "risk_per_unit", "dollar_risk", "qty", "fee_r",
                    "ob_h", "ob_l", "ob_o", "ob_c",
                ])
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                trade.symbol, session, trade.direction,
                fill_price, trade.stop_loss, trade.take_profit,
                trade.risk_per_unit, trade.dollar_risk, trade.position_size,
                round(trade.fee_r, 4),
                trade.ob_candle.high, trade.ob_candle.low,
                trade.ob_candle.open, trade.ob_candle.close,
            ])

    # ----------------------------------------------------------
    #  Process hunted pair (from PairHunter)
    # ----------------------------------------------------------

    def _process_hunted(self, hunt_result: dict, session: str) -> bool:
        """
        Take a PairHunter result dict and execute the trade.
        The hunter already confirmed the OBR signal -- we just need to:
        1) Setup leverage/margin for this pair (first time only)
        2) Build CandleData + OBRSignal from hunt result
        3) compute_trade() → _execute_trade()
        """
        sym = hunt_result["symbol"]

        # Check state constraints
        # Mod 4+10: pass dynamic max_concurrent and phase daily cap
        _eq = self._state.equity
        _phase_target, _phase_cap, _phase_label = cfg.get_current_phase(_eq)
        _max_conc = cfg.get_max_concurrent(_eq)
        if not self._state.can_trade(sym, session, max_concurrent=_max_conc,
                                     daily_cap=_phase_cap):
            return False

        # Dynamic setup: leverage + margin (first time only for hunted pairs)
        _leverage = cfg.get_leverage(_eq)
        if sym not in self._market_info:
            try:
                if sym not in self._ex.markets:
                    return False
                ex_mod.set_leverage(self._ex, sym, _leverage)
                ex_mod.set_margin_mode(self._ex, sym, "isolated")
                info = ex_mod.get_market_info(self._ex, sym)
                self._market_info[sym] = info
                # Add to WS cache for real-time tracking
                if self._ws_cache:
                    self._ws_cache.add_symbol(sym)
            except Exception as e:
                log.debug(f"  {sym}: hunter setup failed: {e}")
                return False

        # Build CandleData from hunt result dicts
        ob = hunt_result["ob_candle"]
        prev = hunt_result["prev_candle"]
        ob_candle = CandleData(
            timestamp=str(ob.get("ts", "")),
            open=ob["open"], high=ob["high"],
            low=ob["low"], close=ob["close"],
            volume=ob.get("volume", 0),
        )
        prev_candle = CandleData(
            timestamp=str(prev.get("ts", "")),
            open=prev["open"], high=prev["high"],
            low=prev["low"], close=prev["close"],
            volume=prev.get("volume", 0),
        )

        # Build signal
        signal = OBRSignal(
            symbol=sym,
            direction=hunt_result["direction"],
            ob_candle=ob_candle,
            prev_candle=prev_candle,
            signal_type=hunt_result["signal_type"],
        )

        # Get current price
        try:
            ticker = ex_mod.get_ticker(self._ex, sym)
            current_price = float(ticker.get("last", 0) or 0)
        except Exception:
            current_price = hunt_result["entry_est"]

        if current_price <= 0:
            return False

        # ── Conviction scoring (PerformanceSkill) ──
        confirm = hunt_result.get("confirm_candle", {})
        candle_dicts = [prev, ob, confirm]  # minimal 3

        # Try fetching deeper history for key-level detection (WS cache first)
        deep_candles = self._fetch_candles(sym, 30, timeframe=cfg.SIGNAL_TIMEFRAME)
        if len(deep_candles) >= 10:
            candle_dicts_deep = [{"ts": c.timestamp, "open": c.open, "high": c.high,
                                  "low": c.low, "close": c.close, "volume": c.volume}
                                 for c in deep_candles]
        else:
            candle_dicts_deep = candle_dicts

        # Mod 9: Compute x1000 context for Bayesian features (hunted)
        _h_eq = self._state.equity
        _h_regime = self._regime_cache.get(sym)
        _, _, _h_phase = cfg.get_current_phase(_h_eq)
        _h_dd = cfg.get_drawdown_multiplier(_h_eq, self._state.peak_equity)
        _h_dd_zone = ("normal" if _h_dd >= 0.9 else "caution" if _h_dd >= 0.5
                      else "defensive" if _h_dd >= 0.25 else "emergency")

        skill_result = self._skill.evaluate(
            ob_candle=ob, prev_candle=prev, confirm_candle=confirm,
            direction=hunt_result["direction"],
            fee_r=hunt_result.get("fee_r", 0.15),
            current_price=current_price,
            candles=candle_dicts_deep,
            symbol=sym,
            market_regime=_h_regime, equity_phase=_h_phase,
            drawdown_zone=_h_dd_zone,
        )

        from obr.logger import C
        short_h = sym.split('/')[0]
        grade = skill_result["grade"]
        score = skill_result["score"]

        if not skill_result["pass"]:
            log.debug(f"  {short_h}: Skill REJECT {grade} ({score:.0f}) "
                      f"< {skill_result['min_conviction']:.0f}")
            return False

        bayes_adj = skill_result.get('bayes_adjustment', 0)
        adj_label = f" bayes={bayes_adj:+.0f}" if bayes_adj != 0 else ""
        log.info(f"  🧠 {C.BOLD}{short_h}{C.RESET} conviction "
                 f"{C.BCYAN}{grade}{C.RESET} ({score:.0f}/100) "
                 f"{C.DIM}hunted | min={skill_result['min_conviction']:.0f}{adj_label}{C.RESET}")

        # Market info
        info = self._market_info.get(sym, {})
        price_prec = int(info.get("price_precision", 4))
        qty_prec = int(info.get("amount_precision", 3))
        min_qty = info.get("min_qty", 0.001) or 0.001
        min_notional = info.get("min_notional", 5.0) or 5.0

        equity = self._state.equity
        avail_bal = ex_mod.get_available_balance(self._ex)

        # ── x1000 RISK CHAIN (hunted) ──
        base_risk = cfg.get_risk_pct(equity)
        conv_mult = cfg.get_conviction_mult(grade)
        dd_mult = cfg.get_drawdown_multiplier(equity, self._state.peak_equity)
        risk_pct = min(base_risk * conv_mult * dd_mult, cfg.MAX_RISK_PCT)
        leverage = cfg.get_leverage(equity)
        max_concurrent = cfg.get_max_concurrent(equity)
        regime = self._regime_cache.get(sym)
        pair_tp = cfg.get_dynamic_tp(cfg.get_pair_tp(sym), grade, regime)

        log.info(f"  ⚙️  RISK_CHAIN: {short_h} risk={risk_pct*100:.1f}% "
                 f"lev={leverage}x tp={pair_tp:.2f}R regime={regime} (hunted)")

        trade = compute_trade(
            signal=signal,
            current_price=current_price,
            equity=equity,
            risk_pct=risk_pct,
            price_precision=price_prec,
            qty_precision=qty_prec,
            min_qty=min_qty,
            min_notional=min_notional,
            tp_r=pair_tp,
            fixed_risk_usd=cfg.FIXED_RISK_USD,
            max_positions=max_concurrent,
            leverage=leverage,
            available_balance=avail_bal,
        )

        if trade is None:
            log.debug(f"  {sym}: hunted signal rejected by compute_trade "
                      f"(risk too small, margin cap, or below min notional)")
            return False

        from obr.logger import C
        short = sym.split('/')[0]
        log.info(f"  🎯 {C.BOLD}HUNTED{C.RESET} {C.BWHITE}{short}{C.RESET} "
                 f"OB={hunt_result['ob_range_pct']:.2f}% "
                 f"fee={hunt_result['fee_r']:.2f}R")

        success = self._execute_trade(trade, session)
        if success:
            dc = C.BGREEN if trade.direction == 'long' else C.BRED
            arrow = '📈' if trade.direction == 'long' else '📉'
            log.info(f"  {arrow} {C.BOLD}ENTRY #{self._day_trades}{C.RESET}: "
                     f"{dc}{trade.direction.upper()}{C.RESET} "
                     f"{C.BWHITE}{short}{C.RESET} "
                     f"{C.DIM}(hunted) | Day growth:{C.RESET} "
                     f"{C.BGREEN}{self._state.daily_growth_pct:+.1f}%{C.RESET}")
        return success

    # ----------------------------------------------------------
    #  Position resolution callback (from guardian)
    # ----------------------------------------------------------

    def _on_position_closed(self, symbol: str, pnl_r: float,
                            pnl_usd: float, reason: str,
                            exit_price: float = 0):
        """Called by guardian when a position is resolved."""
        # Find entry data
        entry_data = None
        for p in self._state.pending_entries:
            if p.get("symbol") == symbol:
                entry_data = p
                break

        self._state.record_outcome(symbol, pnl_r, pnl_usd, reason, entry_data)

        # Feed outcome to agentic skill loop
        self._skill.record_outcome(symbol, pnl_r, pnl_usd)

        direction = entry_data.get("direction", "?") if entry_data else "?"
        entry_price = entry_data.get("entry_price", 0) if entry_data else 0

        log.position_closed(symbol, direction, entry_price, exit_price,
                            pnl_r, pnl_usd, reason)

        # Log to JSONL event stream
        tlog.log_exit(
            symbol=symbol, direction=direction,
            entry_price=entry_price, exit_price=exit_price,
            pnl_r=pnl_r, pnl_usd=pnl_usd,
            reason=reason,
        )

        # Record to growth tracker
        eq = self._state.equity
        session_label = cfg.current_session_name(datetime.now(timezone.utc).hour)
        self._tracker.record_session(
            equity=eq, session=session_label,
            trades=1, wins=1 if pnl_r > 0 else 0,
            losses=0 if pnl_r > 0 else 1,
            r_total=pnl_r,
        )

        # Update equity
        try:
            eq = ex_mod.get_equity(self._ex)
            self._state.update_equity(eq)
        except Exception:
            pass

    # ----------------------------------------------------------
    #  Main loop
    # ----------------------------------------------------------

    def run(self):
        """Main bot loop -- 24/7 continuous scanning."""
        equity = self._connect()
        self._setup_pairs()
        self._startup_report(equity)

        # Start guardian daemon
        self._guardian = Guardian(
            exchange=self._ex,
            state=self._state,
            on_position_closed=self._on_position_closed,
        )
        self._guardian.start()

        # Restore any positions that survived a restart
        self._restore_guardian_positions()

        # Start pair hunter (scans full Bybit market for A+ OBR setups)
        if cfg.HUNTER_ENABLED:
            self._hunter = PairHunter(self._ex)
            from obr.logger import C
            log.info(f"  🎯 {C.DIM}Pair Hunter{C.RESET} {C.BGREEN}enabled{C.RESET} "
                     f"{C.DIM}(max {cfg.HUNTER_MAX_RESULTS} results/cycle){C.RESET}")
        else:
            from obr.logger import C

        # Start WebSocket candle cache (eliminates REST calls for static pairs)
        try:
            self._ws_cache = WSCandleCache(
                api_key=cfg.API_KEY,
                api_secret=cfg.API_SECRET,
                timeframe=cfg.SIGNAL_TIMEFRAME,
                max_candles=35,  # 30+ for skill key-level detection
            )
            self._ws_cache.start(self._valid_pairs)
            ws_count = len(self._ws_cache.cached_symbols)
            log.info(f"  📡 {C.DIM}WebSocket cache{C.RESET} {C.BGREEN}active{C.RESET} "
                     f"{C.DIM}({ws_count} pairs streaming){C.RESET}")
        except Exception as e:
            log.warning(f"  📡 WSCache failed to start: {e} -- falling back to REST")
            self._ws_cache = None

        log.info(f"  \U0001f6e1\ufe0f  {C.DIM}Guardian daemon{C.RESET} {C.BGREEN}started{C.RESET}")
        skill_label = f"min_conv={self._skill.min_conviction:.0f}"
        log.info(f"  🧠 {C.DIM}PerformanceSkill{C.RESET} {C.BGREEN}active{C.RESET} "
                 f"{C.DIM}({skill_label}){C.RESET}")
        learner_n = self._skill.learner.total_updates
        log.info(f"  📊 {C.DIM}BayesianLearner{C.RESET} {C.BGREEN}active{C.RESET} "
                 f"{C.DIM}({learner_n} outcomes learned){C.RESET}")
        log.info("")
        hunter_label = f"+ Hunter" if cfg.HUNTER_ENABLED else ""
        ws_label = "+ WS" if self._ws_cache else ""
        _phase_t, _phase_c, _phase_l = cfg.get_current_phase(equity)
        log.banner_box([
            f"\U0001f30a  24/7 x1000 MODE ACTIVE",
            f"\U0001f48e  Equity: ${equity:.2f}  |  Phase: {_phase_l}",
            f"\U0001f50d  Static: {len(self._valid_pairs)} {hunter_label} {ws_label}",
            f"\U0001f6e1\ufe0f  Signal: {cfg.SIGNAL_TIMEFRAME} | Exec: {cfg.TIMEFRAME}",
            f"\U0001f9e0  Skill: conviction \u2265 {self._skill.min_conviction:.0f}/100",
            f"\U0001f4ca  Bayes: {learner_n} outcomes learned",
            f"\u2699\ufe0f   Risk: {cfg.get_risk_pct(equity)*100:.1f}% "
            f"| Lev: {cfg.get_leverage(equity)}x "
            f"| Conc: {cfg.get_max_concurrent(equity)}",
            f"\U0001f680  Scanning every {cfg.SIGNAL_TIMEFRAME} candle...",
        ], color=C.BGREEN)

        try:
            while True:
                self._state.check_new_day()

                # Reset day counter on new day
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if not hasattr(self, '_current_day') or self._current_day != today:
                    self._current_day = today
                    self._day_trades = 0

                # Mod 4: Phase-aware daily growth cap
                _eq = self._state.equity
                _phase_target, _phase_cap, _phase_label = cfg.get_current_phase(_eq)
                day_growth = self._state.daily_growth_pct
                if _phase_cap > 0 and day_growth >= _phase_cap:
                    from obr.logger import C
                    log.info(f"  🔥 {C.BOLD}{C.BYELLOW}DAILY CAP HIT{C.RESET}: "
                             f"{C.BGREEN}{day_growth:.1f}%{C.RESET} "
                             f"{C.DIM}(phase cap={_phase_cap:.0f}% | {_phase_label})"
                             f" -- pausing new entries{C.RESET}")
                    self._wait_for_candle_close()

                    # Still update equity while capped
                    try:
                        eq = ex_mod.get_equity(self._ex)
                        self._state.update_equity(eq)
                        log.heartbeat(eq, self._state.pending_count, "capped")
                    except Exception:
                        pass
                    continue

                # Wait for next candle close
                self._wait_for_candle_close()

                # Equity floor check
                try:
                    equity = ex_mod.get_equity(self._ex)
                    self._state.update_equity(equity)
                except Exception:
                    equity = self._state.equity

                peak = self._state.peak_equity
                if peak > 0 and equity / peak < cfg.EQUITY_FLOOR_PCT:
                    log.critical(f"🛑 EQUITY FLOOR HIT: ${equity:.2f} / peak ${peak:.2f} "
                                 f"= {equity/peak*100:.1f}% < {cfg.EQUITY_FLOOR_PCT*100:.0f}%")
                    log.critical("🛑 Trading halted for safety")
                    time.sleep(300)
                    continue

                # Scan all pairs
                session_label = cfg.current_session_name(datetime.now(timezone.utc).hour)
                from obr.logger import C

                # Mod 5: Update global regime from BTC (once per cycle)
                try:
                    btc_candles = self._fetch_candles("BTC/USDT:USDT", 30,
                                                      timeframe=cfg.SIGNAL_TIMEFRAME)
                    if len(btc_candles) >= 10:
                        btc_dicts = [{"open": c.open, "high": c.high,
                                      "low": c.low, "close": c.close}
                                     for c in btc_candles]
                        g_regime = self._regime_cache.update_global(btc_dicts)
                        log.debug(f"  REGIME_GLOBAL: {g_regime}")
                except Exception:
                    pass

                # Mod 10: Dynamic max concurrent for this cycle
                _max_conc = cfg.get_max_concurrent(equity)
                log.debug(f"🔍 {C.DIM}Scanning {len(self._valid_pairs)} pairs "
                          f"(max_conc={_max_conc})...{C.RESET}")
                signals_found = 0
                _traded_this_cycle = set()  # dedup guard: one entry per symbol per cycle

                # 1) Scan static pairs
                for pair in self._valid_pairs:
                    if self._state.pending_count >= _max_conc:
                        break
                    if pair in _traded_this_cycle:
                        continue

                    # Mod 5: Update per-symbol regime from deep candles
                    try:
                        deep = self._fetch_candles(pair, 30,
                                                    timeframe=cfg.SIGNAL_TIMEFRAME)
                        if len(deep) >= 10:
                            d = [{"open": c.open, "high": c.high,
                                  "low": c.low, "close": c.close}
                                 for c in deep]
                            self._regime_cache.update(pair, d)
                    except Exception:
                        pass

                    trade = self._scan_pair(pair, session_label)
                    if trade:
                        signals_found += 1
                        success = self._execute_trade(trade, session_label)
                        if success:
                            _traded_this_cycle.add(pair)
                            self._day_trades += 1
                            from obr.logger import C
                            short = trade.symbol.split('/')[0]
                            dc = C.BGREEN if trade.direction == 'long' else C.BRED
                            arrow = '📈' if trade.direction == 'long' else '📉'
                            log.info(f"  {arrow} {C.BOLD}ENTRY #{self._day_trades}{C.RESET}: "
                                     f"{dc}{trade.direction.upper()}{C.RESET} "
                                     f"{C.BWHITE}{short}{C.RESET} "
                                     f"{C.DIM}| Day growth:{C.RESET} "
                                     f"{C.BGREEN}{self._state.daily_growth_pct:+.1f}%{C.RESET}")

                    time.sleep(0.5)  # rate limit courtesy

                # 2) Pair Hunter: scan full market for A+ OBR setups
                if self._hunter and self._state.pending_count < _max_conc:
                    hunted = self._hunter.hunt(
                        static_pairs=set(self._valid_pairs),
                        max_results=cfg.HUNTER_MAX_RESULTS,
                    )
                    for h in hunted:
                        if self._state.pending_count >= _max_conc:
                            break
                        sym = h.get("symbol", "")
                        if sym in _traded_this_cycle:
                            continue
                        traded = self._process_hunted(h, session_label)
                        if traded:
                            _traded_this_cycle.add(sym)
                            signals_found += 1
                            self._day_trades += 1

                if signals_found > 0:
                    from obr.logger import C
                    log.info(f"  ⚡ {C.BOLD}{C.BCYAN}Scan:{C.RESET} "
                             f"{C.BWHITE}{signals_found}{C.RESET} signals, "
                             f"{C.BWHITE}{self._day_trades}{C.RESET} entries today, "
                             f"growth: {C.BGREEN}{self._state.daily_growth_pct:+.1f}%{C.RESET}")

                # Heartbeat
                log.heartbeat(equity, self._state.pending_count, session_label)

                # Mod 8: Check withdrawal milestones
                try:
                    milestones = self._state.check_milestones(
                        cfg.WITHDRAWAL_MILESTONES)
                    for level, pct, label in milestones:
                        withdraw_amt = equity * pct
                        log.info(f"  🏦 MILESTONE: ${level:.0f} reached! "
                                 f"{label} (≈${withdraw_amt:.2f})")
                except Exception:
                    pass

                # Skill status every 12 cycles (~1h on 5m)
                if not hasattr(self, '_skill_log_counter'):
                    self._skill_log_counter = 0
                self._skill_log_counter += 1
                if self._skill_log_counter % 12 == 0:
                    self._skill.log_status()

        except KeyboardInterrupt:
            log.info("\n👋 Bot stopped by user")
        except Exception as e:
            log.critical(f"💀 Fatal error: {e}")
            log.log_exception("main_loop", e)
        finally:
            if self._ws_cache:
                self._ws_cache.stop()
            if self._guardian:
                self._guardian.stop()

    # ----------------------------------------------------------
    #  Status
    # ----------------------------------------------------------

    def status(self) -> str:
        """Human-readable bot status."""
        lt = self._state.lifetime_summary()
        ds = self._state.daily_summary()
        lines = [
            f"OBR Bot Status",
            f"  Equity: ${lt['equity']:.2f} (peak: ${lt['peak']:.2f}, DD: {lt['dd']:.1f}%)",
            f"  Lifetime: {lt['total_trades']} trades, WR={lt['wr']:.1f}%, "
            f"R={lt['total_r']:+.2f}",
            f"  Today: {ds['entries']} entries, W:{ds['wins']} L:{ds['losses']}, "
            f"R={ds['pnl_r']:+.2f}",
            f"  Open: {ds['pending']} positions",
        ]
        return "\n".join(lines)
