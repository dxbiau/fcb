"""
obr_deep_scan.py -- Deep scan: find pairs where OBR actually works AND have 
a setup forming right now. Extended backtest over 1000 candles.

$10 risk, 20x leverage.
"""

import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
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

def fetch_candles(ex, symbol, tf="5m", limit=1000):
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in raw]

def detect_obr(prev, curr):
    if prev["high"] == prev["low"] or curr["high"] == curr["low"]:
        return 0
    # Long (2): bearish OB, closes below prev low -> fade to LONG
    if (curr["open"] > curr["close"] and curr["high"] > prev["high"] and
        curr["low"] < prev["low"] and curr["close"] < prev["low"]):
        return 2
    # Short (1): bullish OB, closes above prev high -> fade to SHORT
    if (curr["open"] < curr["close"] and curr["high"] > prev["high"] and
        curr["low"] < prev["low"] and curr["close"] > prev["high"]):
        return 1
    return 0

def nextbar_confirms(sig, candle):
    if sig == 2: return candle["close"] > candle["open"]
    if sig == 1: return candle["close"] < candle["open"]
    return False

def backtest(candles, tp_r=2.0, fee_r=0.04):
    trades = []
    i = 0
    while i < len(candles) - 3:
        sig = detect_obr(candles[i], candles[i+1])
        if sig == 0:
            i += 1; continue
        if not nextbar_confirms(sig, candles[i+2]):
            i += 1; continue
        
        ep = candles[i+3]["open"]
        ob = candles[i+1]
        
        if sig == 2:  # long
            sl = ob["low"]; rpu = ep - sl
            if rpu <= 0: i += 1; continue
            tp = ep + tp_r * rpu
            result = None
            exit_idx = i + 3
            for j in range(i+3, len(candles)):
                if candles[j]["low"] <= sl: result = -1.0 - fee_r; exit_idx = j; break
                if candles[j]["high"] >= tp: result = tp_r - fee_r; exit_idx = j; break
            if result is None: result = (candles[-1]["close"] - ep) / rpu - fee_r; exit_idx = len(candles)-1
        else:  # short
            sl = ob["high"]; rpu = sl - ep
            if rpu <= 0: i += 1; continue
            tp = ep - tp_r * rpu
            result = None
            exit_idx = i + 3
            for j in range(i+3, len(candles)):
                if candles[j]["high"] >= sl: result = -1.0 - fee_r; exit_idx = j; break
                if candles[j]["low"] <= tp: result = tp_r - fee_r; exit_idx = j; break
            if result is None: result = (ep - candles[-1]["close"]) / rpu - fee_r; exit_idx = len(candles)-1
        
        trades.append({"sig": "L" if sig==2 else "S", "ep": ep, "sl": sl, "tp": tp, "rpu": rpu,
                       "r": round(result, 3), "entry_i": i+3, "exit_i": exit_idx, "ts": candles[i+3]["ts"]})
        i = exit_idx + 1
    return trades

def proximity(prev, curr):
    """Return (best_score, direction, met_conditions)."""
    if prev["high"] == prev["low"]: return 0, "none", []
    # Long
    lm = []
    if curr["open"] > curr["close"]: lm.append("bearish")
    if curr["high"] > prev["high"]: lm.append("H>prevH")
    if curr["low"] < prev["low"]: lm.append("L<prevL")
    if curr["close"] < prev["low"]: lm.append("C<prevL")
    # Short
    sm = []
    if curr["open"] < curr["close"]: sm.append("bullish")
    if curr["high"] > prev["high"]: sm.append("H>prevH")
    if curr["low"] < prev["low"]: sm.append("L<prevL")
    if curr["close"] > prev["high"]: sm.append("C>prevH")
    
    if len(lm) >= len(sm):
        return len(lm), "long", lm
    return len(sm), "short", sm


def main():
    now = datetime.now(timezone.utc)
    print("=" * 70)
    print("  OBR DEEP SCAN -- Find A-Grade Setups with Proven Edge")
    print(f"  {now.strftime('%Y-%m-%d %H:%M UTC')} | $10 risk @ 20x")
    print("=" * 70)
    
    ex = connect()
    
    # Get liquid USDT perps
    usdt_perps = []
    for sym, mkt in ex.markets.items():
        if (mkt.get("swap") and mkt.get("linear") and
            mkt.get("settle") == "USDT" and mkt.get("active")):
            usdt_perps.append(sym)
    
    tickers = ex.fetch_tickers(symbols=usdt_perps[:500])
    liquid = [(sym, float(t.get("quoteVolume") or 0)) for sym, t in tickers.items() if float(t.get("quoteVolume") or 0) > 2_000_000]
    liquid.sort(key=lambda x: -x[1])
    
    print(f"  {len(liquid)} liquid pairs. Deep scanning with 1000 candles each...\n")
    
    results = []
    
    for idx, (sym, vol) in enumerate(liquid):
        try:
            candles = fetch_candles(ex, sym, "5m", 1000)
            if len(candles) < 50: continue
            
            # --- BACKTEST at multiple TP levels ---
            best_tp = None
            best_total_r = -999
            best_stats = None
            
            for tp_r in [1.5, 2.0, 2.5, 3.0]:
                trades = backtest(candles, tp_r, 0.04)
                if len(trades) >= 5:
                    wins = sum(1 for t in trades if t["r"] > 0)
                    total_r = sum(t["r"] for t in trades)
                    wr = wins / len(trades) * 100
                    
                    eq = 100.0; peak = 100.0; max_dd = 0.0
                    for t in trades:
                        eq += t["r"] * 2
                        if eq > peak: peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > max_dd: max_dd = dd
                    
                    win_r = sum(t["r"] for t in trades if t["r"] > 0)
                    loss_r = abs(sum(t["r"] for t in trades if t["r"] < 0))
                    pf = win_r / loss_r if loss_r > 0 else 999
                    
                    if total_r > best_total_r:
                        best_total_r = total_r
                        best_tp = tp_r
                        best_stats = {
                            "trades": len(trades), "wins": wins, "wr": wr,
                            "total_r": round(total_r, 2), "max_dd": round(max_dd, 1),
                            "pf": round(pf, 2), "tp_r": tp_r,
                            "last_5": [t["r"] for t in trades[-5:]],
                        }
            
            if best_stats is None or best_total_r <= 0:
                if (idx + 1) % 30 == 0: print(f"  Scanned {idx+1}/{len(liquid)}...")
                time.sleep(0.1)
                continue
            
            # --- CURRENT SIGNAL STATUS ---
            sig_status = "quiet"
            sig_dir = None
            sig_detail = {}
            
            # Check: did candle[-2] vs [-3] form OBR with [-1] as confirm?
            if len(candles) >= 4:
                s = detect_obr(candles[-3], candles[-2])
                if s != 0:
                    d = "LONG" if s == 2 else "SHORT"
                    if nextbar_confirms(s, candles[-1]):
                        sig_status = "CONFIRMED"
                        sig_dir = d
                        ob = candles[-2]
                        ep_est = candles[-1]["close"]
                        if s == 2:
                            sl = ob["low"]; rpu = ep_est - sl
                        else:
                            sl = ob["high"]; rpu = sl - ep_est
                        if rpu > 0:
                            sig_detail = {"entry": ep_est, "sl": sl, "rpu": rpu,
                                          "risk_%": rpu/ep_est*100,
                                          "tp2": ep_est + (2*rpu if s==2 else -2*rpu)}
                    else:
                        sig_status = "UNCONFIRMED"
                        sig_dir = d
            
            # Check: did candle[-1] vs [-2] just form OBR (waiting for confirm)?
            if sig_status == "quiet" and len(candles) >= 3:
                s = detect_obr(candles[-2], candles[-1])
                if s != 0:
                    d = "LONG" if s == 2 else "SHORT"
                    sig_status = "AWAITING_CONFIRM"
                    sig_dir = d
                    ob = candles[-1]
                    if s == 2:
                        sl = ob["low"]; ep_est = ob["close"]; rpu = ep_est - sl
                    else:
                        sl = ob["high"]; ep_est = ob["close"]; rpu = sl - ep_est
                    if rpu > 0:
                        sig_detail = {"entry_est": ep_est, "sl": sl, "rpu": rpu,
                                      "risk_%": rpu/ep_est*100}
            
            # Proximity check
            prox_score, prox_dir, prox_met = proximity(candles[-2], candles[-1])
            
            # --- GRADE ---
            grade_score = 0
            
            # Backtest quality (max 50 points)
            if best_stats["wr"] >= 50: grade_score += 15
            if best_stats["wr"] >= 60: grade_score += 10
            if best_stats["pf"] >= 1.5: grade_score += 10
            if best_stats["pf"] >= 2.0: grade_score += 5
            if best_stats["max_dd"] < 15: grade_score += 10
            if best_stats["total_r"] >= 5: grade_score += 5
            if best_stats["total_r"] >= 10: grade_score += 5
            
            # Signal proximity (max 50 points)
            if sig_status == "CONFIRMED": grade_score += 50
            elif sig_status == "AWAITING_CONFIRM": grade_score += 35
            elif prox_score >= 3: grade_score += 20
            elif prox_score >= 2: grade_score += 5
            
            grade = "A" if grade_score >= 70 else "B" if grade_score >= 50 else "C" if grade_score >= 30 else "D"
            
            results.append({
                "pair": sym, "vol": vol, "grade": grade, "gscore": grade_score,
                "bt": best_stats, "sig_status": sig_status, "sig_dir": sig_dir,
                "sig_detail": sig_detail, "prox": prox_score, "prox_dir": prox_dir,
                "prox_met": prox_met, "price": candles[-1]["close"],
            })
            
            if (idx + 1) % 30 == 0:
                print(f"  Scanned {idx+1}/{len(liquid)}... ({len(results)} with edge)")
            
            time.sleep(0.1)
        except Exception as e:
            pass
    
    results.sort(key=lambda x: -x["gscore"])
    
    # --- PRINT ALL A/B GRADES ---
    print("\n" + "=" * 70)
    print(f"  RESULTS: {len(results)} pairs with positive backtest edge")
    print("=" * 70)
    
    for r in results:
        if r["grade"] not in ("A", "B") and results.index(r) >= 15:
            break
        
        bt = r["bt"]
        status_str = ""
        if r["sig_status"] == "CONFIRMED":
            status_str = f"*** LIVE {r['sig_dir']} SIGNAL ***"
        elif r["sig_status"] == "AWAITING_CONFIRM":
            status_str = f"** OBR {r['sig_dir']} -- waiting confirm **"
        elif r["prox"] >= 3:
            status_str = f"* Building {r['prox_dir'].upper()} ({r['prox']}/4) *"
        else:
            status_str = f"Prox {r['prox']}/4 {r['prox_dir']}"
        
        print(f"\n  [{r['grade']}] {r['pair']}  score={r['gscore']}  "
              f"Vol=${r['vol']/1e6:.0f}M  Price={r['price']:.6g}")
        print(f"      Signal: {status_str}")
        print(f"      BT({bt['tp_r']}R): {bt['trades']}t  WR={bt['wr']:.0f}%  "
              f"R={bt['total_r']:+.1f}  DD={bt['max_dd']:.0f}%  PF={bt['pf']:.2f}")
        print(f"      Last 5 trades R: {bt['last_5']}")
    
    # --- TOP 3 DETAILED ---
    top3 = results[:3]
    
    print("\n\n" + "#" * 70)
    print(f"  TOP 3 A-GRADE SETUPS FOR LIVE TEST")
    print(f"  $10 risk per trade | 20x leverage | Structural SL")
    print("#" * 70)
    
    for rank, r in enumerate(top3, 1):
        bt = r["bt"]
        print(f"\n  {'='*60}")
        print(f"  #{rank}  {r['pair']}  [Grade {r['grade']}  Score: {r['gscore']}]")
        print(f"  {'='*60}")
        print(f"  Price: {r['price']:.6g}  |  24h Vol: ${r['vol']/1e6:.1f}M")
        print(f"  Backtest (1000 candles / ~3.5 days):")
        print(f"    Best TP: {bt['tp_r']}R  |  {bt['trades']} trades")
        print(f"    Win Rate: {bt['wr']:.0f}%  |  Total R: {bt['total_r']:+.1f}")
        print(f"    Max DD: {bt['max_dd']:.0f}%  |  Profit Factor: {bt['pf']:.2f}")
        print(f"    Last 5 trades: {bt['last_5']}")
        
        if r["sig_status"] == "CONFIRMED" and r["sig_detail"]:
            d = r["sig_detail"]
            rpu = d["rpu"]
            qty = 10.0 / rpu if rpu > 0 else 0
            margin = qty * d["entry"] / 20.0
            
            print(f"\n  >>> LIVE {r['sig_dir']} SIGNAL <<<")
            print(f"  Entry: {d['entry']:.6g}")
            print(f"  SL: {d['sl']:.6g}  (risk: {d['risk_%']:.3f}%)")
            print(f"  TP: {d.get('tp2', 'N/A')}")
            print(f"  Qty: {qty:.4f}  (margin: ${margin:.2f} @ 20x)")
            print(f"  Risk: $10  |  Reward at 2R: $20")
        
        elif r["sig_status"] == "AWAITING_CONFIRM" and r["sig_detail"]:
            d = r["sig_detail"]
            print(f"\n  >> OBR {r['sig_dir']} formed -- NEXT CANDLE confirms it")
            print(f"  Estimated entry: {d.get('entry_est', '?'):.6g}")
            print(f"  SL: {d['sl']:.6g}  (risk: {d['risk_%']:.3f}%)")
            print(f"  Action: Watch next 5m candle -- if it closes {'bullish' if r['sig_dir']=='LONG' else 'bearish'}, ENTER")
        
        elif r["prox"] >= 3:
            print(f"\n  > Building {r['prox_dir'].upper()} ({r['prox']}/4 conditions met)")
            print(f"  Met: {r['prox_met']}")
            print(f"  Action: Watch for outside bar completion")
        
        else:
            print(f"\n  Proximity: {r['prox']}/4 toward {r['prox_dir']}")
            print(f"  Action: On watchlist -- wait for OBR to form")
    
    # Save
    save = [{k: v for k, v in r.items()} for r in results[:20]]
    with open("obr/a_grade_deep.json", "w") as f:
        json.dump(save, f, indent=2, default=str)
    
    print(f"\n\n  Saved top 20 to obr/a_grade_deep.json")
    return results


if __name__ == "__main__":
    main()
