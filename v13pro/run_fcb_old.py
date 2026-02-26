"""
run_fcb.py -- Launch the FCB v13 multi-strategy portfolio bot.

Usage:
    python run_fcb.py              # Run with defaults
    python run_fcb.py --maker      # Enable full maker fees (limit TP)
    python run_fcb.py --dry-run    # Dry run (print signals, no orders)
"""

import sys
import os
import argparse

# Ensure obr package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="FCB v13 Portfolio Bot")
    parser.add_argument("--maker", action="store_true",
                        help="Enable full maker fee model (limit TP orders)")
    parser.add_argument("--maker-entry", action="store_true",
                        help="Enable maker (limit) entries as well")
    parser.add_argument("--combos", type=str, default=None,
                        help="Path to combo JSON file (default: _v13_deploy_combos.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals but don't place orders")
    args = parser.parse_args()

    # Apply maker settings before importing bot (config is imported at module level)
    from obr import config as cfg
    if args.maker:
        cfg.MAKER_TP_ENABLED = True
        cfg.EFFECTIVE_FEE_MODEL = "full_maker"
        print("💸 Maker TP enabled (limit take-profit orders)")
    if args.maker_entry:
        cfg.MAKER_ENTRY_ENABLED = True
        print("💸 Maker entry enabled (limit entry orders)")

    from obr.fcb_bot import FCBBot
    bot = FCBBot(combo_file=args.combos, auto_start=True)


if __name__ == "__main__":
    main()
