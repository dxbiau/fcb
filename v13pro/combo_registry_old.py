"""
obr/combo_registry.py -- Portfolio combo registry for FCB v13 deployment.

Loads the validated top-N combos from the v13 discovery results and provides
an efficient lookup: given a (pair, timeframe), return which strategies to scan
and what exit mode to use for each.

Usage:
    from obr.combo_registry import ComboRegistry
    registry = ComboRegistry()  # loads _v13_deploy_combos.json
    combos = registry.get_combos("DOGEUSDT", "15m")
    # -> [{'strat': 'RSI_FADE', 'exit': 'fix1.2', ...}, ...]
"""

import json
import os
from typing import List, Dict, Optional, Set, Tuple
from collections import defaultdict


# Exit mode parameters (matching discovery exactly)
EXIT_PARAMS = {
    'fix1.2': {'type': 'fixed', 'tp_r': 1.2},
    'fix1.5': {'type': 'fixed', 'tp_r': 1.5},
    'fix2.0': {'type': 'fixed', 'tp_r': 2.0},
    'fix2.5': {'type': 'fixed', 'tp_r': 2.5},
    'fix3.0': {'type': 'fixed', 'tp_r': 3.0},
    'trl1.5': {'type': 'trail', 'trail_atr': 1.5},
    'trl2.0': {'type': 'trail', 'trail_atr': 2.0},
}


class ComboRegistry:
    """Registry of validated strategy combos for live deployment."""

    def __init__(self, combo_file: str = None):
        if combo_file is None:
            combo_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                '_v13_deploy_combos.json'
            )
        self._combos: List[Dict] = []
        self._by_pair_tf: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        self._pairs_by_tf: Dict[str, Set[str]] = defaultdict(set)
        self._all_pairs: Set[str] = set()
        self._all_tfs: Set[str] = set()
        self._all_strategies: Set[str] = set()

        self._load(combo_file)

    def _load(self, path: str):
        """Load combos from JSON file."""
        with open(path) as f:
            raw = json.load(f)

        for combo in raw:
            # Normalise pair name to Bybit format (e.g., "DOGEUSDT")
            pair_raw = combo.get('pair', '')
            # The discovery uses CSV names like "DOGE", "binance_futures_DOGE_USDT"
            # Normalise to exchange symbol format
            pair = self._normalise_pair(pair_raw)
            combo['pair_norm'] = pair

            tf = combo['tf']
            strat = combo['strat']

            self._combos.append(combo)
            self._by_pair_tf[(pair, tf)].append(combo)
            self._pairs_by_tf[tf].add(pair)
            self._all_pairs.add(pair)
            self._all_tfs.add(tf)
            self._all_strategies.add(strat)

    @staticmethod
    def _normalise_pair(pair: str) -> str:
        """Convert discovery pair names to Bybit symbol format.

        Discovery uses CSV filenames which can be:
          - 'DOGE' (from binance_futures csvs, add USDT)
          - 'DOGE_USDT' (already has USDT suffix)
          - 'bybit_futures_DOGE_USDT_USDT' (double USDT from filename parsing)

        Bybit symbols are like 'DOGE/USDT:USDT'
        """
        # Strip common prefixes
        for prefix in ['binance_futures_', 'bybit_futures_']:
            if pair.startswith(prefix):
                pair = pair[len(prefix):]

        # Strip _5m suffix if present
        if pair.endswith('_5m'):
            pair = pair[:-3]

        # Handle double USDT (e.g. DOGE_USDT_USDT → DOGE_USDT)
        if pair.endswith('_USDT_USDT'):
            pair = pair[:-5]  # remove trailing _USDT

        # If pair doesn't end with USDT, add it
        if not pair.endswith('_USDT') and not pair.endswith('USDT'):
            pair = pair + '_USDT'

        # Convert to Bybit ccxt format: DOGE_USDT → DOGE/USDT:USDT
        base = pair.replace('_USDT', '')
        return f"{base}/USDT:USDT"

    def get_combos(self, pair: str, tf: str) -> List[Dict]:
        """Get all active combos for a (pair, TF) tuple."""
        return self._by_pair_tf.get((pair, tf), [])

    def get_strategies_for_pair_tf(self, pair: str, tf: str) -> List[str]:
        """Get unique strategy names needed for this pair+TF."""
        combos = self.get_combos(pair, tf)
        return list(set(c['strat'] for c in combos))

    def get_pairs_for_tf(self, tf: str) -> Set[str]:
        """Get all pairs that need scanning on a given timeframe."""
        return self._pairs_by_tf.get(tf, set())

    @property
    def all_pairs(self) -> Set[str]:
        return self._all_pairs

    @property
    def all_tfs(self) -> Set[str]:
        return self._all_tfs

    @property
    def all_strategies(self) -> Set[str]:
        return self._all_strategies

    @property
    def n_combos(self) -> int:
        return len(self._combos)

    def get_exit_params(self, exit_mode: str) -> Dict:
        """Get exit mode parameters."""
        return EXIT_PARAMS.get(exit_mode, EXIT_PARAMS['fix2.0'])

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"ComboRegistry: {self.n_combos} combos",
            f"  Pairs: {len(self.all_pairs)}",
            f"  TFs: {sorted(self.all_tfs)}",
            f"  Strategies: {sorted(self.all_strategies)}",
        ]
        for tf in sorted(self.all_tfs):
            pairs = self.get_pairs_for_tf(tf)
            lines.append(f"  {tf}: {len(pairs)} pairs")
        return '\n'.join(lines)


if __name__ == '__main__':
    reg = ComboRegistry()
    print(reg.summary())
    print()
    # Show a few example lookups
    for tf in sorted(reg.all_tfs):
        for pair in sorted(reg.get_pairs_for_tf(tf))[:2]:
            combos = reg.get_combos(pair, tf)
            print(f"  {pair} @ {tf}: {len(combos)} combos")
            for c in combos:
                print(f"    {c['strat']:12s} {c['exit']:8s} "
                      f"vPF={c['val_pf']:.2f} tPF={c['test_pf']:.2f}")
