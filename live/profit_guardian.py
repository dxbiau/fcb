"""
live/profit_guardian.py — Profit Guardian v3: Trail Intelligence

A daemon thread that trails SL behind peak profit for every open position.

Data-driven design — tested across 12,355 FCB trades:
  Fixed 1.5R TP:            +334R total, 41.2% WR, PF 1.05
  Guardian v3 (0.3R trail): +1,738R total, 50.3% WR, PF 1.28

HOW IT WORKS:
  1. Until +1.0R: exchange SL at midpoint (original). No intervention.
     The trade needs room to develop — data proved BE moves at +0.75R
     kill trades that would become winners.

  2. Once R >= 1.0: start trailing SL at (peak_R - 0.3R) in R-terms.
     This means: if peak was +2.5R, SL sits at +2.2R.
     If price retraces 0.3R from peak, the trailing SL on the exchange
     catches it. No need to market-close — the exchange SL does the work.

  3. Progressive SL tiers are the EXCHANGE SAFETY NET.
     If the bot crashes, Bybit's SL orders still protect you:
       +0.5R  → SL at -0.25R (cut max loss 75%)
       +0.75R → SL at breakeven
       +1.0R  → SL at +0.5R, then trail takes over

  4. No TP cap on exchange (set at 10R safety net).
     Winners run as far as the market takes them.
     Mean peak R is 5.41R — the 1.5R cap was leaving 4R on the table.

Uses its own exchange connection for thread safety.
Polls every 2 seconds.
"""

import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional

import ccxt

from live import exchange as exch
from live import logger as log
from live import trade_logger as tlog
from live.config import (
    API_KEY, API_SECRET, MAINNET, DEMO_MODE,
    PROFIT_TIERS, GUARDIAN_POLL_SECS,
    TRAIL_ENABLED, TRAIL_ACTIVATION_R, TRAIL_DISTANCE_R,
)


def _create_guardian_exchange() -> ccxt.bybit:
    """Create a separate exchange connection for the guardian thread."""
    ex = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
            "recvWindow": 20_000,        # 20s — handles Windows clock drift
        },
    })

    if not MAINNET:
        if DEMO_MODE:
            ex.enable_demo_trading(True)
        else:
            ex.set_sandbox_mode(True)

    ex.load_markets()
    return ex


class ProfitGuardian(threading.Thread):
    """Background trailing-SL thread.

    One simple rule: once R >= 1.0, trail SL at peak - 0.3R.
    Progressive tiers handle the exchange SL as a crash safety net.

    Why this beats everything else we tested:
    - No retrace detection (closed winners too early, -37R/200 trades)
    - No velocity/momentum math (added noise, no edge)
    - No BE at +0.75R (shook out 20% of eventual winners)
    - Just trail. Let the market decide when you exit.
    """

    def __init__(self, state):
        super().__init__(daemon=True, name="ProfitGuardian")
        self.state = state
        self.ex: Optional[ccxt.bybit] = None
        self._running = True

        # Per-position tracking
        self._current_tier: Dict[str, int] = {}
        self._trail_active: Dict[str, bool] = {}
        self._last_sl_update: Dict[str, float] = {}  # throttle SL moves

    @staticmethod
    def _secs_since_entry(entry: dict) -> float:
        """Seconds elapsed since entry_time (safe on missing/bad data)."""
        ts = entry.get("entry_time", "")
        if not ts:
            return 0.0
        try:
            et = datetime.fromisoformat(ts)
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - et).total_seconds()
        except Exception:
            return 0.0

    def run(self):
        """Main guardian loop."""
        try:
            log.info("Profit Guardian v3: creating exchange connection...")
            self.ex = _create_guardian_exchange()
            if TRAIL_ENABLED:
                log.info(
                    f"Profit Guardian v3: ONLINE — poll {GUARDIAN_POLL_SECS}s | "
                    f"trail activates at {TRAIL_ACTIVATION_R}R | "
                    f"trail distance {TRAIL_DISTANCE_R}R behind peak | "
                    f"{len(PROFIT_TIERS)} SL tiers (safety net)"
                )
            else:
                log.info(
                    f"Profit Guardian v3: ONLINE — poll {GUARDIAN_POLL_SECS}s | "
                    f"trail DISABLED (fixed TP on exchange) | "
                    f"{len(PROFIT_TIERS)} progressive SL tiers active"
                )
        except Exception as e:
            log.error(f"Profit Guardian v3: FAILED to connect — {e}")
            log.error(traceback.format_exc())
            return

        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.debug(f"Guardian tick error: {e}")
            time.sleep(GUARDIAN_POLL_SECS)

    def stop(self):
        """Signal the guardian to stop."""
        self._running = False
        log.info("Profit Guardian v3: stopping...")

    # ═══════════════════════════════════════════════════════════
    #  MAIN TICK
    # ═══════════════════════════════════════════════════════════

    def _tick(self):
        """One monitoring cycle — check all open positions."""
        entries = list(self.state.pending_entries)  # snapshot
        if not entries:
            return

        # Clean up tracking for closed positions
        active_symbols = {e.get("symbol", "") for e in entries}
        for s in [s for s in list(self._current_tier) if s not in active_symbols]:
            self._current_tier.pop(s, None)
            self._trail_active.pop(s, None)
            self._last_sl_update.pop(s, None)

        for entry in entries:
            if entry.get("guardian_closed"):
                continue
            try:
                self._check_position(entry)
            except ccxt.RateLimitExceeded:
                log.debug("Guardian: rate limit hit, backing off 3s")
                time.sleep(3)
            except ccxt.NetworkError as e:
                log.debug(f"Guardian: network error — {e}")
                time.sleep(1)
            except Exception as e:
                sym = entry.get("symbol", "?")
                log.debug(f"Guardian {sym}: {e}")

    # ═══════════════════════════════════════════════════════════
    #  POSITION CHECK — the core logic
    # ═══════════════════════════════════════════════════════════

    def _check_position(self, entry: dict):
        """Check one position. Two systems:

        1. Progressive SL tiers → move exchange SL as safety net
        2. Trail → once R >= activation, SL = peak - trail_distance (in R)
        """
        symbol = entry.get("symbol", "")
        direction = entry.get("direction", "")
        entry_price = entry.get("entry_price", 0)
        risk_per_unit = entry.get("risk_per_unit") or abs(
            entry_price - entry.get("original_sl", entry.get("sl", 0))
        )
        if risk_per_unit <= 0 or not symbol:
            return

        # ── Get current price ──
        ticker = self.ex.fetch_ticker(symbol)
        current_price = float(ticker.get("last", 0))
        if not current_price:
            return

        # ── Calculate current R ──
        if direction == "long":
            current_r = (current_price - entry_price) / risk_per_unit
        else:
            current_r = (entry_price - current_price) / risk_per_unit

        # ── Update peak R ──
        peak_r = entry.get("_max_r", current_r)
        if current_r > peak_r:
            peak_r = current_r
            entry["_max_r"] = peak_r
            entry["_peak_price"] = current_price

        # ── Combined: tiers + trail → compute best SL ──
        current_sl = entry.get("sl", 0)
        new_sl = current_sl
        reason = None

        # 1. Progressive SL tiers (exchange safety net)
        tier_idx = self._current_tier.get(symbol, -1)
        for i, (trigger_r, sl_r, label) in enumerate(PROFIT_TIERS):
            if i <= tier_idx:
                continue
            if current_r >= trigger_r:
                self._current_tier[symbol] = i
                tier_idx = i
                entry["_guardian_tier"] = i
                tier_sl = entry_price + (sl_r * risk_per_unit) if direction == "long" \
                    else entry_price - (sl_r * risk_per_unit)
                tier_sl = exch.round_price(self.ex, symbol, tier_sl)

                if direction == "long" and tier_sl > new_sl:
                    new_sl = tier_sl
                    reason = label
                elif direction == "short" and tier_sl < new_sl:
                    new_sl = tier_sl
                    reason = label

        # 2. Trail: once R >= activation, SL = peak - trail_distance (in R)
        #    Only active when TRAIL_ENABLED=True. Otherwise fixed TP on exchange handles exit.
        if TRAIL_ENABLED and peak_r >= TRAIL_ACTIVATION_R:
            if not self._trail_active.get(symbol):
                self._trail_active[symbol] = True
                entry["_trail_active"] = True
                log.info(
                    f"  🎯 {symbol}: TRAIL ACTIVATED — "
                    f"R={current_r:+.2f}, peak={peak_r:.2f}R, "
                    f"trailing {TRAIL_DISTANCE_R}R behind peak"
                )
                tlog.log_trail_activate(
                    symbol=symbol, current_r=current_r, peak_r=peak_r,
                    direction=direction, session=entry.get("session", ""),
                    current_price=current_price, entry_price=entry_price,
                    secs_since_entry=self._secs_since_entry(entry),
                )

            trail_r = peak_r - TRAIL_DISTANCE_R
            if direction == "long":
                trail_sl = entry_price + (trail_r * risk_per_unit)
            else:
                trail_sl = entry_price - (trail_r * risk_per_unit)
            trail_sl = exch.round_price(self.ex, symbol, trail_sl)

            # Trail SL only moves forward
            if direction == "long" and trail_sl > new_sl:
                new_sl = trail_sl
                reason = f"TRAIL peak={peak_r:.2f}R → SL at +{trail_r:.2f}R"
            elif direction == "short" and trail_sl < new_sl:
                new_sl = trail_sl
                reason = f"TRAIL peak={peak_r:.2f}R → SL at +{trail_r:.2f}R"

        # ── Move SL on exchange if improved ──
        sl_moved = False
        if reason and new_sl != current_sl:
            # Only move forward
            if direction == "long" and new_sl > current_sl:
                sl_moved = True
            elif direction == "short" and new_sl < current_sl:
                sl_moved = True

        if sl_moved:
            # Throttle: don't spam exchange SL updates (max once per 3s per position)
            now = time.time()
            last_update = self._last_sl_update.get(symbol, 0)
            if now - last_update < 3.0:
                return  # too soon, will catch up next tick

            tp = entry.get("tp", 0)
            ok = exch.set_trading_stop(
                self.ex, symbol, side=direction,
                sl_price=new_sl, tp_price=tp if tp else None,
            )
            if ok == "CLOSED":
                # Position already closed on exchange — stop tracking
                log.info(f"  {symbol}: position already closed on exchange — stopping guardian updates")
                entry["guardian_closed"] = True
                return
            elif ok:
                self._last_sl_update[symbol] = now
                old_sl_val = entry.get("sl", 0)
                entry["sl"] = new_sl
                log.info(
                    f"  ★ {symbol}: SL → {new_sl} ({reason}) | "
                    f"R={current_r:+.2f} peak={peak_r:.2f}R"
                )
                log.audit(
                    "GUARDIAN_SL", symbol=symbol,
                    current_r=f"{current_r:+.3f}",
                    peak_r=f"{peak_r:.3f}",
                    new_sl=f"{new_sl}",
                    reason=reason,
                )
                tlog.log_guardian_sl(
                    symbol=symbol, current_r=current_r, peak_r=peak_r,
                    new_sl=new_sl, old_sl=old_sl_val, reason=reason,
                    direction=direction, session=entry.get("session", ""),
                    current_price=current_price, entry_price=entry_price,
                    tier_idx=tier_idx, polls=entry.get("_guardian_polls", 0),
                    secs_since_entry=self._secs_since_entry(entry),
                )
                self.state._save()
            else:
                log.warning(f"  {symbol}: SL move failed — will retry next tick")

        # ── Health log (every ~30s) ──
        poll_count = entry.get("_guardian_polls", 0) + 1
        entry["_guardian_polls"] = poll_count
        health_interval = max(1, int(30 / GUARDIAN_POLL_SECS))
        if poll_count % health_interval == 1:
            tag = "↗" if current_r > 0 else "↘"
            tier_label = f"T{tier_idx + 1}" if tier_idx >= 0 else "T0"
            trail_tag = "TRAIL" if self._trail_active.get(symbol) else ("wait" if TRAIL_ENABLED else "off")
            log.info(
                f"  {tag} {symbol}: R={current_r:+.2f} | "
                f"peak={peak_r:.2f}R | {tier_label} | {trail_tag} | "
                f"SL={entry.get('sl', '?')}"
            )
