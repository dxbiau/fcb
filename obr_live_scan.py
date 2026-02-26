"""
obr_live_scan.py -- Scan Bybit RIGHT NOW for pairs close to OBR entry.

Checks all 30 configured pairs for:
  1. Active OBR signal (outside bar just formed)
  2. Near-signal (current candle is building toward an outside bar)
  3. Recent signals in last few candles

Reports proximity to entry for each pair.
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from obr import config as cfg
from obr import exchange as ex_mod

def check_ob_conditions(candles):
    """
    Check OBR conditions on the last few candles.
    Returns a dict with signal info and proximity metrics.
    
    OBR Long (signal=2): bearish OB that engulfs prev candle + closes below prev low
      - Open > Close (bearish)
      - High > prev High
      - Low < prev Low  
      - Close < prev Low (closes beyond)
      -> Enter LONG (fade the bearish exhaustion)
    
    OBR Short (signal=1): bullish OB that engulfs prev candle + closes above prev high
      - Open < Close (bullish)
      - High > prev High
      - Low < prev Low
      - Close > prev High (closes beyond)
      -> Enter SHORT (fade the bullish exhaustion)
    """
    results = []
    
    for i in range(1, len(candles)):
        prev = candles[i-1]
        curr = candles[i]
        
        if prev["high"] == prev["low"] or curr["high"] == curr["low"]:
            continue
        
        # Check long signal (bearish OB -> fade to long)
        c0_long = curr["open"] > curr["close"]  # bearish candle
        c1 = curr["high"] > prev["high"]          # engulfs high
        c2 = curr["low"] < prev["low"]            # engulfs low
        c3_long = curr["close"] < prev["low"]     # closes below prev low
        
        # Check short signal (bullish OB -> fade to short)
        c0_short = curr["open"] < curr["close"]  # bullish candle
        c3_short = curr["close"] > prev["high"]   # closes above prev high
        
        signal = 0
        if c0_long and c1 and c2 and c3_long:
            signal = 2  # long
        elif c0_short and c1 and c2 and c3_short:
            signal = 1  # short
        
        results.append({
            "idx": i,
            "ts": curr["ts"],
            "signal": signal,
            "c0_long": c0_long,
            "c0_short": c0_short,
            "c1_high_engulf": c1,
            "c2_low_engulf": c2,
            "c3_close_below": c3_long,
            "c3_close_above": c3_short,
            "prev_high": prev["high"],
            "prev_low": prev["low"],
            "curr_open": curr["open"],
            "curr_high": curr["high"],
            "curr_low": curr["low"],
            "curr_close": curr["close"],
        })
    
    return results


def proximity_score(prev, curr_building):
    """
    Score how close the CURRENT (still-forming) candle is to becoming an OBR.
    
    Returns dict with:
      - score: 0-4 (how many of 4 conditions are met)
      - direction: 'long', 'short', or 'none'
      - conditions: which are met
      - distances: how far from meeting unmet conditions
    """
    if prev["high"] == prev["low"]:
        return {"score": 0, "direction": "none", "conditions": [], "distances": {}}
    
    prev_range = prev["high"] - prev["low"]
    
    # Check conditions for LONG (bearish OB)
    long_conds = []
    long_dists = {}
    
    is_bearish = curr_building["open"] > curr_building["close"]
    if is_bearish:
        long_conds.append("bearish_candle")
    else:
        # How far from being bearish? close needs to drop below open
        long_dists["need_bearish"] = f"close needs to drop {(curr_building['close'] - curr_building['open'])/prev_range*100:.1f}% of prev range"
    
    if curr_building["high"] > prev["high"]:
        long_conds.append("high_engulf")
    else:
        gap = prev["high"] - curr_building["high"]
        long_dists["need_high"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    if curr_building["low"] < prev["low"]:
        long_conds.append("low_engulf")
    else:
        gap = curr_building["low"] - prev["low"]
        long_dists["need_low"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    if curr_building["close"] < prev["low"]:
        long_conds.append("close_below_prev_low")
    else:
        gap = curr_building["close"] - prev["low"]
        long_dists["need_close_below"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    long_score = len(long_conds)
    
    # Check conditions for SHORT (bullish OB)
    short_conds = []
    short_dists = {}
    
    is_bullish = curr_building["open"] < curr_building["close"]
    if is_bullish:
        short_conds.append("bullish_candle")
    else:
        short_dists["need_bullish"] = f"close needs to rise {(curr_building['open'] - curr_building['close'])/prev_range*100:.1f}% of prev range"
    
    if curr_building["high"] > prev["high"]:
        short_conds.append("high_engulf")
    else:
        gap = prev["high"] - curr_building["high"]
        short_dists["need_high"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    if curr_building["low"] < prev["low"]:
        short_conds.append("low_engulf")
    else:
        gap = curr_building["low"] - prev["low"]
        short_dists["need_low"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    if curr_building["close"] > prev["high"]:
        short_conds.append("close_above_prev_high")
    else:
        gap = prev["high"] - curr_building["close"]
        short_dists["need_close_above"] = f"{gap:.6f} ({gap/prev_range*100:.1f}% of range)"
    
    short_score = len(short_conds)
    
    if long_score >= short_score:
        return {"score": long_score, "direction": "long" if long_score > 0 else "none",
                "conditions": long_conds, "distances": long_dists}
    else:
        return {"score": short_score, "direction": "short" if short_score > 0 else "none",
                "conditions": short_conds, "distances": short_dists}


def main():
    print("=" * 70)
    print("  OBR LIVE SIGNAL SCANNER -- Bybit Mainnet")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Scanning {len(cfg.PAIRS)} pairs on {cfg.TIMEFRAME}")
    print("=" * 70)
    
    ex = ex_mod.create_exchange()
    print(f"  Connected -- {len(ex.markets)} markets\n")
    
    pair_results = []
    
    for pair in sorted(cfg.PAIRS):
        try:
            # Fetch last 20 closed candles + current forming candle
            raw = ex_mod.fetch_latest_candles(ex, pair, 20)
            if len(raw) < 3:
                continue
            
            # Check historical signals (on closed candles)
            hist_signals = check_ob_conditions(raw[:-1])  # exclude last (may be incomplete)
            recent_signals = [s for s in hist_signals if s["signal"] != 0]
            
            # The last closed candle = raw[-2], current building = raw[-1]
            # Actually fetch_latest_candles returns closed candles
            # Let's also get current forming via ticker
            
            # Check proximity on the LAST closed candle pair
            # raw[-2] = prev, raw[-1] = current (just closed)
            prev_candle = raw[-2]
            last_closed = raw[-1]
            
            # Check if last closed candle IS an OBR signal
            last_check = check_ob_conditions([prev_candle, last_closed])
            has_active_signal = any(c["signal"] != 0 for c in last_check)
            active_signal_dir = None
            if has_active_signal:
                for c in last_check:
                    if c["signal"] == 2:
                        active_signal_dir = "LONG"
                    elif c["signal"] == 1:
                        active_signal_dir = "SHORT"
            
            # Now check: is the current forming candle building toward an OBR?
            # We need to get the current live candle data
            try:
                ticker = ex.fetch_ticker(pair)
                last_price = float(ticker.get("last", 0))
                high_24h = float(ticker.get("high", 0) or 0)
                low_24h = float(ticker.get("low", 0) or 0)
            except:
                last_price = last_closed["close"]
            
            # Proximity: how close is last_closed to being OBR vs its prev?
            prox = proximity_score(prev_candle, last_closed)
            
            # Also check 2nd-to-last pair for a signal that just needs nextbar confirm
            if len(raw) >= 3:
                prev2 = raw[-3]
                check2 = check_ob_conditions([prev2, prev_candle])
                has_pending_confirm = any(c["signal"] != 0 for c in check2)
                pending_dir = None
                if has_pending_confirm:
                    for c in check2:
                        if c["signal"] == 2:
                            pending_dir = "LONG"
                        elif c["signal"] == 1:
                            pending_dir = "SHORT"
                    
                    # Check if last_closed confirms (nextbar confirmation)
                    if pending_dir == "LONG" and last_closed["close"] > last_closed["open"]:
                        has_pending_confirm = "CONFIRMED"
                    elif pending_dir == "SHORT" and last_closed["close"] < last_closed["open"]:
                        has_pending_confirm = "CONFIRMED"
                    else:
                        has_pending_confirm = "UNCONFIRMED"
            else:
                has_pending_confirm = False
                pending_dir = None
            
            pair_results.append({
                "pair": pair,
                "active_signal": active_signal_dir,
                "proximity": prox,
                "recent_signals_5": len([s for s in hist_signals[-5:] if s["signal"] != 0]),
                "recent_signals_10": len([s for s in hist_signals[-10:] if s["signal"] != 0]),
                "total_signals_20": len(recent_signals),
                "last_price": last_price,
                "prev_range_pct": (prev_candle["high"] - prev_candle["low"]) / prev_candle["close"] * 100,
                "last_range_pct": (last_closed["high"] - last_closed["low"]) / last_closed["close"] * 100,
                "pending_confirm": has_pending_confirm,
                "pending_dir": pending_dir,
                "candles": raw,  # store for later analysis
            })
            
            time.sleep(0.12)
        except Exception as e:
            print(f"  {pair}: ERROR -- {e}")
    
    # Sort by proximity score (highest = closest to signal)
    pair_results.sort(key=lambda x: (
        1 if x["active_signal"] else 0,  # active signals first
        1 if x["pending_confirm"] == "CONFIRMED" else 0,  # confirmed pending
        x["proximity"]["score"],  # then by proximity
    ), reverse=True)
    
    # Print results
    print("\n" + "=" * 70)
    print("  RESULTS: Sorted by proximity to OBR entry")
    print("=" * 70)
    
    top_candidates = []
    
    for i, r in enumerate(pair_results):
        prox = r["proximity"]
        status = ""
        
        if r["active_signal"]:
            status = f"*** ACTIVE SIGNAL: {r['active_signal']} ***"
            top_candidates.append(r)
        elif r["pending_confirm"] == "CONFIRMED":
            status = f"** NEXTBAR CONFIRMED: {r['pending_dir']} -- READY TO TRADE **"
            top_candidates.append(r)
        elif r["pending_confirm"] == "UNCONFIRMED":
            status = f"* PENDING CONFIRM: {r['pending_dir']} (nextbar didn't confirm)"
        elif prox["score"] >= 3:
            status = f"VERY CLOSE ({prox['score']}/4): {prox['direction']}"
            top_candidates.append(r)
        elif prox["score"] >= 2:
            status = f"Building ({prox['score']}/4): {prox['direction']}"
        else:
            status = f"Quiet ({prox['score']}/4)"
        
        tp_r = cfg.get_pair_tp(r["pair"])
        
        print(f"\n  {i+1}. {r['pair']}  (optimal TP: {tp_r}R)")
        print(f"     Price: {r['last_price']:.6g} | "
              f"Prev range: {r['prev_range_pct']:.2f}% | "
              f"Last range: {r['last_range_pct']:.2f}%")
        print(f"     Status: {status}")
        
        if prox["score"] >= 2 or r["active_signal"] or r["pending_confirm"]:
            if prox["conditions"]:
                print(f"     Met: {', '.join(prox['conditions'])}")
            if prox["distances"]:
                for k, v in prox["distances"].items():
                    print(f"     Missing: {k} -> {v}")
        
        if r["recent_signals_10"] > 0:
            print(f"     Recent: {r['recent_signals_10']} signals in last 10 candles, "
                  f"{r['total_signals_20']} in last 20")
    
    # Summary of top candidates
    print("\n" + "=" * 70)
    print(f"  TOP CANDIDATES (score >= 3 or active signal): {len(top_candidates)}")
    print("=" * 70)
    
    for r in top_candidates[:5]:
        tp_r = cfg.get_pair_tp(r["pair"])
        dir_str = r["active_signal"] or r["pending_dir"] or r["proximity"]["direction"]
        print(f"  -> {r['pair']}  |  {dir_str}  |  TP={tp_r}R  |  "
              f"Score={r['proximity']['score']}/4  |  Price={r['last_price']:.6g}")
    
    if not top_candidates:
        print("  No pairs currently at or near OBR entry.")
        print("  Closest pairs:")
        for r in pair_results[:5]:
            print(f"  -> {r['pair']}  |  Score={r['proximity']['score']}/4  |  "
                  f"{r['proximity']['direction']}")
    
    return pair_results, top_candidates


if __name__ == "__main__":
    results, candidates = main()
