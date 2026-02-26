"""
quick_live_test.py — Find an FCB setup NOW and place a $2 risk test trade on Bybit.

Scans London session pairs for any active 5m FCB breakout.
Places a single market order with SL at midpoint, TP at 1.5R.
Risk: $2 fixed (overrides config 2%).
"""

import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live import exchange as exch
from live.config import (
    PAIRS, LEVERAGE, TP_R, MIN_RANGE_PCT, FEE_RATE,
    MICRO_FILTER_ENABLED, MIN_C2_BODY_RATIO, FC_COUNTER_5M,
)
from live.strategy import capture_first_candle, check_breakout

# ── Config ──
RISK_USD = 2.00          # Fixed $2 risk
SESSION = None           # Auto-detect

def get_session():
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 8:
        return "asia"
    elif 8 <= h < 16:
        return "london"
    else:
        return "ny"

def main():
    session = SESSION or get_session()
    pairs = [p for p, _ in PAIRS.get(session, [])]
    
    print(f"\n{'='*60}")
    print(f"  FCB QUICK SCAN — {session.upper()} session")
    print(f"  Risk: ${RISK_USD:.2f} | TP: {TP_R}R | Leverage: {LEVERAGE}x")
    print(f"  Scanning {len(pairs)} pairs...")
    print(f"{'='*60}\n")

    # Connect
    ex = exch.create_exchange()
    equity = exch.get_equity(ex)
    print(f"  Equity: ${equity:.2f}\n")

    # Get market info for all pairs
    market_info = {}
    for pair in pairs:
        try:
            mkt = ex.market(pair)
            market_info[pair] = {
                "contract_size": float(mkt.get("contractSize", 1) or 1),
                "price_precision": mkt.get("precision", {}).get("price", 4),
                "amount_precision": mkt.get("precision", {}).get("amount", 2),
                "min_qty": float(mkt.get("limits", {}).get("amount", {}).get("min", 0.001) or 0.001),
                "min_notional": float(mkt.get("limits", {}).get("cost", {}).get("min", 5) or 5),
            }
        except Exception as e:
            print(f"  {pair}: market not found — {e}")

    # Scan all pairs for FCB setups
    setups = []
    
    for pair in pairs:
        if pair not in market_info:
            continue
        try:
            # Get last 4 closed 5m candles
            candles = exch.fetch_latest_candles(ex, pair, n=4)
            if len(candles) < 2:
                continue

            # Candle 1 = second-to-last, Candle 2 = last closed
            c1 = candles[-2]
            c2 = candles[-1]

            fc = capture_first_candle(pair, session, c1)
            if not fc.valid:
                continue

            c2_close = c2["close"]
            direction = check_breakout(fc, c2_close)
            if direction is None:
                continue

            # Compute metrics
            info = market_info[pair]
            risk_per_unit = abs(c2_close - fc.midpoint)
            if risk_per_unit <= 0:
                continue

            slip_r = ((c2_close - fc.high) / risk_per_unit if direction == "long"
                      else (fc.low - c2_close) / risk_per_unit)

            c2_body = abs(c2["close"] - c2["open"])
            c2_range = c2["high"] - c2["low"]
            c2_body_ratio = c2_body / c2_range if c2_range > 0 else 0

            fc_body_dir = "long" if fc.close > fc.open else "short"
            fc_is_counter = (fc_body_dir != direction)

            # Micro-filter check
            filtered = False
            filter_reason = ""
            if MICRO_FILTER_ENABLED:
                if c2_body_ratio < MIN_C2_BODY_RATIO:
                    filtered = True
                    filter_reason = f"weak C2 body {c2_body_ratio:.0%}"
                elif FC_COUNTER_5M and not fc_is_counter:
                    filtered = True
                    filter_reason = f"FC leaned {fc_body_dir} (need counter)"

            # Fee R
            fee_r = 2.0 * FEE_RATE * c2_close / risk_per_unit

            # TP price
            if direction == "long":
                tp = c2_close + TP_R * risk_per_unit
            else:
                tp = c2_close - TP_R * risk_per_unit

            # Position size for $2 risk
            qty_base = RISK_USD / risk_per_unit
            qty = qty_base / info["contract_size"]
            notional = qty * info["contract_size"] * c2_close

            status = "✋ FILTERED" if filtered else "✅ VALID"
            print(f"  {status} {pair}")
            print(f"    Dir: {direction.upper()} | Entry: {c2_close:.6f} | SL: {fc.midpoint:.6f} | TP: {tp:.6f}")
            print(f"    Range: {fc.range_pct*100:.3f}% | Slip: {slip_r:.3f}R | Body: {c2_body_ratio:.0%} | Counter: {'Y' if fc_is_counter else 'N'}")
            print(f"    Fee: {fee_r:.3f}R | Qty: {qty:.4f} | Notional: ${notional:.2f}")
            if filtered:
                print(f"    Reason: {filter_reason}")
            print()

            if not filtered:
                setups.append({
                    "pair": pair,
                    "direction": direction,
                    "entry": c2_close,
                    "sl": fc.midpoint,
                    "tp": tp,
                    "risk_per_unit": risk_per_unit,
                    "slip_r": slip_r,
                    "c2_body_ratio": c2_body_ratio,
                    "fc_counter": fc_is_counter,
                    "fee_r": fee_r,
                    "qty": qty,
                    "notional": notional,
                    "info": info,
                })

        except Exception as e:
            print(f"  {pair}: error — {e}")

    print(f"\n{'='*60}")
    if not setups:
        print("  No valid FCB setups found right now.")
        print("  This is normal — FCBs only form at session boundaries.")
        print(f"  Current UTC: {datetime.now(timezone.utc).strftime('%H:%M')}")
        print(f"  London opened at 08:00 UTC. Next session: NY at 16:00 UTC.")
        
        # Show if any breakouts exist (even filtered)
        print(f"\n  Tip: The bot catches setups at the exact session open (08:00/16:00/00:00 UTC).")
        print(f"  Running mid-session means candles have moved past the first candle window.")
        print(f"{'='*60}")
        
        # Offer to scan current candle pair instead
        print(f"\n  Want to force a trade anyway? Looking for best current setup...")
        print(f"  (Using last 2 candles as proxy for FC + breakout)\n")
        
        # Find best non-filtered setup
        best = None
        for pair in pairs:
            if pair not in market_info:
                continue
            try:
                candles = exch.fetch_latest_candles(ex, pair, n=4)
                if len(candles) < 2:
                    continue
                c1 = candles[-2]
                c2 = candles[-1]
                fc = capture_first_candle(pair, session, c1)
                if not fc.valid:
                    continue
                c2_close = c2["close"]
                direction = check_breakout(fc, c2_close)
                if direction is None:
                    continue
                risk_per_unit = abs(c2_close - fc.midpoint)
                if risk_per_unit <= 0:
                    continue
                slip_r = ((c2_close - fc.high) / risk_per_unit if direction == "long"
                          else (fc.low - c2_close) / risk_per_unit)
                c2_body = abs(c2["close"] - c2["open"])
                c2_range = c2["high"] - c2["low"]
                c2_body_ratio = c2_body / c2_range if c2_range > 0 else 0
                fee_r = 2.0 * FEE_RATE * c2_close / risk_per_unit
                
                if direction == "long":
                    tp = c2_close + TP_R * risk_per_unit
                else:
                    tp = c2_close - TP_R * risk_per_unit
                
                info = market_info[pair]
                qty_base = RISK_USD / risk_per_unit
                qty = qty_base / info["contract_size"]
                notional = qty * info["contract_size"] * c2_close

                score = c2_body_ratio - slip_r * 0.5 - fee_r * 0.3
                if best is None or score > best["score"]:
                    best = {
                        "pair": pair, "direction": direction,
                        "entry": c2_close, "sl": fc.midpoint, "tp": tp,
                        "risk_per_unit": risk_per_unit, "slip_r": slip_r,
                        "c2_body_ratio": c2_body_ratio, "fee_r": fee_r,
                        "qty": qty, "notional": notional, "info": info,
                        "score": score,
                    }
            except:
                continue
        
        if best:
            setups = [best]
            print(f"  Found candidate (micro-filters bypassed for test):")
            print(f"    {best['pair']} {best['direction'].upper()}")
            print(f"    Entry: {best['entry']:.6f} | SL: {best['sl']:.6f} | TP: {best['tp']:.6f}")
            print(f"    Slip: {best['slip_r']:.3f}R | Body: {best['c2_body_ratio']:.0%} | Notional: ${best['notional']:.2f}")
            print()
        else:
            print("  No breakouts at all in the last 2 candles. Try again at session open.")
            return

    # Pick the best setup (lowest slip + highest body ratio)
    best = min(setups, key=lambda s: s["slip_r"] - s["c2_body_ratio"])
    
    print(f"  SELECTED: {best['pair']} {best['direction'].upper()}")
    print(f"    Entry:  {best['entry']:.6f}")
    print(f"    SL:     {best['sl']:.6f}  (midpoint)")
    print(f"    TP:     {best['tp']:.6f}  ({TP_R}R)")
    print(f"    Risk:   ${RISK_USD:.2f}")
    print(f"    Qty:    {best['qty']:.4f}")
    print(f"    Notional: ${best['notional']:.2f}")
    print(f"{'='*60}")
    
    # Confirm
    confirm = input("\n  Place this trade? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("  Cancelled.")
        return

    # Execute
    pair = best["pair"]
    info = best["info"]
    
    # Set leverage + margin mode
    print(f"\n  Setting leverage {LEVERAGE}x & isolated margin...")
    exch.set_leverage(ex, pair, LEVERAGE)
    exch.set_margin_mode(ex, pair, "isolated")

    # Round prices
    pp = info["price_precision"]
    if isinstance(pp, int) and pp > 1:
        # pp is number of decimals
        sl_price = round(best['sl'], pp)
        tp_price = round(best['tp'], pp)
    else:
        sl_price = best['sl']
        tp_price = best['tp']

    # Round quantity  
    ap = info["amount_precision"]
    if isinstance(ap, int) and ap > 1:
        qty = round(best['qty'], ap)
    else:
        qty = best['qty']

    # Check minimums
    min_qty = info.get("min_qty", 0.001)
    if qty < min_qty:
        print(f"  Qty {qty} < min {min_qty} — adjusting up")
        qty = min_qty

    side = "buy" if best["direction"] == "long" else "sell"
    
    print(f"\n  Placing {side.upper()} {qty} {pair}...")
    print(f"    SL: {sl_price} | TP: {tp_price}")
    
    order = exch.place_market_order(ex, pair, side, qty, sl_price, tp_price)
    
    print(f"\n  ✅ ORDER PLACED!")
    print(f"    Order ID: {order.get('id', 'N/A')}")
    print(f"    Status:   {order.get('status', 'N/A')}")
    print(f"    Avg Fill: {order.get('average', 'N/A')}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
