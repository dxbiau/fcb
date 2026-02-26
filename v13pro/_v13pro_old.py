#!/usr/bin/env python3
"""
_v13pro.py -- FCB v13 PRO: Production multi-strategy portfolio bot.

Self-contained file that implements the full v13 validated portfolio:
  - 12 strategies + ENS2/ENS3 ensembles
  - 50 combos across 37 pairs, 3 TFs (15m/30m/1H), 7 exit modes
  - Full-maker fee execution (limit TP orders)
  - x1000 dynamic risk/leverage curves
  - Guardian daemon for progressive SL + trailing stop
  - Multi-TF candle-close scheduling

Monte Carlo validated: P(x10<=30d, DD<55%) = 57%, median x10 = 24d

Usage:
    python _v13pro.py                     # Run with market TP (current fee model)
    python _v13pro.py --maker             # Enable limit TP (full maker fees)
    python _v13pro.py --maker --entry     # Limit entry + limit TP
    python _v13pro.py --dry-run           # Scan + log signals, no orders
    python _v13pro.py --dry-run --once    # Single scan cycle then exit
"""

import os, sys, json, time, argparse, threading
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
#  Infrastructure imports (from obr package)
# ═══════════════════════════════════════════════════════════════════

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from obr import config as cfg
from obr import logger as log
from obr import exchange as ex_mod
from obr.state import BotState
from obr.guardian import Guardian
from obr.tracker import OBRTracker
from obr import trade_logger as tlog


# ═══════════════════════════════════════════════════════════════════
#  PART 1: INDICATOR LIBRARY
#  (exact match to _discovery_v13.py — no deviations)
# ═══════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════
#  PART 2: SIGNAL + MSL
# ═══════════════════════════════════════════════════════════════════

class Signal:
    """Standardised signal from any strategy."""
    __slots__ = ('bar', 'side', 'entry', 'stop_dist', 'strategy', 'pair', 'tf')
    def __init__(self, bar, side, entry, stop_dist, strategy='', pair='', tf=''):
        self.bar = bar; self.side = side; self.entry = entry
        self.stop_dist = stop_dist; self.strategy = strategy
        self.pair = pair; self.tf = tf

def msl(price: float, atr_val: float, maker: bool = True) -> float:
    """Minimum stop-loss distance ensuring stop > 3x round-trip fees."""
    fee = 0.0004 if maker else 0.00105
    return max(price * fee * 2 * 3, atr_val, price * 0.003)


# ═══════════════════════════════════════════════════════════════════
#  PART 3: 12 STRATEGIES (exact match to _discovery_v13.py)
# ═══════════════════════════════════════════════════════════════════

def S_ema_rib(o, h, l, c, v, a, mk=True):
    e8, e21, e55 = ema(c, 8), ema(c, 21), ema(c, 55)
    sigs = []
    for i in range(2, len(c)):
        if np.isnan(e55[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        body = abs(c[i] - o[i]); rng = h[i] - l[i]
        if rng <= 0: continue
        if e8[i] > e21[i] > e55[i] and l[i] <= e8[i]*1.005 and c[i] > o[i] and body/rng > 0.3:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif e8[i] < e21[i] < e55[i] and h[i] >= e8[i]*0.995 and c[i] < o[i] and body/rng > 0.3:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_bb_break(o, h, l, c, v, a, mk=True):
    upper, mid, lower = bollinger_bands(c)
    e50 = ema(c, 50); vm = sma(v, 20)
    sigs = []
    for i in range(1, len(c)):
        if np.isnan(upper[i]) or np.isnan(e50[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        vok = not np.isnan(vm[i]) and vm[i] > 0 and v[i] > vm[i]*1.2
        if c[i] > upper[i] and c[i] > e50[i] and c[i] > o[i] and vok:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif c[i] < lower[i] and c[i] < e50[i] and c[i] < o[i] and vok:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_donchian(o, h, l, c, v, a, mk=True):
    du, dl = donchian_channels(h, l)
    sigs = []
    for i in range(1, len(c)):
        if np.isnan(du[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        if not np.isnan(du[i-1]) and c[i] > du[i-1]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif not np.isnan(dl[i-1]) and c[i] < dl[i-1]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_rsi_fade(o, h, l, c, v, a, mk=True):
    r = rsi(c, 14); sigs = []
    for i in range(2, len(c)):
        if np.isnan(r[i]) or np.isnan(r[i-1]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        if r[i-1] < 25 and r[i] > 25 and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif r[i-1] > 75 and r[i] < 75 and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_bb_fade(o, h, l, c, v, a, mk=True):
    upper, mid, lower = bollinger_bands(c, 20, 2.0); sigs = []
    for i in range(2, len(c)):
        if np.isnan(upper[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        if l[i] <= lower[i] and c[i] > lower[i] and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif h[i] >= upper[i] and c[i] < upper[i] and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_stoch_x(o, h, l, c, v, a, mk=True):
    k, d = stochastic(h, l, c, 14, 3, 3); s50 = sma(c, 50); sigs = []
    for i in range(2, len(c)):
        if (np.isnan(k[i]) or np.isnan(d[i]) or np.isnan(k[i-1]) or
            np.isnan(d[i-1]) or np.isnan(s50[i]) or np.isnan(a[i]) or a[i] <= 0): continue
        sd = msl(c[i], a[i], mk)
        if k[i-1] < d[i-1] and k[i] > d[i] and k[i-1] < 25 and c[i] > s50[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif k[i-1] > d[i-1] and k[i] < d[i] and k[i-1] > 75 and c[i] < s50[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_pin_bar(o, h, l, c, v, a, mk=True):
    s50 = sma(c, 50); sigs = []
    for i in range(2, len(c)):
        if np.isnan(s50[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        body = abs(c[i] - o[i]); rng = h[i] - l[i]
        if rng <= 0 or body <= 0: continue
        uw = h[i] - max(c[i], o[i]); lw = min(c[i], o[i]) - l[i]
        if lw > 2*body and uw < body*0.5 and c[i] > o[i] and c[i] > s50[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif uw > 2*body and lw < body*0.5 and c[i] < o[i] and c[i] < s50[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_ib_break(o, h, l, c, v, a, mk=True):
    s50 = sma(c, 50); sigs = []
    for i in range(3, len(c)):
        if np.isnan(a[i]) or a[i] <= 0 or np.isnan(s50[i]): continue
        sd = msl(c[i], a[i], mk)
        if h[i-1] < h[i-2] and l[i-1] > l[i-2]:
            if c[i] > h[i-1] and c[i] > s50[i]:
                sigs.append(Signal(i, 'long', c[i], sd))
            elif c[i] < l[i-1] and c[i] < s50[i]:
                sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_engulf(o, h, l, c, v, a, mk=True):
    s50 = sma(c, 50); vm = sma(v, 20); sigs = []
    for i in range(2, len(c)):
        if np.isnan(s50[i]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        vok = not np.isnan(vm[i]) and vm[i] > 0 and v[i] > vm[i]*1.0
        bc = c[i] - o[i]; bp = c[i-1] - o[i-1]
        if bp < 0 and bc > 0 and o[i] <= c[i-1] and c[i] >= o[i-1] and c[i] > s50[i] and vok:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif bp > 0 and bc < 0 and o[i] >= c[i-1] and c[i] <= o[i-1] and c[i] < s50[i] and vok:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_mtf_rsi(o, h, l, c, v, a, mk=True):
    s200 = sma(c, 200); r = rsi(c, 14); sigs = []
    for i in range(2, len(c)):
        if np.isnan(s200[i]) or np.isnan(r[i]) or np.isnan(r[i-1]) or np.isnan(a[i]) or a[i] <= 0: continue
        sd = msl(c[i], a[i], mk)
        if c[i] > s200[i] and r[i-1] < 40 and r[i] > 40 and c[i] > o[i]:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif c[i] < s200[i] and r[i-1] > 60 and r[i] < 60 and c[i] < o[i]:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_tr_pull(o, h, l, c, v, a, mk=True):
    e21 = ema(c, 21); e55 = ema(c, 55); r = rsi(c, 14); sigs = []
    for i in range(3, len(c)):
        if (np.isnan(e21[i]) or np.isnan(e55[i]) or np.isnan(a[i]) or a[i] <= 0
            or np.isnan(r[i]) or np.isnan(r[i-1])): continue
        sd = msl(c[i], a[i], mk)
        if e21[i] > e55[i] and l[i] <= e21[i]*1.005 and c[i] > o[i] and r[i] > r[i-1] and r[i] < 60:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif e21[i] < e55[i] and h[i] >= e21[i]*0.995 and c[i] < o[i] and r[i] < r[i-1] and r[i] > 40:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

def S_mom_surge(o, h, l, c, v, a, mk=True):
    vm = sma(v, 20); e20 = ema(c, 20); sigs = []
    for i in range(2, len(c)):
        if np.isnan(a[i]) or a[i] <= 0 or np.isnan(vm[i]) or vm[i] <= 0 or np.isnan(e20[i]): continue
        sd = msl(c[i], a[i], mk)
        body = abs(c[i] - o[i]); rng = h[i] - l[i]
        if rng <= 0: continue
        if c[i] > o[i] and body > 1.5*a[i] and v[i] > vm[i]*2.0 and c[i] > e20[i] and body/rng > 0.6:
            sigs.append(Signal(i, 'long', c[i], sd))
        elif c[i] < o[i] and body > 1.5*a[i] and v[i] > vm[i]*2.0 and c[i] < e20[i] and body/rng > 0.6:
            sigs.append(Signal(i, 'short', c[i], sd))
    return sigs

# Strategy registry
STRATEGIES = {
    'EMA_RIB': S_ema_rib, 'BB_BREAK': S_bb_break, 'DONCHIAN': S_donchian,
    'RSI_FADE': S_rsi_fade, 'BB_FADE': S_bb_fade, 'STOCH_X': S_stoch_x,
    'PIN_BAR': S_pin_bar, 'IB_BREAK': S_ib_break, 'ENGULF': S_engulf,
    'MTF_RSI': S_mtf_rsi, 'TR_PULL': S_tr_pull, 'MOM_SURGE': S_mom_surge,
}


# ═══════════════════════════════════════════════════════════════════
#  PART 4: SIGNAL SCANNING
# ═══════════════════════════════════════════════════════════════════

def scan_last_bar(o, h, l, c, v, strategies=None, maker=True):
    """Scan the last closed bar for signals from specified strategies."""
    a = atr(h, l, c, 14)
    bar_idx = len(c) - 1
    strats = strategies or list(STRATEGIES.keys())
    fired = []
    for name in strats:
        fn = STRATEGIES.get(name)
        if fn is None:
            continue
        for sig in fn(o, h, l, c, v, a, maker):
            if sig.bar == bar_idx:
                sig.strategy = name
                fired.append(sig)
    return fired

def ensemble_signals(signals, min_agree=2):
    """Generate ensemble signals where min_agree+ strategies agree on same bar+side."""
    groups = defaultdict(list)
    for sig in signals:
        groups[(sig.bar, sig.side)].append(sig)
    ens = []
    for (bar, side), sigs in groups.items():
        if len(sigs) >= min_agree:
            sds = sorted(s.stop_dist for s in sigs)
            median_sd = sds[len(sds) // 2]
            names = '+'.join(s.strategy for s in sigs)
            ens.append(Signal(bar, side, sigs[0].entry, median_sd,
                              strategy=f"ENS{min_agree}({names})"))
    return ens


# ═══════════════════════════════════════════════════════════════════
#  PART 5: COMBO REGISTRY
# ═══════════════════════════════════════════════════════════════════

EXIT_PARAMS = {
    'fix1.2': {'type': 'fixed', 'tp_r': 1.2},
    'fix1.5': {'type': 'fixed', 'tp_r': 1.5},
    'fix2.0': {'type': 'fixed', 'tp_r': 2.0},
    'fix2.5': {'type': 'fixed', 'tp_r': 2.5},
    'fix3.0': {'type': 'fixed', 'tp_r': 3.0},
    'trl1.5': {'type': 'trail', 'trail_atr': 1.5},
    'trl2.0': {'type': 'trail', 'trail_atr': 2.0},
}

def _normalise_pair(pair: str) -> str:
    """Convert discovery pair names to Bybit ccxt format."""
    for prefix in ['binance_futures_', 'bybit_futures_']:
        if pair.startswith(prefix):
            pair = pair[len(prefix):]
    if pair.endswith('_5m'): pair = pair[:-3]
    if pair.endswith('_USDT_USDT'): pair = pair[:-5]
    if not pair.endswith('_USDT') and not pair.endswith('USDT'):
        pair = pair + '_USDT'
    base = pair.replace('_USDT', '')
    return f"{base}/USDT:USDT"

class ComboRegistry:
    """Registry of validated strategy combos for live deployment."""

    def __init__(self, combo_file=None):
        if combo_file is None:
            combo_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '_v13_deploy_combos.json'
            )
        self._combos = []
        self._by_pair_tf = defaultdict(list)
        self._pairs_by_tf = defaultdict(set)
        self._all_pairs = set()
        self._all_tfs = set()
        self._all_strats = set()
        self._load(combo_file)

    def _load(self, path):
        with open(path) as f:
            raw = json.load(f)
        for combo in raw:
            pair = _normalise_pair(combo.get('pair', ''))
            combo['pair_norm'] = pair
            tf = combo['tf']; strat = combo['strat']
            self._combos.append(combo)
            self._by_pair_tf[(pair, tf)].append(combo)
            self._pairs_by_tf[tf].add(pair)
            self._all_pairs.add(pair)
            self._all_tfs.add(tf)
            self._all_strats.add(strat)

    def get_combos(self, pair, tf): return self._by_pair_tf.get((pair, tf), [])
    def get_pairs_for_tf(self, tf): return self._pairs_by_tf.get(tf, set())
    @property
    def all_pairs(self): return self._all_pairs
    @property
    def all_tfs(self): return self._all_tfs
    @property
    def n_combos(self): return len(self._combos)


# ═══════════════════════════════════════════════════════════════════
#  PART 6: TF HELPERS
# ═══════════════════════════════════════════════════════════════════

def tf_minutes(tf: str) -> int:
    if tf.endswith("m"): return int(tf[:-1])
    if tf.lower().endswith("h"): return int(tf[:-1]) * 60
    return 15

def tf_ccxt(tf: str) -> str:
    return tf.lower()


# ═══════════════════════════════════════════════════════════════════
#  PART 7: THE BOT
# ═══════════════════════════════════════════════════════════════════

class V13Pro:
    """Production FCB v13 multi-strategy portfolio bot."""

    LOOKBACK = 210       # candles needed (SMA200 + warmup)

    def __init__(self, args):
        self._args = args
        self._maker_tp = args.maker
        self._maker_entry = args.entry
        self._dry_run = args.dry_run
        self._once = args.once

        # Apply fee model to config
        if self._maker_tp:
            cfg.MAKER_TP_ENABLED = True
            cfg.EFFECTIVE_FEE_MODEL = 'full_maker'
        if self._maker_entry:
            cfg.MAKER_ENTRY_ENABLED = True

        self._ex = None
        self._state = BotState()
        self._guardian: Optional[Guardian] = None
        self._tracker = OBRTracker()
        self._registry = ComboRegistry(args.combos)
        self._market_info: Dict[str, dict] = {}
        self._valid_pairs: List[str] = []
        self._position_meta: Dict[str, dict] = {}
        self._day_trades = 0

    # ─────────────────────────────────────────────
    #  Startup
    # ─────────────────────────────────────────────

    def _connect(self) -> float:
        from obr.logger import C
        log.info("")
        log.header("FCB v13 PRO", "\U0001f680")

        fee_label = "FULL MAKER" if self._maker_tp else "CURRENT"
        mode_label = "DRY RUN" if self._dry_run else "LIVE"

        log.info(f"  \U0001f4ca {C.DIM}Portfolio:{C.RESET} "
                 f"{C.BOLD}{C.BCYAN}{self._registry.n_combos} combos{C.RESET}  "
                 f"{C.DIM}|{C.RESET} "
                 f"{C.BWHITE}{len(self._registry.all_pairs)} pairs{C.RESET}  "
                 f"{C.DIM}|{C.RESET} "
                 f"{C.BWHITE}{sorted(self._registry.all_tfs)}{C.RESET}")
        log.info(f"  \U0001f4b8 {C.DIM}Fees:{C.RESET} {C.BCYAN}{fee_label}{C.RESET}  "
                 f"\U0001f3af {C.DIM}Mode:{C.RESET} "
                 f"{C.BOLD}{'DRY RUN' if self._dry_run else C.BGREEN + 'LIVE'}{C.RESET}")

        self._ex = ex_mod.create_exchange()
        equity = ex_mod.get_equity(self._ex)
        self._state.update_equity(equity)

        risk = cfg.get_risk_pct(equity)
        lev = cfg.get_leverage(equity)

        log.info(f"  \U0001f4b0 {C.DIM}Risk:{C.RESET} {C.BGREEN}{risk*100:.1f}%{C.RESET}  "
                 f"\u26a1 {C.DIM}Lev:{C.RESET} {C.BYELLOW}{lev}x{C.RESET}  "
                 f"\U0001f48e {C.DIM}Equity:{C.RESET} "
                 f"{C.BOLD}{C.BGREEN}${equity:.2f}{C.RESET}")
        log.divider()
        return equity

    def _setup_pairs(self):
        log.info("Setting up pairs...")
        all_pairs = sorted(self._registry.all_pairs)
        valid = []
        for pair in all_pairs:
            try:
                info = ex_mod.get_market_info(self._ex, pair)
                self._market_info[pair] = info
                lev = cfg.get_leverage(self._state.equity)
                ex_mod.set_leverage(self._ex, pair, lev)
                ex_mod.set_margin_mode(self._ex, pair, "cross")
                valid.append(pair)
            except Exception as e:
                short = pair.split('/')[0]
                log.warning(f"  {short}: skip ({e})")
        self._valid_pairs = valid
        log.info(f"  \u2705 {len(valid)}/{len(all_pairs)} pairs active")

    # ─────────────────────────────────────────────
    #  Candle fetch + array conversion
    # ─────────────────────────────────────────────

    def _fetch_candles(self, pair, tf):
        try:
            return ex_mod.fetch_latest_candles(
                self._ex, pair, self.LOOKBACK, timeframe=tf_ccxt(tf))
        except Exception as e:
            log.debug(f"  Fetch {pair} {tf}: {e}")
            return []

    @staticmethod
    def _to_arrays(candles):
        if len(candles) < 20:
            return None
        o = np.array([c["open"] for c in candles], dtype=float)
        h = np.array([c["high"] for c in candles], dtype=float)
        l = np.array([c["low"] for c in candles], dtype=float)
        cl = np.array([c["close"] for c in candles], dtype=float)
        v = np.array([c["volume"] for c in candles], dtype=float)
        return o, h, l, cl, v

    # ─────────────────────────────────────────────
    #  Multi-TF signal scanning
    # ─────────────────────────────────────────────

    def _scan_tf(self, tf) -> List[Tuple[Signal, dict]]:
        pairs = self._registry.get_pairs_for_tf(tf)
        valid = [p for p in pairs if p in self._valid_pairs]
        results = []

        for pair in valid:
            session = cfg.current_session_name(datetime.now(timezone.utc).hour)
            mc = cfg.get_max_concurrent(self._state.equity)
            if not self._state.can_trade(pair, session, max_concurrent=mc):
                continue

            candles = self._fetch_candles(pair, tf)
            if not candles: continue
            arrays = self._to_arrays(candles)
            if arrays is None: continue
            o, h, l, cl, v = arrays

            combos = self._registry.get_combos(pair, tf)
            if not combos: continue

            # Split base vs ensemble combos
            base_combos = [cb for cb in combos if cb["strat"] not in ("ENS2", "ENS3")]
            ens_combos  = [cb for cb in combos if cb["strat"] in ("ENS2", "ENS3")]

            # Strategy names needed
            strat_names = list(set(cb["strat"] for cb in base_combos))
            if ens_combos:
                strat_names = list(STRATEGIES.keys())  # all 12 for ensemble

            signals = scan_last_bar(o, h, l, cl, v,
                                     strategies=strat_names,
                                     maker=self._maker_tp)
            if not signals and not ens_combos:
                continue

            # Pre-compute ATR
            a = atr(h, l, cl, 14)
            atr_now = float(a[-1]) if not np.isnan(a[-1]) else (
                float(signals[0].stop_dist) if signals else 1.0)

            # Match base signals → combos
            for sig in signals:
                matching = [cb for cb in base_combos if cb["strat"] == sig.strategy]
                if matching:
                    best = max(matching, key=lambda x: x.get("val_wr", 0) * x.get("val_pf", 0))
                    sig.pair = pair; sig.tf = tf
                    best["_atr"] = atr_now
                    results.append((sig, best))

            # Ensemble combos
            for ecb in ens_combos:
                min_agree = int(ecb["strat"][-1])  # ENS2→2, ENS3→3
                for esig in ensemble_signals(signals, min_agree):
                    esig.pair = pair; esig.tf = tf
                    ecb_copy = dict(ecb); ecb_copy["_atr"] = atr_now
                    results.append((esig, ecb_copy))

            time.sleep(0.3)

        return results

    # ─────────────────────────────────────────────
    #  Trade execution
    # ─────────────────────────────────────────────

    def _execute(self, sig: Signal, combo: dict) -> bool:
        pair = sig.pair
        side = "buy" if sig.side == "long" else "sell"
        entry = sig.entry
        sd = sig.stop_dist
        short = pair.split('/')[0]

        # SL
        sl = entry - sd if sig.side == "long" else entry + sd

        # TP from exit mode
        exit_mode = combo.get("exit", "fix2.0")
        ep = EXIT_PARAMS.get(exit_mode, EXIT_PARAMS['fix2.0'])
        atr_val = combo.get("_atr", sd)

        if ep["type"] == "fixed":
            tp_r = ep["tp_r"]
            tp = entry + sd * tp_r if sig.side == "long" else entry - sd * tp_r
            exchange_tp = tp
        else:
            # Trailing: TP set far out, guardian manages exit
            tp = entry + sd * 10 if sig.side == "long" else entry - sd * 10
            exchange_tp = tp

        # Risk sizing (x1000 curves)
        equity = self._state.equity
        base_risk = cfg.get_risk_pct(equity)
        dd_mult = cfg.get_drawdown_multiplier(equity, self._state.peak_equity)
        risk_pct = min(base_risk * dd_mult, cfg.MAX_RISK_PCT)
        leverage = cfg.get_leverage(equity)
        dollar_risk = equity * risk_pct
        if sd <= 0: return False
        qty = dollar_risk / sd

        # Margin cap
        avail = ex_mod.get_available_balance(self._ex)
        margin = (qty * entry) / leverage
        if margin > avail * 0.95:
            qty = (avail * 0.95 * leverage) / entry
            dollar_risk = qty * sd
        if dollar_risk < 1.0: return False

        # Round
        try:
            qty = ex_mod.round_qty(self._ex, pair, qty)
            sl = ex_mod.round_price(self._ex, pair, sl)
            exchange_tp = ex_mod.round_price(self._ex, pair, exchange_tp)
        except Exception as e:
            log.warning(f"  {short}: precision error: {e}"); return False

        # Validate direction
        if side == "buy":
            if exchange_tp <= entry or sl >= entry: return False
        else:
            if exchange_tp >= entry or sl <= entry: return False

        # DRY RUN — log signal and skip
        if self._dry_run:
            from obr.logger import C
            dc = C.BGREEN if sig.side == 'long' else C.BRED
            log.info(f"  \U0001f4cb {C.DIM}[DRY]{C.RESET} "
                     f"{'📈' if sig.side == 'long' else '📉'} "
                     f"{dc}{sig.side.upper()}{C.RESET} "
                     f"{C.BOLD}{short}{C.RESET} "
                     f"{sig.strategy}@{sig.tf} | "
                     f"exit={exit_mode} risk=${dollar_risk:.2f} "
                     f"entry={entry:.6g} sl={sl:.6g} tp={exchange_tp:.6g}")
            return True  # count as "signal found"

        # Set leverage
        try: ex_mod.set_leverage(self._ex, pair, leverage)
        except: pass

        from obr.logger import C
        log.info(f"  \u26a1 {C.BOLD}FCB ENTRY{C.RESET}: "
                 f"{'📈' if sig.side == 'long' else '📉'} "
                 f"{C.BWHITE}{short}{C.RESET} "
                 f"{sig.strategy}@{sig.tf} | "
                 f"exit={exit_mode} risk=${dollar_risk:.2f}")

        try:
            if self._maker_entry:
                limit_price = ex_mod.round_price(self._ex, pair, entry)
                order = ex_mod.place_limit_order(
                    self._ex, pair, side, qty, limit_price, sl, exchange_tp)
                if not order: return False
                oid = order.get("id", "")
                deadline = time.time() + cfg.MAKER_ENTRY_TIMEOUT_SEC
                avg_price = 0.0; filled = False
                while time.time() < deadline:
                    time.sleep(3)
                    try:
                        st = ex_mod.fetch_order(self._ex, pair, oid)
                        if st and st.get("status") == "closed":
                            avg_price = float(st.get("average") or st.get("price") or limit_price)
                            filled = True; break
                        elif st and st.get("status") == "canceled":
                            return False
                    except: pass
                if not filled:
                    ex_mod.cancel_order(self._ex, pair, oid)
                    log.info(f"  {short}: Limit unfilled, cancelled"); return False
            else:
                order = ex_mod.place_market_order(
                    self._ex, pair, side, qty, sl, exchange_tp)
                if not order: return False
                avg_price = float(order.get("average") or order.get("price") or entry)

            # Record state
            session = cfg.current_session_name(datetime.now(timezone.utc).hour)
            entry_data = {
                "direction": sig.side, "entry_price": avg_price,
                "stop_loss": float(sl), "take_profit": float(exchange_tp),
                "exchange_tp": float(exchange_tp),
                "risk_per_unit": sd, "dollar_risk": dollar_risk,
                "position_size": float(qty), "order_id": order.get("id", ""),
                "ob_high": 0.0, "ob_low": 0.0, "ob_open": 0.0, "ob_close": 0.0,
            }
            self._state.record_entry(pair, session, entry_data)

            # Position metadata
            self._position_meta[pair] = {
                "combo": combo, "exit_mode": exit_mode, "exit_params": ep,
                "strategy": sig.strategy, "tf": sig.tf,
                "atr_at_entry": atr_val, "stop_dist": sd,
            }

            # Guardian
            self._guardian.track_position(
                symbol=pair, direction=sig.side, entry_price=avg_price,
                stop_loss=float(sl), risk_per_unit=sd, dollar_risk=dollar_risk,
            )

            log.position_opened(pair, sig.side, avg_price, sl,
                                float(exchange_tp), qty, dollar_risk)

            tlog.log_entry(
                symbol=pair, direction=sig.side, entry_price=avg_price,
                stop_loss=float(sl), take_profit=float(exchange_tp),
                qty=float(qty), dollar_risk=dollar_risk,
                risk_per_unit=sd, session=session,
                order_id=order.get("id", ""),
                ob_high=0, ob_low=0, ob_open=0, ob_close=0,
            )
            return True
        except Exception as e:
            log.error(f"  {short}: Order failed: {e}"); return False

    # ─────────────────────────────────────────────
    #  Guardian callback
    # ─────────────────────────────────────────────

    def _on_closed(self, symbol, pnl_r, pnl_usd, reason, exit_price=0):
        self._position_meta.pop(symbol, None)
        entry_data = None
        for p in self._state.pending_entries:
            if p.get("symbol") == symbol:
                entry_data = p; break
        self._state.record_outcome(symbol, pnl_r, pnl_usd, reason, entry_data)
        direction = entry_data.get("direction", "?") if entry_data else "?"
        entry_price = entry_data.get("entry_price", 0) if entry_data else 0
        log.position_closed(symbol, direction, entry_price, exit_price,
                            pnl_r, pnl_usd, reason)
        tlog.log_exit(symbol=symbol, direction=direction,
                      entry_price=entry_price, exit_price=exit_price,
                      pnl_r=pnl_r, pnl_usd=pnl_usd, reason=reason)
        self._tracker.record_session(
            equity=self._state.equity,
            session=cfg.current_session_name(datetime.now(timezone.utc).hour),
            trades=1, wins=1 if pnl_r > 0 else 0,
            losses=0 if pnl_r > 0 else 1, r_total=pnl_r,
        )
        try:
            eq = ex_mod.get_equity(self._ex)
            self._state.update_equity(eq)
        except: pass

    # ─────────────────────────────────────────────
    #  Candle-close wait + TF detection
    # ─────────────────────────────────────────────

    def _wait_15m(self):
        now = datetime.now(timezone.utc)
        minute = now.minute
        nb = ((minute // 15) + 1) * 15
        if nb >= 60:
            target = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
        else:
            target = now.replace(minute=nb, second=5, microsecond=0)
        wait = (target - now).total_seconds()
        if wait > 0:
            log.debug(f"Waiting {wait:.0f}s for 15m candle close...")
            time.sleep(wait)

    def _closed_tfs(self):
        now = datetime.now(timezone.utc)
        total_min = now.hour * 60 + now.minute
        closed = [tf for tf in sorted(self._registry.all_tfs)
                  if total_min % tf_minutes(tf) == 0]
        return closed if closed else ["15m"]

    # ─────────────────────────────────────────────
    #  Main loop
    # ─────────────────────────────────────────────

    def run(self):
        equity = self._connect()
        self._setup_pairs()

        # Guardian (skip for dry run)
        if not self._dry_run:
            self._guardian = Guardian(
                exchange=self._ex, state=self._state,
                on_position_closed=self._on_closed,
            )
            self._guardian.start()
        else:
            self._guardian = None

        from obr.logger import C

        for tf in sorted(self._registry.all_tfs):
            pairs = self._registry.get_pairs_for_tf(tf)
            active = [p for p in pairs if p in self._valid_pairs]
            log.info(f"  \u23f1\ufe0f  {tf}: {len(active)} pairs active")

        mode_str = "DRY RUN" if self._dry_run else "LIVE"
        fee_str = "FULL MAKER" if self._maker_tp else "CURRENT"
        log.banner_box([
            f"\U0001f30a  FCB v13 PRO — {mode_str}",
            f"\U0001f48e  Equity: ${equity:.2f}",
            f"\U0001f4ca  {self._registry.n_combos} combos | "
            f"{len(self._valid_pairs)} pairs",
            f"\u23f1\ufe0f   TFs: {sorted(self._registry.all_tfs)}",
            f"\U0001f4b8  Fee model: {fee_str} "
            f"{'+ maker entry' if self._maker_entry else ''}",
            f"\U0001f680  Scanning every 15m candle...",
        ], color=C.BGREEN)

        try:
            while True:
                self._state.check_new_day()

                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if not hasattr(self, '_today') or self._today != today:
                    self._today = today; self._day_trades = 0

                # Phase cap
                eq = self._state.equity
                _, pcap, _ = cfg.get_current_phase(eq)
                dg = self._state.daily_growth_pct
                if pcap > 0 and dg >= pcap:
                    log.info(f"  \U0001f525 DAILY CAP: {dg:.1f}% (cap={pcap:.0f}%)")
                    self._wait_15m()
                    try: self._state.update_equity(ex_mod.get_equity(self._ex))
                    except: pass
                    continue

                self._wait_15m()

                # Equity check
                try:
                    equity = ex_mod.get_equity(self._ex)
                    self._state.update_equity(equity)
                except: equity = self._state.equity

                peak = self._state.peak_equity
                if peak > 0 and equity / peak < cfg.EQUITY_FLOOR_PCT:
                    log.critical(f"\U0001f6d1 EQUITY FLOOR: ${equity:.2f} / ${peak:.2f}")
                    time.sleep(300); continue

                closed_tfs = self._closed_tfs()
                session = cfg.current_session_name(datetime.now(timezone.utc).hour)
                log.debug(f"\U0001f50d Scanning TFs: {closed_tfs}")

                found = 0; traded: Set[str] = set()

                for tf in closed_tfs:
                    results = self._scan_tf(tf)
                    for sig, combo in results:
                        if sig.pair in traded: continue
                        mc = cfg.get_max_concurrent(equity)
                        if self._state.pending_count >= mc: break
                        ok = self._execute(sig, combo)
                        if ok:
                            traded.add(sig.pair); found += 1
                            self._day_trades += 1

                if found > 0:
                    log.info(f"  \u26a1 Scan: {found} "
                             f"{'signals' if self._dry_run else 'entries'}, "
                             f"{self._day_trades} today, "
                             f"growth: {self._state.daily_growth_pct:+.1f}%")

                log.heartbeat(equity, self._state.pending_count, session)

                # Single-cycle mode
                if self._once:
                    log.info("Single cycle complete. Exiting.")
                    break

        except KeyboardInterrupt:
            log.info("\n\U0001f44b v13 PRO stopped by user")
        except Exception as e:
            log.critical(f"\U0001f480 Fatal: {e}")
            log.log_exception("v13pro_main", e)
        finally:
            if self._guardian:
                self._guardian.stop()


# ═══════════════════════════════════════════════════════════════════
#  PART 8: CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        description="FCB v13 PRO — Production multi-strategy portfolio bot")
    p.add_argument("--maker", action="store_true",
                   help="Enable full maker fee model (limit TP orders)")
    p.add_argument("--entry", action="store_true",
                   help="Enable limit entries (maker fee on entry)")
    p.add_argument("--combos", type=str, default=None,
                   help="Path to combo JSON (default: _v13_deploy_combos.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="Scan + log signals, no orders placed")
    p.add_argument("--once", action="store_true",
                   help="Run single scan cycle then exit")
    args = p.parse_args()

    bot = V13Pro(args)
    bot.run()


if __name__ == "__main__":
    main()
