"""
run_obr.py -- Entry point for OBR (Outside Bar Reversal) live bot.

ALL skill agents (supervisor, log scanner, heartbeat monitor,
position guardian) start automatically with every bot launch.

Usage:
  .venv\\Scripts\\python.exe run_obr.py              # supervised (default)
  .venv\\Scripts\\python.exe run_obr.py --yes         # auto-confirm, supervised
  .venv\\Scripts\\python.exe run_obr.py --bare --yes  # direct (no supervisor)
  .venv\\Scripts\\python.exe run_obr.py --status      # show status only
  .venv\\Scripts\\python.exe run_obr.py --errors      # show error digest (24h)
  .venv\\Scripts\\python.exe run_obr.py --errors 48   # error digest (48h)
  .venv\\Scripts\\python.exe run_obr.py --backtest    # run honest backtest

Requires:
  - BYBIT_API_KEY and BYBIT_API_SECRET env vars (mainnet)
  - ccxt package: pip install ccxt
"""

import sys
import os

# Force UTF-8 stdout (needed when piped through supervisor on Windows)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def preflight():
    """Check all requirements before starting."""
    errors = []

    # 1. ccxt
    try:
        import ccxt
    except ImportError:
        errors.append("ccxt not installed: pip install ccxt")

    # 2. API keys (mainnet only)
    from obr import config as cfg
    if cfg.MAINNET:
        if not cfg.API_KEY:
            errors.append("BYBIT_API_KEY env var not set")
        if not cfg.API_SECRET:
            errors.append("BYBIT_API_SECRET env var not set")

    # 3. obr/ module imports
    try:
        from obr.bot import OBRBot
        from obr.strategy_nts import scan_for_signal
        from obr.exchange import create_exchange
        from obr.state import BotState
        from obr.guardian import Guardian
        from obr.tracker import OBRTracker
    except ImportError as e:
        errors.append(f"Module import failed: {e}")

    return errors


def show_banner():
    from obr import config as cfg
    mode = "MAINNET" if cfg.MAINNET else ("DEMO" if cfg.DEMO_MODE else "TESTNET")
    risk_label = f"${cfg.FIXED_RISK_USD:.0f}/trade" if cfg.FIXED_RISK_USD > 0 else f"{cfg.RISK_PCT*100:.0f}%"
    tp_vals = sorted(set(cfg.PAIR_TP.values()))
    tp_label = f"{min(tp_vals)}-{max(tp_vals)}R" if tp_vals else f"{cfg.TP_R}R"

    R = "\033[0m"; B = "\033[1m"; D = "\033[2m";
    CY = "\033[96m"; GR = "\033[92m"; YL = "\033[93m";
    MG = "\033[95m"; WH = "\033[97m"

    w = 55
    print()
    print(f"{MG}{'\u2550' * w}{R}")
    print(f"{MG}\u2551{R}  \U0001f680 {B}{WH}OBR  --  Outside Bar Reversal{R}{' ' * 15}{MG}\u2551{R}")
    print(f"{MG}{'\u2550' * w}{R}")
    print(f"  \U0001f4ca {D}Mode:{R}     {B}{GR}{mode}{R}")
    print(f"  \U0001f3af {D}TP:{R}       {CY}{tp_label}{R} {D}(per-pair){R}")
    print(f"  \U0001f4b0 {D}Risk:{R}     {GR}{risk_label}{R}  {D}Leverage:{R} {YL}{cfg.LEVERAGE}x{R}")
    print(f"  \U0001f50d {D}Pairs:{R}    {WH}{len(cfg.PAIRS)}{R}  {D}Max concurrent:{R} {WH}{cfg.MAX_CONCURRENT_POSITIONS}{R}")
    print(f"  \U0001f5f3\ufe0f  {D}Confirm:{R}  {CY}Nextbar={cfg.REQUIRE_NEXTBAR_CONFIRM}{R}")
    print(f"  \U0001f30a {D}Mode:{R}     {B}{GR}24/7 continuous{R}  {D}Cap:{R} {YL}{cfg.DAILY_GROWTH_CAP_PCT}%/day{R}")
    print(f"  \U0001f3c6 {D}Target:{R}   {WH}${cfg.START_EQUITY}{R} {D}\u2192{R} {B}{GR}${cfg.TARGET_EQUITY}{R} {D}in {cfg.TARGET_DAYS}d{R}")
    print(f"{MG}{'\u2500' * w}{R}")
    print()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OBR Live Trading Bot")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("--bare", action="store_true",
                        help="Run bot directly without supervisor (debug mode)")
    parser.add_argument("--status", "-s", action="store_true",
                        help="Show bot status and exit")
    parser.add_argument("--backtest", "-b", action="store_true",
                        help="Run honest backtest instead of live")
    parser.add_argument("--tracker", "-t", action="store_true",
                        help="Show growth tracker dashboard")
    parser.add_argument("--errors", "-e", nargs="?", const=24, type=int,
                        metavar="HOURS",
                        help="Show error/incident digest (default: 24h)")
    # Hidden flag: set by supervisor when it spawns the bot subprocess
    parser.add_argument("--_supervised", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    # Backtest mode
    if args.backtest:
        from obr.backtest import main as bt_main
        bt_main()
        return

    # Status mode
    if args.status:
        from obr.state import BotState
        state = BotState()
        lt = state.lifetime_summary()
        ds = state.daily_summary()
        print(f"\n  OBR Bot Status")
        print(f"  Equity: ${lt['equity']:.2f} (peak: ${lt['peak']:.2f}, DD: {lt['dd']:.1f}%)")
        print(f"  Lifetime: {lt['total_trades']} trades, WR={lt['wr']:.1f}%, R={lt['total_r']:+.2f}")
        print(f"  Today: {ds['entries']} entries, W:{ds['wins']} L:{ds['losses']}, R={ds['pnl_r']:+.2f}")
        print(f"  Open: {ds['pending']} positions")
        return

    # Tracker mode
    if args.tracker:
        from obr.tracker import OBRTracker
        from obr.state import BotState
        tracker = OBRTracker()
        state = BotState()
        print(tracker.get_dashboard(state.equity))
        print(tracker.recent_sessions(10))
        return

    # Error digest mode
    if args.errors is not None:
        from obr.supervisor import show_error_digest
        show_error_digest(hours=args.errors)
        return

    # ── Live mode ─────────────────────────────────────────────
    # If not --bare and not already inside a supervisor subprocess,
    # delegate to the supervisor which wraps the bot with all agents.

    if not args.bare and not args._supervised:
        # Confirmation prompt (before handing off to supervisor)
        if not args.yes:
            from obr import config as cfg
            mode = "\033[91mMAINNET (REAL MONEY)\033[0m" if cfg.MAINNET else "demo"
            ans = input(f"\n  Start OBR bot (supervised) on {mode}? [y/N] ").strip().lower()
            if ans != "y":
                print("  \u274c Aborted.")
                return

        # Launch through supervisor (all skill agents turn on automatically)
        from obr.supervisor import OBRSupervisor
        supervisor = OBRSupervisor()
        supervisor.run()
        return

    # ── Bare / supervised-subprocess mode ─────────────────────
    show_banner()

    errors = preflight()
    if errors:
        print("  PREFLIGHT FAILED:")
        for e in errors:
            print(f"    - {e}")
        sys.exit(1)

    print("  \u2705 Preflight: OK")

    if not args.yes and not args._supervised:
        from obr import config as cfg
        mode = "\033[91mMAINNET (REAL MONEY)\033[0m" if cfg.MAINNET else "demo"
        ans = input(f"\n  Start OBR bot on {mode}? [y/N] ").strip().lower()
        if ans != "y":
            print("  \u274c Aborted.")
            return

    print("\n  \U0001f680 Starting OBR bot...\n")

    from obr.bot import OBRBot
    bot = OBRBot(auto_start=True)


if __name__ == "__main__":
    main()
