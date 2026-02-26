"""
obr_a_grade_scan.py -- Find A-grade OBR setups on Bybit RIGHT NOW.

Scans ALL liquid Bybit USDT perps for:
  1. Active OBR signal (outside bar just closed)
  2. Pending nextbar confirmation  
  3. Proximity to OBR (3/4 or 4/4 conditions building)

Then backtests the top candidates over 30 days to validate edge.

Trade params: $10 risk, 20x leverage, structural SL.
"""

import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone

# --- Minimal exchange setup (avoid importing full obr config) ---
import ccxt

def connect():
    ex = ccxt.bybit({
        "apiKey": os.environ.get("BYBIT_API_KEY", ""),
        "secret": os.environ.get("BYBIT_API_SECRET", ""),
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    return ex

def fetch_candles(ex, symbol, tf="5m", limit=100):
    """Fetch OHLCV and return list of dicts."""
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    candles = []
    for r in raw:
        candles.append({
            "ts": r[0], "open": r[1], "high": r[2],
            "low": r[3], "close": r[4], "volume": r[5],
        })
    return candles

# --- OBR signal detection (matches notebook logic exactly) ---

def detect_obr(prev, curr):
    """
    Detect Outside Bar Reversal.
    Returns: 2=long signal, 1=short signal, 0=no signal
    
    Long (signal=2): bearish OB engulfs prev, closes below prev low -> fade to LONG
    Short (signal=1): bullish OB engulfs prev, closes above prev high -> fade to SHORT
    """
    if prev["high"] == prev["low"] or curr["high"] == curr["low"]:
        return 0
    
    # Long signal: bearish candle engulfs and closes below
    c0 = curr["open"] > curr["close"]          # bearish
    c1 = curr["high"] > prev["high"]            # engulfs high
    c2 = curr["low"] < prev["low"]              # engulfs low
    c3 = curr["close"] < prev["low"]            # closes below prev low
    if c0 and c1 and c2 and c3:
        return 2
    
    # Short signal: bullish candle engulfs and closes above
    c0 = curr["open"] < curr["close"]           # bullish
    c1 = curr["high"] > prev["high"]            # engulfs high  
    c2 = curr["low"] < prev["low"]              # engulfs low
    c3 = curr["close"] > prev["high"]           # closes above prev high
    if c0 and c1 and c2 and c3:
        return 1
    
    return 0

def nextbar_confirms(signal, confirm_candle):
    """Check if the confirmation candle agrees with the reversal."""
    if signal == 2:  # long -- confirm candle should close bullish
        return confirm_candle["close"] > confirm_candle["open"]
    elif signal == 1:  # short -- confirm candle should close bearish
        return confirm_candle["close"] < confirm_candle["open"]
    return False

def proximity_check(prev, curr):
    """
    How close is curr to being an OBR vs prev?
    Returns (long_score, short_score, long_detail, short_detail)
    Each score is 0-4 (how many conditions met).
    """
    if prev["high"] == prev["low"]:
        return 0, 0, {}, {}
    
    pr = prev["high"] - prev["low"]  # prev range
    
    # Long conditions
    l_conds = {}
    l_conds["bearish"] = curr["open"] > curr["close"]
    l_conds["high_engulf"] = curr["high"] > prev["high"]
    l_conds["low_engulf"] = curr["low"] < prev["low"]
    l_conds["close_below"] = curr["close"] < prev["low"]
    l_score = sum(l_conds.values())
    
    # How close to meeting unmet long conditions
    l_dist = {}
    if not l_conds["bearish"]:
        l_dist["need_bearish"] = (curr["close"] - curr["open"]) / pr * 100
    if not l_conds["high_engulf"]:
        l_dist["high_gap_%"] = (prev["high"] - curr["high"]) / pr * 100
    if not l_conds["low_engulf"]:
        l_dist["low_gap_%"] = (curr["low"] - prev["low"]) / pr * 100
    if not l_conds["close_below"]:
        l_dist["close_gap_%"] = (curr["close"] - prev["low"]) / pr * 100
    
    # Short conditions
    s_conds = {}
    s_conds["bullish"] = curr["open"] < curr["close"]
    s_conds["high_engulf"] = curr["high"] > prev["high"]
    s_conds["low_engulf"] = curr["low"] < prev["low"]
    s_conds["close_above"] = curr["close"] > prev["high"]
    s_score = sum(s_conds.values())
    
    s_dist = {}
    if not s_conds["bullish"]:
        s_dist["need_bullish"] = (curr["open"] - curr["close"]) / pr * 100
    if not s_conds["high_engulf"]:
        s_dist["high_gap_%"] = (prev["high"] - curr["high"]) / pr * 100
    if not s_conds["low_engulf"]:
        s_dist["low_gap_%"] = (curr["low"] - prev["low"]) / pr * 100
    if not s_conds["close_above"]:
        s_dist["close_gap_%"] = (prev["high"] - curr["close"]) / pr * 100
    
    return l_score, s_score, l_dist, s_dist


def backtest_pair(candles, tp_r=2.0, fee_r=0.04):
    """
    Backtest OBR-NEXTBAR on candle history.
    Returns list of trade dicts.
    """
    trades = []
    i = 0
    while i < len(candles) - 2:
        prev = candles[i]
        curr = candles[i + 1]
        sig = detect_obr(prev, curr)
        
        if sig == 0:
            i += 1
            continue
        
        # Check nextbar confirmation
        confirm = candles[i + 2]
        if not nextbar_confirms(sig, confirm):
            i += 1
            continue
        
        # Entry at candle after confirm (i+3)
        if i + 3 >= len(candles):
            break
        
        entry_candle = candles[i + 3]
        ep = entry_candle["open"]
        
        if sig == 2:  # long
            sl = curr["low"]
            rpu = ep - sl
            if rpu <= 0:
                i += 1
                continue
            tp = ep + tp_r * rpu
            
            # Walk forward
            result = None
            for j in range(i + 3, len(candles)):
                c = candles[j]
                if c["low"] <= sl:
                    result = -1.0 - fee_r
                    break
                if c["high"] >= tp:
                    result = tp_r - fee_r
                    break
            if result is None:
                # Still open at end
                last = candles[-1]["close"]
                result = (last - ep) / rpu - fee_r
            
            trades.append({
                "idx": i, "signal": "LONG", "entry": ep, "sl": sl, "tp": tp,
                "rpu": rpu, "result_r": round(result, 3),
                "ts": entry_candle["ts"],
            })
        
        elif sig == 1:  # short
            sl = curr["high"]
            rpu = sl - ep
            if rpu <= 0:
                i += 1
                continue
            tp = ep - tp_r * rpu
            
            result = None
            for j in range(i + 3, len(candles)):
                c = candles[j]
                if c["high"] >= sl:
                    result = -1.0 - fee_r
                    break
                if c["low"] <= tp:
                    result = tp_r - fee_r
                    break
            if result is None:
                last = candles[-1]["close"]
                result = (ep - last) / rpu - fee_r
            
            trades.append({
                "idx": i, "signal": "SHORT", "entry": ep, "sl": sl, "tp": tp,
                "rpu": rpu, "result_r": round(result, 3),
                "ts": entry_candle["ts"],
            })
        
        i += 4  # skip past this trade setup
    
    return trades


def main():
    now = datetime.now(timezone.utc)
    print("=" * 70)
    print("  OBR A-GRADE SETUP SCANNER -- Bybit Mainnet")
    print(f"  {now.strftime('%Y-%m-%d %H:%M UTC')} | Trade: $10 risk @ 20x lev")
    print("=" * 70)
    
    ex = connect()
    
    # Get all USDT linear perpetuals with decent volume
    usdt_perps = []
    for sym, mkt in ex.markets.items():
        if (mkt.get("swap") and mkt.get("linear") and 
            mkt.get("settle") == "USDT" and mkt.get("active")):
            usdt_perps.append(sym)
    
    print(f"  {len(usdt_perps)} USDT perps found. Fetching tickers for volume filter...")
    
    # Fetch tickers to filter by volume
    tickers = ex.fetch_tickers(symbols=usdt_perps[:500])
    
    # Filter: 24h volume > $5M (need liquidity for $10 trades)
    liquid = []
    for sym, t in tickers.items():
        vol = float(t.get("quoteVolume") or 0)
        if vol > 5_000_000:
            liquid.append((sym, vol))
    
    liquid.sort(key=lambda x: -x[1])
    print(f"  {len(liquid)} pairs with >$5M 24h volume\n")
    
    # Scan each pair
    scan_results = []
    
    for idx, (sym, vol24h) in enumerate(liquid):
        try:
            candles = fetch_candles(ex, sym, "5m", 100)
            if len(candles) < 10:
                continue
            
            # --- Check last 3 closed candle pairs for signals ---
            # candles[-1] might be the currently forming candle
            # We check closed candles: [-4,-3], [-3,-2], [-2,-1]
            
            active_signal = None
            pending_confirm = None
            best_proximity = 0
            best_prox_dir = "none"
            best_prox_detail = {}
            
            # Check if candle[-2] vs [-3] gave a signal (would need [-1] as confirm)
            if len(candles) >= 4:
                sig = detect_obr(candles[-3], candles[-2])
                if sig != 0:
                    confirmed = nextbar_confirms(sig, candles[-1])
                    dir_str = "LONG" if sig == 2 else "SHORT"
                    if confirmed:
                        # ACTIVE! Signal on [-2], confirmed by [-1], entry at NEXT candle open
                        ob_candle = candles[-2]
                        if sig == 2:
                            sl = ob_candle["low"]
                            entry_est = candles[-1]["close"]  # approximate next open
                            rpu = entry_est - sl
                            if rpu > 0:
                                tp = entry_est + 2.0 * rpu
                                risk_pct = rpu / entry_est * 100
                                active_signal = {
                                    "type": dir_str, "entry_est": entry_est,
                                    "sl": sl, "tp": tp, "rpu": rpu,
                                    "risk_distance_%": risk_pct,
                                    "ob_range_%": (ob_candle["high"] - ob_candle["low"]) / ob_candle["close"] * 100,
                                }
                        else:
                            sl = ob_candle["high"]
                            entry_est = candles[-1]["close"]
                            rpu = sl - entry_est
                            if rpu > 0:
                                tp = entry_est - 2.0 * rpu
                                risk_pct = rpu / entry_est * 100
                                active_signal = {
                                    "type": dir_str, "entry_est": entry_est,
                                    "sl": sl, "tp": tp, "rpu": rpu,
                                    "risk_distance_%": risk_pct,
                                    "ob_range_%": (ob_candle["high"] - ob_candle["low"]) / ob_candle["close"] * 100,
                                }
                    else:
                        pending_confirm = {"type": dir_str, "status": "UNCONFIRMED"}
            
            # Check if [-1] vs [-2] just formed an OBR (needs NEXT candle to confirm)
            if len(candles) >= 3 and active_signal is None:
                sig = detect_obr(candles[-2], candles[-1])
                if sig != 0:
                    dir_str = "LONG" if sig == 2 else "SHORT"
                    ob_candle = candles[-1]
                    if sig == 2:
                        sl = ob_candle["low"]
                        entry_est = ob_candle["close"]  # rough
                        rpu = entry_est - sl
                        risk_pct = rpu / entry_est * 100 if entry_est > 0 else 0
                    else:
                        sl = ob_candle["high"]
                        entry_est = ob_candle["close"]
                        rpu = sl - entry_est
                        risk_pct = rpu / entry_est * 100 if entry_est > 0 else 0
                    
                    pending_confirm = {
                        "type": dir_str, "status": "WAITING_CONFIRM",
                        "sl": sl, "entry_est": entry_est,
                        "risk_distance_%": risk_pct,
                        "ob_range_%": (ob_candle["high"] - ob_candle["low"]) / ob_candle["close"] * 100,
                    }
            
            # Proximity check on last closed candle
            if len(candles) >= 2:
                l_score, s_score, l_dist, s_dist = proximity_check(candles[-2], candles[-1])
                if l_score >= s_score:
                    best_proximity = l_score
                    best_prox_dir = "long"
                    best_prox_detail = l_dist
                else:
                    best_proximity = s_score
                    best_prox_dir = "short"
                    best_prox_detail = s_dist
            
            # Backtest on recent 100 candles at multiple TP levels
            bt_results = {}
            for tp_r in [1.5, 2.0, 2.5, 3.0]:
                trades = backtest_pair(candles, tp_r, fee_r=0.04)
                if trades:
                    wins = sum(1 for t in trades if t["result_r"] > 0)
                    total_r = sum(t["result_r"] for t in trades)
                    wr = wins / len(trades) * 100
                    bt_results[tp_r] = {
                        "trades": len(trades), "wins": wins, "wr": wr,
                        "total_r": round(total_r, 2),
                    }
            
            # Count recent signals in history
            sig_count = 0
            for k in range(1, len(candles) - 1):
                if detect_obr(candles[k-1], candles[k]) != 0:
                    sig_count += 1
            
            # Grade the setup
            grade = "F"
            grade_score = 0
            
            if active_signal:
                grade_score += 50  # confirmed and ready
            elif pending_confirm and pending_confirm.get("status") == "WAITING_CONFIRM":
                grade_score += 40  # OBR formed, waiting confirm
            elif best_proximity >= 3:
                grade_score += 20
            
            # Bonus for backtest edge
            best_bt = None
            best_bt_tp = 2.0
            for tp_r, bt in bt_results.items():
                if bt["total_r"] > 0 and bt["trades"] >= 3:
                    if best_bt is None or bt["total_r"] > best_bt["total_r"]:
                        best_bt = bt
                        best_bt_tp = tp_r
            
            if best_bt:
                grade_score += min(30, best_bt["total_r"] * 3)
                if best_bt["wr"] >= 55:
                    grade_score += 10
                if best_bt["wr"] >= 65:
                    grade_score += 10
            
            if grade_score >= 60:
                grade = "A"
            elif grade_score >= 40:
                grade = "B"
            elif grade_score >= 25:
                grade = "C"
            elif grade_score >= 10:
                grade = "D"
            
            scan_results.append({
                "pair": sym, "vol24h": vol24h, "grade": grade,
                "grade_score": grade_score,
                "active_signal": active_signal,
                "pending_confirm": pending_confirm,
                "proximity": best_proximity,
                "prox_dir": best_prox_dir,
                "prox_detail": best_prox_detail,
                "bt_results": bt_results,
                "best_bt": best_bt,
                "best_bt_tp": best_bt_tp,
                "signal_count_100": sig_count,
                "last_price": candles[-1]["close"],
                "candles": candles,  # keep for detailed backtest later
            })
            
            if (idx + 1) % 25 == 0:
                print(f"  Scanned {idx+1}/{len(liquid)}...")
            
            time.sleep(0.08)
            
        except Exception as e:
            pass  # skip problematic pairs
    
    # Sort by grade score
    scan_results.sort(key=lambda x: -x["grade_score"])
    
    # Print results
    print("\n" + "=" * 70)
    print("  SCAN COMPLETE -- Sorted by Grade")
    print("=" * 70)
    
    # Show A and B grades
    shown = 0
    for r in scan_results:
        if r["grade"] not in ("A", "B"):
            if shown >= 15:
                break
        
        shown += 1
        pair = r["pair"]
        
        status = ""
        if r["active_signal"]:
            s = r["active_signal"]
            status = (f">>> ACTIVE {s['type']} | Entry~{s['entry_est']:.6g} | "
                     f"SL={s['sl']:.6g} | Risk={s['risk_distance_%']:.2f}%")
        elif r["pending_confirm"] and r["pending_confirm"].get("status") == "WAITING_CONFIRM":
            p = r["pending_confirm"]
            status = (f">> WAITING CONFIRM: {p['type']} | SL~{p.get('sl',0):.6g} | "
                     f"Risk~{p.get('risk_distance_%',0):.2f}%")
        elif r["proximity"] >= 3:
            status = f"> BUILDING {r['prox_dir'].upper()} ({r['proximity']}/4 met)"
        else:
            status = f"  Prox: {r['proximity']}/4 {r['prox_dir']}"
        
        bt_str = ""
        if r["best_bt"]:
            b = r["best_bt"]
            bt_str = (f"BT({r['best_bt_tp']}R): {b['trades']}t, "
                     f"WR={b['wr']:.0f}%, R={b['total_r']:+.1f}")
        else:
            bt_str = "BT: no edge"
        
        print(f"\n  [{r['grade']}] {pair}  (score={r['grade_score']:.0f})"
              f"  Vol=${r['vol24h']/1e6:.0f}M")
        print(f"      {status}")
        print(f"      {bt_str} | Signals in 100 candles: {r['signal_count_100']}")
    
    # --- DETAILED REPORT ON TOP 3 A-GRADE ---
    a_grade = [r for r in scan_results if r["grade"] == "A"]
    top3 = scan_results[:3]  # top 3 regardless
    
    print("\n\n" + "=" * 70)
    print(f"  TOP 3 CANDIDATES FOR LIVE TEST ($10 risk, 20x leverage)")
    print("=" * 70)
    
    for rank, r in enumerate(top3, 1):
        pair = r["pair"]
        price = r["last_price"]
        
        print(f"\n  {'='*60}")
        print(f"  #{rank}  {pair}  [Grade {r['grade']}]")
        print(f"  {'='*60}")
        print(f"  Price: {price:.6g} | 24h Vol: ${r['vol24h']/1e6:.1f}M")
        
        if r["active_signal"]:
            s = r["active_signal"]
            rpu = s["rpu"]
            qty = 10.0 / (rpu * 20) if rpu > 0 else 0  # $10 risk at 20x
            # Actually: risk = qty * rpu, and risk = $10 
            # So qty = $10 / rpu
            # With 20x leverage: margin = qty * price / 20
            qty = 10.0 / rpu if rpu > 0 else 0
            margin = qty * price / 20.0
            
            print(f"  STATUS: **CONFIRMED {s['type']} SIGNAL**")
            print(f"  Entry: ~{s['entry_est']:.6g}")
            print(f"  SL: {s['sl']:.6g} (risk distance: {s['risk_distance_%']:.2f}%)")
            print(f"  TP (2.0R): {s['tp']:.6g}")
            print(f"  Position size: {qty:.4f} ({margin:.2f} USDT margin @ 20x)")
            print(f"  Dollar risk: $10.00 | Dollar reward: $20.00 at 2.0R")
        
        elif r["pending_confirm"]:
            p = r["pending_confirm"]
            print(f"  STATUS: OBR {p['type']} formed -- WAITING for confirm candle")
            if "sl" in p:
                print(f"  SL if confirmed: {p['sl']:.6g}")
                print(f"  Risk distance: {p.get('risk_distance_%', 0):.2f}%")
            print(f"  Action: Watch next 5m candle close for confirmation")
        
        else:
            print(f"  STATUS: Building toward {r['prox_dir'].upper()} "
                  f"({r['proximity']}/4 conditions)")
            if r["prox_detail"]:
                for k, v in r["prox_detail"].items():
                    print(f"    Missing: {k} = {v:.1f}% of prev range")
        
        # Backtest stats
        print(f"\n  Backtest (last ~8 hours, 100x 5m candles):")
        if r["bt_results"]:
            for tp_r, bt in sorted(r["bt_results"].items()):
                tag = " <<<" if tp_r == r["best_bt_tp"] and r["best_bt"] else ""
                print(f"    TP={tp_r}R: {bt['trades']}t, WR={bt['wr']:.0f}%, "
                      f"R={bt['total_r']:+.1f}{tag}")
        else:
            print(f"    No trades in recent candles (signal is fresh)")
        
        print(f"  Signal frequency: {r['signal_count_100']} OBR signals in 100 candles")
    
    # Extended backtest on top 3
    print("\n\n" + "=" * 70)
    print("  EXTENDED BACKTEST -- Top 3 over 500 candles (~1.7 days)")
    print("=" * 70)
    
    for rank, r in enumerate(top3, 1):
        pair = r["pair"]
        try:
            candles_500 = fetch_candles(ex, pair, "5m", 500)
            print(f"\n  #{rank} {pair} ({len(candles_500)} candles):")
            
            for tp_r in [1.5, 2.0, 2.5, 3.0]:
                trades = backtest_pair(candles_500, tp_r, fee_r=0.04)
                if trades:
                    wins = sum(1 for t in trades if t["result_r"] > 0)
                    losses = len(trades) - wins
                    total_r = sum(t["result_r"] for t in trades)
                    wr = wins / len(trades) * 100
                    
                    # Equity curve
                    eq = 100.0
                    peak = 100.0
                    max_dd = 0.0
                    for t in trades:
                        eq += t["result_r"] * 2  # 2% risk
                        if eq > peak:
                            peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > max_dd:
                            max_dd = dd
                    
                    pf = sum(t["result_r"] for t in trades if t["result_r"] > 0) / abs(sum(t["result_r"] for t in trades if t["result_r"] < 0)) if any(t["result_r"] < 0 for t in trades) else 999
                    
                    print(f"    TP={tp_r}R: {len(trades)}t  W:{wins} L:{losses}  "
                          f"WR={wr:.0f}%  R={total_r:+.1f}  DD={max_dd:.1f}%  PF={pf:.2f}")
                else:
                    print(f"    TP={tp_r}R: 0 trades")
            
            time.sleep(0.2)
        except Exception as e:
            print(f"    Error: {e}")
    
    # Save results
    save_data = []
    for r in scan_results[:20]:
        d = {k: v for k, v in r.items() if k != "candles"}
        save_data.append(d)
    
    with open("obr/a_grade_scan.json", "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    
    print(f"\n  Results saved to obr/a_grade_scan.json")
    
    return scan_results, top3


if __name__ == "__main__":
    results, top3 = main()
