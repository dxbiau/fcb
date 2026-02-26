"""
download_1m_data.py -- Download 1-minute OHLCV candle data from Bybit.

Downloads 1m data for all v13 portfolio pairs (or all 186 pairs) and saves
to data/ directory for Path C 1-minute strategy discovery.

Usage:
    python download_1m_data.py                    # Portfolio pairs only (37)
    python download_1m_data.py --all              # All 186 valid pairs
    python download_1m_data.py --days 90          # Custom lookback (default 180)
    python download_1m_data.py --pairs BTC ETH    # Specific pairs only
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import ccxt
import pandas as pd

# ═══════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════

DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
EXCHANGE_ID = "bybit"
MARKET_TYPE = "linear"      # USDT perpetual swap
TIMEFRAME = "1m"
LIMIT_PER_REQUEST = 200     # Bybit max per request
RATE_LIMIT_SLEEP = 0.15     # 150ms between requests (conservative)


def _sanitize_pair(pair: str) -> str:
    """BTC/USDT:USDT → BTC_USDT_USDT"""
    return pair.replace("/", "_").replace(":", "_")


def _csv_path(pair: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"bybit_1m_{_sanitize_pair(pair)}.csv"


def get_all_bybit_pairs() -> List[str]:
    """Get all USDT perpetual pairs from Bybit."""
    ex = ccxt.bybit({"enableRateLimit": True})
    ex.load_markets()
    pairs = [
        s for s, m in ex.markets.items()
        if m.get("linear") and m.get("active")
        and s.endswith("/USDT:USDT")
        and m.get("type") == "swap"
    ]
    return sorted(pairs)


def get_portfolio_pairs() -> List[str]:
    """Get pairs from v13 deployment portfolio."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from obr.combo_registry import ComboRegistry
    reg = ComboRegistry()
    return sorted(reg.all_pairs)


def get_existing_5m_pairs() -> List[str]:
    """Get all pairs that have existing 5m CSV data."""
    pairs = set()
    for f in DATA_DIR.glob("*_5m.csv"):
        # bybit_futures_DOGE_USDT_USDT_5m.csv → DOGE/USDT:USDT
        name = f.stem  # e.g. bybit_futures_DOGE_USDT_USDT_5m
        # Strip prefix and suffix
        for prefix in ["binance_futures_", "bybit_futures_"]:
            if name.startswith(prefix):
                name = name[len(prefix):]
        if name.endswith("_5m"):
            name = name[:-3]

        # DOGE_USDT_USDT → DOGE/USDT:USDT
        # DOGE_USDT → DOGE/USDT:USDT
        parts = name.split("_")
        if len(parts) >= 3 and parts[-1] == "USDT" and parts[-2] == "USDT":
            base = "_".join(parts[:-2])
            pairs.add(f"{base}/USDT:USDT")
        elif len(parts) >= 2 and parts[-1] == "USDT":
            base = "_".join(parts[:-1])
            pairs.add(f"{base}/USDT:USDT")

    return sorted(pairs)


def download_1m(pair: str, days: int = 180, force: bool = False) -> bool:
    """
    Download 1m candles for a single pair.

    Returns True if new data was downloaded, False if skipped (cached).
    """
    csv_path = _csv_path(pair)

    # Check cache
    if csv_path.exists() and not force:
        try:
            cached = pd.read_csv(csv_path, parse_dates=["date"])
            if len(cached) > 1000:
                age_s = (datetime.now(timezone.utc) -
                         pd.Timestamp(cached["date"].iloc[-1]).tz_localize(
                             "UTC" if cached["date"].dt.tz is None else None)
                         ).total_seconds()
                if age_s < 86400:  # less than 1 day old
                    print(f"  [cache] {pair}: {len(cached):,} candles "
                          f"(age: {age_s/3600:.1f}h)")
                    return False
        except Exception:
            pass  # re-download on any cache error

    short = pair.split("/")[0]
    print(f"  [download] {short}: downloading {days}d of 1m data...")

    ex = ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })

    start = datetime.now(timezone.utc) - timedelta(days=days)
    since = int(start.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    all_candles = []
    retries = 0
    max_retries = 5

    while since < end_ms:
        try:
            candles = ex.fetch_ohlcv(
                pair, TIMEFRAME, since=since, limit=LIMIT_PER_REQUEST
            )
        except Exception as e:
            retries += 1
            if retries >= max_retries:
                print(f"    ⚠ {short}: giving up after {max_retries} retries: {e}")
                break
            print(f"    ⚠ {short}: error, retry {retries}/{max_retries}: {e}")
            time.sleep(5)
            continue

        if not candles:
            break

        all_candles.extend(candles)
        since = candles[-1][0] + 1  # next ms after last candle
        time.sleep(RATE_LIMIT_SLEEP)

        # Progress
        if len(all_candles) % 10000 < LIMIT_PER_REQUEST:
            elapsed_days = (candles[-1][0] - all_candles[0][0]) / 86400000
            print(f"    ... {short}: {len(all_candles):,} candles "
                  f"({elapsed_days:.0f}d)")

        # Safety: Bybit 1m caps at ~200 days
        if len(all_candles) > 500000:
            break

    if not all_candles:
        print(f"    ❌ {short}: no data returned")
        return False

    # Build DataFrame
    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.drop(columns=["timestamp"], inplace=True)
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df.drop_duplicates(subset=["date"], inplace=True)
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Save
    df.to_csv(csv_path, index=False)
    span_days = (df["date"].iloc[-1] - df["date"].iloc[0]).total_seconds() / 86400
    print(f"    ✅ {short}: {len(df):,} candles ({span_days:.0f}d) "
          f"→ {csv_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Download 1m candle data")
    parser.add_argument("--all", action="store_true",
                        help="Download all pairs with existing 5m data")
    parser.add_argument("--bybit-all", action="store_true",
                        help="Download ALL active Bybit USDT perpetual pairs")
    parser.add_argument("--days", type=int, default=180,
                        help="Number of days of data (default: 180)")
    parser.add_argument("--pairs", nargs="+", type=str, default=None,
                        help="Specific base tokens (e.g., BTC ETH DOGE)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if cache exists")
    args = parser.parse_args()

    print(f"═══════════════════════════════════════════")
    print(f"  1m Data Downloader — {args.days}d lookback")
    print(f"═══════════════════════════════════════════")

    if args.pairs:
        pairs = [f"{p}/USDT:USDT" for p in args.pairs]
        print(f"  Mode: specific pairs ({len(pairs)})")
    elif args.bybit_all:
        pairs = get_all_bybit_pairs()
        print(f"  Mode: ALL Bybit USDT perps ({len(pairs)} pairs)")
    elif args.all:
        pairs = get_existing_5m_pairs()
        print(f"  Mode: all pairs with 5m data ({len(pairs)} pairs)")
    else:
        pairs = get_portfolio_pairs()
        print(f"  Mode: v13 portfolio pairs ({len(pairs)} pairs)")

    print(f"  Output: {DATA_DIR}/bybit_1m_*.csv")
    print(f"  TF: 1m | Days: {args.days}")
    print()

    downloaded = 0
    skipped = 0
    failed = 0

    t0 = time.time()
    for i, pair in enumerate(pairs, 1):
        short = pair.split("/")[0]
        print(f"[{i}/{len(pairs)}] {short}:")
        try:
            new = download_1m(pair, days=args.days, force=args.force)
            if new:
                downloaded += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"    ❌ {short}: {e}")
            failed += 1

    elapsed = time.time() - t0
    print()
    print(f"═══════════════════════════════════════════")
    print(f"  Done in {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"  Downloaded: {downloaded}")
    print(f"  Cached: {skipped}")
    print(f"  Failed: {failed}")
    print(f"═══════════════════════════════════════════")


if __name__ == "__main__":
    main()
