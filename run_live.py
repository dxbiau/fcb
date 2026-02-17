"""
run_live.py — Launch FCB bot in LIVE (real money) mode on Bybit mainnet.

This script:
  1. Verifies config is set to MAINNET mode
  2. Runs full pre-flight checks (connection, balance, pairs, leverage)
  3. Requires explicit user confirmation before starting
  4. Resets state.json for fresh start (backup created)
  5. Starts the bot with enhanced logging

Usage:
  # First set your API keys as environment variables:
  $env:BYBIT_API_KEY = "your_mainnet_key"
  $env:BYBIT_API_SECRET = "your_mainnet_secret"

  # Run the bot:
  python run_live.py

  # Pre-flight checks only (no trading):
  python run_live.py --preflight

  # Skip the confirmation prompt (for automation):
  python run_live.py --yes

The bot runs 24/7, trading 3 sessions:
  - Asia   00:00-08:00 UTC  (22 pairs)
  - London 08:00-16:00 UTC  (11 pairs)
  - NY     16:00-24:00 UTC  (12 pairs)
  - 37 unique pairs total

Strategy: FCB trail 0.3R | 2% risk | 10x leverage | midpoint SL
Equity floor: $500 — stops all trading if breached
"""

import sys, os, time, json, shutil, argparse, traceback, atexit, signal
from datetime import datetime, timezone

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live", "bot.lock")


def acquire_lock():
    """Acquire PID lock file. Exits if another instance is running."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if that PID is still alive
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, old_pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                print(f"ERROR: Bot is already running (PID {old_pid}).")
                print(f"  If this is stale, delete {LOCK_FILE} and retry.")
                sys.exit(1)
            else:
                # Process is dead — stale lock
                print(f"  Removing stale lock (PID {old_pid} no longer running)")
        except (ValueError, OSError):
            print(f"  Removing invalid lock file")
    # Write our PID
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    """Release PID lock file."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception:
        pass


def preflight():
    """Run all pre-flight checks before starting the bot."""
    print("=" * 70)
    print("  FCB BOT — LIVE (MAINNET) PRE-FLIGHT CHECK")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 70)

    # 1. Verify config
    print("\n[1/7] Checking config...")
    from live.config import (
        MAINNET, DEMO_MODE, API_KEY, API_SECRET,
        ALL_PAIRS, PAIRS, LEVERAGE, RISK_PCT_A, RISK_PCT_B,
        TP_R, EQUITY_FLOOR,
    )
    assert MAINNET, "MAINNET must be True for live trading"
    assert not DEMO_MODE, "DEMO_MODE must be False for live trading"
    assert API_KEY, "API_KEY is empty — set BYBIT_API_KEY env var"
    assert API_SECRET, "API_SECRET is empty — set BYBIT_API_SECRET env var"
    print(f"  ✓ MAINNET={MAINNET}, DEMO_MODE={DEMO_MODE}")
    print(f"  ✓ API_KEY={API_KEY[:4]}...{API_KEY[-4:]} ({len(API_KEY)} chars)")
    print(f"  ✓ Pairs: {len(ALL_PAIRS)} unique across {len(PAIRS)} sessions")
    print(f"  ✓ Risk: A={RISK_PCT_A*100:.0f}% B={RISK_PCT_B*100:.0f}% | "
          f"TP={TP_R}R | Leverage={LEVERAGE}x")
    print(f"  ✓ Equity floor: ${EQUITY_FLOOR:.0f}")

    for sess, plist in PAIRS.items():
        a_count = sum(1 for _, c in plist if c == "A")
        b_count = len(plist) - a_count
        print(f"    {sess:>7}: {len(plist)} pairs ({a_count}A, {b_count}B)")

    # 2. Connect to exchange
    print("\n[2/7] Connecting to Bybit MAINNET...")
    from live import exchange as exch
    ex = exch.create_exchange()
    print(f"  ✓ Connected — {len(ex.markets)} markets loaded")

    # 3. Check balance
    print("\n[3/7] Checking balance...")
    equity = exch.get_equity(ex)
    print(f"  ✓ Equity: ${equity:.2f} USDT")
    if equity < 100:
        print(f"  ⚠ WARNING: Equity very low (${equity:.2f}).")
    if equity < EQUITY_FLOOR:
        print(f"  ✗ FATAL: Equity ${equity:.2f} < floor ${EQUITY_FLOOR:.0f}. "
              "Cannot start.")
        return False

    # 4. Verify all pairs exist and get market info
    print(f"\n[4/7] Verifying {len(ALL_PAIRS)} pairs on exchange...")
    ok = 0
    fail_pairs = []
    for pair in ALL_PAIRS:
        try:
            exch.get_market_info(ex, pair)
            ok += 1
        except Exception as e:
            print(f"  ✗ {pair}: {e}")
            fail_pairs.append(pair)
    print(f"  ✓ {ok}/{len(ALL_PAIRS)} pairs available" +
          (f" ({len(fail_pairs)} failed: {', '.join(fail_pairs)})" if fail_pairs else ""))
    if fail_pairs:
        print("  ⚠ Failed pairs will be skipped at runtime")

    # 5. Set leverage + margin mode
    print(f"\n[5/7] Setting leverage & margin mode...")
    lev_ok = 0
    for pair in ALL_PAIRS:
        try:
            exch.set_leverage(ex, pair, LEVERAGE)
            exch.set_margin_mode(ex, pair, "isolated")
            lev_ok += 1
        except Exception as e:
            if "not modified" in str(e).lower():
                lev_ok += 1
            else:
                print(f"  ⚠ {pair}: {e}")
    print(f"  ✓ {lev_ok}/{len(ALL_PAIRS)} pairs configured")

    # 6. Test order placement (place + cancel a tiny limit order)
    print("\n[6/7] Testing order placement (place + cancel)...")
    test_pair = "BTC/USDT:USDT"
    try:
        ticker = exch.get_ticker(ex, test_pair)
        last = float(ticker["last"])
        test_price = exch.round_price(ex, test_pair, last * 0.5)
        info = exch.get_market_info(ex, test_pair)
        test_qty = info.get("min_qty", 0.001)

        order = ex.create_order(
            test_pair, "limit", "buy", test_qty, test_price,
            params={"category": "linear", "timeInForce": "GTC"},
        )
        order_id = order["id"]
        print(f"  ✓ Limit order placed: {order_id}")

        ex.cancel_order(order_id, test_pair, params={"category": "linear"})
        print(f"  ✓ Order cancelled successfully")
    except Exception as e:
        print(f"  ✗ Order test failed: {e}")
        return False

    # 7. Check existing positions/orders
    print("\n[7/7] Checking existing positions & orders...")
    try:
        positions = exch.get_open_positions(ex)
        open_count = len(positions)
        print(f"  ✓ Open positions: {open_count}")
        for p in positions:
            sym = p.get("symbol", "?")
            side = p.get("side", "?")
            sz = p.get("contracts", 0)
            pnl = p.get("unrealizedPnl", 0)
            print(f"    {sym} {side} size={sz} uPnL={pnl}")
        if open_count > 0:
            print(f"  ⚠ WARNING: {open_count} open position(s)! "
                  "Close them manually before starting or the bot will track them.")
    except Exception as e:
        print(f"  ⚠ Could not check positions: {e}")

    try:
        stale = 0
        for pair in ALL_PAIRS[:5]:
            orders = exch.get_open_orders(ex, pair)
            stale += len(orders)
        if stale:
            print(f"  ⚠ Found {stale} stale orders (will be cleaned on bot start)")
        else:
            print(f"  ✓ No stale orders found (checked 5 pairs)")
    except Exception as e:
        print(f"  ⚠ Could not check orders: {e}")

    print("\n" + "=" * 70)
    print("  PRE-FLIGHT COMPLETE — All systems GO for LIVE trading")
    print(f"  Equity: ${equity:.2f} | Pairs: {len(ALL_PAIRS)} | "
          f"Risk: A={RISK_PCT_A*100:.0f}% B={RISK_PCT_B*100:.0f}%")
    print("=" * 70)

    return True


def reset_state_for_live(equity: float = 0):
    """Backup demo state and create fresh state for live trading."""
    state_file = "live/state.json"

    if os.path.exists(state_file):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"live/state_demo_backup_{ts}.json"
        shutil.copy2(state_file, backup)
        print(f"  ✓ Demo state backed up → {backup}")

    # Write clean state
    fresh = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "daily_counts": {},
        "session_traded": {},
        "equity": equity,
        "total_trades": 0,
        "total_pnl_r": 0.0,
        "trade_history": [],
        "day_start_equity": equity,
        "wins_today": 0,
        "losses_today": 0,
        "entries_today": 0,
        "pnl_today_usd": 0.0,
        "total_wins": 0,
        "total_losses": 0,
        "pending_entries": [],
        "equity_floor_hit": False,
        "pair_classes": {},
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(state_file, "w") as f:
        json.dump(fresh, f, indent=2)
    print(f"  ✓ Fresh state created (equity=${equity:.2f})")


def run_bot():
    """Run the FCB bot in LIVE mode."""
    from live.bot import FCBBot
    from live import logger as log

    log.info("=" * 70)
    log.info("  FCB BOT — LIVE MAINNET")
    log.info(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("=" * 70)
    log.audit("BOT_START", mode="LIVE_MAINNET",
              time=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))

    bot = FCBBot()

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Shutdown via Ctrl+C")
        log.audit("BOT_STOP", reason="keyboard_interrupt")
    except Exception as e:
        log.critical(f"FATAL: {e}")
        log.debug(traceback.format_exc())
        log.audit("BOT_CRASH", error=str(e))
        raise
    finally:
        log.info("Bot shutdown complete.")
        log.audit("BOT_STOP",
                  time=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))


def main():
    parser = argparse.ArgumentParser(description="FCB Bot — LIVE Trading Runner")
    parser.add_argument("--preflight", action="store_true",
                        help="Run pre-flight checks only, don't trade")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    # Verify we're in LIVE mode
    from live.config import MAINNET, DEMO_MODE
    if not MAINNET:
        print("ERROR: MAINNET=False in config.py. Set MAINNET=True for live trading.")
        sys.exit(1)
    if DEMO_MODE:
        print("ERROR: DEMO_MODE=True in config.py. Set DEMO_MODE=False for live trading.")
        sys.exit(1)

    if args.preflight:
        success = preflight()
        sys.exit(0 if success else 1)

    # Full live startup
    print("Running pre-flight checks...")
    if not preflight():
        print("\n  ✗ Pre-flight FAILED. Fix issues before trading.")
        sys.exit(1)

    if not args.yes:
        print("\n" + "!" * 70)
        print("  ⚠  YOU ARE ABOUT TO TRADE WITH REAL MONEY  ⚠")
        print("!" * 70)
        confirm = input("\n  Type 'GO LIVE' to confirm: ")
        if confirm.strip() != "GO LIVE":
            print("  Aborted.")
            sys.exit(0)

    # Reset state for fresh live start
    print("\nPreparing fresh state for live trading...")
    from live import exchange as exch
    from live.config import API_KEY, API_SECRET
    ex = exch.create_exchange()
    equity = exch.get_equity(ex)
    reset_state_for_live(equity)

    # Acquire lock — prevents duplicate instances
    acquire_lock()
    atexit.register(release_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean lock on kill

    print("\nStarting bot in 5 seconds... (Ctrl+C to abort)")
    time.sleep(5)
    run_bot()


if __name__ == "__main__":
    main()
