"""
live/pair_scanner.py — Dynamic Pair Scanner + Intelligence

Scans Bybit USDT perps BEFORE each session to find pairs that are
actually liquid and moving RIGHT NOW.  Replaces the static PAIRS dict.

Called by bot.py at each session start → returns list of tradeable pairs.

v2: Integrates pair_intel.py to profile each candidate with 24h of
    historical candle data before ranking.  Pairs with poor breakout
    follow-through or heavy congestion are deprioritised.
"""

import time
from typing import List, Dict, Tuple

from live import exchange as exch
from live import logger as log
from live.config import (
    SCAN_MIN_TURNOVER, SCAN_MAX_TURNOVER,
    SCAN_MAX_SPREAD_PCT, SCAN_MIN_RANGE_PCT,
    SCAN_MAX_PRICE, SCAN_MAX_PAIRS, SCAN_ALWAYS_TRADE,
    API_DELAY_SECS, INTEL_ENABLED, INTEL_MIN_FITNESS,
)
from live.pair_intel import rank_pairs


def scan_session_pairs(exchange, session: str = "asia") -> List[Tuple[str, str]]:
    """Scan Bybit for tradeable USDT perps right now.

    Returns list of (symbol, class) tuples sorted by fitness then turnover.
    - "A" class = on the ALWAYS_TRADE list (proven live winners)
    - "B" class = dynamically discovered (meets criteria)

    Phase 1: Ticker-based filtering (volume, spread, range, price)
    Phase 2: Intel profiling — 24h candle history analysis (if enabled)
    """
    t0 = time.time()

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        log.error(f"[SCANNER] Failed to fetch tickers: {e}")
        return []

    markets = exchange.markets
    if not markets:
        try:
            markets = exchange.load_markets()
        except Exception as e:
            log.error(f"[SCANNER] Failed to load markets: {e}")
            return []

    candidates = []
    skipped = {"no_ticker": 0, "no_bid": 0, "spread": 0, "volume": 0,
               "range": 0, "price": 0, "not_linear": 0}

    for sym, m in markets.items():
        # Only linear USDT perps
        if not m.get("linear") or not m.get("active") or ":USDT" not in sym:
            skipped["not_linear"] += 1
            continue

        base = m.get("base", "")
        if base == "USDT":
            continue

        t = tickers.get(sym)
        if not t:
            skipped["no_ticker"] += 1
            continue

        bid = t.get("bid") or 0
        ask = t.get("ask") or 0
        if bid <= 0 or ask <= 0:
            skipped["no_bid"] += 1
            continue

        # Spread check
        spread_pct = (ask - bid) / bid * 100
        if spread_pct > SCAN_MAX_SPREAD_PCT:
            skipped["spread"] += 1
            continue

        # Volume check (rolling 24h turnover in USDT)
        turnover = t.get("quoteVolume", 0) or 0
        if turnover < SCAN_MIN_TURNOVER or turnover > SCAN_MAX_TURNOVER:
            skipped["volume"] += 1
            continue

        # Range check (24h high-low range)
        hi = t.get("high", 0) or 0
        lo = t.get("low", 0) or 0
        if lo <= 0:
            skipped["range"] += 1
            continue
        range_pct = (hi - lo) / lo * 100
        if range_pct < SCAN_MIN_RANGE_PCT:
            skipped["range"] += 1
            continue

        # Price cap (avoids BTC-sized position issues on small accounts)
        price = t.get("last", 0) or 0
        if price > SCAN_MAX_PRICE:
            skipped["price"] += 1
            continue

        # Determine class
        cls = "A" if sym in SCAN_ALWAYS_TRADE else "B"

        candidates.append({
            "symbol": sym,
            "base": base,
            "class": cls,
            "turnover": turnover,
            "spread": spread_pct,
            "range": range_pct,
            "price": price,
        })

    # Sort: A-class first (proven), then by turnover descending
    candidates.sort(key=lambda x: (0 if x["class"] == "A" else 1, -x["turnover"]))

    # ── Phase 2: Intel Profiling ──
    if INTEL_ENABLED and candidates:
        log.info(f"[SCANNER] ── INTEL PROFILING {len(candidates)} candidates... ──")
        candidates = rank_pairs(exchange, candidates, session,
                                min_fitness=INTEL_MIN_FITNESS)
        # rank_pairs returns sorted, filtered list with profiles attached

    # Cap total pairs
    selected = candidates[:SCAN_MAX_PAIRS]

    elapsed = time.time() - t0

    # Log results
    n_a = sum(1 for c in selected if c["class"] == "A")
    n_b = len(selected) - n_a
    log.info(f"[SCANNER] Scanned {len(markets)} markets in {elapsed:.1f}s")
    log.info(f"[SCANNER] Found {len(candidates)} qualifying → selected {len(selected)} "
             f"({n_a} A + {n_b} B)")
    log.info(f"[SCANNER] Filtered out: {skipped}")

    if selected:
        # Log top 10 by turnover
        log.info(f"[SCANNER] Top pairs:")
        for c in selected[:10]:
            log.info(f"  {c['class']} {c['base']:<14} "
                     f"vol=${c['turnover']/1e6:>8.1f}M  "
                     f"spread={c['spread']:.3f}%  "
                     f"range={c['range']:.1f}%  "
                     f"price=${c['price']:.4f}")
        if len(selected) > 10:
            log.info(f"  ... and {len(selected) - 10} more")

    return [(c["symbol"], c["class"]) for c in selected], selected


def scan_and_configure(exchange, state, session: str = "asia") -> List[str]:
    """Full scan + configure leverage/margin for new pairs.

    Called by bot.py before each session.
    Returns (flat_symbols, pairs_with_class, profiles_dict).
    """
    pairs_with_class_tuples, full_candidates = scan_session_pairs(exchange, session=session)

    if not pairs_with_class_tuples:
        log.warning("[SCANNER] No pairs found! Using ALWAYS_TRADE fallback")
        pairs_with_class_tuples = [(sym, "A") for sym in SCAN_ALWAYS_TRADE]
        full_candidates = []

    # Extract profiles dict: symbol → PairProfile (or None)
    profiles = {}
    for cand in full_candidates:
        prof = cand.get("profile")
        if prof is not None:
            profiles[cand["symbol"]] = prof

    symbols = []
    new_pairs = []

    from live.config import LEVERAGE

    for sym, cls in pairs_with_class_tuples:
        symbols.append(sym)

        # Update pair class in state (must be a dict, not bare string)
        if sym not in state.pair_classes:
            state.pair_classes[sym] = {
                "class": cls,
                "consec_wins": 0,
                "consec_losses": 0,
                "live_wins": 0,
                "live_losses": 0,
                "promoted": False,
                "demoted": False,
            }
            new_pairs.append(sym)

    # Configure any newly discovered pairs (leverage + margin)
    if new_pairs:
        log.info(f"[SCANNER] Configuring {len(new_pairs)} new pairs "
                 f"(leverage={LEVERAGE}x, isolated margin)...")
        for pair in new_pairs:
            try:
                exch.set_leverage(exchange, pair, LEVERAGE)
                exch.set_margin_mode(exchange, pair, "isolated")
                # Get market info for order sizing
                info = exch.get_market_info(exchange, pair)
                # Store in global market_info (will be passed back)
            except Exception as e:
                log.warning(f"[SCANNER] {pair}: config failed — {e}")
            time.sleep(API_DELAY_SECS)

    log.info(f"[SCANNER] Session ready: {len(symbols)} pairs "
             f"({sum(1 for _, c in pairs_with_class_tuples if c == 'A')} A + "
             f"{sum(1 for _, c in pairs_with_class_tuples if c == 'B')} B)")

    if profiles:
        log.info(f"[SCANNER] Intel profiles: {len(profiles)} pairs profiled")

    return symbols, pairs_with_class_tuples, profiles
