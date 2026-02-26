"""
obr/strategies.py -- Multi-strategy signal engine for FCB v13 portfolio.

Implements 12 validated strategies from the discovery engine, exactly matching
the signal logic in _discovery_v13.py. Each strategy generates signals on
OHLCV arrays and returns standardised signal dicts.

Strategies:
  EMA_RIB, BB_BREAK, DONCHIAN, RSI_FADE, BB_FADE, STOCH_X,
  PIN_BAR, IB_BREAK, ENGULF, MTF_RSI, TR_PULL, MOM_SURGE

Plus ensemble:
  ENS2 (2+ strategies agree), ENS3 (3+ strategies agree)
"""

import numpy as np
from typing import List, Dict, Optional, Tuple


# ═══════════════════════════════════════════════════════
#  Technical indicator helpers (matching discovery exactly)
# ═══════════════════════════════════════════════════════

def ema(arr: np.ndarray, period: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = np.mean(arr[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def sma(arr: np.ndarray, period: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    cs = np.cumsum(arr)
    out[period - 1] = cs[period - 1] / period
    for i in range(period, n):
        out[i] = (cs[i] - cs[i - period]) / period
    return out


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = np.full(n, np.nan)
    if n >= period:
        out[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger_bands(c: np.ndarray, period: int = 20, mult: float = 2.0):
    mid = sma(c, period)
    n = len(c)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        s = np.std(c[i - period + 1:i + 1])
        upper[i] = mid[i] + mult * s
        lower[i] = mid[i] - mult * s
    return upper, mid, lower


def donchian_channels(h: np.ndarray, l: np.ndarray, period: int = 20):
    n = len(h)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        upper[i] = np.max(h[i - period + 1:i + 1])
        lower[i] = np.min(l[i - period + 1:i + 1])
    return upper, lower


def rsi(c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 2:
        return out
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def stochastic(h: np.ndarray, l: np.ndarray, c: np.ndarray,
               k_period: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    n = len(c)
    raw_k = np.full(n, np.nan)
    for i in range(k_period - 1, n):
        hh = np.max(h[i - k_period + 1:i + 1])
        ll = np.min(l[i - k_period + 1:i + 1])
        raw_k[i] = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
    k = sma(raw_k, k_smooth)
    d = sma(k, d_smooth)
    return k, d


def min_stop_loss(price: float, atr_val: float, maker_fees: bool = True) -> float:
    """Minimum stop-loss distance ensuring stop > 3x round-trip fees."""
    # Full maker: entry=0.02% + SL=0.02% = 0.04% round-trip
    # Current: entry=0.02% + SL=0.055%+0.03%slip = 0.105% round-trip
    msl_fee = 0.0004 if maker_fees else 0.00105
    return max(price * msl_fee * 2 * 3, atr_val, price * 0.003)


# ═══════════════════════════════════════════════════════
#  Signal type
# ═══════════════════════════════════════════════════════

class Signal:
    """Standardised signal from any strategy."""
    __slots__ = ('bar', 'side', 'entry', 'stop_dist', 'strategy', 'pair', 'tf')

    def __init__(self, bar: int, side: str, entry: float, stop_dist: float,
                 strategy: str = '', pair: str = '', tf: str = ''):
        self.bar = bar
        self.side = side  # 'long' or 'short'
        self.entry = entry
        self.stop_dist = stop_dist
        self.strategy = strategy
        self.pair = pair
        self.tf = tf


# ═══════════════════════════════════════════════════════
#  12 Strategy implementations
# ═══════════════════════════════════════════════════════

def S_ema_rib(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """EMA_RIB: 8/21/55 EMA ribbon pullback with body confirmation."""
    e8 = ema(c, 8)
    e21 = ema(c, 21)
    e55 = ema(c, 55)
    sigs = []
    n = len(c)
    for i in range(2, n):
        if np.isnan(e55[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        if (e8[i] > e21[i] > e55[i] and l[i] <= e8[i] * 1.005
                and c[i] > o[i] and body / rng > 0.3):
            sigs.append(Signal(i, 'long', c[i], sd))
        elif (e8[i] < e21[i] < e55[i] and h[i] >= e8[i] * 0.995
              and c[i] < o[i] and body / rng > 0.3):
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_bb_break(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """BB_BREAK: Bollinger Band breakout + EMA50 trend + volume."""
    upper, mid, lower = bollinger_bands(c)
    e50 = ema(c, 50)
    vm = sma(v, 20)
    sigs = []
    n = len(c)
    for i in range(1, n):
        if np.isnan(upper[i]) or np.isnan(e50[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        vok = not np.isnan(vm[i]) and vm[i] > 0 and v[i] > vm[i] * 1.2
        if c[i] > upper[i] and c[i] > e50[i] and c[i] > o[i] and vok:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif c[i] < lower[i] and c[i] < e50[i] and c[i] < o[i] and vok:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_donchian(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """DONCHIAN: 20-period channel breakout."""
    du, dl = donchian_channels(h, l)
    sigs = []
    n = len(c)
    for i in range(1, n):
        if np.isnan(du[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if not np.isnan(du[i - 1]) and c[i] > du[i - 1]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif not np.isnan(dl[i - 1]) and c[i] < dl[i - 1]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_rsi_fade(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """RSI_FADE: RSI reversal from 25/75 extremes."""
    rsi_arr = rsi(c, 14)
    sigs = []
    for i in range(2, len(c)):
        if np.isnan(rsi_arr[i]) or np.isnan(rsi_arr[i - 1]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if rsi_arr[i - 1] < 25 and rsi_arr[i] > 25 and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif rsi_arr[i - 1] > 75 and rsi_arr[i] < 75 and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_bb_fade(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """BB_FADE: Bollinger Band bounce (mean reversion)."""
    upper, mid, lower = bollinger_bands(c, 20, 2.0)
    sigs = []
    for i in range(2, len(c)):
        if np.isnan(upper[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if l[i] <= lower[i] and c[i] > lower[i] and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif h[i] >= upper[i] and c[i] < upper[i] and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_stoch_cross(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """STOCH_X: Stochastic crossover from extremes + SMA50."""
    k, d = stochastic(h, l, c, 14, 3, 3)
    s50 = sma(c, 50)
    sigs = []
    for i in range(2, len(c)):
        if (np.isnan(k[i]) or np.isnan(d[i]) or np.isnan(k[i - 1])
                or np.isnan(d[i - 1]) or np.isnan(s50[i])
                or np.isnan(atr_arr[i]) or atr_arr[i] <= 0):
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if k[i - 1] < d[i - 1] and k[i] > d[i] and k[i - 1] < 25 and c[i] > s50[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif k[i - 1] > d[i - 1] and k[i] < d[i] and k[i - 1] > 75 and c[i] < s50[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_pin_bar(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """PIN_BAR: Pin bar reversal with trend filter."""
    s50 = sma(c, 50)
    sigs = []
    for i in range(2, len(c)):
        if np.isnan(s50[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng <= 0 or body <= 0:
            continue
        upper_shadow = h[i] - max(c[i], o[i])
        lower_shadow = min(c[i], o[i]) - l[i]
        if (lower_shadow > 2 * body and upper_shadow < body * 0.5
                and c[i] > o[i] and c[i] > s50[i]):
            sigs.append(Signal(i, 'long', c[i], sd))
        elif (upper_shadow > 2 * body and lower_shadow < body * 0.5
              and c[i] < o[i] and c[i] < s50[i]):
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_inside_bar(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """IB_BREAK: Inside bar breakout."""
    s50 = sma(c, 50)
    sigs = []
    for i in range(3, len(c)):
        if np.isnan(atr_arr[i]) or atr_arr[i] <= 0 or np.isnan(s50[i]):
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if h[i - 1] < h[i - 2] and l[i - 1] > l[i - 2]:
            if c[i] > h[i - 1] and c[i] > s50[i]:
                sigs.append(Signal(i, 'long', c[i], sd))
            elif c[i] < l[i - 1] and c[i] < s50[i]:
                sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_engulf(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """ENGULF: Engulfing pattern + volume."""
    s50 = sma(c, 50)
    vm = sma(v, 20)
    sigs = []
    for i in range(2, len(c)):
        if np.isnan(s50[i]) or np.isnan(atr_arr[i]) or atr_arr[i] <= 0:
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        vok = not np.isnan(vm[i]) and vm[i] > 0 and v[i] > vm[i] * 1.0
        bc = c[i] - o[i]
        bp = c[i - 1] - o[i - 1]
        if (bp < 0 and bc > 0 and o[i] <= c[i - 1] and c[i] >= o[i - 1]
                and c[i] > s50[i] and vok):
            sigs.append(Signal(i, 'long', c[i], sd))
        elif (bp > 0 and bc < 0 and o[i] >= c[i - 1] and c[i] <= o[i - 1]
              and c[i] < s50[i] and vok):
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_mtf_rsi(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """MTF_RSI: SMA200 trend + RSI pullback."""
    s200 = sma(c, 200)
    rsi_arr = rsi(c, 14)
    sigs = []
    for i in range(2, len(c)):
        if (np.isnan(s200[i]) or np.isnan(rsi_arr[i])
                or np.isnan(rsi_arr[i - 1]) or np.isnan(atr_arr[i])
                or atr_arr[i] <= 0):
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if c[i] > s200[i] and rsi_arr[i - 1] < 40 and rsi_arr[i] > 40 and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif c[i] < s200[i] and rsi_arr[i - 1] > 60 and rsi_arr[i] < 60 and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_trend_pullback(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """TR_PULL: Trend pullback (EMA21/55 + RSI confirmation)."""
    e21 = ema(c, 21)
    e55 = ema(c, 55)
    rsi_arr = rsi(c, 14)
    sigs = []
    for i in range(3, len(c)):
        if (np.isnan(e21[i]) or np.isnan(e55[i]) or np.isnan(atr_arr[i])
                or atr_arr[i] <= 0 or np.isnan(rsi_arr[i])
                or np.isnan(rsi_arr[i - 1])):
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        if (e21[i] > e55[i] and l[i] <= e21[i] * 1.005 and c[i] > o[i]
                and rsi_arr[i] > rsi_arr[i - 1] and rsi_arr[i] < 60):
            sigs.append(Signal(i, 'long', c[i], sd))
        elif (e21[i] < e55[i] and h[i] >= e21[i] * 0.995 and c[i] < o[i]
              and rsi_arr[i] < rsi_arr[i - 1] and rsi_arr[i] > 40):
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


def S_mom_surge(o, h, l, c, v, atr_arr, maker_fees=True) -> List[Signal]:
    """MOM_SURGE: Momentum surge (1.5xATR body + 2x volume)."""
    vm = sma(v, 20)
    e20 = ema(c, 20)
    sigs = []
    for i in range(2, len(c)):
        if (np.isnan(atr_arr[i]) or atr_arr[i] <= 0
                or np.isnan(vm[i]) or vm[i] <= 0
                or np.isnan(e20[i])):
            continue
        sd = min_stop_loss(c[i], atr_arr[i], maker_fees)
        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        if (c[i] > o[i] and body > 1.5 * atr_arr[i] and v[i] > vm[i] * 2.0
                and c[i] > e20[i] and body / rng > 0.6):
            sigs.append(Signal(i, 'long', c[i], sd))
        elif (c[i] < o[i] and body > 1.5 * atr_arr[i] and v[i] > vm[i] * 2.0
              and c[i] < e20[i] and body / rng > 0.6):
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs


# ═══════════════════════════════════════════════════════
#  Strategy registry
# ═══════════════════════════════════════════════════════

STRATEGY_REGISTRY = {
    'EMA_RIB': S_ema_rib,
    'BB_BREAK': S_bb_break,
    'DONCHIAN': S_donchian,
    'RSI_FADE': S_rsi_fade,
    'BB_FADE': S_bb_fade,
    'STOCH_X': S_stoch_cross,
    'PIN_BAR': S_pin_bar,
    'IB_BREAK': S_inside_bar,
    'ENGULF': S_engulf,
    'MTF_RSI': S_mtf_rsi,
    'TR_PULL': S_trend_pullback,
    'MOM_SURGE': S_mom_surge,
}


def scan_bar_all_strategies(o, h, l, c, v, atr_arr, bar_idx: int,
                            strategies: List[str] = None,
                            maker_fees: bool = True) -> List[Signal]:
    """
    Check if ANY registered strategy fires on the given bar index.

    This is the LIVE version -- runs strategies on full arrays but only
    returns signals for the specific bar_idx (the just-closed candle).
    Efficient: computes indicators once per full array, checks all bars,
    filters to the target bar.

    Args:
        o, h, l, c, v: full OHLCV arrays (enough history for indicators)
        atr_arr: pre-computed ATR array
        bar_idx: which bar to check (-1 for last)
        strategies: list of strategy names to check (None = all)
        maker_fees: True for full-maker fee model

    Returns:
        List of Signal objects that fired on bar_idx
    """
    if bar_idx < 0:
        bar_idx = len(c) + bar_idx

    strat_names = strategies or list(STRATEGY_REGISTRY.keys())
    fired = []

    for name in strat_names:
        func = STRATEGY_REGISTRY.get(name)
        if func is None:
            continue
        all_sigs = func(o, h, l, c, v, atr_arr, maker_fees)
        for sig in all_sigs:
            if sig.bar == bar_idx:
                sig.strategy = name
                fired.append(sig)

    return fired


def scan_last_bar(o, h, l, c, v, strategies: List[str] = None,
                  maker_fees: bool = True) -> List[Signal]:
    """
    Convenience: scan the last closed candle for signals.

    Args:
        o, h, l, c, v: OHLCV arrays (last element = just-closed candle)
        strategies: list of strategy names to check
        maker_fees: True for full-maker fee model

    Returns:
        List of Signal objects that fired on the last bar
    """
    atr_arr = atr(h, l, c, 14)
    return scan_bar_all_strategies(o, h, l, c, v, atr_arr, -1, strategies, maker_fees)


def generate_ensemble_signals(signals: List[Signal], min_agree: int = 2) -> List[Signal]:
    """
    Generate ensemble signals where min_agree strategies agree on the same bar+side.

    Args:
        signals: all signals from individual strategies
        min_agree: minimum number of strategies that must agree

    Returns:
        List of ensemble Signal objects
    """
    from collections import defaultdict

    # Group by (bar, side)
    groups = defaultdict(list)
    for sig in signals:
        groups[(sig.bar, sig.side)].append(sig)

    ensemble = []
    for (bar, side), sigs in groups.items():
        if len(sigs) >= min_agree:
            # Use the median stop distance among agreeing strategies
            stop_dists = [s.stop_dist for s in sigs]
            median_sd = sorted(stop_dists)[len(stop_dists) // 2]
            ens_name = f"ENS{min_agree}"
            strat_names = '+'.join(s.strategy for s in sigs)
            ensemble.append(Signal(bar, side, sigs[0].entry, median_sd,
                                   strategy=f"{ens_name}({strat_names})"))

    return ensemble
