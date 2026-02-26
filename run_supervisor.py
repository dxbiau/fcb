"""
run_supervisor.py -- Launch OBR bot under the Skill Agent supervisor.

The supervisor wraps the bot process and provides:
  - Auto-restart on crash (only when safe / no open positions)
  - Real-time error scanning from log files
  - Heartbeat monitoring (detects frozen bot)
  - Exponential backoff to prevent restart storms
  - Structured incident log (obr/logs/incidents.jsonl)

Usage:
  .venv\\Scripts\\python.exe run_supervisor.py              # Start supervised
  .venv\\Scripts\\python.exe run_supervisor.py --errors      # Show error digest
  .venv\\Scripts\\python.exe run_supervisor.py --status      # Show status
  .venv\\Scripts\\python.exe run_supervisor.py --errors 48   # Last 48h errors
"""

import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OBR Skill Agent Supervisor")
    parser.add_argument("--errors", "-e", nargs="?", const=24, type=int,
                        metavar="HOURS",
                        help="Show error digest (default: last 24h)")
    parser.add_argument("--status", "-s", action="store_true",
                        help="Show supervisor + bot status")
    args = parser.parse_args()

    from obr.supervisor import OBRSupervisor, show_error_digest, show_supervisor_status

    if args.errors is not None:
        show_error_digest(hours=args.errors)
        return

    if args.status:
        show_supervisor_status()
        return

    # Preflight
    errors = []
    try:
        import ccxt
    except ImportError:
        errors.append("ccxt not installed")

    from obr import config as cfg
    if cfg.MAINNET:
        if not cfg.API_KEY:
            errors.append("BYBIT_API_KEY not set")
        if not cfg.API_SECRET:
            errors.append("BYBIT_API_SECRET not set")

    if errors:
        print("\n  PREFLIGHT FAILED:")
        for e in errors:
            print(f"    - {e}")
        sys.exit(1)

    # Launch supervisor
    supervisor = OBRSupervisor()
    supervisor.run()


if __name__ == "__main__":
    main()
