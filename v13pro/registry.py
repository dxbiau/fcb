"""
v13pro/registry.py -- Combo registry + exit params.
"""
import json
import os
from collections import defaultdict
from v13pro import config as cfg


EXIT_PARAMS = {
    'fix0.5': {'type': 'fixed', 'tp_r': 0.5},
    'fix0.75': {'type': 'fixed', 'tp_r': 0.75},
    'fix1.2': {'type': 'fixed', 'tp_r': 1.2},
    'fix1.5': {'type': 'fixed', 'tp_r': 1.5},
    'fix2.0': {'type': 'fixed', 'tp_r': 2.0},
    'fix2.5': {'type': 'fixed', 'tp_r': 2.5},
    'fix3.0': {'type': 'fixed', 'tp_r': 3.0},
    'trl':    {'type': 'trail'},                     # pure trail (guardian manages exit)
    'trl1.5': {'type': 'trail', 'trail_atr': 1.5},
    'trl2.0': {'type': 'trail', 'trail_atr': 2.0},
    # trail_tight: activate at 1.5R, trail 0.5R — minimum capture = 1.0R
    # NEVER exit below 1R on a trailed position. Risk 1R to make 1R+ minimum.
    'trl_tight': {'type': 'trail', 'trail_activation_r': 1.5, 'trail_distance_r': 0.5},
}


# Bybit uses 1000X denomination for sub-cent tokens
_BYBIT_REMAP = {
    'BONK': '1000BONK', 'FLOKI': '1000FLOKI', 'PEPE': '1000PEPE',
    'SHIB': '1000SHIB', 'LUNC': '1000LUNC', 'XEC': '1000XEC',
    'SATS': '1000SATS', 'RATS': '1000RATS', 'CAT': '1000CAT',
}

# Pairs known to be delisted / not on Bybit
_SKIP_PAIRS = {'OM'}

# ccxt uses lowercase timeframes (1h not 1H)
_TF_MAP = {'1H': '1h', '4H': '4h', '2H': '2h', '6H': '6h', '12H': '12h',
           '1D': '1d', '1W': '1w', '1M': '1M'}


def _normalise_tf(tf: str) -> str:
    return _TF_MAP.get(tf, tf)


def _normalise_pair(pair: str) -> str:
    for prefix in ['binance_futures_', 'bybit_futures_']:
        if pair.startswith(prefix):
            pair = pair[len(prefix):]
    if pair.endswith('_5m'): pair = pair[:-3]
    if pair.endswith('_USDT_USDT'): pair = pair[:-5]
    if not pair.endswith('_USDT') and not pair.endswith('USDT'):
        pair = pair + '_USDT'
    base = pair.replace('_USDT', '')
    # Remap 1000X tokens
    base = _BYBIT_REMAP.get(base, base)
    return f"{base}/USDT:USDT"


class ComboRegistry:
    def __init__(self, combo_file=None):
        if combo_file is None:
            combo_file = cfg.DEPLOY_COMBOS
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
        skipped = 0
        for combo in raw:
            raw_pair = combo.get('pair', '')
            # Skip delisted / unavailable pairs
            base = raw_pair.replace('_USDT', '')
            if base in _SKIP_PAIRS:
                skipped += 1
                continue
            pair = _normalise_pair(raw_pair)
            combo['pair_norm'] = pair
            tf = _normalise_tf(combo['tf'])
            combo['tf'] = tf  # overwrite with ccxt-compatible TF
            strat = combo['strat']
            self._combos.append(combo)
            self._by_pair_tf[(pair, tf)].append(combo)
            self._pairs_by_tf[tf].add(pair)
            self._all_pairs.add(pair)
            self._all_tfs.add(tf)
            self._all_strats.add(strat)
        if skipped:
            from v13pro import logger as log
            log.info(f"  Registry: skipped {skipped} combos (delisted pairs)")

    def get_combos(self, pair, tf):
        return self._by_pair_tf.get((pair, tf), [])

    def get_pairs_for_tf(self, tf):
        return self._pairs_by_tf.get(tf, set())

    @property
    def all_pairs(self): return self._all_pairs
    @property
    def all_tfs(self): return self._all_tfs
    @property
    def n_combos(self): return len(self._combos)
    @property
    def combos(self): return self._combos
