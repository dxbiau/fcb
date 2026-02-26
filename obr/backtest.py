"""
obr/backtest.py -- Honest OBR backtester using session_sim infrastructure.

Fixes the look-ahead bias from the lab:
  - NEXTBAR variant: signal detected on candle N, confirmation checked
    on candle N+1 CLOSE, entry at candle N+2 OPEN (no within-bar).
  - Pure variant: signal on candle N, entry at candle N+1 OPEN.

Uses OHLC simulation for SL/TP resolution (high touches SL before TP
or vice versa within the candle).

Usage:
  .venv\\Scripts\\python.exe -m obr.backtest [--pairs bybit|binance|all]
                                              [--tp 1.0|1.5|2.0|2.5]
                                              [--nextbar|--pure]
"""

import os
import sys
import csv
import math
import time
from typing import List, Optional
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obr import config as cfg


# ==================================================================
#  Data loading (pure stdlib, no numpy/pandas)
# ==================================================================

class Candle:
    __slots__ = ("dt", "o", "h", "l", "c", "v")

    def __init__(self, dt: str, o: float, h: float, l: float,
                 c: float, v: float):
        self.dt = dt
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v

    @property
    def body_top(self) -> float:
        return max(self.o, self.c)

    @property
    def body_bot(self) -> float:
        return min(self.o, self.c)

    @property
    def range(self) -> float:
        return self.h - self.l


def load_csv(path: str) -> List[Candle]:
    """Load a 5m OHLCV CSV into list of Candle objects."""
    candles = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        # Find column indices
        h_lower = [c.strip().lower() for c in header]
        dt_idx = 0
        o_idx = h_lower.index("open") if "open" in h_lower else 1
        hi_idx = h_lower.index("high") if "high" in h_lower else 2
        lo_idx = h_lower.index("low") if "low" in h_lower else 3
        c_idx = h_lower.index("close") if "close" in h_lower else 4
        v_idx = h_lower.index("volume") if "volume" in h_lower else 5

        for row in reader:
            if len(row) < 6:
                continue
            try:
                candles.append(Candle(
                    dt=row[dt_idx].strip(),
                    o=float(row[o_idx]),
                    h=float(row[hi_idx]),
                    l=float(row[lo_idx]),
                    c=float(row[c_idx]),
                    v=float(row[v_idx]),
                ))
            except (ValueError, IndexError):
                continue
    return candles


def load_pairs(exchange: str = "bybit") -> dict:
    """Load all CSV files for an exchange. Returns {symbol: [Candle]}."""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data")
    pairs = {}
    if not os.path.isdir(data_dir):
        print(f"Data directory not found: {data_dir}")
        return pairs

    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith("_5m.csv"):
            continue
        if exchange == "binance" and not fname.startswith("binance_"):
            continue
        if exchange == "bybit" and fname.startswith("binance_"):
            continue

        symbol = fname.replace("binance_", "").replace("_5m.csv", "")
        path = os.path.join(data_dir, fname)
        candles = load_csv(path)
        if len(candles) > 100:
            pairs[symbol] = candles
    return pairs


# ==================================================================
#  OBR signal detection (exact notebook replica)
# ==================================================================

def detect_outside_bar(prev: Candle, curr: Candle) -> int:
    """
    Detect outside bar pattern (exact notebook replica).
    Returns: 2=long (bearish OB -> fade up), 1=short (bullish OB -> fade down), 0=none

    Requires candle color check:
      - LONG (2): bearish candle (open > close) + engulfs + close < prev low
      - SHORT (1): bullish candle (open < close) + engulfs + close > prev high
    """
    # Current candle must engulf previous candle's range
    if curr.h <= prev.h or curr.l >= prev.l:
        return 0

    # LONG signal (signal=2): bearish outside bar
    # c0: Bearish candle (Open > Close)
    # c1+c2: Engulfs (already checked above)
    # c3: Close < previous Low (extreme close beyond prev range)
    if curr.o > curr.c and curr.c < prev.l:
        return 2

    # SHORT signal (signal=1): bullish outside bar
    # c0: Bullish candle (Open < Close)
    # c1+c2: Engulfs (already checked above)
    # c3: Close > previous High (extreme close beyond prev range)
    if curr.o < curr.c and curr.c > prev.h:
        return 1

    return 0


def nextbar_confirms(signal_type: int, confirm: Candle) -> bool:
    """Check if next bar confirms the reversal direction."""
    if signal_type == 2:  # long signal -> confirm candle should close up
        return confirm.c > confirm.o
    elif signal_type == 1:  # short signal -> confirm candle should close down
        return confirm.c < confirm.o
    return False


# ==================================================================
#  Trade simulation (OHLC within-candle SL/TP resolution)
# ==================================================================

class SimTrade:
    __slots__ = ("symbol", "direction", "entry", "sl", "tp",
                 "risk_per_unit", "entry_idx", "exit_idx",
                 "exit_price", "pnl_r", "reason")

    def __init__(self, symbol, direction, entry, sl, tp,
                 risk_per_unit, entry_idx):
        self.symbol = symbol
        self.direction = direction
        self.entry = entry
        self.sl = sl
        self.tp = tp
        self.risk_per_unit = risk_per_unit
        self.entry_idx = entry_idx
        self.exit_idx = -1
        self.exit_price = 0.0
        self.pnl_r = 0.0
        self.reason = ""


def resolve_candle(trade: SimTrade, candle: Candle, idx: int,
                   fee_r: float = 0.04) -> bool:
    """
    Resolve trade against a candle using OHLC logic.
    Returns True if trade closed on this candle.

    SL/TP hit detection:
      - For LONG: SL hit if low <= sl, TP hit if high >= tp
      - For SHORT: SL hit if high >= sl, TP hit if low <= tp
      - If both hit on same candle: SL wins (conservative)
    """
    if trade.direction == "long":
        sl_hit = candle.l <= trade.sl
        tp_hit = candle.h >= trade.tp
    else:
        sl_hit = candle.h >= trade.sl
        tp_hit = candle.l <= trade.tp

    if sl_hit and tp_hit:
        # Both hit -- conservative: SL wins
        trade.exit_price = trade.sl
        trade.reason = "SL (ambiguous)"
    elif sl_hit:
        trade.exit_price = trade.sl
        trade.reason = "SL"
    elif tp_hit:
        trade.exit_price = trade.tp
        trade.reason = "TP"
    else:
        return False

    # Calculate R
    if trade.direction == "long":
        raw_r = (trade.exit_price - trade.entry) / trade.risk_per_unit
    else:
        raw_r = (trade.entry - trade.exit_price) / trade.risk_per_unit

    trade.pnl_r = raw_r - fee_r  # subtract round-trip fee
    trade.exit_idx = idx
    return True


# ==================================================================
#  Backtest engine
# ==================================================================

def backtest_pair(symbol: str, candles: List[Candle],
                  tp_r: float = 2.0, use_nextbar: bool = True,
                  max_concurrent: int = 2,
                  fee_r: float = 0.04) -> List[SimTrade]:
    """
    Backtest OBR on a single pair.

    Timeline (honest, no look-ahead):
      Pure:     Signal on bar[i], entry at bar[i+1].open
      Nextbar:  Signal on bar[i], confirm bar[i+1].close,
                entry at bar[i+2].open

    After entry: simulate bar-by-bar until SL or TP hit.
    """
    trades = []
    open_trades: List[SimTrade] = []
    n = len(candles)

    i = 1  # start at 1 so we have prev candle
    while i < n - 2:  # need room for confirm + entry candle
        # First resolve any open trades on this candle
        still_open = []
        for t in open_trades:
            if not resolve_candle(t, candles[i], i, fee_r):
                still_open.append(t)
        open_trades = still_open

        # Check for new signal
        if len(open_trades) < max_concurrent:
            prev = candles[i - 1]
            curr = candles[i]

            # Skip flat candles
            if curr.range > 0 and prev.range > 0:
                sig = detect_outside_bar(prev, curr)

                if sig != 0:
                    if use_nextbar:
                        # Need confirm candle [i+1] and entry at [i+2]
                        if i + 2 < n:
                            confirm = candles[i + 1]
                            if nextbar_confirms(sig, confirm):
                                # Resolve open trades on confirm candle too
                                still2 = []
                                for t in open_trades:
                                    if not resolve_candle(t, confirm, i + 1, fee_r):
                                        still2.append(t)
                                open_trades = still2

                                entry_candle = candles[i + 2]
                                entry_price = entry_candle.o

                                # SL = OB extreme
                                if sig == 2:  # long
                                    sl = curr.l
                                    risk_pu = entry_price - sl
                                    tp = entry_price + tp_r * risk_pu
                                else:  # short
                                    sl = curr.h
                                    risk_pu = sl - entry_price
                                    tp = entry_price - tp_r * risk_pu

                                if risk_pu > 0 and risk_pu / entry_price >= 0.001:
                                    direction = "long" if sig == 2 else "short"
                                    t = SimTrade(symbol, direction, entry_price,
                                                 sl, tp, risk_pu, i + 2)
                                    # Don't resolve on entry candle itself
                                    # (entered at open, resolve from next candle)
                                    open_trades.append(t)
                                    trades.append(t)
                                    i = i + 2  # skip to entry candle
                                    continue
                    else:
                        # Pure: entry at next bar open
                        if i + 1 < n:
                            entry_candle = candles[i + 1]
                            entry_price = entry_candle.o

                            if sig == 2:  # long
                                sl = curr.l
                                risk_pu = entry_price - sl
                                tp = entry_price + tp_r * risk_pu
                            else:  # short
                                sl = curr.h
                                risk_pu = sl - entry_price
                                tp = entry_price - tp_r * risk_pu

                            if risk_pu > 0 and risk_pu / entry_price >= 0.001:
                                direction = "long" if sig == 2 else "short"
                                t = SimTrade(symbol, direction, entry_price,
                                             sl, tp, risk_pu, i + 1)
                                open_trades.append(t)
                                trades.append(t)
                                i = i + 1
                                continue
        i += 1

    # Resolve any remaining open trades on remaining candles
    for j in range(i, n):
        still_open = []
        for t in open_trades:
            if not resolve_candle(t, candles[j], j, fee_r):
                still_open.append(t)
        open_trades = still_open

    # Force-close any still open at market close
    if open_trades and n > 0:
        last = candles[-1]
        for t in open_trades:
            t.exit_price = last.c
            t.exit_idx = n - 1
            if t.direction == "long":
                t.pnl_r = (last.c - t.entry) / t.risk_per_unit - fee_r
            else:
                t.pnl_r = (t.entry - last.c) / t.risk_per_unit - fee_r
            t.reason = "FORCE_CLOSE"

    return trades


# ==================================================================
#  Portfolio-level backtest with equity curve
# ==================================================================

def backtest_portfolio(pairs_data: dict, tp_r: float = 2.0,
                       use_nextbar: bool = True,
                       start_equity: float = 50.0,
                       risk_pct: float = 0.02,
                       max_concurrent: int = 2,
                       fee_r: float = 0.04) -> dict:
    """
    Run backtest across all pairs and produce summary stats.
    Trades are INDEPENDENT per pair (no cross-pair equity tracking
    to keep it honest -- just sum R).
    """
    all_trades = []
    per_pair = {}

    for symbol, candles in sorted(pairs_data.items()):
        trades = backtest_pair(symbol, candles, tp_r, use_nextbar,
                               max_concurrent, fee_r)
        per_pair[symbol] = trades
        all_trades.extend(trades)

    # Aggregate stats
    total = len(all_trades)
    closed = [t for t in all_trades if t.reason != ""]
    wins = sum(1 for t in closed if t.pnl_r > 0)
    losses = sum(1 for t in closed if t.pnl_r <= 0)
    total_r = sum(t.pnl_r for t in closed)
    avg_r = total_r / max(1, len(closed))
    wr = wins / max(1, wins + losses) * 100

    # Max drawdown in R stream
    peak_r = 0.0
    cum_r = 0.0
    max_dd_r = 0.0
    for t in sorted(closed, key=lambda x: x.entry_idx):
        cum_r += t.pnl_r
        if cum_r > peak_r:
            peak_r = cum_r
        dd = peak_r - cum_r
        if dd > max_dd_r:
            max_dd_r = dd

    # Equity simulation (compound)
    equity = start_equity
    peak_eq = start_equity
    max_dd_pct = 0.0
    for t in sorted(closed, key=lambda x: x.entry_idx):
        dollar_risk = equity * risk_pct
        pnl_usd = dollar_risk * t.pnl_r
        equity += pnl_usd
        if equity > peak_eq:
            peak_eq = equity
        dd_pct = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return {
        "total_trades": total,
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 4),
        "max_dd_r": round(max_dd_r, 2),
        "final_equity": round(equity, 2),
        "peak_equity": round(peak_eq, 2),
        "max_dd_pct": round(max_dd_pct, 1),
        "per_pair": {
            sym: {
                "n": len(trades),
                "w": sum(1 for t in trades if t.pnl_r > 0),
                "l": sum(1 for t in trades if t.pnl_r <= 0),
                "r": round(sum(t.pnl_r for t in trades), 2),
            }
            for sym, trades in per_pair.items()
        },
    }


# ==================================================================
#  CLI
# ==================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="OBR Honest Backtest (no look-ahead bias)")
    parser.add_argument("--pairs", choices=["bybit", "binance", "all"],
                        default="bybit")
    parser.add_argument("--tp", type=float, default=2.0,
                        help="TP in R-multiples (default: 2.0)")
    parser.add_argument("--pure", action="store_true",
                        help="Use PURE variant (no nextbar confirm)")
    parser.add_argument("--nextbar", action="store_true", default=True,
                        help="Use NEXTBAR variant (default)")
    parser.add_argument("--equity", type=float, default=50.0,
                        help="Start equity (default: 50)")
    parser.add_argument("--risk", type=float, default=0.02,
                        help="Risk per trade (default: 0.02)")
    args = parser.parse_args()

    use_nextbar = not args.pure
    variant = "NEXTBAR" if use_nextbar else "PURE"

    print(f"OBR Honest Backtest")
    print(f"  Variant: {variant} | TP: {args.tp}R | Risk: {args.risk*100:.0f}%")
    print(f"  Equity: ${args.equity} | Exchange: {args.pairs}")
    print(f"  Fee: 0.04R round-trip")
    print()

    # Load data
    t0 = time.time()
    if args.pairs == "all":
        data = {}
        data.update(load_pairs("bybit"))
        data.update({f"BIN_{k}": v for k, v in load_pairs("binance").items()})
    else:
        data = load_pairs(args.pairs)

    print(f"  Loaded {len(data)} pairs in {time.time()-t0:.1f}s")

    if not data:
        print("  No data found! Make sure data/ directory has CSV files.")
        return

    # Run backtest
    t0 = time.time()
    results = backtest_portfolio(
        data,
        tp_r=args.tp,
        use_nextbar=use_nextbar,
        start_equity=args.equity,
        risk_pct=args.risk,
    )
    elapsed = time.time() - t0

    # Print results
    print(f"\n{'='*60}")
    print(f"  OBR-{variant}-{args.tp}R  ({args.pairs.upper()})")
    print(f"{'='*60}")
    print(f"  Trades:  {results['closed']}")
    print(f"  Wins:    {results['wins']}  ({results['wr']:.1f}%)")
    print(f"  Losses:  {results['losses']}")
    print(f"  Total R: {results['total_r']:+.2f}")
    print(f"  Avg R:   {results['avg_r']:+.4f}")
    print(f"  Max DD:  {results['max_dd_r']:.2f}R  |  {results['max_dd_pct']:.1f}%")
    print(f"  Equity:  ${args.equity} -> ${results['final_equity']:.2f}")
    print(f"  Peak:    ${results['peak_equity']:.2f}")
    print(f"  Time:    {elapsed:.1f}s")

    # Per-pair breakdown (top 20 by R)
    pp = sorted(results["per_pair"].items(), key=lambda x: -x[1]["r"])
    print(f"\n  Top 20 pairs by R:")
    print(f"  {'Pair':<20s} {'Trades':>6} {'Wins':>5} {'WR':>6} {'TotalR':>8}")
    for sym, stats in pp[:20]:
        wr = stats["w"] / max(1, stats["w"] + stats["l"]) * 100
        print(f"  {sym:<20s} {stats['n']:>6} {stats['w']:>5} "
              f"{wr:>5.1f}% {stats['r']:>+8.2f}")

    # Bottom 10
    print(f"\n  Bottom 10 pairs by R:")
    for sym, stats in pp[-10:]:
        wr = stats["w"] / max(1, stats["w"] + stats["l"]) * 100
        print(f"  {sym:<20s} {stats['n']:>6} {stats['w']:>5} "
              f"{wr:>5.1f}% {stats['r']:>+8.2f}")


if __name__ == "__main__":
    main()
