"""
v13pro/indicators.py -- Indicator library (exact match to discovery v13).
"""
import numpy as np


def ema(arr: np.ndarray, period: int) -> np.ndarray:
    n = len(arr); out = np.full(n, np.nan)
    if n < period: return out
    out[period - 1] = np.mean(arr[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out

def sma(arr: np.ndarray, period: int) -> np.ndarray:
    n = len(arr); out = np.full(n, np.nan)
    if n < period: return out
    cs = np.cumsum(arr)
    out[period - 1] = cs[period - 1] / period
    for i in range(period, n):
        out[i] = (cs[i] - cs[i - period]) / period
    return out

def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c); tr = np.empty(n); tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    out = np.full(n, np.nan)
    if n >= period:
        out[period-1] = np.mean(tr[:period])
        for i in range(period, n):
            out[i] = (out[i-1] * (period-1) + tr[i]) / period
    return out

def bollinger_bands(c: np.ndarray, period: int = 20, mult: float = 2.0):
    mid = sma(c, period); n = len(c)
    upper = np.full(n, np.nan); lower = np.full(n, np.nan)
    for i in range(period-1, n):
        s = np.std(c[i-period+1:i+1])
        upper[i] = mid[i] + mult * s; lower[i] = mid[i] - mult * s
    return upper, mid, lower

def donchian_channels(h: np.ndarray, l: np.ndarray, period: int = 20):
    n = len(h)
    upper = np.full(n, np.nan); lower = np.full(n, np.nan)
    for i in range(period-1, n):
        upper[i] = np.max(h[i-period+1:i+1])
        lower[i] = np.min(l[i-period+1:i+1])
    return upper, lower

def rsi(c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c); out = np.full(n, np.nan)
    if n < period + 2: return out
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    ag = np.mean(gains[:period]); al = np.mean(losses[:period])
    out[period] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
    for i in range(period, len(deltas)):
        ag = (ag*(period-1) + gains[i]) / period
        al = (al*(period-1) + losses[i]) / period
        out[i+1] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
    return out

def stochastic(h, l, c, k_period=14, k_smooth=3, d_smooth=3):
    n = len(c); raw_k = np.full(n, np.nan)
    for i in range(k_period-1, n):
        hh = np.max(h[i-k_period+1:i+1]); ll = np.min(l[i-k_period+1:i+1])
        raw_k[i] = 50.0 if hh == ll else (c[i]-ll)/(hh-ll)*100
    k = sma(raw_k, k_smooth); d = sma(k, d_smooth)
    return k, d
