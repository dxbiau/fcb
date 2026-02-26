"""
v13pro/aftermath.py -- Async post-exit price tracker.

After a position closes, schedules price checks at:
  1m, 5m, 15m, 1h after exit

This reveals:
  - TP exits: did price keep running? (premature TP → need wider TP)
  - SL exits: did price recover? (unnecessary SL → need looser SL)
  - Trail exits: how much left on table? (trail too tight / too loose)

Runs as an asyncio task. Uses WS data if available, REST fallback.
Feeds results to journal.log_aftermath() for research.
"""

import asyncio
import time
from collections import deque
from typing import Optional

from v13pro import config as cfg
from v13pro import logger as log
from v13pro.journal import log_aftermath

# Checkpoints in minutes after exit
CHECKPOINTS_MIN = [1, 5, 15, 60]

# Max pending items (avoid unbounded growth)
MAX_PENDING = 200


class AftermathTracker:
    """Async post-exit price tracker."""

    def __init__(self, exchange=None, ws_data=None):
        self._ex = exchange
        self._ws = ws_data
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._pending: deque = deque(maxlen=MAX_PENDING)
        self._completed = 0
        self._errors = 0

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="aftermath")
        log.info("Aftermath tracker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info(f"Aftermath tracker stopped "
                 f"({self._completed} completed, {len(self._pending)} pending)")

    def schedule(self, symbol: str, side: str, exit_price: float,
                 stop_dist: float, reason: str, strategy: str = "",
                 tf: str = "", entry_price: float = 0):
        """Schedule aftermath tracking for a closed position."""
        now_ms = int(time.time() * 1000)
        self._pending.append({
            "symbol": symbol,
            "side": side,
            "exit_price": exit_price,
            "stop_dist": stop_dist,
            "reason": reason,
            "strategy": strategy,
            "tf": tf,
            "entry_price": entry_price,
            "exit_ts_ms": now_ms,
            "checkpoints_done": [],
            "checkpoints_remaining": list(CHECKPOINTS_MIN),
        })
        log.debug(f"Aftermath scheduled: {symbol} ({reason}) "
                  f"exit={exit_price:.6f}")

    @property
    def stats(self) -> dict:
        return {
            "pending": len(self._pending),
            "completed": self._completed,
            "errors": self._errors,
        }

    async def _loop(self):
        """Main loop — checks every 30s for due checkpoints."""
        while self._running:
            try:
                await self._process_pending()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._errors += 1
                log.log_exception("Aftermath loop", e)
            await asyncio.sleep(30)

    async def _process_pending(self):
        """Process all pending aftermath items."""
        now_ms = int(time.time() * 1000)
        done_indices = []

        for i, item in enumerate(self._pending):
            exit_ts = item["exit_ts_ms"]
            elapsed_min = (now_ms - exit_ts) / 60_000

            remaining = list(item["checkpoints_remaining"])
            newly_done = []

            for cp_min in remaining:
                if elapsed_min >= cp_min:
                    # This checkpoint is due
                    price = await self._get_price(item["symbol"])
                    if price and price > 0:
                        cp_data = self._build_checkpoint(item, cp_min, price)
                        item["checkpoints_done"].append(cp_data)
                        newly_done.append(cp_min)

            # Remove completed checkpoints
            for cp_min in newly_done:
                item["checkpoints_remaining"].remove(cp_min)

            # If all checkpoints done, finalize
            if not item["checkpoints_remaining"]:
                self._finalize(item)
                done_indices.append(i)

        # Remove completed items (reverse to preserve indices)
        for i in sorted(done_indices, reverse=True):
            try:
                del self._pending[i]
            except IndexError:
                pass

    def _build_checkpoint(self, item: dict, minutes: int,
                          current_price: float) -> dict:
        """Build a single checkpoint record."""
        exit_price = item["exit_price"]
        stop_dist = item["stop_dist"]
        side = item["side"]

        if exit_price <= 0:
            return {"minutes": minutes, "price": current_price,
                    "move_pct": 0, "move_r": 0}

        # Raw move
        raw_move = current_price - exit_price
        move_pct = raw_move / exit_price * 100

        # Move in R terms (relative to stop distance)
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
        """Finalize aftermath and write to journal."""
        verdict = log_aftermath(
            symbol=item["symbol"],
            side=item["side"],
            exit_price=item["exit_price"],
            exit_ts_ms=item["exit_ts_ms"],
            reason=item["reason"],
            checkpoints=item["checkpoints_done"],
        )
        self._completed += 1
        log.debug(f"Aftermath {item['symbol']}: {verdict} "
                  f"({len(item['checkpoints_done'])} checkpoints)")

    async def _get_price(self, symbol: str) -> Optional[float]:
        """Get current price, prefer WS, fallback to REST with retry."""
        # Try WS 1m buffer first (latest close)
        if self._ws:
            try:
                candles = await self._ws.get_1m_candles(symbol, n=1)
                if candles and len(candles) > 0:
                    price = candles[-1]["close"]
                    if price and price > 0:
                        return price
            except Exception:
                pass

        # REST fallback with retry (2 attempts)
        if self._ex:
            for attempt in range(2):
                try:
                    ticker = await self._ex.fetch_ticker(symbol)
                    price = float(ticker.get("last", 0) or 0)
                    if price > 0:
                        return price
                except Exception as e:
                    if attempt == 0:
                        await asyncio.sleep(2)  # brief pause before retry
                    else:
                        self._errors += 1
                        log.debug(f"Aftermath price fetch {symbol}: {e}")

        return None
