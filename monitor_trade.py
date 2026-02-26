"""
monitor_trade.py — Standalone Profit Guardian for a single manual trade.

Polls Bybit every 2s, moves SL through the same progressive tiers
the main bot uses. Stops when the position closes.
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt
from live.config import (
    API_KEY, API_SECRET, MAINNET, DEMO_MODE,
    PROFIT_TIERS, LEVERAGE,
)
from live import exchange as exch

POLL_SECS = 2

def main():
    # ── Connect ──
    ex = ccxt.bybit({
        "apiKey": API_KEY, "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "adjustForTimeDifference": True, "recvWindow": 20000},
    })
    if not MAINNET:
        if DEMO_MODE:
            ex.enable_demo_trading(True)
        else:
            ex.set_sandbox_mode(True)
    ex.load_markets()
    print("Connected to Bybit. Scanning open positions...\n")

    # ── Find open positions ──
    positions = ex.fetch_positions(params={"category": "linear"})
    open_pos = [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]

    if not open_pos:
        print("No open positions found.")
        return

    for p in open_pos:
        sym = p["symbol"]
        side = p["side"]
        entry = float(p.get("entryPrice", 0) or 0)
        qty = abs(float(p.get("contracts", 0) or 0))
        sl = float(p.get("stopLossPrice", 0) or 0)
        tp = float(p.get("takeProfitPrice", 0) or 0)
        pnl = float(p.get("unrealizedPnl", 0) or 0)
        print(f"  {sym} {side.upper()} @ {entry}")
        print(f"    Qty: {qty} | SL: {sl} | TP: {tp} | uPnL: ${pnl:.4f}")

    # Monitor the first one (or user can specify)
    pos = open_pos[0]
    symbol = pos["symbol"]
    direction = pos["side"]  # "long" or "short"
    entry_price = float(pos.get("entryPrice", 0))
    original_sl = float(pos.get("stopLossPrice", 0) or 0)
    tp_price = float(pos.get("takeProfitPrice", 0) or 0)

    # Compute risk_per_unit from entry → SL
    risk_per_unit = abs(entry_price - original_sl)
    if risk_per_unit <= 0:
        print("Cannot determine risk per unit from SL. Exiting.")
        return

    print(f"\n{'='*60}")
    print(f"  MONITORING: {symbol} {direction.upper()}")
    print(f"  Entry: {entry_price} | SL: {original_sl} | TP: {tp_price}")
    print(f"  Risk/unit: {risk_per_unit:.6f}")
    print(f"  Progressive tiers: {len(PROFIT_TIERS)}")
    for trigger_r, sl_r, label in PROFIT_TIERS:
        print(f"    {label}: trigger at +{trigger_r}R → SL at {sl_r:+.2f}R")
    print(f"{'='*60}\n")

    current_tier = -1
    current_sl = original_sl
    peak_r = -999

    while True:
        try:
            # Check if position still exists
            positions = ex.fetch_positions([symbol], params={"category": "linear"})
            open_pos = [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]

            if not open_pos:
                # Position closed!
                ticker = ex.fetch_ticker(symbol)
                last_price = float(ticker.get("last", 0))
                final_r = ((last_price - entry_price) / risk_per_unit if direction == "long"
                           else (entry_price - last_price) / risk_per_unit)
                result = "WIN" if final_r > 0 else "LOSS"
                print(f"\n  {'✅' if result == 'WIN' else '❌'} POSITION CLOSED — {result}")
                print(f"  Peak R reached: {peak_r:.3f}R")
                print(f"  Final tier: T{current_tier + 1}")
                break

            pos = open_pos[0]
            pnl = float(pos.get("unrealizedPnl", 0) or 0)
            current_price = float(ex.fetch_ticker(symbol).get("last", 0))

            if direction == "long":
                current_r = (current_price - entry_price) / risk_per_unit
            else:
                current_r = (entry_price - current_price) / risk_per_unit

            if current_r > peak_r:
                peak_r = current_r

            # ── Check progressive tiers ──
            new_sl = current_sl
            reason = None
            for i, (trigger_r, sl_r, label) in enumerate(PROFIT_TIERS):
                if i <= current_tier:
                    continue
                if current_r >= trigger_r:
                    current_tier = i
                    if direction == "long":
                        tier_sl = entry_price + (sl_r * risk_per_unit)
                    else:
                        tier_sl = entry_price - (sl_r * risk_per_unit)

                    if direction == "long" and tier_sl > new_sl:
                        new_sl = tier_sl
                        reason = label
                    elif direction == "short" and tier_sl < new_sl:
                        new_sl = tier_sl
                        reason = label

            # Move SL on exchange if improved
            if reason and new_sl != current_sl:
                moved = False
                if direction == "long" and new_sl > current_sl:
                    moved = True
                elif direction == "short" and new_sl < current_sl:
                    moved = True

                if moved:
                    try:
                        ex.set_trading_stop(symbol, params={
                            "category": "linear",
                            "symbol": symbol.replace("/", "").replace(":USDT", ""),
                            "positionIdx": 0,
                            "stopLoss": str(new_sl),
                        })
                        current_sl = new_sl
                        print(f"  ★ SL MOVED → {new_sl:.6f} | {reason} | R={current_r:+.3f} peak={peak_r:.3f}R")
                    except Exception as e:
                        if "not modified" not in str(e).lower():
                            print(f"  ⚠ SL move failed: {e}")

            # Status line
            tag = "↗" if current_r > 0 else "↘"
            tier_label = f"T{current_tier + 1}" if current_tier >= 0 else "T0"
            print(f"  {tag} R={current_r:+.3f} | peak={peak_r:.3f}R | {tier_label} | "
                  f"SL={current_sl:.6f} | uPnL=${pnl:.4f}", end="\r")

        except KeyboardInterrupt:
            print("\n\n  Monitor stopped (Ctrl+C). Position still open on exchange with current SL.")
            break
        except Exception as e:
            print(f"  Error: {e}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
