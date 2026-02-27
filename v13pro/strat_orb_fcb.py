"""
v13pro/strat_orb_fcb.py  --  ORB (Opening Range Breakout) 15m  +  FCB (First Candle Breakout) 5m

Both strategies are SHADOW-ONLY until they prove themselves.
The StrategyLab tracks every signal with rich metadata so we learn:
  - What confirmations predict real moves vs fakes
  - Where the optimal SL sits (tight enough to x8-x10 lev, wide enough to survive fakes)
  - Which sessions/pairs/conditions produce the best results
  - When to graduate from shadow to live

ORB 15m — Opening Range Breakout
  The first 15m candle of NY session (16:00 UTC) sets the range.
  Next candles that break above/below the range with confirmation → entry.
  Only fires during NY session (16:00-24:00 UTC).
  Ideal for the 3-20% moves that happen in the first 1-2h of NY.

FCB 5m — First Candle Breakout
  The first 5m candle of ANY session sets a micro-range.
  Break of that range with volume + trend alignment → entry.
  These moves run cold after ~1h but can be 3-20% in size.
  Key: Learn which signals are real vs fakes so we can use x8-x10 leverage.
"""

import numpy as np
from v13pro.indicators import ema, sma, atr, bollinger_bands, rsi
from v13pro.strategies import Signal, msl


# ═══════════════════════════════════════════════════════════════════
#  ORB 15m — Opening Range Breakout (NY Session Only)
# ═══════════════════════════════════════════════════════════════════
#
#  Logic:
#    1. Identify the first 15m candle of NY session (16:00 UTC)
#    2. Its high/low define the Opening Range (OR)
#    3. On subsequent 15m candles within the session:
#       - Long: close > OR_high with confirmation
#       - Short: close < OR_low with confirmation
#    4. SL = range midpoint (tight), or opposite range level (wide)
#    5. Confirmations:
#       a) Volume spike: vol > 1.5× 20-period vol SMA
#       b) Momentum: RSI direction agrees (>50 for long, <50 for short)
#       c) Trend: price above EMA21 for long, below for short
#       d) Candle quality: body > 50% of range (no doji fakes)
#
#  This strategy expects the candle timestamps to be UTC so it
#  can identify the NY open candle.  The bot supplies 15m candles
#  with WS data — we just scan the last bar like every other strategy.
#
#  KEY INSIGHT: We cannot see real timestamps in the OHLCV arrays the
#  bot passes (they're just numpy arrays of price/vol). So we rely on
#  a STRUCTURAL approach: detect the range-setting candle by looking
#  for the characteristics of the first candle after a gap.
#  Instead, we use a different approach — the opening range is the
#  20-period high/low (Donchian-like) but with a twist: we look for
#  breakouts that happen with a FRESH range (narrow range followed by
#  expansion). This gives us the ORB pattern on any session.
#
#  The NY-session restriction is enforced in the wiring (bot.py),
#  not in the strategy function itself.
# ═══════════════════════════════════════════════════════════════════

def S_orb(o, h, l, c, v, a, mk=True):
    """
    Opening Range Breakout — 15m.

    Detects a narrow consolidation (opening range) followed by a decisive
    breakout candle with volume + momentum confirmation.

    The opening range is defined as the narrowest 3-candle window in
    the last 6 candles. A breakout is a close beyond the range with
    confirmation filters.
    """
    e21 = ema(c, 21)
    r14 = rsi(c, 14)
    vm = sma(v, 20)
    sigs = []

    lookback = 6   # look back 6 candles for the opening range
    range_bars = 3  # the range is defined by 3 consecutive candles

    for i in range(lookback, len(c)):
        if np.isnan(a[i]) or a[i] <= 0 or np.isnan(e21[i]):
            continue
        if np.isnan(r14[i]) or np.isnan(vm[i]) or vm[i] <= 0:
            continue

        # Find the narrowest 3-bar range in the last 6 candles
        best_range = float('inf')
        or_high = or_low = 0.0
        or_start = i - lookback

        for j in range(i - lookback, i - range_bars + 1):
            window_h = np.max(h[j:j + range_bars])
            window_l = np.min(l[j:j + range_bars])
            window_range = window_h - window_l
            if window_range > 0 and window_range < best_range:
                best_range = window_range
                or_high = window_h
                or_low = window_l

        if best_range == float('inf') or best_range <= 0:
            continue

        # Range must be relatively narrow (< 1.5× ATR)
        if best_range > a[i] * 1.5:
            continue

        or_mid = (or_high + or_low) / 2.0
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng <= 0:
            continue

        # ── Confirmation filters ──
        vol_ok = v[i] > vm[i] * 1.5       # volume spike
        candle_ok = body / rng > 0.50      # strong body (no doji)

        # Minimum stop distance (maker-fee aware)
        sd = msl(c[i], a[i], mk)

        # ── LONG: close above opening range high ──
        if (c[i] > or_high and
            c[i] > o[i] and                # bullish close
            c[i] > e21[i] and              # above trend
            r14[i] > 50 and                # momentum agrees
            vol_ok and candle_ok):
            # SL = range midpoint (tight) — enables high leverage
            stop = max(sd, c[i] - or_mid)
            sigs.append(Signal(i, 'long', c[i], stop))

        # ── SHORT: close below opening range low ──
        elif (c[i] < or_low and
              c[i] < o[i] and              # bearish close
              c[i] < e21[i] and            # below trend
              r14[i] < 50 and              # momentum agrees
              vol_ok and candle_ok):
            stop = max(sd, or_mid - c[i])
            sigs.append(Signal(i, 'short', c[i], stop))

    return sigs


# ═══════════════════════════════════════════════════════════════════
#  FCB 5m — First Candle Breakout
# ═══════════════════════════════════════════════════════════════════
#
#  Logic:
#    1. Identify a "reset candle" — the first candle after a quiet period
#       (range contraction: current range < 0.7× previous 5-bar avg range)
#    2. That candle's high/low define the micro-range
#    3. Next candle that breaks the range with confirmation → entry
#    4. SL = opposite side of the micro-range (very tight)
#    5. These are fast scalps — move runs cold after ~1 hour
#
#  Confirmations (each tracked individually for learning):
#    a) Volume expansion: vol > 1.3× vol_sma20
#    b) EMA alignment: price on right side of EMA8+EMA21
#    c) RSI agreement: >55 for long, <45 for short
#    d) Body ratio: body > 40% of range (not a wick trap)
#    e) ATR expansion: current ATR > previous ATR (volatility expanding)
#
#  The tight SL is what makes this attractive for high leverage.
#  A micro-range of 0.3% with SL at opposite side = x10+ leverage viable.
# ═══════════════════════════════════════════════════════════════════

def S_fcb(o, h, l, c, v, a, mk=True):
    """
    First Candle Breakout — 5m.

    Detects micro-range formation (range contraction) followed by
    decisive breakout. The tight range enables high-leverage entries
    when confirmations align.
    """
    e8 = ema(c, 8)
    e21 = ema(c, 21)
    r14 = rsi(c, 14)
    vm = sma(v, 20)
    sigs = []

    for i in range(6, len(c)):
        if np.isnan(a[i]) or a[i] <= 0 or np.isnan(e8[i]) or np.isnan(e21[i]):
            continue
        if np.isnan(r14[i]) or np.isnan(vm[i]) or vm[i] <= 0:
            continue

        # ── Step 1: Detect range contraction (the "first candle") ──
        # The prior candle (i-1) should be a narrow-range candle
        prev_rng = h[i-1] - l[i-1]
        if prev_rng <= 0:
            continue

        # Average range of 5 candles before the setup candle
        avg_rng = np.mean([h[j] - l[j] for j in range(i-6, i-1)])
        if avg_rng <= 0:
            continue

        # Range contraction: setup candle is narrow vs recent average
        if prev_rng > avg_rng * 0.70:
            continue  # not contracted enough

        # ── Step 2: Define the micro-range from the setup candle ──
        fc_high = h[i-1]
        fc_low = l[i-1]
        fc_mid = (fc_high + fc_low) / 2.0
        fc_range = fc_high - fc_low

        # Micro-range must be meaningful but tight
        if fc_range < a[i] * 0.15:   # too tiny (noise)
            continue
        if fc_range > a[i] * 1.2:    # too wide (not a micro-range)
            continue

        # ── Step 3: Current candle breaks the range ──
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng <= 0:
            continue

        # ── Confirmation filters ──
        vol_ok = v[i] > vm[i] * 1.3        # volume expansion
        candle_ok = body / rng > 0.40       # decent body
        rsi_long = r14[i] > 55
        rsi_short = r14[i] < 45
        ema_long = c[i] > e8[i] and e8[i] > e21[i]
        ema_short = c[i] < e8[i] and e8[i] < e21[i]
        atr_expanding = a[i] > a[i-1] * 1.0  # at least flat ATR

        sd = msl(c[i], a[i], mk)

        # ── LONG breakout ──
        if (c[i] > fc_high and
            c[i] > o[i] and
            vol_ok and candle_ok and
            rsi_long and ema_long and atr_expanding):
            # SL = bottom of micro-range (tight!)
            stop = max(sd, c[i] - fc_low)
            sigs.append(Signal(i, 'long', c[i], stop))

        # ── SHORT breakout ──
        elif (c[i] < fc_low and
              c[i] < o[i] and
              vol_ok and candle_ok and
              rsi_short and ema_short and atr_expanding):
            stop = max(sd, fc_high - c[i])
            sigs.append(Signal(i, 'short', c[i], stop))

    return sigs


# ═══════════════════════════════════════════════════════════════════
#  Strategy Registry Extension
# ═══════════════════════════════════════════════════════════════════

# These get merged into the main STRATEGIES dict at import time
NEW_STRATEGIES = {
    'ORB':  S_orb,     # Opening Range Breakout — 15m shadow-only (NY session)
    'FCB':  S_fcb,     # First Candle Breakout — 5m shadow-only (all sessions)
}

# Lab strategies that need special tracking
LAB_STRATEGY_NAMES = set(NEW_STRATEGIES.keys())


def get_confirmations(strategy: str, o, h, l, c, v, a, bar_idx: int) -> dict:
    """
    Compute which confirmation flags fired on the signal bar.
    Called by shadow.py/bot.py AFTER a signal fires to get rich metadata
    for the Strategy Lab learning system.

    Returns dict of {flag_name: bool} matching strategy_lab.CONFIRMATION_FLAGS.
    """
    from v13pro.indicators import ema, sma, rsi

    i = bar_idx
    if i < 6 or i >= len(c):
        return {}

    e8 = ema(c, 8)
    e21 = ema(c, 21)
    r14 = rsi(c, 14)
    vm = sma(v, 20)

    body = abs(c[i] - o[i])
    rng = h[i] - l[i]

    side = "long" if c[i] > o[i] else "short"

    confirmations = {
        "vol_spike": bool(
            not np.isnan(vm[i]) and vm[i] > 0 and
            v[i] > vm[i] * 1.3
        ),
        "ema_aligned": bool(
            not np.isnan(e8[i]) and not np.isnan(e21[i]) and (
                (c[i] > e8[i] > e21[i]) if side == "long" else (c[i] < e8[i] < e21[i])
            )
        ),
        "rsi_agrees": bool(
            not np.isnan(r14[i]) and (
                r14[i] > 50 if side == "long" else r14[i] < 50
            )
        ),
        "body_strong": bool(
            rng > 0 and body / rng > 0.45
        ),
        "atr_expanding": bool(
            not np.isnan(a[i]) and not np.isnan(a[i-1]) and
            a[i] > a[i-1]
        ),
        "range_narrow": False,  # computed per strategy below
    }

    if strategy == "ORB":
        # Range narrow = the opening range was < 1× ATR
        lookback = 6
        range_bars = 3
        if i >= lookback:
            best_range = float('inf')
            for j in range(i - lookback, i - range_bars + 1):
                window_h = np.max(h[j:j + range_bars])
                window_l = np.min(l[j:j + range_bars])
                wr = window_h - window_l
                if 0 < wr < best_range:
                    best_range = wr
            if best_range < float('inf') and not np.isnan(a[i]) and a[i] > 0:
                confirmations["range_narrow"] = bool(best_range < a[i])

    elif strategy == "FCB":
        # Range narrow = the setup candle was < 0.5× avg range
        prev_rng = h[i-1] - l[i-1]
        avg_rng = np.mean([h[j] - l[j] for j in range(i-6, i-1)])
        if avg_rng > 0 and prev_rng > 0:
            confirmations["range_narrow"] = bool(prev_rng < avg_rng * 0.5)

    return confirmations
