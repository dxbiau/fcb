"""
v13pro/run.py -- CLI entry point for v13pro bot.

Usage:
    python -m v13pro.run                  # live trading
    python -m v13pro.run --dry-run        # scan only, no orders
    python -m v13pro.run --once           # single scan then exit
    python -m v13pro.run --maker          # enable maker TP
    python -m v13pro.run --entry          # enable maker entry
    python -m v13pro.run --supervised     # run under supervisor
"""

import argparse
import asyncio
import sys
import os

# Ensure the workspace root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(
        description="v13pro FCB Bot — 24/7 async trading engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan for signals but don't place orders")
    parser.add_argument("--once", action="store_true",
                        help="Single scan across all TFs then exit")
    parser.add_argument("--maker", action="store_true",
                        help="Enable maker TP orders (limit TP)")
    parser.add_argument("--entry", action="store_true",
                        help="Enable maker entry orders (limit entry)")
    parser.add_argument("--supervised", action="store_true",
                        help="Run under supervisor (auto-restart)")
    parser.add_argument("--demo", action="store_true",
                        help="Use demo/testnet mode")
    parser.add_argument("--combos", type=str, default=None,
                        help="Path to custom deploy_combos.json")

    args = parser.parse_args()

    # Apply config overrides BEFORE importing bot
    from v13pro import config as cfg

    if args.maker:
        cfg.MAKER_TP_ENABLED = True
    if args.entry:
        cfg.MAKER_ENTRY_ENABLED = True
    if args.demo:
        cfg.MAINNET = False
        cfg.DEMO_MODE = True
    if args.combos:
        cfg.DEPLOY_COMBOS = args.combos

    if args.supervised:
        # Run under supervisor (blocking process manager)
        from v13pro.supervisor import Supervisor
        bot_args = []
        if args.maker:
            bot_args.append("--maker")
        if args.entry:
            bot_args.append("--entry")
        if args.dry_run:
            bot_args.append("--dry-run")
        if args.demo:
            bot_args.append("--demo")
        if args.combos:
            bot_args.extend(["--combos", args.combos])

        sv = Supervisor(bot_args=bot_args)
        sv.run()
    else:
        # Run bot directly
        from v13pro.bot import FCBBot

        bot = FCBBot(dry_run=args.dry_run, once=args.once)

        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            print("\nInterrupted")
            sys.exit(0)


if __name__ == "__main__":
    main()
