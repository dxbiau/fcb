"""
v13pro/shadow.py -- Shadow Trader for passive data collection.

Simulates entries on ALL signals (passed AND rejected) without placing
real orders. Tracks what would have happened:
  - Did price hit TP? SL? Neither?
  - At 1m/5m/15m/60m checkpoints, where was price?
  - Full orderflow + sentiment snapshot at signal time

Writes to separate shadow journal for analysis.
Runs as async background task alongside the real bot.

This is the DATA GOLDMINE — every signal the bot sees gets tracked
regardless of whether we traded it or not.
"""

import asyncio
import json
import os
import time
from collections import deque
from typing import Dict, List, Optional

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

_SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")

# Checkpoints to track after signal (minutes)
CHECKPOINTS_MIN = [1, 5, 15, 60]

# Max concurrent shadow tracks
MAX_SHADOW = 500


def _ensure_dir():
    os.makedirs(_SHADOW_DIR, exist_ok=True)


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_shadow(record: dict):
    """Append record to today's shadow JSONL."""
    _ensure_dir()
    path = os.path.join(_SHADOW_DIR, f"shadow_{_today()}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


class ShadowTrader:
    """Passive shadow trader — simulates ALL signals for data collection."""

    def __init__(self, exchange=None, ws_data=None,
                 orderflow=None, sentiment=None):
        self._ex = exchange
        self._ws = ws_data
        self._orderflow = orderflow
        self._sentiment = sentiment
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._pending: deque = deque(maxlen=MAX_SHADOW)
        self._lock = asyncio.Lock()  # protects _pending
        self._completed = 0
        self._wins = 0
        self._losses = 0
        self._errors = 0
        self._signals_seen = 0
        self._thesis = None  # ThesisLogger (set via set_thesis_logger)
        self._regime = None   # RegimeDetector (set via set_regime_detector)
        self._directional = None  # DirectionalIntelligence (set via set_directional)
        self._edge_radar = None   # EdgeRadar (set via set_edge_radar)
        self._micro_tf = None     # MicroTFIntelligence (set via set_micro_tf)
        self._strategy_lab = None  # StrategyLab (set via set_strategy_lab)

    def set_thesis_logger(self, thesis):
        """Wire in thesis logger to receive all shadow outcomes."""
        self._thesis = thesis

    def set_regime_detector(self, regime):
        """Wire in regime detector for incremental outcome updates."""
        self._regime = regime

    def set_directional(self, directional):
        """Wire in directional intelligence for incremental updates."""
        self._directional = directional

    def set_edge_radar(self, edge_radar):
        """Wire in edge radar for incremental shadow outcome updates."""
        self._edge_radar = edge_radar

    def set_micro_tf(self, micro_tf):
        """Wire in micro-TF intelligence for 3m/5m outcome tracking."""
        self._micro_tf = micro_tf

    def set_strategy_lab(self, lab):
        """Wire in strategy lab for ORB/FCB learning outcome tracking."""
        self._strategy_lab = lab

    async def start(self):
        """Start the shadow tracking loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="shadow")
        log.info("Shadow trader started (passive mode — no real orders)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info(f"Shadow trader stopped "
                 f"({self._completed} completed, {len(self._pending)} pending)")

    @property
    def stats(self) -> dict:
        total = self._wins + self._losses
        wr = (self._wins / total * 100) if total > 0 else 0
        return {
            "signals_seen": self._signals_seen,
            "pending": len(self._pending),
            "completed": self._completed,
            "wins": self._wins,
            "losses": self._losses,
            "wr_pct": round(wr, 1),
            "errors": self._errors,
        }

    async def record_signal(self, symbol: str, side: str, strategy: str,
                            tf: str, entry_price: float, stop_dist: float,
                            conviction: float, grade: str, passed: bool,
                            rejection_reason: str = "",
                            exit_mode: str = "fix1.5",
                            session: str = "",
                            source: str = "portfolio",
                            skill_breakdown: dict = None,
                            dna_features: dict = None,
                            bayes_adjustment: float = 0.0,
                            level_info: dict = None,
                            lab_confirmations: dict = None):
        """
        Record a signal for shadow tracking.

        Called for EVERY signal — both passed and rejected.
        Captures orderflow + sentiment, then schedules price monitoring.
        """
        self._signals_seen += 1

        # Compute TP and SL for simulation
        if side == "long":
            sl_price = entry_price - stop_dist
        else:
            sl_price = entry_price + stop_dist

        # Get exit TP ratio from exit_mode
        tp_r = 1.5
        try:
            tp_r = float(exit_mode.replace("fix", "").replace("trl", ""))
        except Exception:
            pass

        if side == "long":
            tp_price = entry_price + stop_dist * tp_r
        else:
            tp_price = entry_price - stop_dist * tp_r

        # Capture orderflow snapshot
        of_snap = {}
        if self._orderflow:
            try:
                of_snap = await self._orderflow.snapshot(
                    symbol, side, entry_price)
            except Exception:
                pass

        # Capture sentiment
        sent_snap = {}
        if self._sentiment:
            try:
                sent_snap = await self._sentiment.get_sentiment()
            except Exception:
                try:
                    sent_snap = self._sentiment.get_cached()
                except Exception:
                    pass

        now_ms = int(time.time() * 1000)

        # Build shadow entry record
        entry_record = {
            "event": "shadow_entry",
            "ts_ms": now_ms,
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "tf": tf,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "tp_r": tp_r,
            "stop_dist": stop_dist,
            "conviction": conviction,
            "grade": grade,
            "passed": passed,
            "rejection_reason": rejection_reason,
            "exit_mode": exit_mode,
            "session": session,
            "source": source,
            "orderflow": of_snap,
            "sentiment": sent_snap,
            # Rich signal DNA data (previously discarded)
            "skill_breakdown": skill_breakdown or {},
            "dna_features": dna_features or {},
            "bayes_adjustment": bayes_adjustment,
            "level_info": level_info or {},
        }

        # Write entry immediately
        _write_shadow(entry_record)

        # Schedule price monitoring (lock protects deque from mutation)
        async with self._lock:
            self._pending.append({
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "tf": tf,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "tp_r": tp_r,
                "stop_dist": stop_dist,
                "conviction": conviction,
                "grade": grade,
                "passed": passed,
                "rejection_reason": rejection_reason,
                "exit_mode": exit_mode,
                "session": session,
                "source": source,
                "entry_ts_ms": now_ms,
                "checkpoints_done": [],
                "checkpoints_remaining": list(CHECKPOINTS_MIN),
                "hit_tp": False,
                "hit_sl": False,
                "peak_r": 0.0,
                "trough_r": 0.0,
                "orderflow": of_snap,
                "sentiment": sent_snap,
                # Rich signal DNA data for outcome correlation
                "skill_breakdown": skill_breakdown or {},
                "dna_features": dna_features or {},
                "bayes_adjustment": bayes_adjustment,
                "level_info": level_info or {},
                "lab_confirmations": lab_confirmations or {},
            })

        tag = "PASS" if passed else f"SKIP({rejection_reason})"
        of_tag = ""
        if of_snap.get("spread_bps"):
            of_tag = (f" spread={of_snap['spread_bps']:.1f}bps"
                      f" imb={of_snap.get('imbalance',0):+.2f}"
                      f" [{of_snap.get('quality','?')}]")
        log.debug(f"  Shadow: {side.upper()} {symbol} [{strategy}/{tf}] "
                  f"conv={conviction:.0f}{grade} {tag}{of_tag}")

    async def _loop(self):
        """Main loop — checks every 20s for due checkpoints + TP/SL hits."""
        while self._running:
            try:
                await self._process_pending()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._errors += 1
                log.log_exception("Shadow loop", e)
            await asyncio.sleep(20)

    async def _process_pending(self):
        """Process all pending shadow trades.

        Takes a snapshot of _pending under lock so record_signal()
        can safely append new items while we iterate.
        """
        # Snapshot under lock — prevents 'deque mutated during iteration'
        async with self._lock:
            snapshot = list(self._pending)

        now_ms = int(time.time() * 1000)
        done_items = []  # track completed item ids

        # Batch price lookups: get unique symbols first
        symbols_needed = {item["symbol"] for item in snapshot}
        price_cache: dict = {}
        for sym in symbols_needed:
            p = await self._get_price(sym)
            if p and p > 0:
                price_cache[sym] = p

        for item in snapshot:
            price = price_cache.get(item["symbol"])
            if not price:
                continue

            entry_ts = item["entry_ts_ms"]
            elapsed_min = (now_ms - entry_ts) / 60_000

            # Check TP/SL hit (if not already hit)
            if not item["hit_tp"] and not item["hit_sl"]:
                side = item["side"]
                if side == "long":
                    if price >= item["tp_price"]:
                        item["hit_tp"] = True
                    elif price <= item["sl_price"]:
                        item["hit_sl"] = True
                else:
                    if price <= item["tp_price"]:
                        item["hit_tp"] = True
                    elif price >= item["sl_price"]:
                        item["hit_sl"] = True

            # Track peak/trough R
            entry_p = item["entry_price"]
            sd = item["stop_dist"]
            if sd > 0 and entry_p > 0:
                if item["side"] == "long":
                    current_r = (price - entry_p) / sd
                else:
                    current_r = (entry_p - price) / sd
                item["peak_r"] = max(item["peak_r"], current_r)
                item["trough_r"] = min(item["trough_r"], current_r)

            # Process due checkpoints
            remaining = list(item["checkpoints_remaining"])
            newly_done = []

            for cp_min in remaining:
                if elapsed_min >= cp_min:
                    cp_data = self._build_checkpoint(item, cp_min, price)
                    item["checkpoints_done"].append(cp_data)
                    newly_done.append(cp_min)

            for cp_min in newly_done:
                item["checkpoints_remaining"].remove(cp_min)

            # If all checkpoints done, finalize
            if not item["checkpoints_remaining"]:
                self._finalize(item)
                done_items.append(id(item))

        # Remove completed items under lock
        if done_items:
            done_set = set(done_items)
            async with self._lock:
                # Rebuild deque without completed items
                remaining_items = [it for it in self._pending
                                   if id(it) not in done_set]
                self._pending.clear()
                self._pending.extend(remaining_items)

    def _build_checkpoint(self, item: dict, minutes: int,
                          current_price: float) -> dict:
        """Build a single checkpoint record."""
        entry_price = item["entry_price"]
        stop_dist = item["stop_dist"]
        side = item["side"]

        if entry_price <= 0:
            return {"minutes": minutes, "price": current_price,
                    "move_pct": 0, "move_r": 0}

        raw_move = current_price - entry_price
        move_pct = raw_move / entry_price * 100

        if stop_dist > 0:
            if side == "long":
                move_r = raw_move / stop_dist
            else:
                move_r = -raw_move / stop_dist
        else:
            move_r = 0

        return {
            "minutes": minutes,
            "price": round(current_price, 8),
            "move_pct": round(move_pct, 4),
            "move_r": round(move_r, 3),
        }

    def _finalize(self, item: dict):
        """Finalize shadow trade and write outcome."""
        # Determine simulated outcome
        if item["hit_tp"]:
            outcome = "tp"
            pnl_r = item["tp_r"]
            self._wins += 1
        elif item["hit_sl"]:
            outcome = "sl"
            pnl_r = -1.0
            self._losses += 1
        else:
            # Neither hit within tracking window — use last checkpoint
            cps = item["checkpoints_done"]
            if cps:
                last_r = cps[-1].get("move_r", 0)
                pnl_r = last_r
                if last_r > 0:
                    outcome = "open_profit"
                    self._wins += 1
                else:
                    outcome = "open_loss"
                    self._losses += 1
            else:
                outcome = "no_data"
                pnl_r = 0
                self._losses += 1

        record = {
            "event": "shadow_outcome",
            "ts_ms": int(time.time() * 1000),
            "symbol": item["symbol"],
            "side": item["side"],
            "strategy": item["strategy"],
            "tf": item["tf"],
            "entry_price": item["entry_price"],
            "sl_price": item["sl_price"],
            "tp_price": item["tp_price"],
            "tp_r": item["tp_r"],
            "stop_dist": item["stop_dist"],
            "conviction": item["conviction"],
            "grade": item["grade"],
            "passed": item["passed"],
            "rejection_reason": item.get("rejection_reason", ""),
            "exit_mode": item["exit_mode"],
            "session": item["session"],
            "source": item["source"],
            "outcome": outcome,
            "pnl_r": round(pnl_r, 3),
            "hit_tp": item["hit_tp"],
            "hit_sl": item["hit_sl"],
            "peak_r": round(item["peak_r"], 3),
            "trough_r": round(item["trough_r"], 3),
            "checkpoints": item["checkpoints_done"],
            "orderflow": item.get("orderflow", {}),
            "sentiment": item.get("sentiment", {}),
            "regime": self._regime.regime if self._regime else "UNKNOWN",
            "duration_ms": int(time.time() * 1000) - item["entry_ts_ms"],
            # Rich signal DNA data for outcome correlation
            "skill_breakdown": item.get("skill_breakdown", {}),
            "dna_features": item.get("dna_features", {}),
            "bayes_adjustment": item.get("bayes_adjustment", 0.0),
            "level_info": item.get("level_info", {}),
        }

        _write_shadow(record)
        self._completed += 1

        # Feed thesis logger (all shadow outcomes, not just live)
        if self._thesis:
            try:
                self._thesis.record_outcome(record)
                self._thesis.maybe_print_summary()
            except Exception:
                pass

        # Feed regime detector (incremental rolling stats update)
        if self._regime:
            try:
                self._regime.record_outcome(
                    pnl_r=pnl_r,
                    session=item.get("session", "unknown"),
                    strategy=item.get("strategy", ""),
                    tf=item.get("tf", ""),
                    symbol=item.get("symbol", ""),
                )
            except Exception:
                pass

        # Feed directional intelligence (ALL sides, ALL regimes)
        if self._directional:
            try:
                sent = item.get("sentiment", {})
                self._directional.record_outcome(
                    pnl_r=pnl_r,
                    side=item.get("side", ""),
                    tf=item.get("tf", ""),
                    sentiment_bias=sent.get("bias", "neutral"),
                    strategy=item.get("strategy", ""),
                    symbol=item.get("symbol", ""),
                    peak_r=item.get("peak_r", 0),
                )
            except Exception:
                pass

        # Feed edge radar (full shadow intelligence — combo heat, market heat)
        if self._edge_radar:
            try:
                sent = item.get("sentiment", {})
                self._edge_radar.record_outcome(
                    pnl_r=pnl_r,
                    peak_r=item.get("peak_r", 0),
                    strategy=item.get("strategy", ""),
                    tf=item.get("tf", ""),
                    side=item.get("side", ""),
                    sentiment_score=sent.get("score", 0.0),
                )
            except Exception:
                pass

        # Feed micro-TF intelligence (3m/5m outcome tracking for cross-TF validation)
        if self._micro_tf:
            try:
                self._micro_tf.record_outcome(
                    strategy=item.get("strategy", ""),
                    tf=item.get("tf", ""),
                    pnl_r=pnl_r,
                    peak_r=item.get("peak_r", 0),
                    side=item.get("side", ""),
                    symbol=item.get("symbol", ""),
                )
            except Exception:
                pass

        # Feed Strategy Lab (ORB/FCB rich confirmation learning)
        if self._strategy_lab:
            _strat_name = item.get("strategy", "")
            from v13pro.strategy_lab import LAB_STRATEGIES as _LAB_SET
            if _strat_name in _LAB_SET:
                try:
                    _dur_ms = int(time.time() * 1000) - item["entry_ts_ms"]
                    self._strategy_lab.record_outcome(
                        strategy=_strat_name,
                        symbol=item.get("symbol", ""),
                        side=item.get("side", ""),
                        tf=item.get("tf", ""),
                        session=item.get("session", ""),
                        entry_price=item.get("entry_price", 0),
                        stop_dist=item.get("stop_dist", 0),
                        peak_r=item.get("peak_r", 0),
                        trough_r=item.get("trough_r", 0),
                        outcome_r=pnl_r,
                        hit_tp=item.get("hit_tp", False),
                        hit_sl=item.get("hit_sl", False),
                        confirmations=item.get("lab_confirmations", {}),
                        duration_min=_dur_ms / 60_000,
                    )
                except Exception:
                    pass

        emoji = "✅" if pnl_r > 0 else "❌"
        traded = "TRADED" if item["passed"] else "REJECTED"
        log.debug(f"  Shadow {emoji} {item['symbol']}: {outcome} "
                  f"{pnl_r:+.2f}R peak={item['peak_r']:+.2f}R "
                  f"[{traded}] {item['strategy']}/{item['tf']} "
                  f"conv={item['conviction']:.0f}{item['grade']}")

    async def _get_price(self, symbol: str) -> Optional[float]:
        """Get current price, prefer WS, fallback to REST.

        WS path is free (no API call).  REST fallback uses fetch_ticker
        which is cheaper than fetch_ohlcv.
        """
        if self._ws:
            try:
                candles = await self._ws.get_1m_candles(symbol, n=1)
                if candles and len(candles) > 0:
                    return candles[-1]["close"]
            except Exception:
                pass

        if self._ex:
            try:
                ticker = await self._ex.fetch_ticker(symbol)
                price = float(ticker.get("last", 0) or 0)
                # Trim ccxt ticker cache to limit memory growth
                try:
                    if hasattr(self._ex, 'tickers') and len(self._ex.tickers) > 50:
                        keep = set(item["symbol"] for item in (list(self._pending)[:50]
                                                               if self._pending else []))
                        for k in list(self._ex.tickers.keys()):
                            if k not in keep:
                                del self._ex.tickers[k]
                except Exception:
                    pass
                return price
            except Exception:
                self._errors += 1

        return None
