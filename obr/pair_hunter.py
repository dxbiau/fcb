"""
obr/pair_hunter.py -- Real-time pair hunter for A+ OBR setups.

Every 5m candle cycle, scans ALL liquid Bybit USDT perps for active
OBR signals.  Returns pairs with confirmed signals ready to trade.

Design:
  - Fetches tickers in ONE bulk call (1 API request for all pairs)
  - Filters by 24h volume > threshold
  - Fetches last 5 candles (5m) for each candidate
  - Returns pairs with active OBR-NEXTBAR signal + quality filters
  - Caches the liquid pair universe for 1 hour (no need to re-filter)
  - Skips pairs already in the static PAIRS list (bot handles those)

Integration: Called by bot.py each cycle BEFORE scanning static pairs.
"""

import time
import threading
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple, Set

from obr import logger as log
from obr import config as cfg


# ── Quality filters for hunted pairs ──────────────────────────
MIN_VOLUME_24H = 3_000_000       # $3M min 24h volume
MIN_OB_RANGE_PCT = 0.20          # OB candle range must be >= 0.20% (avoids fee-drag)
MAX_SPREAD_PCT = 0.15            # max bid-ask spread
UNIVERSE_REFRESH_MINUTES = 60    # re-fetch liquid pair list every hour


class PairHunter:
    """Scans full Bybit market for live OBR signals."""

    def __init__(self, exchange):
        self._ex = exchange
        self._universe: List[str] = []       # liquid USDT perps
        self._universe_ts: float = 0         # last refresh time
        self._lock = threading.Lock()

    # ─── Universe management ─────────────────────────────────

    def _refresh_universe(self):
        """Build list of all liquid USDT linear perps."""
        pairs = []
        for sym, mkt in self._ex.markets.items():
            if (mkt.get("swap") and mkt.get("linear")
                    and mkt.get("settle") == "USDT"
                    and mkt.get("active")):
                pairs.append(sym)

        if not pairs:
            return

        # Bulk fetch tickers (1 API call)
        try:
            tickers = self._ex.fetch_tickers(symbols=pairs[:500])
        except Exception as e:
            log.warning(f"PairHunter ticker fetch: {e}")
            # Fallback: use whatever we had
            return

        liquid = []
        for sym, t in tickers.items():
            vol = float(t.get("quoteVolume") or 0)
            spread = 0
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            if bid > 0 and ask > 0:
                spread = (ask - bid) / bid * 100

            if vol >= MIN_VOLUME_24H and spread <= MAX_SPREAD_PCT:
                liquid.append(sym)

        with self._lock:
            self._universe = liquid
            self._universe_ts = time.time()

        log.info(f"  🔭 PairHunter: {len(liquid)} liquid pairs in universe "
                 f"(from {len(pairs)} total)")

    def _ensure_universe(self):
        """Refresh universe if stale."""
        elapsed = time.time() - self._universe_ts
        if elapsed > UNIVERSE_REFRESH_MINUTES * 60 or not self._universe:
            self._refresh_universe()

    # ─── Signal detection ────────────────────────────────────

    @staticmethod
    def _detect_obr(prev: dict, curr: dict) -> int:
        """OBR signal detection (matches notebook logic exactly)."""
        if prev["high"] == prev["low"] or curr["high"] == curr["low"]:
            return 0
        # Long: bearish OB engulfs, close < prev low
        if (curr["open"] > curr["close"]
                and curr["high"] > prev["high"]
                and curr["low"] < prev["low"]
                and curr["close"] < prev["low"]):
            return 2
        # Short: bullish OB engulfs, close > prev high
        if (curr["open"] < curr["close"]
                and curr["high"] > prev["high"]
                and curr["low"] < prev["low"]
                and curr["close"] > prev["high"]):
            return 1
        return 0

    @staticmethod
    def _nextbar_confirms(signal: int, confirm: dict) -> bool:
        if signal == 2:
            return confirm["close"] > confirm["open"]
        elif signal == 1:
            return confirm["close"] < confirm["open"]
        return False

    # ─── Main hunt ───────────────────────────────────────────

    def hunt(self, static_pairs: Set[str],
             max_results: int = 10) -> List[Dict]:
        """
        Scan all liquid pairs for active OBR-NEXTBAR signals on 5m.

        Args:
            static_pairs: set of symbols already in bot's static list (skip these)
            max_results: max number of hunted pairs to return

        Returns:
            List of dicts: {symbol, direction, signal_type, ob_range_pct,
                           volume_24h, entry_est, sl, rpu}
        """
        self._ensure_universe()

        with self._lock:
            candidates = [s for s in self._universe if s not in static_pairs]

        # Scan full universe -- quality filters (trend + volume) handle
        # selectivity, we need max funnel size with $1.4K equity
        # Rate limiting handles API pressure (0.25s between fetches)

        if not candidates:
            return []

        found = []
        scanned = 0
        errors = 0
        rate_limit_hits = 0

        for sym in candidates:
            try:
                # Fetch last 5 closed 5m candles via validated helper
                from obr.exchange import fetch_latest_candles as _fetch_candles
                candles_raw = _fetch_candles(self._ex, sym, n=5, timeframe=cfg.SIGNAL_TIMEFRAME)
                if not candles_raw or len(candles_raw) < 3:
                    continue

                # Already validated & closed-only from fetch_latest_candles
                candles = candles_raw

                scanned += 1

                # Check OBR on candles[-3] vs candles[-2], confirm on candles[-1]
                if len(candles) < 3:
                    continue

                prev = candles[-3]
                ob = candles[-2]
                confirm = candles[-1]

                sig = self._detect_obr(prev, ob)
                if sig == 0:
                    # Rate limit between candle fetches (only on non-signals)
                    time.sleep(0.25)
                    continue

                if not self._nextbar_confirms(sig, confirm):
                    time.sleep(0.25)
                    continue

                # Quality check: OB range must be meaningful
                mid = (ob["high"] + ob["low"]) / 2
                ob_range_pct = (ob["high"] - ob["low"]) / mid * 100 if mid > 0 else 0

                if ob_range_pct < MIN_OB_RANGE_PCT:
                    continue

                # Compute trade params
                direction = "long" if sig == 2 else "short"
                entry_est = confirm["close"]  # approximate next open

                if direction == "long":
                    sl = ob["low"]
                    rpu = entry_est - sl
                else:
                    sl = ob["high"]
                    rpu = sl - entry_est

                if rpu <= 0:
                    continue

                # Fee check: if fee > 0.25R, skip (too tight)
                fee_r = (cfg.FEE_RATE * 2 * entry_est) / rpu if rpu > 0 else 99
                if fee_r > 0.25:
                    continue

                # Use volume from universe refresh tickers (no extra API call)
                vol_24h = MIN_VOLUME_24H  # passed volume filter in universe

                found.append({
                    "symbol": sym,
                    "direction": direction,
                    "signal_type": sig,
                    "ob_range_pct": round(ob_range_pct, 3),
                    "fee_r": round(fee_r, 3),
                    "volume_24h": vol_24h,
                    "entry_est": entry_est,
                    "sl": sl,
                    "rpu": rpu,
                    "ob_candle": ob,
                    "prev_candle": prev,
                    "confirm_candle": confirm,
                })

                time.sleep(0.25)

                if len(found) >= max_results:
                    break

            except Exception as e:
                errors += 1
                err_str = str(e).lower()
                if "rate limit" in err_str or "10006" in err_str:
                    rate_limit_hits += 1
                    time.sleep(2.0 * rate_limit_hits)  # progressive backoff
                elif errors > 5:
                    time.sleep(1)
                continue

        # Sort by quality: wider OB range = better signal (less noise)
        found.sort(key=lambda x: -x["ob_range_pct"])

        if found:
            log.info(f"  🎯 PairHunter: {len(found)} A+ signals from "
                     f"{scanned} scanned (universe={len(candidates)})")
            for h in found[:5]:
                short = h["symbol"].split("/")[0]
                dc = "🟢" if h["direction"] == "long" else "🔴"
                log.info(f"    {dc} {short:<8} {h['direction'].upper():<5} "
                         f"OB={h['ob_range_pct']:.2f}% fee={h['fee_r']:.2f}R "
                         f"vol=${h['volume_24h']/1e6:.1f}M")
        else:
            log.debug(f"  🔍 PairHunter: 0 signals from {scanned} scanned "
                      f"(universe={len(candidates)})")

        return found[:max_results]
