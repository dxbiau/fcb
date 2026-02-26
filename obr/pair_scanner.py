r"""
obr/pair_scanner.py -- Pull fresh Bybit data & find OBR-compatible pairs.

1. Connects to Bybit (public API, no auth needed for candles)
2. Fetches top USDT perpetual pairs by 24h volume
3. Downloads 5m candles (last ~30 days = 8640 candles)
4. Runs honest OBR backtest on each pair at multiple TP levels
5. Ranks by: WR, Total R, Max DD, Expectancy
6. Outputs a shortlist of "scalp-worthy" pairs (DD < 30%, positive R)

Usage:
  .venv\Scripts\python.exe -m obr.pair_scanner [--top N] [--days D]
"""

import os
import sys
import csv
import time
import math
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ccxt
except ImportError:
    print("ccxt not installed. Run: pip install ccxt")
    sys.exit(1)


# =====================================================================
#  Candle / signal / trade -- reuse from backtest.py
# =====================================================================

class Candle:
    __slots__ = ("dt", "o", "h", "l", "c", "v")
    def __init__(self, dt, o, h, l, c, v):
        self.dt = dt; self.o = o; self.h = h
        self.l = l; self.c = c; self.v = v
    @property
    def range(self): return self.h - self.l


def detect_outside_bar(prev: Candle, curr: Candle) -> int:
    """Exact notebook replica with candle color check."""
    if curr.h <= prev.h or curr.l >= prev.l:
        return 0
    # LONG: bearish candle + close < prev low
    if curr.o > curr.c and curr.c < prev.l:
        return 2  # fade LONG
    # SHORT: bullish candle + close > prev high
    if curr.o < curr.c and curr.c > prev.h:
        return 1  # fade SHORT
    return 0


def nextbar_confirms(sig: int, confirm: Candle) -> bool:
    if sig == 2: return confirm.c > confirm.o
    if sig == 1: return confirm.c < confirm.o
    return False


class SimTrade:
    __slots__ = ("direction", "entry", "sl", "tp", "risk_pu",
                 "entry_idx", "exit_idx", "exit_price", "pnl_r", "reason")
    def __init__(self, direction, entry, sl, tp, risk_pu, entry_idx):
        self.direction = direction; self.entry = entry
        self.sl = sl; self.tp = tp; self.risk_pu = risk_pu
        self.entry_idx = entry_idx; self.exit_idx = -1
        self.exit_price = 0.0; self.pnl_r = 0.0; self.reason = ""


def resolve(t: SimTrade, c: Candle, idx: int, fee_r=0.04) -> bool:
    if t.direction == "long":
        sl_hit = c.l <= t.sl; tp_hit = c.h >= t.tp
    else:
        sl_hit = c.h >= t.sl; tp_hit = c.l <= t.tp
    if sl_hit and tp_hit:
        t.exit_price = t.sl; t.reason = "SL(amb)"
    elif sl_hit:
        t.exit_price = t.sl; t.reason = "SL"
    elif tp_hit:
        t.exit_price = t.tp; t.reason = "TP"
    else:
        return False
    if t.direction == "long":
        t.pnl_r = (t.exit_price - t.entry) / t.risk_pu - fee_r
    else:
        t.pnl_r = (t.entry - t.exit_price) / t.risk_pu - fee_r
    t.exit_idx = idx
    return True


# =====================================================================
#  Backtest engine (single pair)
# =====================================================================

def backtest_pair(candles: List[Candle], tp_r: float = 2.0,
                  use_nextbar: bool = True, fee_r: float = 0.04,
                  max_concurrent: int = 1) -> List[SimTrade]:
    trades = []
    open_trades = []
    n = len(candles)
    i = 1
    while i < n - 2:
        still = []
        for t in open_trades:
            if not resolve(t, candles[i], i, fee_r):
                still.append(t)
        open_trades = still

        if len(open_trades) < max_concurrent:
            prev, curr = candles[i-1], candles[i]
            if curr.range > 0 and prev.range > 0:
                sig = detect_outside_bar(prev, curr)
                if sig != 0:
                    if use_nextbar:
                        if i + 2 < n:
                            confirm = candles[i+1]
                            if nextbar_confirms(sig, confirm):
                                st2 = []
                                for t in open_trades:
                                    if not resolve(t, confirm, i+1, fee_r):
                                        st2.append(t)
                                open_trades = st2
                                ep = candles[i+2].o
                                if sig == 2:
                                    sl = curr.l; rpu = ep - sl; tp = ep + tp_r * rpu
                                else:
                                    sl = curr.h; rpu = sl - ep; tp = ep - tp_r * rpu
                                if rpu > 0 and rpu / ep >= 0.0005:
                                    d = "long" if sig == 2 else "short"
                                    t = SimTrade(d, ep, sl, tp, rpu, i+2)
                                    open_trades.append(t); trades.append(t)
                                    i = i + 2; continue
                    else:
                        if i + 1 < n:
                            ep = candles[i+1].o
                            if sig == 2:
                                sl = curr.l; rpu = ep - sl; tp = ep + tp_r * rpu
                            else:
                                sl = curr.h; rpu = sl - ep; tp = ep - tp_r * rpu
                            if rpu > 0 and rpu / ep >= 0.0005:
                                d = "long" if sig == 2 else "short"
                                t = SimTrade(d, ep, sl, tp, rpu, i+1)
                                open_trades.append(t); trades.append(t)
                                i = i + 1; continue
        i += 1

    # Resolve remaining
    for j in range(i, n):
        still = []
        for t in open_trades:
            if not resolve(t, candles[j], j, fee_r):
                still.append(t)
        open_trades = still
    # Force close
    if open_trades and n > 0:
        last = candles[-1]
        for t in open_trades:
            t.exit_price = last.c; t.exit_idx = n-1
            if t.direction == "long":
                t.pnl_r = (last.c - t.entry) / t.risk_pu - fee_r
            else:
                t.pnl_r = (t.entry - last.c) / t.risk_pu - fee_r
            t.reason = "FORCE"
    return trades


def compute_stats(trades: List[SimTrade], start_eq: float = 50.0,
                  risk_pct: float = 0.02) -> dict:
    closed = [t for t in trades if t.reason]
    if not closed:
        return {"n": 0, "wr": 0, "total_r": 0, "avg_r": 0,
                "max_dd_pct": 0, "final_eq": start_eq, "expectancy": 0,
                "profit_factor": 0, "avg_win_r": 0, "avg_loss_r": 0}
    wins = [t for t in closed if t.pnl_r > 0]
    losses = [t for t in closed if t.pnl_r <= 0]
    n = len(closed)
    w = len(wins); lo = len(losses)
    total_r = sum(t.pnl_r for t in closed)
    wr = w / n * 100
    avg_r = total_r / n

    gross_win = sum(t.pnl_r for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_r for t in losses)) if losses else 0.001
    pf = gross_win / gross_loss if gross_loss > 0 else 999

    avg_win = gross_win / max(1, w)
    avg_loss = -sum(t.pnl_r for t in losses) / max(1, lo) if losses else 0

    # Equity curve DD
    eq = start_eq; peak = start_eq; max_dd = 0
    for t in sorted(closed, key=lambda x: x.entry_idx):
        pnl = eq * risk_pct * t.pnl_r
        eq += pnl
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    return {
        "n": n, "w": w, "l": lo,
        "wr": round(wr, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 4),
        "max_dd_pct": round(max_dd, 1),
        "final_eq": round(eq, 2),
        "expectancy": round(avg_r, 4),
        "profit_factor": round(pf, 2),
        "avg_win_r": round(avg_win, 3),
        "avg_loss_r": round(avg_loss, 3),
    }


# =====================================================================
#  Data fetching from Bybit
# =====================================================================

def create_public_exchange() -> ccxt.bybit:
    """Create unauthenticated Bybit instance (public data only)."""
    ex = ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    return ex


def get_top_pairs(ex: ccxt.bybit, top_n: int = 150,
                  min_turnover: float = 1_000_000) -> List[str]:
    """Get top USDT perpetual pairs by 24h volume."""
    tickers = ex.fetch_tickers()
    perps = []
    for sym, tk in tickers.items():
        if not sym.endswith(":USDT"):
            continue
        if "/USDT" not in sym:
            continue
        # Must be active swap
        mkt = ex.markets.get(sym)
        if not mkt or mkt.get("type") != "swap" or not mkt.get("active"):
            continue
        vol_usd = float(tk.get("quoteVolume") or 0)
        if vol_usd < min_turnover:
            continue
        perps.append((sym, vol_usd))

    perps.sort(key=lambda x: -x[1])
    return [p[0] for p in perps[:top_n]]


def fetch_candles(ex: ccxt.bybit, symbol: str,
                  timeframe: str = "5m",
                  days: int = 30) -> List[Candle]:
    """Fetch historical 5m candles for a symbol (paginated)."""
    candles = []
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    limit = 200  # Bybit max per request

    while True:
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            print(f"    Error fetching {symbol}: {e}")
            break

        if not ohlcv:
            break

        for bar in ohlcv:
            ts, o, h, l, c, v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
            if h != l:  # skip flat candles
                dt_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S")
                candles.append(Candle(dt_str, o, h, l, c, v))

        last_ts = ohlcv[-1][0]
        since = last_ts + 1  # next batch starts after last candle

        if len(ohlcv) < limit:
            break

        time.sleep(0.1)  # rate limit

    return candles


# =====================================================================
#  Main scanner
# =====================================================================

def scan(top_n: int = 100, days: int = 60,
         tp_levels: List[float] = None,
         use_nextbar: bool = True,
         risk_pct: float = 0.02,
         start_eq: float = 50.0) -> List[dict]:
    """
    Full scan: pull data -> backtest -> rank.
    Returns list of pair results sorted by total_r.
    """
    if tp_levels is None:
        tp_levels = [1.0, 1.5, 2.0, 2.5, 3.0]

    print(f"OBR Pair Scanner")
    print(f"  Top {top_n} pairs | {days} days | TP levels: {tp_levels}")
    print(f"  Variant: {'NEXTBAR' if use_nextbar else 'PURE'}")
    print(f"  Risk: {risk_pct*100:.0f}% | Start: ${start_eq}")
    print()

    # Connect
    print("  Connecting to Bybit...")
    ex = create_public_exchange()
    print(f"  Markets loaded: {len(ex.markets)}")

    # Get top pairs
    print(f"  Fetching top {top_n} pairs by volume...")
    pairs = get_top_pairs(ex, top_n)
    print(f"  Found {len(pairs)} qualifying pairs")
    print()

    # Scan each pair
    all_results = []
    for idx, symbol in enumerate(pairs):
        short_name = symbol.replace("/USDT:USDT", "")
        print(f"  [{idx+1}/{len(pairs)}] {short_name}...", end="", flush=True)

        candles = fetch_candles(ex, symbol, "5m", days)
        if len(candles) < 200:
            print(f" skip ({len(candles)} candles)")
            continue

        # Test each TP level
        best_tp = None
        best_stats = None
        best_total_r = -9999

        for tp_r in tp_levels:
            trades = backtest_pair(candles, tp_r, use_nextbar, fee_r=0.04)
            stats = compute_stats(trades, start_eq, risk_pct)
            stats["tp_r"] = tp_r

            if stats["total_r"] > best_total_r:
                best_total_r = stats["total_r"]
                best_stats = stats
                best_tp = tp_r

        if best_stats:
            best_stats["symbol"] = symbol
            best_stats["short"] = short_name
            best_stats["candles"] = len(candles)
            best_stats["best_tp"] = best_tp
            all_results.append(best_stats)
            wr = best_stats["wr"]
            tr = best_stats["total_r"]
            dd = best_stats["max_dd_pct"]
            print(f" {len(candles)} bars | best TP={best_tp}R | "
                  f"WR={wr:.0f}% | R={tr:+.1f} | DD={dd:.0f}%")
        else:
            print(" no trades")

        time.sleep(0.05)

    # Sort by total_r descending
    all_results.sort(key=lambda x: -x["total_r"])

    print()
    print("=" * 80)
    print("  OBR PAIR COMPATIBILITY RANKING")
    print("=" * 80)
    print()

    # Show all pairs in a table
    print(f"  {'#':>3} {'Pair':<14} {'TP':>4} {'Trades':>6} {'W':>4} {'L':>4} "
          f"{'WR':>5} {'TotR':>7} {'AvgR':>7} {'DD%':>5} {'PF':>5} "
          f"{'AvgW':>6} {'AvgL':>6} {'$Eq':>8}")
    print(f"  {'-'*3} {'-'*14} {'-'*4} {'-'*6} {'-'*4} {'-'*4} "
          f"{'-'*5} {'-'*7} {'-'*7} {'-'*5} {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*8}")

    for rank, r in enumerate(all_results, 1):
        marker = ""
        if r["total_r"] > 0 and r["max_dd_pct"] < 30:
            marker = " <<<" if r["total_r"] > 5 else " <"
        print(f"  {rank:>3} {r['short']:<14} {r['best_tp']:>3.1f} "
              f"{r['n']:>6} {r.get('w',0):>4} {r.get('l',0):>4} "
              f"{r['wr']:>4.1f}% {r['total_r']:>+7.1f} {r['avg_r']:>+7.4f} "
              f"{r['max_dd_pct']:>4.1f}% {r['profit_factor']:>5.2f} "
              f"{r['avg_win_r']:>+5.3f} {r['avg_loss_r']:>+5.3f} "
              f"${r['final_eq']:>7.2f}{marker}")

    # Filter for scalp-worthy
    scalp_worthy = [r for r in all_results
                    if r["total_r"] > 0 and r["max_dd_pct"] < 30 and r["n"] >= 10]

    print()
    print("=" * 80)
    print(f"  SCALP-WORTHY PAIRS (positive R + DD < 30%): {len(scalp_worthy)}")
    print("=" * 80)
    if scalp_worthy:
        for r in scalp_worthy:
            score = r["total_r"] / max(1, r["max_dd_pct"]) * r["wr"] / 100
            print(f"  {r['short']:<14} TP={r['best_tp']}R | "
                  f"{r['n']} trades | WR={r['wr']:.1f}% | "
                  f"R={r['total_r']:+.1f} | DD={r['max_dd_pct']:.1f}% | "
                  f"PF={r['profit_factor']:.2f} | "
                  f"${r['final_eq']:.2f} | Score={score:.2f}")
    else:
        print("  None found. Try more days or relaxed criteria.")

    # Also find "low DD" pairs even if slightly negative
    low_dd = [r for r in all_results
              if r["max_dd_pct"] < 25 and r["n"] >= 10]
    low_dd.sort(key=lambda x: x["max_dd_pct"])

    print()
    print(f"  LOW DRAWDOWN PAIRS (DD < 25%, any R): {len(low_dd)}")
    for r in low_dd[:20]:
        print(f"  {r['short']:<14} TP={r['best_tp']}R | "
              f"{r['n']} trades | WR={r['wr']:.1f}% | "
              f"R={r['total_r']:+.1f} | DD={r['max_dd_pct']:.1f}% | "
              f"PF={r['profit_factor']:.2f}")

    # Save results
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "scan_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Full results saved to {out_path}")

    return all_results


# =====================================================================
#  CLI
# =====================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="OBR Pair Scanner - Bybit")
    parser.add_argument("--top", type=int, default=100,
                        help="Top N pairs by volume (default: 100)")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history (default: 60)")
    parser.add_argument("--pure", action="store_true",
                        help="Use PURE variant (no nextbar confirm)")
    parser.add_argument("--tp", type=str, default="1.0,1.5,2.0,2.5,3.0",
                        help="Comma-separated TP levels (default: 1.0,1.5,2.0,2.5,3.0)")
    args = parser.parse_args()

    tp_levels = [float(x) for x in args.tp.split(",")]
    use_nextbar = not args.pure

    scan(top_n=args.top, days=args.days, tp_levels=tp_levels,
         use_nextbar=use_nextbar)


if __name__ == "__main__":
    main()
