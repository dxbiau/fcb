"""
v13pro/bot.py -- Main async 24/7 trading bot.

The orchestrator that ties everything together:
  - WebSocket data streams (ws_data.py)
  - Strategy scanning (strategies.py)
  - Combo matching (registry.py)
  - Order execution (exchange.py)
  - Position management (guardian.py)
  - Pair discovery (hunter.py)
  - Risk management (state.py)

Runs as a single asyncio event loop with concurrent tasks.
No blocking calls anywhere in the hot path.
"""

import asyncio
import math
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import numpy as np
from ccxt.base.errors import InsufficientFunds as _InsufficientFunds

from v13pro import config as cfg
from v13pro import logger as log
from v13pro import exchange as ex_mod
from v13pro import trade_logger as tlog
from v13pro import journal
from v13pro.state import BotState
from v13pro.registry import ComboRegistry, EXIT_PARAMS
from v13pro.strategies import scan_last_bar, ensemble_signals, Signal, STRATEGIES
from v13pro.strat_orb_fcb import NEW_STRATEGIES as _LAB_STRATS
STRATEGIES.update(_LAB_STRATS)  # Register ORB + FCB into strategy dict
from v13pro.ws_data import WSDataEngine
from v13pro.guardian import Guardian
from v13pro.hunter import PairHunter
from v13pro.skill import PerformanceSkill
from v13pro.aftermath import AftermathTracker
from v13pro.watchdog import Watchdog
from v13pro.dna import SetupDNA, extract_features
from v13pro.sentiment import SentimentGauge
from v13pro.orderflow import OrderFlowIntel
from v13pro.shadow import ShadowTrader
from v13pro.signal_quality import SignalQualityEngine
from v13pro.combo_promoter import promotion_loop as _combo_promotion_loop
from v13pro.adaptive import AdaptiveParams
from v13pro.thesis import ThesisLogger
from v13pro.regime import RegimeDetector
from v13pro.lifecycle import LifecycleTracker
from v13pro.cross_sectional import CrossSectionalAwareness
from v13pro.calibrator import SelfCalibrator
from v13pro.burst_engine import BurstEngine
from v13pro.burst_optimizer import BurstOptimizer
from v13pro.directional import DirectionalIntelligence
from v13pro.edge_radar import EdgeRadar
from v13pro.micro_tf import MicroTFIntelligence, MICRO_TFS
from v13pro.momentum import MomentumAlignment
from v13pro.session_lifecycle import SessionTracker
from v13pro.strategy_lab import StrategyLab, LAB_STRATEGIES, ORB_SESSIONS
from v13pro.strat_orb_fcb import LAB_STRATEGY_NAMES, get_confirmations as _lab_confirmations
from v13pro.shadow_live import ShadowLive
from v13pro.correlation_engine import CorrelationEngine
from v13pro.flow_throttle import FlowThrottle
from v13pro import preflight


class FCBBot:
    """Fully-async 24/7 trading bot."""

    def __init__(self, dry_run: bool = False, once: bool = False):
        self.dry_run = dry_run
        self.once = once          # single scan then exit
        self._running = False
        self._ex = None           # ccxt.pro exchange
        self._state = BotState()
        self._registry = ComboRegistry()
        self._ws = None           # WSDataEngine
        self._guardian = None     # Guardian
        self._hunter = None       # PairHunter
        self._skill = PerformanceSkill()
        self._dna = SetupDNA()    # Setup DNA learning engine
        self._aftermath = None    # AftermathTracker
        self._sentiment = None    # SentimentGauge
        self._orderflow = None    # OrderFlowIntel
        self._shadow = None       # ShadowTrader
        self._signal_quality = None  # SignalQualityEngine
        self._adaptive = None     # AdaptiveParams
        self._thesis = None       # ThesisLogger
        self._regime = None       # RegimeDetector
        self._lifecycle = None    # LifecycleTracker
        self._cross_sect = None   # CrossSectionalAwareness
        self._calibrator = None   # SelfCalibrator
        self._burst = None        # BurstEngine
        self._burst_optim = None  # BurstOptimizer (Phase 2A)
        self._directional = None  # DirectionalIntelligence
        self._edge_radar = None   # EdgeRadar (full shadow exploitation)
        self._micro_tf = None     # MicroTFIntelligence (3m/5m cross-TF validation)
        self._alignment = None    # MomentumAlignment (BTC/ETH/SOL trend alignment)
        self._strategy_lab = None # StrategyLab (ORB/FCB learning system)
        self._session_lc = None   # SessionTracker (intra-session lifecycle)
        self._shadow_live = None  # ShadowLive (pair momentum + focus)
        self._correlation = None  # CorrelationEngine (cross-pair confirmation)
        self._flow_throttle = None  # FlowThrottle (strategy-level adaptive throttle)
        self._watchdog = None     # Watchdog
        self._scan_count = 0
        self._signal_count = 0
        self._trade_count = 0
        self._start_time = 0.0
        self._no_funds_until = 0.0  # cooldown timestamp when balance is too low
        # Per-trade metadata for journal (keyed by symbol)
        self._trade_meta: Dict[str, dict] = {}

    async def run(self):
        """Main entry point — runs forever until stopped."""
        self._start_time = time.time()
        self._running = True

        # ── Preflight checks ──
        pf_result = preflight.run_sync()
        preflight.print_report(pf_result)
        if not pf_result.passed:
            log.info("PREFLIGHT FAILED — aborting launch")
            journal.log_event("preflight_fail", {
                "failures": pf_result.critical_failures,
                "warnings": pf_result.warnings,
            })
            return

        W = 58
        mode_str = f"{'🔬 DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}"
        mode_c = log.C.BYELLOW if self.dry_run else log.C.BRED
        R = log.C.RESET; B = log.C.BOLD; G = log.C.BGREEN; Y = log.C.BYELLOW
        CY = log.C.BCYAN; D = log.C.DIM; W_ = log.C.BWHITE
        log.info(f"\n{CY}{'═' * W}{R}")
        log.info(f"  🤖 {B}{W_}v13pro FCB Bot{R}    {mode_c}{B}{mode_str}{R}")
        log.info(f"{CY}{'═' * W}{R}")
        log.info(f"  {B}📦 PORTFOLIO{R}")
        log.info(f"     Combos     {G}{self._registry.n_combos}{R}"
                 f"              Pairs  {G}{len(self._registry.all_pairs)}{R}")
        log.info(f"     Timeframes {G}{sorted(self._registry.all_tfs)}{R}")
        log.info(f"  {D}{'─' * (W - 4)}{R}")
        log.info(f"  {B}⚙️  EXECUTION{R}")
        log.info(f"     Maker TP   {G}ON{R}" if cfg.MAKER_TP_ENABLED else f"     Maker TP   {D}OFF{R}")
        log.info(f"     Maker Entry{G} ON{R}" if cfg.MAKER_ENTRY_ENABLED else f"     Maker Entry{D} OFF{R}")
        log.info(f"     Skill min  {Y}{self._skill.min_conviction}{R}")
        risk_pct = cfg.get_risk_pct(cfg.START_EQUITY)
        lev = cfg.get_leverage(cfg.START_EQUITY)
        max_c = cfg.get_max_concurrent(cfg.START_EQUITY)
        log.info(f"     Risk       {Y}{risk_pct*100:.1f}%{R}"
                 f"   Lev  {Y}{lev}x{R}"
                 f"   MaxPos  {Y}{max_c}{R}")
        log.info(f"{CY}{'═' * W}{R}\n")

        journal.log_event("bot_start", {
            "dry_run": self.dry_run, "combos": self._registry.n_combos,
            "pairs": len(self._registry.all_pairs),
        }, "Bot starting")

        # Connect exchange
        self._ex = await ex_mod.create_exchange()
        equity = await ex_mod.get_equity(self._ex)
        self._state.update_equity(equity)
        log.info(f"Equity: ${equity:.2f} | Peak: ${self._state.peak_equity:.2f}")

        # Init WS engine
        self._ws = WSDataEngine(max_candles=cfg.WS_CANDLE_BUFFER)
        subs = self._build_subscriptions()
        await self._ws.start(subs)

        # Init guardian
        self._guardian = Guardian(
            self._ex, self._state, self._ws,
            on_position_closed=self._on_position_closed)
        await self._guardian.start()

        # Recover any positions that survived a restart
        await self._recover_positions()

        # Init hunter
        self._hunter = PairHunter(
            self._ex, self._registry,
            on_signals=self._process_hunter_signals if cfg.HUNTER_TRADE_ENABLED else None)
        await self._hunter.start()
        self._hunter_positions = 0  # count of open hunter-sourced positions

        # Init aftermath tracker
        self._aftermath = AftermathTracker(self._ex, self._ws)
        await self._aftermath.start()

        # Init sentiment gauge (data collection — not trade filtering)
        self._sentiment = SentimentGauge(ws_data=self._ws, exchange=self._ex)
        # Pre-warm sentiment cache
        try:
            s = await self._sentiment.get_sentiment(force=True)
            log.info(f"Sentiment gauge ready: {s['bias'].upper()} "
                     f"({s['arrows']}) score={s['score']:+.3f}")
        except Exception as e:
            log.warning(f"Sentiment gauge init: {e}")

        # Init order flow intelligence (data collection only)
        self._orderflow = OrderFlowIntel(exchange=self._ex)
        log.info("Order flow intel ready")

        # Init shadow trader (passive simulation on ALL signals)
        self._shadow = ShadowTrader(
            exchange=self._ex, ws_data=self._ws,
            orderflow=self._orderflow, sentiment=self._sentiment)
        await self._shadow.start()

        # Init adaptive signal quality engine (reads shadow data)
        self._signal_quality = SignalQualityEngine()
        sq = self._signal_quality.get_stats_summary()
        log.info(f"Signal quality engine ready: "
                 f"{sq['combos_tracked']} combos, "
                 f"{sq['total_outcomes']} outcomes, "
                 f"global WR={sq['global_wr']:.1%}")

        # Init adaptive parameter engine (replaces hardcoded gates)
        self._adaptive = AdaptiveParams()
        self._adaptive.log_status()

        # Wire adaptive params into subsystems
        self._skill.set_adaptive(self._adaptive)
        self._state.set_adaptive(self._adaptive)

        # Init thesis logger (pair x strategy win tracking)
        self._thesis = ThesisLogger()
        # Wire thesis into shadow trader for automatic recording
        if self._shadow:
            self._shadow.set_thesis_logger(self._thesis)

        # Init regime detector (self-calibrating exposure modulation)
        self._regime = RegimeDetector()
        self._regime.log_status()
        # Wire regime into shadow for incremental updates
        if self._shadow:
            self._shadow.set_regime_detector(self._regime)

        # Init directional intelligence (adaptive side + TF selection from shadow)
        self._directional = DirectionalIntelligence()
        self._directional.log_status()
        # Wire directional into shadow for incremental updates
        if self._shadow:
            self._shadow.set_directional(self._directional)

        # Init Edge Radar (full shadow intelligence exploitation)
        self._edge_radar = EdgeRadar()
        self._edge_radar.log_status()
        # Wire edge radar into shadow for incremental updates
        if self._shadow:
            self._shadow.set_edge_radar(self._edge_radar)

        # Init Micro-TF Intelligence (3m/5m shadow → cross-TF validation)
        self._micro_tf = MicroTFIntelligence()
        log.info("Micro-TF intelligence ready (3m/5m shadow cross-validation)")
        # Wire micro TF into shadow for incremental updates
        if self._shadow:
            self._shadow.set_micro_tf(self._micro_tf)

        # Init ShadowLive (pair momentum + passed-only combo focus)
        self._shadow_live = ShadowLive()
        self._shadow_live.log_status()
        # Wire shadow_live into shadow for incremental updates
        if self._shadow:
            self._shadow.set_shadow_live(self._shadow_live)

        # Init Correlation Engine (cross-pair signal confirmation)
        self._correlation = CorrelationEngine()
        await self._correlation.initialize(
            self._ws, self._registry.all_pairs)
        self._correlation.log_status()
        # Wire correlation into shadow for signal logging
        if self._shadow:
            self._shadow.set_correlation(self._correlation)

        # Init Momentum Alignment Detector (BTC/ETH/SOL unanimous trend)
        self._alignment = MomentumAlignment(
            sentiment_gauge=self._sentiment,
            micro_tf=self._micro_tf)
        # Pre-warm alignment from existing sentiment cache
        try:
            sent = await self._sentiment.get_sentiment()
            self._alignment.update(sent)
            self._alignment.log_status()
        except Exception:
            log.info("Momentum alignment ready (will warm on first sentiment)")

        # Init Strategy Lab (ORB/FCB shadow learning system)
        self._strategy_lab = StrategyLab()
        log.info(f"Strategy lab ready: tracking {', '.join(sorted(LAB_STRATEGIES))}")
        # Wire lab into shadow for outcome tracking
        if self._shadow:
            self._shadow.set_strategy_lab(self._strategy_lab)

        # Init Flow Throttle (smart strategy-level adaptive throttle)
        self._flow_throttle = FlowThrottle()
        log.info("FlowThrottle ready (strategy-level adaptive throttle + priority queue)")

        # Init Session Lifecycle Tracker (intra-session risk modulation)
        self._session_lc = SessionTracker()
        self._session_lc.log_status()

        # Init lifecycle tracker (per-pair expansion/compression/drift scoring)
        self._lifecycle = LifecycleTracker()
        self._lifecycle.log_status()

        # Init cross-sectional awareness (cluster risk + entry spacing)
        self._cross_sect = CrossSectionalAwareness()
        self._cross_sect.log_status()

        # Init self-calibrator (grade recalibration + stationarity + edge decay)
        self._calibrator = SelfCalibrator()
        self._calibrator.log_status()

        # Init burst engine (edge exploitation + dynamic leverage/TP/risk)
        self._burst = BurstEngine(
            lifecycle=self._lifecycle,
            cross_sect=self._cross_sect)
        self._burst.log_status()

        # Wire burst engine into guardian for partial TP during BURST windows
        if self._guardian and self._burst:
            self._guardian.set_burst_engine(self._burst)

        # Init burst optimizer (Phase 2A — iterative self-tuning)
        self._burst_optim = BurstOptimizer(burst_engine=self._burst)
        self._burst_optim.log_status()

        # Start combo promoter (auto-promote shadow combos to live)
        self._promoter_task = asyncio.create_task(
            _combo_promotion_loop(), name="combo_promoter")
        live_n = len(cfg.LIVE_COMBOS)
        log.info(f"Combo promoter ready: {live_n} live combos, "
                 f"review every {cfg.SHADOW_REVIEW_INTERVAL}s")

        # Init watchdog
        self._watchdog = Watchdog(
            bot=self, ws_data=self._ws,
            guardian=self._guardian, state=self._state)
        await self._watchdog.start()

        # Warm up — wait for WS buffers to fill
        log.info("Warming up WS buffers...")
        await self._warmup()

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(
                    getattr(signal, sig_name),
                    lambda: asyncio.create_task(self.shutdown()))
            except (NotImplementedError, AttributeError):
                pass  # Windows doesn't support add_signal_handler

        log.info("Bot ready — entering main loop")

        try:
            if self.once:
                await self._scan_all_tfs()
            else:
                # Launch heartbeat as independent task so it fires reliably
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="heartbeat")
                await self._main_loop()
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            if hasattr(self, '_heartbeat_task') and self._heartbeat_task:
                self._heartbeat_task.cancel()
            await self.shutdown()

    async def shutdown(self):
        """Graceful shutdown."""
        if not self._running:
            return
        self._running = False
        log.header("Shutting down...")

        if self._watchdog:
            await self._watchdog.stop()
        if hasattr(self, '_promoter_task') and self._promoter_task:
            self._promoter_task.cancel()
        if self._thesis:
            self._thesis.save()
        if self._regime:
            self._regime._save_state()
        if self._aftermath:
            await self._aftermath.stop()
        if self._hunter:
            await self._hunter.stop()
        if self._guardian:
            await self._guardian.stop()
        if self._ws:
            await self._ws.stop()
        await ex_mod.close_exchange()

        elapsed = (time.time() - self._start_time) / 3600
        log.info(f"Session: {elapsed:.1f}h | Scans: {self._scan_count} | "
                 f"Signals: {self._signal_count} | Trades: {self._trade_count}")
        journal.log_event("bot_stop", {
            "uptime_h": round(elapsed, 2),
            "scans": self._scan_count,
            "trades": self._trade_count,
        }, "Bot stopped cleanly")
        log.info("Bot stopped cleanly")

    # ── Main Loop ─────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Independent heartbeat task — fires every 60s regardless of scan state."""
        await asyncio.sleep(10)  # Let the first scan cycle start
        while self._running:
            try:
                self._heartbeat()
            except Exception as e:
                log.log_exception("Heartbeat", e)
            await asyncio.sleep(60)

    async def _main_loop(self):
        """24/7 event-driven loop: wait for candle close, scan, execute."""
        last_equity_check = time.time()

        while self._running:
            try:
                # Wait for a candle close event (or timeout)
                await self._ws.wait_for_close(timeout=60)

                # Drain all close events
                closes = await self._ws.drain_closes()

                if closes:
                    # Group by TF
                    tf_symbols = defaultdict(set)
                    for sym, tf in closes:
                        tf_symbols[tf].add(sym)

                    # Scan all TFs that had closes
                    for tf, symbols in tf_symbols.items():
                        if tf == "1m":
                            continue  # 1m is for guardian, not strategy
                        await self._scan_tf(tf, symbols)

                # Periodic equity update (every 60s)
                now = time.time()
                if now - last_equity_check > 60:
                    try:
                        eq = await ex_mod.get_equity(self._ex)
                        self._state.update_equity(eq)
                        self._state.check_new_day()
                    except Exception:
                        pass
                    last_equity_check = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.log_exception("Main loop", e)
                await asyncio.sleep(5)

    async def _scan_all_tfs(self):
        """Single scan of all TFs (for --once mode or initial)."""
        for tf in sorted(self._registry.all_tfs):
            pairs = self._registry.get_pairs_for_tf(tf)
            if pairs:
                await self._scan_tf(tf, pairs)

    async def _scan_tf(self, tf: str, symbols: Set[str]):
        """Scan all pairs on a timeframe for signals."""
        self._scan_count += 1
        all_signals = []

        for pair in symbols:
            # Get arrays from WS buffer
            arrays = await self._ws.get_arrays(pair, tf)
            if arrays is None:
                continue

            o, h, l, c, v = arrays

            # Get strategy list for combos on this pair/tf
            combos = self._registry.get_combos(pair, tf)
            if not combos:
                continue

            strat_names = list(set(cb["strat"] for cb in combos))

            # Scan
            try:
                signals = scan_last_bar(o, h, l, c, v, pair, tf, strat_names)
                ens = ensemble_signals(signals, pair, tf)
                all_sigs = signals + ens
            except Exception as e:
                log.debug(f"Scan error {pair}/{tf}: {e}")
                continue

            if all_sigs:
                all_signals.extend([(sig, combos) for sig in all_sigs])

        if not all_signals:
            return

        self._signal_count += len(all_signals)
        log.info(f"  Scan {tf}: {len(all_signals)} signals from "
                 f"{len(symbols)} pairs")

        # Execute matching combos
        for sig, combos in all_signals:
            for combo in combos:
                combo_strat = combo["strat"]
                sig_strat = sig.strategy
                # Exact match for regular strategies,
                # prefix match for ensembles: ENS2 matches ENS2(EMA_RIB+BB_BREAK)
                if combo_strat == sig_strat or (
                    combo_strat.startswith("ENS") and sig_strat.startswith(combo_strat)
                ):
                    await self._execute_signal(sig, combo)

    # ── Hunter Signals ────────────────────────────────────────

    async def _process_hunter_signals(self, signals: list):
        """Process hunter-discovered signals as quick scalps.

        Hunter signals on non-portfolio pairs get their own dedicated
        position slots. Scalp exit (fix0.75), reduced risk (0.35×).
        Slot checks happen inside _execute_signal.
        Per-batch dedup prevents multiple entries on the same symbol
        within one hunter scan cycle.
        """
        if not cfg.HUNTER_TRADE_ENABLED:
            return

        # Per-batch dedup: only attempt one signal per symbol per scan
        seen_symbols = set()

        for sig_dict in signals:
            if self._hunter_positions >= cfg.HUNTER_MAX_POSITIONS:
                break

            sym = sig_dict["pair"]
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)

            # Convert dict → Signal object
            sig = Signal(
                bar=0,  # not relevant for live execution
                side=sig_dict["side"],
                entry=sig_dict["entry"],
                stop_dist=sig_dict["stop_dist"],
                strategy=sig_dict["strategy"],
                pair=sig_dict["pair"],
                tf=sig_dict["tf"],
            )

            # Create synthetic combo for hunter signal
            # Per-strategy exit mode (shadow 3R+ analysis)
            _exit = cfg.HUNTER_EXIT_MAP.get(
                (sig_dict["strategy"], sig_dict["tf"]),
                cfg.HUNTER_EXIT_DEFAULT,
            )
            combo = {
                "strat": sig_dict["strategy"],
                "exit": _exit,
                "source": "hunter",
            }

            await self._execute_signal(sig, combo)

    # ── Execution ─────────────────────────────────────────────

    async def _execute_signal(self, sig, combo: dict):
        """Execute a signal through conviction scoring + risk checks + order placement."""
        symbol = sig.pair
        tf = sig.tf
        strat = sig.strategy
        exit_mode = combo.get("exit", "fix1.5")
        is_hunter = combo.get("source") == "hunter"
        session = cfg.current_session_name(datetime.now(timezone.utc).hour)

        entry_price = sig.entry
        stop_dist = sig.stop_dist
        if stop_dist <= 0 or entry_price <= 0:
            return

        # ── ORB NY-session-only filter ──
        # ORB strategy only fires during NY session.
        # Skip entirely outside NY (don't even shadow-track — data is meaningless).
        if strat == 'ORB' and session not in ORB_SESSIONS:
            return

        # ── Directional Intelligence: adaptive side filtering ──
        # Instead of hardcoded LONG_ONLY, let shadow data decide
        # which sides are profitable in current market sentiment.
        _sent_bias = "neutral"  # default
        if self._sentiment:
            try:
                _sent = await self._sentiment.get_sentiment()
                _sent_bias = _sent.get("bias", "neutral")
            except Exception:
                pass

        if self._directional:
            side_allowed = self._directional.is_side_allowed(sig.side, _sent_bias)
            if not side_allowed:
                reason = (f"directional_block ({sig.side} in {_sent_bias} market: "
                          f"shadow says no edge)")
                log.debug(f"  Shadow: {sig.side.upper()} {symbol} [{strat}/{tf}] "
                          f"SKIP({reason})")
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason=reason,
                            exit_mode=exit_mode, session=session,
                            source="hunter" if is_hunter else "portfolio",
                        )
                    except Exception:
                        pass
                return
        elif cfg.LONG_ONLY_MODE and sig.side == "short":
            # Sentiment-gated shorts: block only in BEAR when gating enabled
            if cfg.SENTIMENT_GATED_SHORTS and _sent_bias != "bear":
                pass  # allow short through — DI gate already passed/absent
            else:
                # Fallback: no directional data yet, use static config
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason=f"long_only_mode ({_sent_bias})",
                            exit_mode=exit_mode, session=session,
                            source="hunter" if is_hunter else "portfolio",
                        )
                    except Exception:
                        pass
                return

        # ── Edge Radar: block FROZEN strategy/tf combos ──
        if self._edge_radar:
            if self._edge_radar.is_combo_blocked(strat, tf):
                _combo_label = self._edge_radar.combo_label(strat, tf)
                reason = f"edge_radar_{_combo_label.lower()} ({strat}/{tf})"
                log.info(f"  Skip {symbol}: {reason}")
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason=reason,
                            exit_mode=exit_mode, session=session,
                            source="hunter" if is_hunter else "portfolio",
                        )
                    except Exception:
                        pass
                return

        # ── COLD Regime Freeze: block all new entries in COLD regime ──
        if cfg.COLD_REGIME_FREEZE and self._regime:
            if self._regime.regime == "COLD":
                log.info(f"  Skip {symbol}: COLD regime freeze — no new entries")
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason="cold_regime_freeze",
                            exit_mode=exit_mode, session=session,
                            source="hunter" if is_hunter else "portfolio",
                        )
                    except Exception:
                        pass
                return

        # Risk checks
        equity = self._state.equity
        peak = self._state.peak_equity
        max_conc = cfg.get_max_concurrent(equity)

        # Burst engine drawdown compression — reduce slots during DECAY
        if self._burst:
            pos_mult = self._burst.max_positions_multiplier()
            if pos_mult < 1.0:
                max_conc = max(1, int(max_conc * pos_mult))

        # Hunter scalps: separate slot check (bypass portfolio max_concurrent)
        if is_hunter:
            if self._hunter_positions >= cfg.HUNTER_MAX_POSITIONS:
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason="hunter_slots_full",
                            exit_mode=exit_mode, session=session,
                            source="hunter",
                        )
                    except Exception:
                        pass
                return
            if not self._state.can_trade_hunter(symbol):
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason="hunter_risk_gate",
                            exit_mode=exit_mode, session=session,
                            source="hunter",
                        )
                    except Exception:
                        pass
                return
        else:
            # ── FlowThrottle combo block: freeze losing combos, keep winners ──
            if self._flow_throttle:
                ft_blocked, ft_reason = self._flow_throttle.is_combo_blocked(strat, tf)
                if ft_blocked:
                    if self._shadow:
                        try:
                            await self._shadow.record_signal(
                                symbol=symbol, side=sig.side, strategy=strat,
                                tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                                conviction=0, grade="X", passed=False,
                                rejection_reason=ft_reason,
                                exit_mode=exit_mode, session=session,
                                source="portfolio",
                            )
                        except Exception:
                            pass
                    return

                # FlowThrottle portfolio pause (extreme safety net)
                dd_pct_check = (
                    (peak - equity) / peak * 100 if peak > 0 else 0)
                if self._flow_throttle.is_portfolio_paused(dd_pct_check):
                    if self._shadow:
                        try:
                            await self._shadow.record_signal(
                                symbol=symbol, side=sig.side, strategy=strat,
                                tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                                conviction=0, grade="X", passed=False,
                                rejection_reason="portfolio_pause",
                                exit_mode=exit_mode, session=session,
                                source="portfolio",
                            )
                        except Exception:
                            pass
                    return

            if not self._state.can_trade(symbol, session, max_conc):
                # ── Priority queue: check if this signal outranks an existing slot ──
                # When slots are full, a high-priority signal can still proceed
                # if FlowThrottle scores it above the minimum slot occupant.
                # (For now, respect the limit — priority logic is via combo_risk_mult
                #  which rewards HOT combos and penalises COLD ones.)
                # Shadow: record risk-gated signal (no conviction data)
                if self._shadow:
                    try:
                        await self._shadow.record_signal(
                            symbol=symbol, side=sig.side, strategy=strat,
                            tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                            conviction=0, grade="X", passed=False,
                            rejection_reason="risk_gate",
                            exit_mode=exit_mode,
                            session=session,
                            source="portfolio",
                        )
                    except Exception as e:
                        log.warning(f"Shadow record error (risk gate): {e}")
                return

        # ── Conviction scoring ──
        # Hunter signals use REST candles (not in WS buffer)
        if is_hunter:
            try:
                raw = await ex_mod.fetch_ohlcv(self._ex, symbol, tf, limit=50)
                candles = [{"ts": c[0], "open": c[1], "high": c[2],
                            "low": c[3], "close": c[4], "volume": c[5]}
                           for c in raw] if raw else []
            except Exception:
                candles = []
        else:
            candles = await self._ws.get_candles(symbol, tf, n=50)
        dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0
        phase_target, phase_cap, phase_label = cfg.get_current_phase(equity)
        eq_phase = phase_label.split(":")[0].strip() if phase_label else "unknown"
        dd_zone = "normal"
        if dd_pct > 20: dd_zone = "elevated"
        if dd_pct > 30: dd_zone = "danger"

        skill_result = self._skill.evaluate(
            candles=candles or [],
            direction=sig.side,
            current_price=entry_price,
            symbol=symbol,
            strategy=strat,
            timeframe=tf,
            stop_dist=stop_dist,
            entry_price=entry_price,
            maker=cfg.MAKER_TP_ENABLED or cfg.MAKER_ENTRY_ENABLED,
            equity_phase=eq_phase,
            drawdown_zone=dd_zone,
        )
        conviction = skill_result["score"]
        grade = skill_result["grade"]
        passed = skill_result["pass"]
        kl_risk_mult = skill_result.get("kl_risk_mult", 1.0)

        # Build rejection reason from skill oracle or conviction threshold
        if passed:
            rej_reason = ""
        elif skill_result.get("rejection_reason"):
            rej_reason = skill_result["rejection_reason"]
        else:
            rej_reason = f"conv={conviction:.0f}<{skill_result['min_conviction']}"

        # Log key-level sigmoid and stop-dist rejections visibly
        if kl_risk_mult < 0.95:
            _kl_score = skill_result.get("breakdown", {}).get("key_level", (0, ""))[0]
            log.info(f"  {symbol}: KL sigmoid x{kl_risk_mult:.2f} "
                     f"(kl_score={_kl_score:.0f})")
        if not passed and rej_reason and "stop_dist" in rej_reason:
            log.info(f"  Skip {symbol}: {rej_reason}")

        # Journal: log every signal (pass or reject)
        journal.log_signal(
            symbol=symbol, tf=tf, strategy=strat, side=sig.side,
            entry=entry_price, stop_dist=stop_dist, passed=passed,
            conviction=conviction, grade=grade,
            rejection_reason=rej_reason,
        )

        # Compute DNA features for ALL signals (not just passed)
        # This is critical for shadow analysis — previously only passed signals got DNA
        try:
            all_dna_features = extract_features(candles, sig.side, entry_price, stop_dist)
        except Exception:
            all_dna_features = {}

        # Compute lab confirmations for ORB/FCB strategies (rich metadata for learning)
        _lab_conf = {}
        if strat in LAB_STRATEGY_NAMES:
            try:
                arrays = await self._ws.get_arrays(symbol, tf)
                if arrays is not None:
                    _o, _h, _l, _c, _v = arrays
                    from v13pro.indicators import atr as _atr_fn
                    _a = _atr_fn(_h, _l, _c, 14)
                    _lab_conf = _lab_confirmations(strat, _o, _h, _l, _c, _v, _a, len(_c) - 1)
            except Exception:
                pass

        # Shadow: track conviction-scored signals (passed AND rejected)
        if self._shadow:
            try:
                await self._shadow.record_signal(
                    symbol=symbol, side=sig.side, strategy=strat,
                    tf=tf, entry_price=entry_price, stop_dist=stop_dist,
                    conviction=conviction, grade=grade, passed=passed,
                    rejection_reason=rej_reason,
                    exit_mode=exit_mode,
                    session=session,
                    source="hunter" if is_hunter else "portfolio",
                    # Rich signal DNA data (previously discarded)
                    skill_breakdown=skill_result.get("breakdown", {}),
                    dna_features=all_dna_features,
                    bayes_adjustment=skill_result.get("bayes_adjustment", 0.0),
                    level_info=skill_result.get("level_info", {}),
                    lab_confirmations=_lab_conf,
                )
            except Exception as e:
                log.warning(f"Shadow record error: {e}")

        # Record micro signal freshness for cross-TF validation
        if self._micro_tf and tf in MICRO_TFS:
            self._micro_tf.record_signal(
                strategy=strat, tf=tf, side=sig.side, symbol=symbol)

        if not passed:
            return

        # ── Session lifecycle stop signal ──
        # If session is fatigued/stopped (massive giveback), block new entries
        if self._session_lc and self._session_lc.should_stop_trading():
            log.info(f"  Skip {symbol}: session STOPPED (giveback too large)")
            return

        # ── Alignment side filter ──
        # During BULL alignment, only allow longs (maximize edge direction)
        if self._alignment and not self._alignment.side_filter(sig.side):
            log.info(f"  Skip {symbol}: alignment direction={self._alignment.direction} "
                     f"blocks {sig.side}")
            return

        # ── Live combo gate ──
        # All signals are already shadow-tracked above.
        # Only combos in LIVE_COMBOS proceed to real order placement.
        # Micro TFs (3m/5m) are ALWAYS shadow-only — they feed cross-TF validation.
        if tf in MICRO_TFS:
            log.debug(f"  Micro shadow: {symbol} [{strat}/{tf}] "
                      f"conv={conviction:.0f}{grade} — micro TF (shadow-only)")
            return

        # ── Alignment-conditional combo promotion ──
        # EMA_RIB/15m and DONCHIAN/1h are shadow-only normally but
        # promote to live during sustained alignment (proven Feb 25 edge)
        _alignment_promoted = False
        if self._alignment:
            base_strat_chk = strat.split("(")[0] if "(" in strat else strat
            if self._alignment.is_combo_promoted(base_strat_chk, tf):
                _alignment_promoted = True
                log.info(f"  {symbol}: ALIGNMENT PROMOTED {strat}/{tf} → live "
                         f"(alignment={self._alignment.state} "
                         f"dir={self._alignment.direction})")

        if cfg.LIVE_COMBOS and not _alignment_promoted:
            base_strat = strat.split("(")[0] if "(" in strat else strat
            tf_norm = tf.lower()
            if (base_strat, tf_norm) not in cfg.LIVE_COMBOS and (base_strat, tf.upper()) not in cfg.LIVE_COMBOS:
                log.info(f"  Shadow-only: {symbol} [{strat}/{tf}] "
                         f"conv={conviction:.0f}{grade} — not in LIVE_COMBOS")
                return

        # ── DNA conviction boost (capped) ──
        dna_features = all_dna_features or extract_features(candles, sig.side, entry_price, stop_dist)
        dna_boost, dna_matches = self._dna.get_conviction_boost(
            dna_features, strategy=strat, tf=tf)
        if dna_boost > 0:
            _dna_cap = self._adaptive.dna_boost_cap if self._adaptive else cfg.DNA_BOOST_CAP
            capped_boost = min(dna_boost, _dna_cap)
            conviction += capped_boost
            if capped_boost < dna_boost:
                log.info(f"  DNA boost +{capped_boost:.0f} (capped from +{dna_boost:.0f}) "
                         f"-> conv={conviction:.0f} ({len(dna_matches)} edges)")
            else:
                log.info(f"  DNA boost +{capped_boost:.0f} -> conv={conviction:.0f} "
                         f"({len(dna_matches)} edges matched)")
            # Re-evaluate grade after boost
            if conviction >= 80: grade = "A+"
            elif conviction >= 65: grade = "A"
            elif conviction >= 50: grade = "B"
            elif conviction >= 35: grade = "C"
            else: grade = "D"

        # ── OB alignment: sigmoid multiplier from shadow data ──
        # Shadow audit: hard block destroyed +110.5R at 76.8% WR.
        # Sigmoid replaces binary block with smooth risk scaling.
        # aligned=boost, unaligned=sigmoid(severity) ∈ [0.15, 1.0]
        ob_snap = {}
        of_risk_mult = 1.0  # default: no OF data → full risk
        if self._orderflow:
            try:
                ob_snap = await self._orderflow.snapshot(
                    symbol, sig.side, entry_price)
                imb = ob_snap.get("imbalance", 0)
                pres = ob_snap.get('pressure', '?')

                if ob_snap.get("side_aligned"):
                    # Aligned — adaptive boost
                    ob_boost = self._adaptive.of_boost if self._adaptive else 8
                    conviction += ob_boost
                    log.info(f"  OB aligned +{ob_boost} -> conv={conviction:.0f} "
                             f"(imb={imb:+.2f} pres={pres})")
                else:
                    # ── Unaligned: sigmoid risk multiplier (replaces hard block) ──
                    # Shadow audit: hard block destroyed +110.5R at 76.8% WR.
                    # Sigmoid smoothly scales risk: neutral→1.0x, threshold→0.58x,
                    # extreme→0.15x.  Safety preserved, proven edge recovered.
                    _of_thresh = self._adaptive.of_block_threshold if self._adaptive else cfg.OF_HARD_BLOCK_IMB

                    # Normalised severity: 0=neutral, 1.0=at threshold, >1=beyond
                    if sig.side == "long":
                        _raw_sev = max(0.0, -imb) / max(_of_thresh, 0.01)
                    else:
                        _raw_sev = max(0.0, imb) / max(_of_thresh, 0.01)

                    # Sigmoid: floor 0.15, ceiling 1.0, steepness 4.5
                    _of_floor, _of_ceil, _of_steep = 0.15, 1.0, 4.5
                    of_risk_mult = _of_floor + (_of_ceil - _of_floor) / (
                        1.0 + math.exp(_of_steep * (_raw_sev - 1.0)))

                    # Conviction penalty proportional to severity
                    ob_penalty = self._adaptive.of_penalty if self._adaptive else 3
                    _scaled_penalty = round(ob_penalty * min(_raw_sev, 2.0))
                    conviction -= _scaled_penalty

                    log.info(f"  OB unaligned: risk x{of_risk_mult:.2f} "
                             f"conv-{_scaled_penalty} -> {conviction:.0f} "
                             f"(imb={imb:+.2f} sev={_raw_sev:.2f} "
                             f"pres={pres} side={sig.side})")

                # Re-evaluate grade after OB adjustment
                if conviction >= 80: grade = "A+"
                elif conviction >= 65: grade = "A"
                elif conviction >= 50: grade = "B"
                elif conviction >= 35: grade = "C"
                else: grade = "D"
            except Exception:
                pass  # if OB check fails, proceed without gate

        # ── Sentiment filter: DI-aware short gating ──
        # Shadow audit: BULL shorts 78% WR (+0.619 ExpR), NEUTRAL 61% WR.
        # Old code BLOCKED shorts in BULL — exactly inverted from data.
        # Now: DirectionalIntel handles this via is_side_allowed().
        # Only hard-block shorts in BEAR when no DI data available.
        if sig.side == "short" and not self._directional:
            if self._sentiment:
                try:
                    sent = await self._sentiment.get_sentiment()
                    s_bias = sent.get("bias", "neutral")
                    if s_bias == "bear":
                        log.info(f"  Skip {symbol}: SHORT blocked — BEAR sentiment "
                                 f"(no DI data, safety fallback)")
                        journal.log_signal(
                            symbol=symbol, tf=tf, strategy=strat, side=sig.side,
                            entry=entry_price, stop_dist=stop_dist, passed=False,
                            conviction=conviction, grade=grade,
                            rejection_reason=f"bear_short_block_no_di",
                        )
                        return
                except Exception:
                    pass  # if sentiment fails, allow the trade

        # Hunter signals: reject only below minimum grade
        _GRADE_RANK = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1}
        if is_hunter:
            min_rank = _GRADE_RANK.get(cfg.HUNTER_MIN_GRADE, 3)
            sig_rank = _GRADE_RANK.get(grade, 0)
            if sig_rank < min_rank:
                log.debug(f"  Hunter {symbol} rejected: grade={grade} "
                          f"(need >={cfg.HUNTER_MIN_GRADE})")
                return

        # Calculate position sizing (conviction-adjusted + quality-scored)
        risk_pct = cfg.get_risk_pct(equity)
        dd_mult = cfg.get_drawdown_multiplier(equity, peak)

        # ── Alignment-aware conviction ──
        # During ALIGNED: use CONFIG conviction values (A+=1.50, A=1.15)
        # not adaptive-crushed values (A+ was 1.09, A was 0.93)
        if self._alignment and self._alignment.should_use_config_conviction():
            conv_mult = cfg.CONVICTION_MULTIPLIER.get(grade, 1.0)
            log.info(f"  {symbol}: alignment conviction override → "
                     f"{grade}={conv_mult:.2f}x (config values)")
        else:
            conv_mult = self._adaptive.conviction_multiplier(grade) if self._adaptive else cfg.CONVICTION_MULTIPLIER.get(grade, 1.0)

        # ── Alignment DD floor override ──
        # During ALIGNED: prevent DD throttle from crushing position sizes
        # (breaks the self-reinforcing recovery trap)
        if self._alignment:
            dd_floor = self._alignment.dd_floor()
            if dd_floor is not None and dd_mult < dd_floor:
                log.info(f"  {symbol}: alignment DD floor {dd_floor:.2f} "
                         f"(was {dd_mult:.2f})")
                dd_mult = dd_floor
        # Hunter signals use half risk
        hunter_mult = cfg.HUNTER_RISK_MULT if is_hunter else 1.0
        # Loss streak risk reduction (repeat losers get smaller size)
        consec_losses = self._state.get_consecutive_losses(symbol)
        streak_mult = cfg.get_loss_streak_risk_mult(consec_losses)

        # Adaptive quality scoring — data-driven from shadow outcomes
        # Adjusts sizing based on how well this strategy/tf performs
        # under current conditions (sentiment, grade, OF alignment)
        quality_mult = 1.0
        quality_info = {}
        if self._signal_quality:
            sent_snap_q = {}
            if self._sentiment:
                try:
                    sent_snap_q = await self._sentiment.get_sentiment()
                except Exception:
                    try: sent_snap_q = self._sentiment.get_cached()
                    except Exception: pass
            quality_info = self._signal_quality.score_signal(
                strategy=strat, tf=tf, grade=grade,
                sentiment=sent_snap_q, orderflow=ob_snap, side=sig.side)
            quality_mult = quality_info.get("quality_mult", 1.0)
            if quality_mult != 1.0:
                log.info(f"  {symbol}: quality={quality_mult:.2f}x "
                         f"({quality_info.get('reason', '')})")

        # Regime-aware exposure scaling (self-calibrating from shadow data)
        regime_mult = 1.0
        if self._regime:
            regime_mult = self._regime.exposure_multiplier(session)

        # Lifecycle-aware risk scaling (per-pair expansion/compression/drift)
        lifecycle_risk_mult = 1.0
        if self._lifecycle:
            lifecycle_risk_mult = self._lifecycle.risk_multiplier(symbol)

        # Cross-sectional awareness (entry clustering + loss clustering)
        cross_sect_mult = 1.0
        if self._cross_sect:
            cross_sect_mult = self._cross_sect.risk_multiplier()

        # Self-calibration risk adjustment (edge decay + stationarity)
        calibrator_mult = 1.0
        if self._calibrator:
            calibrator_mult = self._calibrator.risk_multiplier()
            # Apply grade-specific calibration adjustment to conv_mult
            grade_adj = self._calibrator.grade_adjustment(grade)
            if abs(grade_adj - 1.0) > 0.01:
                conv_mult *= grade_adj

        if streak_mult < 1.0:
            log.info(f"  {symbol}: loss streak {consec_losses} "
                     f"risk x{streak_mult:.2f}")
        if regime_mult != 1.0:
            log.info(f"  {symbol}: regime={self._regime.regime} "
                     f"exposure x{regime_mult:.2f}")
        if lifecycle_risk_mult != 1.0:
            log.info(f"  {symbol}: lifecycle risk x{lifecycle_risk_mult:.2f}")
        if cross_sect_mult < 1.0:
            log.info(f"  {symbol}: cross-sectional risk x{cross_sect_mult:.2f}")
        if calibrator_mult < 1.0:
            log.info(f"  {symbol}: calibrator risk x{calibrator_mult:.2f}")

        # Directional intelligence: scale risk by proven edge in this direction
        directional_mult = 1.0
        if self._directional:
            directional_mult = self._directional.side_risk_multiplier(
                sig.side, _sent_bias)
            if abs(directional_mult - 1.0) > 0.01:
                log.info(f"  {symbol}: directional ({sig.side} in {_sent_bias}) "
                         f"risk x{directional_mult:.2f}")

        # Burst engine: dynamic risk/leverage/TP scaling
        burst_risk_mult = 1.0
        burst_lev_mult = 1.0
        if self._burst:
            burst_risk_mult = self._burst.risk_multiplier(symbol, strat, tf)
            burst_lev_mult = self._burst.leverage_multiplier(symbol)
            if burst_risk_mult != 1.0:
                log.info(f"  {symbol}: burst risk x{burst_risk_mult:.2f} "
                         f"[{self._burst.burst_state}]")
            if burst_lev_mult != 1.0:
                log.info(f"  {symbol}: burst leverage x{burst_lev_mult:.2f}")

        # Cross-TF validation: micro (3m/5m) shadow validates macro signals
        cross_tf_mult = 1.0
        cross_tf_conv_boost = 0
        if self._micro_tf:
            cross_tf_mult = self._micro_tf.cross_tf_multiplier(
                strat, tf, sig.side)
            cross_tf_conv_boost = self._micro_tf.cross_tf_conviction_boost(
                strat, tf, sig.side)
            if cross_tf_conv_boost > 0:
                conviction += cross_tf_conv_boost
                log.info(f"  {symbol}: micro-TF cross-validated! "
                         f"conv+{cross_tf_conv_boost} -> {conviction:.0f}")
            # Market barometer from micro TFs
            baro = self._micro_tf.market_barometer()
            if baro.get('mult', 1.0) != 1.0:
                cross_tf_mult *= baro['mult']
            if abs(cross_tf_mult - 1.0) > 0.05:
                log.info(f"  {symbol}: micro-TF risk x{cross_tf_mult:.2f} "
                         f"(barometer={baro.get('label','?')})")

        # Edge Radar: combo heat + market heat + sentiment edge + hot seat
        edge_combo_mult = 1.0
        edge_market_mult = 1.0
        edge_sent_mult = 1.0
        edge_hot_mult = 1.0
        if self._edge_radar:
            edge_combo_mult = self._edge_radar.combo_risk_multiplier(strat, tf)
            edge_market_mult = self._edge_radar.market_heat_multiplier()
            # Get continuous sentiment score for fine-grained edge
            _sent_score = 0.0
            if self._sentiment:
                try:
                    _sent_snap = await self._sentiment.get_sentiment()
                    _sent_score = _sent_snap.get("score", 0.0)
                except Exception:
                    pass
            edge_sent_mult = self._edge_radar.sentiment_risk_multiplier(
                sig.side, _sent_score)
            edge_hot_mult = self._edge_radar.hot_seat_boost()

            # Log significant edge radar adjustments
            _edge_total = edge_combo_mult * edge_market_mult * edge_sent_mult * edge_hot_mult
            if abs(_edge_total - 1.0) > 0.05:
                _mkt_label = self._edge_radar.market_heat_label()
                _combo_label = self._edge_radar.combo_label(strat, tf)
                _hot = self._edge_radar.is_hot_seat()
                log.info(
                    f"  {symbol}: EdgeRadar x{_edge_total:.2f} "
                    f"[combo={_combo_label} x{edge_combo_mult:.2f}, "
                    f"mkt={_mkt_label} x{edge_market_mult:.2f}, "
                    f"sent={_sent_score:+.2f} x{edge_sent_mult:.2f}"
                    f"{', HOT_SEAT' if _hot else ''}]"
                )

        # Momentum Alignment: BTC/ETH/SOL unanimous trend detection
        alignment_mult = 1.0
        bb_priority = None
        if self._alignment:
            alignment_mult = self._alignment.risk_multiplier()
            if abs(alignment_mult - 1.0) > 0.05:
                log.info(f"  {symbol}: alignment={self._alignment.state} "
                         f"dir={self._alignment.direction} "
                         f"risk x{alignment_mult:.2f}"
                         f"{' SUSTAINED' if self._alignment.is_aligned else ''}")
            # BB_BREAK/1h priority during alignment: wider trail + extra risk
            bb_priority = self._alignment.bb_break_priority(strat, tf)
            if bb_priority:
                alignment_mult *= bb_priority["risk_mult"]
                log.info(f"  {symbol}: BB_BREAK/1h PRIORITY during alignment! "
                         f"risk x{bb_priority['risk_mult']:.2f} "
                         f"trail={bb_priority['trail_activation_r']:.1f}R/"
                         f"{bb_priority['trail_distance_r']:.2f}R")

        # Session Lifecycle: intra-session risk modulation
        session_lc_mult = 1.0
        if self._session_lc:
            session_lc_mult = self._session_lc.risk_multiplier()
            if abs(session_lc_mult - 1.0) > 0.05:
                log.info(f"  {symbol}: session={self._session_lc.phase} "
                         f"risk x{session_lc_mult:.2f}"
                         f"{' MOMENTUM' if self._session_lc.summary().get('momentum') else ''}"
                         f"{' HOT' if self._session_lc.summary().get('hot') else ''}"
                         f"{' FATIGUED' if self._session_lc.summary().get('fatigued') else ''}")

        # ShadowLive: pair momentum + passed-only combo focus
        shadow_live_mult = 1.0
        if self._shadow_live:
            shadow_live_mult = self._shadow_live.shadow_live_mult(
                symbol, strat, tf)
            if abs(shadow_live_mult - 1.0) > 0.05:
                _pair_label = self._shadow_live.pair_label(symbol)
                _focus_label = self._shadow_live.combo_focus_label(strat, tf)
                _pair_m = self._shadow_live.pair_momentum_mult(symbol)
                _focus_m = self._shadow_live.combo_focus_mult(strat, tf)
                log.info(
                    f"  {symbol}: ShadowLive x{shadow_live_mult:.2f} "
                    f"[pair={_pair_label} x{_pair_m:.2f}, "
                    f"focus={_focus_label} x{_focus_m:.2f}]")

        # Correlation Engine: cross-pair signal confirmation
        correlation_mult = 1.0
        if self._correlation:
            correlation_mult = self._correlation.correlation_mult(
                symbol, sig.side)
            if abs(correlation_mult - 1.0) > 0.05:
                _cluster = self._correlation.get_cluster(symbol)
                _c_names = [m.split('/')[0] for m in _cluster[:3]]
                log.info(
                    f"  {symbol}: Correlation x{correlation_mult:.2f} "
                    f"[cluster={','.join(_c_names) or 'none'}]")

        # FlowThrottle: per-combo adaptive risk (rewards HOT, penalises COLD)
        flow_throttle_mult = 1.0
        if self._flow_throttle:
            flow_throttle_mult = self._flow_throttle.combo_risk_mult(strat, tf)
            if abs(flow_throttle_mult - 1.0) > 0.05:
                ft_s = self._flow_throttle.summary()
                log.info(f"  {symbol}: FlowThrottle x{flow_throttle_mult:.2f} "
                         f"[{strat}/{tf} H={ft_s['hot']} W={ft_s['warm']} "
                         f"C={ft_s['cold']} F={ft_s['frozen']}]")

        effective_risk = (risk_pct * dd_mult * conv_mult * hunter_mult
                         * streak_mult * quality_mult * regime_mult
                         * lifecycle_risk_mult * cross_sect_mult * calibrator_mult
                         * burst_risk_mult * directional_mult
                         * edge_combo_mult * edge_market_mult
                         * edge_sent_mult * edge_hot_mult
                         * cross_tf_mult
                         * alignment_mult * session_lc_mult
                         * of_risk_mult * kl_risk_mult
                         * shadow_live_mult * correlation_mult
                         * flow_throttle_mult)
        dollar_risk = equity * effective_risk
        leverage = int(cfg.get_leverage(equity) * burst_lev_mult)

        if sig.side == "long":
            sl_price = entry_price - stop_dist
        else:
            sl_price = entry_price + stop_dist

        exit_params = EXIT_PARAMS.get(exit_mode, {"tp_r": 1.5})
        # Trail modes: far safety-net TP on exchange, guardian handles real exit
        if exit_params.get("type") == "trail":
            tp_r = cfg.EXCHANGE_TP_R   # 10R safety net
        else:
            # Use adaptive optimal TP if available, else static exit_params
            adaptive_tp = None
            if self._adaptive:
                adaptive_tp = self._adaptive.optimal_tp_r(strat, tf)
            if adaptive_tp is not None:
                tp_r = adaptive_tp
            else:
                tp_r = exit_params.get("tp_r", 1.5)

        # Lifecycle TP modulation (expanding pairs → bigger TP, compressing → smaller)
        if self._lifecycle and exit_params.get("type") != "trail":
            lc_tp_mult = self._lifecycle.tp_multiplier(symbol)
            if abs(lc_tp_mult - 1.0) > 0.01:
                old_tp = tp_r
                tp_r = round(tp_r * lc_tp_mult, 2)
                tp_r = max(1.0, min(5.0, tp_r))  # clamp to sane range
                log.info(f"  {symbol}: lifecycle TP {old_tp:.2f}R → {tp_r:.2f}R "
                         f"(x{lc_tp_mult:.2f})")

        # Burst TP modulation (wider during burst, tighter during decay)
        if self._burst and exit_params.get("type") != "trail":
            burst_tp_mult = self._burst.tp_multiplier(symbol, strat, tf)
            if abs(burst_tp_mult - 1.0) > 0.01:
                old_tp = tp_r
                tp_r = round(tp_r * burst_tp_mult, 2)
                tp_r = max(1.0, min(6.0, tp_r))  # burst allows up to 6R
                log.info(f"  {symbol}: burst TP {old_tp:.2f}R → {tp_r:.2f}R "
                         f"(x{burst_tp_mult:.2f} [{self._burst.burst_state}])")

        # Session lifecycle TP modulation (EARLY → wider, LATE → tighter)
        if self._session_lc and exit_params.get("type") != "trail":
            slc_tp_mult = self._session_lc.tp_multiplier()
            if abs(slc_tp_mult - 1.0) > 0.01:
                old_tp = tp_r
                tp_r = round(tp_r * slc_tp_mult, 2)
                tp_r = max(1.0, min(5.0, tp_r))
                log.info(f"  {symbol}: session TP {old_tp:.2f}R → {tp_r:.2f}R "
                         f"(x{slc_tp_mult:.2f} [{self._session_lc.phase}])")

        # BB_BREAK/1h priority: override trail params during alignment
        # (wider activation + distance to let 1h runners breathe)
        if bb_priority:
            exit_params = dict(exit_params)  # copy to avoid mutating shared dict
            exit_params["type"] = "trail"
            exit_params["trail_activation_r"] = bb_priority["trail_activation_r"]
            exit_params["trail_distance_r"] = bb_priority["trail_distance_r"]
            tp_r = cfg.EXCHANGE_TP_R  # safety net — guardian handles real exit
            log.info(f"  {symbol}: BB_BREAK/1h aligned trail override "
                     f"activation={bb_priority['trail_activation_r']:.1f}R "
                     f"distance={bb_priority['trail_distance_r']:.2f}R")

        # ── Minimum 2R target enforcement ──
        # Philosophy: risk small, target 2R minimum for easy captures, trail for more
        if exit_params.get("type") != "trail" and tp_r < 2.0:
            tp_r = 2.0

        # ── Minimum dollar reward gate ──
        # Don't risk money for tiny payoffs — that's gambling
        max_reward_usd = dollar_risk * tp_r
        if max_reward_usd < cfg.MIN_REWARD_USD:
            log.info(f"  Skip {symbol}: reward ${max_reward_usd:.2f} "
                     f"< min ${cfg.MIN_REWARD_USD:.2f} "
                     f"(risk=${dollar_risk:.2f} tp={tp_r:.2f}R)")
            return

        if sig.side == "long":
            tp_price = entry_price + stop_dist * tp_r
        else:
            tp_price = entry_price - stop_dist * tp_r

        risk_per_unit = stop_dist
        qty = dollar_risk / risk_per_unit
        mkt = ex_mod.get_market_info(self._ex, symbol)
        contract_size = mkt.get("contractSize", 1) or 1
        notional = qty * entry_price * contract_size

        if notional < 5:
            return

        qty = ex_mod.round_qty(self._ex, symbol, qty)
        sl_price = ex_mod.round_price(self._ex, symbol, sl_price)
        tp_price = ex_mod.round_price(self._ex, symbol, tp_price)

        if qty <= 0:
            return

        src_tag = " [HUNTER]" if is_hunter else ""
        log.info(f"  >> {sig.side.upper()} {symbol} [{strat}]{src_tag} "
                 f"conv={conviction:.0f}{grade} "
                 f"qty={qty} entry≈{entry_price:.6f} "
                 f"SL={sl_price:.6f} TP={tp_price:.6f} "
                 f"exit={exit_mode} risk=${dollar_risk:.2f}")

        if self.dry_run:
            log.info(f"     [DRY RUN] Order skipped")
            return

        # Balance guard — skip if we know funds are low (cooldown)
        if time.time() < self._no_funds_until:
            return

        # ── Funding rate gate ──
        # Reject trades where funding rate is adversarial and extreme
        try:
            funding_pct = await ex_mod.fetch_funding_rate(self._ex, symbol)
            # Positive rate = longs pay shorts; negative = shorts pay longs
            adverse = (sig.side == "long" and funding_pct > 0) or \
                      (sig.side == "short" and funding_pct < 0)
            rate_abs = abs(funding_pct)
            if adverse and rate_abs >= cfg.FUNDING_RATE_MAX_PCT:
                log.warning(f"  Skip {symbol}: funding rate {funding_pct:+.4f}% "
                            f"adverse for {sig.side} (max {cfg.FUNDING_RATE_MAX_PCT}%)")
                return
            if rate_abs >= cfg.FUNDING_RATE_MAX_PCT:
                log.info(f"  {symbol}: funding {funding_pct:+.4f}% "
                         f"(favorable for {sig.side})")
        except Exception:
            pass  # if funding check fails, proceed anyway

        # Check available balance before placing order
        try:
            avail = await ex_mod.get_available_balance(self._ex)
            needed = dollar_risk * 1.2  # 20% buffer for fees/margin
            if avail < needed:
                log.warning(f"  Skip {symbol}: avail ${avail:.2f} < needed ${needed:.2f}")
                self._no_funds_until = time.time() + 60  # cooldown 60s
                return
        except Exception:
            pass  # if balance check fails, try the order anyway

        # ── Entry freshness gate ──
        # Prevent entering when the market has already moved close to SL.
        # Many 0-minute SL trades happen because the signal's entry price
        # is stale (from a candle that already closed) and by the time we
        # place the order, the market has moved past the SL area.
        # For longs: current price must be at least 25% of stop_dist above SL
        # For shorts: current price must be at least 25% of stop_dist below SL
        try:
            ticker = await self._ex.fetch_ticker(symbol)
            live_price = float(ticker.get("last", 0) or 0)
            if live_price > 0 and stop_dist > 0:
                min_buffer_pct = 0.25  # at least 25% of stop distance from SL
                min_buffer = stop_dist * min_buffer_pct
                if sig.side == "long":
                    room = live_price - sl_price
                else:
                    room = sl_price - live_price

                if room < min_buffer:
                    room_pct = room / stop_dist * 100 if stop_dist > 0 else 0
                    log.info(f"  Skip {symbol}: price too close to SL "
                             f"(room={room_pct:.0f}% of stop, need 25%+) "
                             f"live={live_price:.6f} SL={sl_price:.6f}")
                    if self._shadow:
                        try:
                            await self._shadow.record_signal(
                                symbol=symbol, side=sig.side, strategy=strat,
                                tf=tf, entry_price=entry_price,
                                stop_dist=stop_dist,
                                conviction=conviction, grade=grade,
                                passed=False,
                                rejection_reason=f"price_near_sl (room={room_pct:.0f}%)",
                                exit_mode=exit_mode, session=session,
                                source="hunter" if is_hunter else "portfolio",
                            )
                        except Exception:
                            pass
                    return
        except Exception:
            pass  # if ticker fetch fails, proceed anyway

        # ── Cancel stale orders on this symbol ──
        # Prevents interference from unfilled limit orders from previous trades
        try:
            open_orders = await self._ex.fetch_open_orders(symbol)
            if open_orders:
                for oo in open_orders:
                    try:
                        await ex_mod.cancel_order(
                            self._ex, symbol, oo.get("id"))
                    except Exception:
                        pass
                log.info(f"  {symbol}: cancelled {len(open_orders)} stale order(s)")
        except Exception:
            pass  # proceed even if cleanup fails

        # Set leverage + margin mode (isolated = each position has own margin)
        try:
            await ex_mod.set_position_mode(self._ex, symbol, "oneway")
            await ex_mod.set_leverage(self._ex, symbol, leverage)
            await ex_mod.set_margin_mode(self._ex, symbol, "isolated")
        except Exception as e:
            log.warning(f"  Setup {symbol}: {e}")

        # Place order
        try:
            if cfg.MAKER_ENTRY_ENABLED:
                order = await ex_mod.place_limit_order(
                    self._ex, symbol, sig.side, qty, entry_price,
                    sl_price, tp_price)
            else:
                order = await ex_mod.place_market_order(
                    self._ex, symbol, sig.side, qty,
                    sl_price, tp_price)
        except _InsufficientFunds:
            log.warning(f"  {symbol}: Insufficient funds — skipping")
            self._no_funds_until = time.time() + 60  # cooldown 60s
            return
        except Exception as e:
            log.log_exception(f"Order {symbol}", e)
            return

        order_id = order.get("id", "?")
        fill_price = float(order.get("average", 0) or entry_price)
        slippage_bps = abs(fill_price - entry_price) / entry_price * 10_000 if entry_price > 0 else 0

        # Track in state
        self._state.record_entry(symbol, session, {
            "strategy": strat, "exit": exit_mode, "side": sig.side,
            "entry_price": fill_price, "sl": sl_price, "tp": tp_price,
            "qty": qty, "risk_usd": dollar_risk, "order_id": order_id,
        })

        # Track in guardian
        self._guardian.track_position(
            symbol, sig.side, fill_price, sl_price,
            risk_per_unit, dollar_risk, exit_mode,
            exit_params=exit_params)

        # Mark limit orders so guardian applies a grace period
        # (prevents resolving "position gone" before the limit fills)
        if cfg.MAKER_ENTRY_ENABLED:
            tracked = self._guardian._tracked.get(symbol)
            if tracked:
                tracked["_is_limit"] = True

        # Store trade metadata for exit journal
        # Capture sentiment at entry for later exit logging
        sentiment_snap = {}
        if self._sentiment:
            try:
                sentiment_snap = await self._sentiment.get_sentiment()
            except Exception:
                sentiment_snap = self._sentiment.get_cached()

        # Reuse order flow snapshot from alignment gate (no extra API call)
        of_snap = ob_snap if ob_snap else {}
        if not of_snap and self._orderflow:
            try:
                of_snap = await self._orderflow.snapshot(
                    symbol, sig.side, fill_price)
            except Exception as e:
                log.debug(f"  OrderFlow snap {symbol}: {e}")

        self._trade_meta[symbol] = {
            "strategy": strat, "tf": tf, "exit_mode": exit_mode,
            "side": sig.side, "entry_price": fill_price,
            "conviction": conviction, "grade": grade,
            "skill_breakdown": skill_result.get("breakdown", {}),
            "entry_ts": time.time(),
            "source": "hunter" if is_hunter else "portfolio",
            "sentiment_entry": sentiment_snap,
            "orderflow_entry": of_snap,
            "quality_info": quality_info,
            "quality_mult": quality_mult,
        }

        # Record DNA features for statistical profiling
        self._dna.record_entry(
            symbol=symbol, strategy=strat, tf=tf,
            side=sig.side, entry_price=fill_price,
            stop_dist=stop_dist, conviction=conviction,
            grade=grade, features=dna_features,
            source="hunter" if is_hunter else "portfolio")

        # Track hunter position count
        if is_hunter:
            self._hunter_positions += 1

        # Journal: rich entry record
        journal.log_entry(
            symbol=symbol, side=sig.side, strategy=strat, tf=tf,
            exit_mode=exit_mode, entry_price=entry_price,
            sl_price=sl_price, tp_price=tp_price, qty=qty,
            risk_usd=dollar_risk, leverage=leverage, order_id=order_id,
            conviction=conviction, grade=grade,
            skill_breakdown=skill_result.get("breakdown", {}),
            equity=equity, dd_pct=dd_pct, session=session,
            order_type="limit" if cfg.MAKER_ENTRY_ENABLED else "market",
            fill_price=fill_price, slippage_bps=slippage_bps,
            sentiment=sentiment_snap,
            orderflow=of_snap,
        )

        # Console + trade log
        log.position_opened(symbol, sig.side, fill_price, sl_price,
                           tp_price, qty, dollar_risk)
        tlog.log_entry(symbol, sig.side, fill_price, sl_price, tp_price,
                       qty, dollar_risk, risk_per_unit, session,
                       order_id=order_id, strategy=strat,
                       tf=tf, exit_mode=exit_mode)

        self._trade_count += 1

        # Record entry for cross-sectional clustering awareness
        if self._cross_sect:
            self._cross_sect.record_entry(symbol)

        # If limit entry, start fill monitor
        if cfg.MAKER_ENTRY_ENABLED:
            asyncio.create_task(self._monitor_limit_fill(
                symbol, order_id, sig.side, fill_price, sl_price,
                risk_per_unit, dollar_risk, exit_mode))

    async def _monitor_limit_fill(self, symbol, order_id, side,
                                   entry_price, sl_price,
                                   risk_per_unit, dollar_risk, exit_mode):
        """Monitor a limit entry order for fill or timeout."""
        timeout = cfg.MAKER_ENTRY_TIMEOUT_SEC
        start = time.time()

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            try:
                order = await ex_mod.fetch_order(self._ex, symbol, order_id)
                status = order.get("status", "")
                if status == "closed":
                    fill = float(order.get("average", 0) or entry_price)
                    log.info(f"  Limit fill {symbol} @ {fill:.6f}")
                    # Update guardian with actual fill price and clear limit flag
                    tracked = self._guardian._tracked.get(symbol)
                    if tracked:
                        tracked["_is_limit"] = False
                        tracked["entry_price"] = fill
                        tracked["_tracked_at"] = time.time()  # reset for PnL matching
                    return
                if status == "canceled" or status == "cancelled":
                    log.info(f"  Limit cancelled {symbol}")
                    self._guardian.untrack_position(symbol)
                    self._state.record_outcome(symbol, 0, 0, "cancelled")
                    return
            except Exception:
                continue

        # Timeout — cancel order
        try:
            await ex_mod.cancel_order(self._ex, symbol, order_id)
            log.info(f"  Limit timeout {symbol} -- cancelled after {timeout}s")
            self._guardian.untrack_position(symbol)
            self._state.record_outcome(symbol, 0, 0, "timeout")
        except Exception as e:
            log.warning(f"  Cancel {symbol}: {e}")

    # ── Callbacks ─────────────────────────────────────────────

    async def _on_position_closed(self, symbol: str, pnl_r: float,
                                   pnl_usd: float, reason: str,
                                   exit_price: float):
        """Called by guardian when a position closes."""
        self._state.record_outcome(symbol, pnl_r, pnl_usd, reason)

        # Cancel any outstanding orders on this symbol (orphan TP/SL cleanup)
        try:
            open_orders = await self._ex.fetch_open_orders(symbol)
            for oo in open_orders:
                try:
                    await ex_mod.cancel_order(self._ex, symbol, oo.get("id"))
                except Exception:
                    pass
            if open_orders:
                log.debug(f"  {symbol}: cleaned up {len(open_orders)} orphan order(s)")
        except Exception:
            pass

        # Refresh equity
        try:
            eq = await ex_mod.get_equity(self._ex)
            self._state.update_equity(eq)
        except Exception:
            pass

        # Retrieve trade metadata
        meta = self._trade_meta.pop(symbol, {})
        strat = meta.get("strategy", "")
        tf = meta.get("tf", "")
        exit_mode = meta.get("exit_mode", "")
        side = meta.get("side", "")
        entry_price = meta.get("entry_price", 0)
        conviction = meta.get("conviction", 0)
        grade = meta.get("grade", "")
        entry_ts = meta.get("entry_ts", 0)
        source = meta.get("source", "portfolio")
        sentiment_entry = meta.get("sentiment_entry", {})
        duration_min = (time.time() - entry_ts) / 60 if entry_ts > 0 else 0

        # Capture sentiment at exit
        sentiment_exit = {}
        if self._sentiment:
            try:
                sentiment_exit = await self._sentiment.get_sentiment()
            except Exception:
                sentiment_exit = self._sentiment.get_cached()

        # Decrement hunter position count
        if source == "hunter":
            self._hunter_positions = max(0, self._hunter_positions - 1)

        # Guardian info for journal
        guard_info = self._guardian._tracked.get(symbol, {})
        peak_r = guard_info.get("peak_r", 0)
        trail_active = guard_info.get("trail_active", False)
        sl_moves = guard_info.get("tier_idx", -1) + 1

        # Skill feedback
        self._skill.record_outcome(symbol, pnl_r, pnl_usd)

        # DNA outcome tracking
        self._dna.record_outcome(symbol, pnl_r, win=(pnl_r > 0))

        # Cross-sectional loss tracking
        if self._cross_sect and pnl_r < 0:
            self._cross_sect.record_loss(symbol, pnl_r)

        # Burst engine outcome tracking
        if self._burst:
            self._burst.record_outcome(symbol, strat, tf, pnl_r, passed=True)

        # Session lifecycle: track intra-session trade outcomes
        if self._session_lc:
            self._session_lc.record_trade(pnl_r, strategy=strat, symbol=symbol)

        # FlowThrottle: feed per-combo + per-pair outcome
        if self._flow_throttle:
            self._flow_throttle.record_outcome(strat, tf, symbol, pnl_r)

        # Thesis logger: record live trade outcome
        if self._thesis:
            self._thesis.record_outcome({
                "symbol": symbol, "strategy": strat, "tf": tf,
                "side": side, "pnl_r": pnl_r, "grade": grade,
                "conviction": conviction, "passed": True,
            })
            self._thesis.maybe_print_summary()

        # Console + trade log
        emoji = "✅" if pnl_r > 0 else "❌"
        log.position_closed(symbol, side, entry_price, exit_price,
                           pnl_r, pnl_usd, reason)
        tlog.log_exit(symbol, side, entry_price, exit_price,
                      pnl_r, pnl_usd, reason)

        # Journal: rich exit record
        eq = self._state.equity
        journal.log_exit(
            symbol=symbol, side=side, strategy=strat, tf=tf,
            exit_mode=exit_mode, entry_price=entry_price,
            exit_price=exit_price, pnl_r=pnl_r, pnl_usd=pnl_usd,
            reason=reason, duration_minutes=duration_min,
            peak_r=peak_r, sl_moves=sl_moves, trail_active=trail_active,
            conviction=conviction, grade=grade, equity_after=eq,
            sentiment_entry=sentiment_entry,
            sentiment_exit=sentiment_exit,
        )

        # Schedule aftermath tracking — always schedule if aftermath exists
        # Use current market price as fallback when exit_price is 0 (recovered positions)
        stop_dist = guard_info.get("risk_per_unit", 0)
        if self._aftermath:
            aft_exit_price = exit_price
            if aft_exit_price <= 0:
                # Fallback: fetch current market price for aftermath baseline
                try:
                    ticker = await self._ex.fetch_ticker(symbol)
                    aft_exit_price = float(ticker.get("last", 0) or 0)
                    log.debug(f"  Aftermath {symbol}: exit_price=0, "
                              f"using market price {aft_exit_price:.6f}")
                except Exception:
                    pass
            if aft_exit_price > 0:
                self._aftermath.schedule(
                    symbol=symbol, side=side, exit_price=aft_exit_price,
                    stop_dist=stop_dist, reason=reason,
                    strategy=strat, tf=tf, entry_price=entry_price,
                )

        # Phase tracking
        target, cap, phase = cfg.get_current_phase(eq)
        log.info(f"  {emoji} {symbol}: {pnl_r:+.2f}R (${pnl_usd:+.2f}) "
                 f"[{reason}] conv={conviction:.0f}{grade} | "
                 f"Equity: ${eq:.2f} | {phase}")

    # ── Helpers ────────────────────────────────────────────────

    async def _recover_positions(self):
        """Re-adopt open exchange positions after a bot restart.

        Matches live Bybit positions against saved pending_entries in state.
        Positions with saved metadata get full guardian tracking;
        unknown positions get conservative defaults so they're still
        monitored for SL/TP resolution.
        """
        try:
            live_positions = await ex_mod.get_open_positions(self._ex)
        except Exception as e:
            log.warning(f"Position recovery fetch failed: {e}")
            return

        if not live_positions:
            return

        # Build lookup from saved state
        saved = {p.get("symbol"): p for p in self._state.pending_entries}

        recovered = 0
        for pos in live_positions:
            symbol = pos.get("symbol", "")
            side = pos.get("side", "").lower()
            contracts = abs(float(pos.get("contracts", 0) or 0))
            entry_price = float(pos.get("entryPrice", 0)
                                or pos.get("info", {}).get("avgPrice", 0) or 0)
            sl_price = float(pos.get("stopLossPrice", 0)
                             or pos.get("info", {}).get("stopLoss", 0) or 0)

            if contracts <= 0 or not symbol or not side:
                continue

            # Skip if guardian is already tracking it
            if symbol in self._guardian._tracked:
                continue

            meta = saved.get(symbol, {})
            strat = meta.get("strategy", "unknown")
            tf = meta.get("tf", "")
            exit_mode = meta.get("exit", "fix1.5")
            saved_entry = float(meta.get("entry_price", 0) or 0)
            saved_sl = float(meta.get("sl", 0) or 0)
            dollar_risk = float(meta.get("risk_usd", 0) or 0)

            # Prefer saved entry; fall back to exchange entry
            final_entry = saved_entry if saved_entry > 0 else entry_price
            final_sl = saved_sl if saved_sl > 0 else sl_price

            # Compute risk_per_unit from entry and SL
            if final_entry > 0 and final_sl > 0:
                risk_per_unit = abs(final_entry - final_sl)
            else:
                # Fallback: estimate from ATR-like 1% of entry
                risk_per_unit = final_entry * 0.01 if final_entry > 0 else 1.0

            # Estimate dollar_risk if not saved
            if dollar_risk <= 0:
                dollar_risk = risk_per_unit * contracts
                if dollar_risk <= 0:
                    dollar_risk = 10.0  # absolute fallback

            self._guardian.track_position(
                symbol, side, final_entry, final_sl,
                risk_per_unit, dollar_risk, exit_mode)

            # Restore trade meta for journal on exit
            self._trade_meta[symbol] = {
                "strategy": strat, "tf": tf, "exit_mode": exit_mode,
                "side": side, "entry_price": final_entry,
                "conviction": float(meta.get("conviction", 0) or 0),
                "grade": meta.get("grade", ""),
                "skill_breakdown": {},
                "entry_ts": time.time(),
                "source": meta.get("source", "recovered"),
            }

            # Make sure state knows about it
            if symbol not in saved:
                session = cfg.current_session_name(datetime.now(timezone.utc).hour)
                self._state.record_entry(symbol, session, {
                    "strategy": strat, "exit": exit_mode, "side": side,
                    "entry_price": final_entry, "sl": final_sl,
                    "qty": contracts, "risk_usd": dollar_risk,
                })

            recovered += 1
            log.info(f"  ♻️ Recovered {side.upper()} {symbol} "
                     f"entry={final_entry:.6f} sl={final_sl:.6f} "
                     f"risk=${dollar_risk:.2f} [{strat}/{exit_mode}]")

        if recovered:
            log.info(f"Position recovery: {recovered} position(s) re-adopted")
        else:
            log.info("Position recovery: no open positions found")

    def _build_subscriptions(self) -> Dict[str, Set[str]]:
        """Build WS subscription map: {tf: {symbols}}."""
        subs = defaultdict(set)
        for tf in self._registry.all_tfs:
            for pair in self._registry.get_pairs_for_tf(tf):
                subs[tf].add(pair)
        return dict(subs)

    async def _warmup(self, timeout: int = 120):
        """Wait until WS buffers have enough data."""
        start = time.time()
        target = len(self._registry.all_pairs) * 0.5  # 50% ready

        while time.time() - start < timeout:
            stats = self._ws.stats
            if stats["buffers"] >= target:
                log.info(f"  WS warmup complete: {stats['buffers']} buffers ready")
                return
            await asyncio.sleep(5)

        stats = self._ws.stats
        log.warning(f"  WS warmup timeout: {stats['buffers']} buffers "
                    f"(target was {target:.0f})")

    def _heartbeat(self):
        """Periodic status update — rich dashboard display."""
        eq = self._state.equity
        peak = self._state.peak_equity
        growth = self._state.daily_growth_pct
        pos = self._guardian.tracked_count if self._guardian else 0
        ws = self._ws.stats if self._ws else {}
        elapsed = (time.time() - self._start_time) / 3600

        # DD calculation
        dd_pct = 0.0
        if peak > 0:
            dd_pct = (peak - eq) / peak * 100

        # Win/loss stats from state
        wins = self._state._state.get("wins_today", 0)
        losses = self._state._state.get("losses_today", 0)
        pnl_r = self._state._state.get("pnl_today_r", 0)
        pnl_usd = self._state._state.get("pnl_today_usd", 0)
        total_today = wins + losses
        wr_pct = (wins / total_today * 100) if total_today > 0 else 0

        # Skill stats
        sk = self._skill.stats
        learner_n = sk.get("outcomes", 0)

        # Aftermath
        aft_done, aft_pending = 0, 0
        if self._aftermath:
            aft = self._aftermath.stats
            aft_done = aft.get("completed", 0)
            aft_pending = aft.get("pending", 0)

        # Watchdog
        wd_healthy = True
        if self._watchdog:
            wd_healthy = self._watchdog.is_healthy

        # Config
        risk_pct = cfg.get_risk_pct(eq)
        leverage = cfg.get_leverage(eq)
        max_conc = cfg.get_max_concurrent(eq)
        # Burst DECAY compression of position slots
        if self._burst:
            pos_mult = self._burst.max_positions_multiplier()
            if pos_mult < 1.0:
                max_conc = max(1, int(max_conc * pos_mult))
        phase = cfg.get_current_phase(eq)
        hour = datetime.now(timezone.utc).hour
        session = cfg.current_session_name(hour)

        # Hunter
        hunter_sig = self._hunter.stats.get("signals_found", 0) if self._hunter else 0

        # DNA stats
        dna_s = self._dna.stats

        # Refresh adaptive parameters if stale
        if self._adaptive:
            self._adaptive.maybe_refresh()

        # Refresh regime detector
        if self._regime:
            self._regime.maybe_refresh()

        # Refresh lifecycle tracker
        if self._lifecycle:
            self._lifecycle.maybe_refresh()

        # Refresh self-calibrator
        if self._calibrator:
            self._calibrator.maybe_refresh()

        # Refresh directional intelligence
        if self._directional:
            self._directional.maybe_refresh()

        # Refresh edge radar (full shadow intelligence)
        if self._edge_radar:
            self._edge_radar.maybe_refresh()

        # Refresh shadow live (pair momentum + combo focus)
        if self._shadow_live:
            self._shadow_live.maybe_refresh()

        # FlowThrottle: log status if any combos are penalised
        if self._flow_throttle:
            ft_s = self._flow_throttle.summary()
            if ft_s.get('cold', 0) or ft_s.get('frozen', 0):
                log.info(f"  FlowThrottle: H={ft_s['hot']} W={ft_s['warm']} "
                         f"C={ft_s['cold']} F={ft_s['frozen']} "
                         f"pause={'YES' if ft_s['portfolio_paused'] else 'no'}")

        # Refresh correlation engine (async — schedule as task)
        if self._correlation and self._ws:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(
                        self._correlation.maybe_refresh(
                            self._ws, self._registry.all_pairs))
            except Exception:
                pass

        # Refresh micro-TF intelligence (3m/5m market barometer)
        # (auto-refreshes on access, no explicit refresh needed)

        # Refresh momentum alignment (reads sentiment cache, ~30s refresh)
        if self._alignment and self._sentiment:
            try:
                sent = self._sentiment.get_cached()
                if sent:
                    self._alignment.update(sent)
            except Exception:
                pass

        # Refresh burst engine + feed equity for drawdown tracking
        if self._burst:
            self._burst.update_equity(eq, peak)
            self._burst.maybe_refresh()

        # Refresh burst optimizer (Phase 2A — iterative self-tuning)
        if self._burst_optim:
            self._burst_optim.maybe_optimize()

        # Build live position snapshots for dashboard
        open_positions = []
        if self._guardian:
            for sym, ginfo in self._guardian._tracked.items():
                meta = self._trade_meta.get(sym, {})
                direction = ginfo.get("direction", "")
                entry_p = ginfo.get("entry_price", 0)
                current_sl = ginfo.get("current_sl", 0)
                rpu = ginfo.get("risk_per_unit", 0)
                dollar_risk = ginfo.get("dollar_risk", 0)
                exit_mode = ginfo.get("exit_mode", "fix1.5")
                peak_r = ginfo.get("peak_r", 0)
                trail = ginfo.get("trail_active", False)

                # Compute TP price from exit_mode
                tp_r = 1.5
                try:
                    tp_r = float(exit_mode.replace("fix", "").replace("trl", ""))
                except Exception:
                    pass
                if direction == "long":
                    tp_price = entry_p + rpu * tp_r
                else:
                    tp_price = entry_p - rpu * tp_r

                tp_usd = tp_r * dollar_risk
                strat = meta.get("strategy", "")
                funding = ginfo.get("funding_rate", None)

                open_positions.append({
                    "symbol": sym.replace("/USDT:USDT", ""),
                    "side": direction,
                    "entry": entry_p,
                    "sl": current_sl,
                    "tp": tp_price,
                    "tp_r": tp_r,
                    "tp_usd": tp_usd,
                    "peak_r": peak_r,
                    "trail": trail,
                    "strategy": strat,
                    "exit_mode": exit_mode,
                    "funding": funding,
                })

        # Total all-time counters
        total_wins = self._state._state.get("total_wins", 0)
        total_losses = self._state._state.get("total_losses", 0)

        log.dashboard(
            equity=eq, peak_equity=peak, target_equity=cfg.TARGET_EQUITY,
            daily_growth=growth, dd_pct=dd_pct, uptime_h=elapsed,
            positions=pos, max_positions=max_conc, scans=self._scan_count,
            signals=self._signal_count, trades=self._trade_count,
            wins=wins, losses=losses, pnl_r=pnl_r, pnl_usd=pnl_usd,
            wr_pct=wr_pct,
            skill_min=sk.get("min_conviction", 40),
            skill_eval=sk.get("evaluated", 0),
            skill_pass=sk.get("passed", 0),
            skill_rej=sk.get("rejected", 0),
            learner_outcomes=learner_n,
            aftermath_done=aft_done, aftermath_pending=aft_pending,
            watchdog_healthy=wd_healthy, ws_buffers=ws.get("buffers", 0),
            risk_pct=risk_pct, leverage=leverage,
            maker_tp=cfg.MAKER_TP_ENABLED, maker_entry=cfg.MAKER_ENTRY_ENABLED,
            max_conc=max_conc, phase_label=phase[2] if phase else "",
            session=session, hunter_signals=hunter_sig,
            combo_count=self._registry.n_combos,
            pair_count=len(self._registry.all_pairs),
            dna_records=dna_s.get("total_records", 0),
            dna_clusters=dna_s.get("proven_clusters", 0),
            open_positions=open_positions,
            total_wins=total_wins, total_losses=total_losses,
            sentiment=self._sentiment.get_cached() if self._sentiment else {},
            orderflow=self._orderflow.stats if self._orderflow else {},
            shadow=self._shadow.stats if self._shadow else {},
            regime=self._regime.summary() if self._regime else {},
            lifecycle=self._lifecycle.summary() if self._lifecycle else {},
            cross_sectional=self._cross_sect.summary() if self._cross_sect else {},
            calibrator=self._calibrator.summary() if self._calibrator else {},
            burst=self._burst.summary() if self._burst else {},
            burst_optim=self._burst_optim.summary() if self._burst_optim else {},
            edge_radar=self._edge_radar.summary() if self._edge_radar else {},
            micro_tf=self._micro_tf.summary() if self._micro_tf else {},
            alignment=self._alignment.summary() if self._alignment else {},
            session_lc=self._session_lc.summary() if self._session_lc else {},
            strategy_lab=self._strategy_lab.summary() if self._strategy_lab else {},
        )

        # Daily summary check (once per day at midnight UTC)
        self._check_daily_summary()

    def _check_daily_summary(self):
        """Emit daily summary if day rolled over."""
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < 6:
            s = self._state
            skill_s = self._skill.stats
            learner_i = self._skill.learner.get_insights()
            journal.log_daily_summary(
                equity=s.equity,
                peak_equity=s.peak_equity,
                trades=s._state.get("entries_today", 0),
                wins=s._state.get("wins_today", 0),
                losses=s._state.get("losses_today", 0),
                pnl_r=s._state.get("pnl_today_r", 0),
                pnl_usd=s._state.get("pnl_today_usd", 0),
                skill_stats=skill_s,
                learner_insights=learner_i,
            )
