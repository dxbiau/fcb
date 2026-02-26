"""
OBR Session Analysis -- Does the edge come from specific sessions or is it 24/7?

Breaks down all 15 pairs' OBR performance by:
  1. 24/7 (no session filter)
  2. Asia (00:00-08:00 UTC)
  3. London (08:00-16:00 UTC)
  4. NY (16:00-24:00 UTC)
  5. Each hour of the day (0-23)

Also computes: if we cap at X% daily growth and stop, what's the result?
"""

import sys, time, json
from datetime import datetime, timezone

# --- ccxt import ---
try:
    import ccxt
except ImportError:
    print("ERROR: ccxt not installed"); sys.exit(1)

# --- Bybit connection ---
import os
ex = ccxt.bybit({
    "apiKey": os.environ.get("BYBIT_API_KEY", ""),
    "secret": os.environ.get("BYBIT_API_SECRET", ""),
    "options": {"defaultType": "swap"},
})
ex.load_markets()

# --- Our 15 pairs + TP from config ---
PAIR_TP = {
    "SNX/USDT:USDT": 1.0,
    "GRT/USDT:USDT": 2.0,
    "ICP/USDT:USDT": 1.0,
    "JUP/USDT:USDT": 1.5,
    "AWE/USDT:USDT": 2.5,
    "C98/USDT:USDT": 1.5,
    "GUN/USDT:USDT": 3.0,
    "RESOLV/USDT:USDT": 1.0,
    "ENSO/USDT:USDT": 3.0,
    "SPACE/USDT:USDT": 3.0,
    "RPL/USDT:USDT": 3.0,
    "TIA/USDT:USDT": 3.0,
    "HNT/USDT:USDT": 3.0,
    "NEAR/USDT:USDT": 1.0,
    "HBAR/USDT:USDT": 2.0,
}

FEE_R = 0.04  # round-trip fee in R terms (approx)

SESSIONS = {
    "asia":   (0, 8),
    "london": (8, 16),
    "ny":     (16, 24),
}

def hour_in_session(hour, session):
    s, e = SESSIONS[session]
    return s <= hour < e

def fetch_candles(symbol, tf="5m", limit=1500):
    """Fetch OHLCV candles from Bybit."""
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    candles = []
    for r in raw:
        candles.append({
            "ts": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
            "hour": datetime.fromtimestamp(r[0]/1000, tz=timezone.utc).hour,
        })
    return candles

def detect_ob_signal(prev, curr):
    """Detect OBR signal: outside bar that engulfs + closes beyond prev range."""
    # Bearish OB -> LONG signal (fade)
    if (curr["open"] > curr["close"] and          # bearish candle
        curr["high"] > prev["high"] and            # engulfs high
        curr["low"] < prev["low"] and              # engulfs low
        curr["close"] < prev["low"]):              # closes below prev low
        return "long"
    
    # Bullish OB -> SHORT signal (fade)
    if (curr["open"] < curr["close"] and           # bullish candle
        curr["low"] < prev["low"] and              # engulfs low
        curr["high"] > prev["high"] and            # engulfs high
        curr["close"] > prev["high"]):             # closes above prev high
        return "short"
    
    return None

def backtest_pair(symbol, tp_r, candles):
    """
    Backtest OBR-NEXTBAR on candles.
    Returns list of trades with session/hour info.
    """
    trades = []
    i = 2  # need prev, ob, confirm at minimum
    
    while i < len(candles) - 1:
        prev = candles[i - 2]
        ob = candles[i - 1]
        confirm = candles[i]
        
        direction = detect_ob_signal(prev, ob)
        if direction is None:
            i += 1
            continue
        
        # NEXTBAR confirmation: confirm candle must close in reversal direction
        if direction == "long" and confirm["close"] <= confirm["open"]:
            i += 1
            continue
        if direction == "short" and confirm["close"] >= confirm["open"]:
            i += 1
            continue
        
        # Entry at next candle's open
        if i + 1 >= len(candles):
            break
        entry_candle = candles[i + 1]
        entry = entry_candle["open"]
        
        # SL = OB candle's extreme
        if direction == "long":
            sl = ob["low"]
            risk_per_unit = entry - sl
        else:
            sl = ob["high"]
            risk_per_unit = sl - entry
        
        if risk_per_unit <= 0 or (risk_per_unit / entry < 0.001):
            i += 1
            continue
        
        # TP
        if direction == "long":
            tp = entry + tp_r * risk_per_unit
        else:
            tp = entry - tp_r * risk_per_unit
        
        # Simulate: walk forward from entry candle
        pnl_r = None
        exit_hour = entry_candle["hour"]
        
        for j in range(i + 1, min(i + 200, len(candles))):
            c = candles[j]
            if direction == "long":
                if c["low"] <= sl:
                    pnl_r = -1.0 - FEE_R
                    exit_hour = c["hour"]
                    break
                if c["high"] >= tp:
                    pnl_r = tp_r - FEE_R
                    exit_hour = c["hour"]
                    break
            else:
                if c["high"] >= sl:
                    pnl_r = -1.0 - FEE_R
                    exit_hour = c["hour"]
                    break
                if c["low"] <= tp:
                    pnl_r = tp_r - FEE_R
                    exit_hour = c["hour"]
                    break
        
        if pnl_r is None:
            i += 2
            continue
        
        trades.append({
            "symbol": symbol,
            "direction": direction,
            "entry_hour": ob["hour"],  # signal hour
            "exit_hour": exit_hour,
            "pnl_r": pnl_r,
            "tp_r": tp_r,
            "ts": ob["ts"],
            "day": datetime.fromtimestamp(ob["ts"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        })
        
        # Skip ahead (no re-entry on same bar sequence)
        i += 2
        continue
    
    return trades

def calc_stats(trades):
    if not trades:
        return {"trades": 0, "wr": 0, "total_r": 0, "pf": 0, "avg_r": 0}
    wins = [t for t in trades if t["pnl_r"] > 0]
    losses = [t for t in trades if t["pnl_r"] <= 0]
    total_r = sum(t["pnl_r"] for t in trades)
    gross_win = sum(t["pnl_r"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_r"] for t in losses)) if losses else 0.001
    return {
        "trades": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "total_r": total_r,
        "pf": gross_win / gross_loss if gross_loss > 0 else 99,
        "avg_r": total_r / len(trades),
    }

def simulate_daily_cap(trades, daily_cap_pct, risk_usd=2.0, start_equity=49.0):
    """
    Simulate with daily growth cap: once equity grows X% in a day, stop trading for that day.
    """
    equity = start_equity
    peak = start_equity
    
    # Group trades by day
    days = {}
    for t in sorted(trades, key=lambda x: x["ts"]):
        d = t["day"]
        if d not in days:
            days[d] = []
        days[d].append(t)
    
    daily_results = []
    trades_taken = 0
    trades_skipped = 0
    
    for day, day_trades in sorted(days.items()):
        day_start = equity
        day_target = day_start * (1 + daily_cap_pct / 100)
        stopped = False
        day_trades_taken = 0
        
        for t in day_trades:
            if stopped:
                trades_skipped += 1
                continue
            
            pnl_usd = t["pnl_r"] * risk_usd
            equity += pnl_usd
            trades_taken += 1
            day_trades_taken += 1
            
            if equity >= day_target:
                stopped = True
        
        if equity > peak:
            peak = equity
        
        daily_results.append({
            "day": day,
            "start": day_start,
            "end": equity,
            "pct": (equity - day_start) / day_start * 100 if day_start > 0 else 0,
            "trades": day_trades_taken,
            "stopped": stopped,
        })
    
    dd = (peak - equity) / peak * 100 if peak > 0 else 0
    return {
        "final_equity": equity,
        "peak": peak,
        "dd": dd,
        "total_return_pct": (equity - start_equity) / start_equity * 100,
        "trades_taken": trades_taken,
        "trades_skipped": trades_skipped,
        "days": len(daily_results),
        "days_capped": sum(1 for d in daily_results if d["stopped"]),
        "daily_results": daily_results,
    }


# ====================================================================
#  MAIN
# ====================================================================
print("=" * 70)
print("  OBR SESSION ANALYSIS -- 15 Pairs x 1500 Candles (~5.2 days)")
print("=" * 70)
print()

all_trades = []
for sym, tp in PAIR_TP.items():
    try:
        candles = fetch_candles(sym, "5m", 1500)
        trades = backtest_pair(sym, tp, candles)
        all_trades.extend(trades)
        print(f"  {sym:25s} -> {len(trades):3d} trades")
        time.sleep(0.15)
    except Exception as e:
        print(f"  {sym:25s} -> ERROR: {e}")

print(f"\nTotal trades across all pairs: {len(all_trades)}")

# ------------------------------------------------------------------
#  1. Overall (24/7) stats
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  1. OVERALL 24/7 PERFORMANCE")
print("=" * 70)
s = calc_stats(all_trades)
print(f"  Trades: {s['trades']}  WR: {s['wr']:.1f}%  Total R: {s['total_r']:+.1f}  PF: {s['pf']:.2f}  Avg R: {s['avg_r']:+.3f}")

# ------------------------------------------------------------------
#  2. By Session
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  2. PERFORMANCE BY SESSION (signal hour)")
print("=" * 70)
print(f"  {'Session':<10} {'Trades':>7} {'WR':>7} {'TotalR':>9} {'PF':>7} {'AvgR':>8}")
print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")

for sess_name in ["asia", "london", "ny"]:
    start_h, end_h = SESSIONS[sess_name]
    sess_trades = [t for t in all_trades if start_h <= t["entry_hour"] < end_h]
    s = calc_stats(sess_trades)
    print(f"  {sess_name:<10} {s['trades']:>7} {s['wr']:>6.1f}% {s['total_r']:>+8.1f} {s['pf']:>7.2f} {s['avg_r']:>+7.3f}")

# ------------------------------------------------------------------
#  3. By Hour
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  3. PERFORMANCE BY HOUR (signal hour UTC)")
print("=" * 70)
print(f"  {'Hour':>4} {'Trades':>7} {'WR':>7} {'TotalR':>9} {'PF':>7} {'Session':<8}")
print(f"  {'-'*4} {'-'*7} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")

for h in range(24):
    hour_trades = [t for t in all_trades if t["entry_hour"] == h]
    s = calc_stats(hour_trades)
    sess = "asia" if h < 8 else "london" if h < 16 else "ny"
    marker = " ***" if s["total_r"] > 2 and s["trades"] >= 3 else ""
    if s["trades"] > 0:
        print(f"  {h:>4} {s['trades']:>7} {s['wr']:>6.1f}% {s['total_r']:>+8.1f} {s['pf']:>7.2f} {sess:<8}{marker}")
    else:
        print(f"  {h:>4}       0      -        -       - {sess:<8}")

# ------------------------------------------------------------------
#  4. Per-pair by session
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  4. PER-PAIR BY SESSION")
print("=" * 70)
print(f"  {'Pair':<22} {'Asia':>12} {'London':>12} {'NY':>12} {'24/7':>12}")
print(f"  {'-'*22} {'-'*12} {'-'*12} {'-'*12} {'-'*12}")

for sym in PAIR_TP:
    pair_trades = [t for t in all_trades if t["symbol"] == sym]
    parts = []
    for sess in ["asia", "london", "ny"]:
        sh, eh = SESSIONS[sess]
        st = [t for t in pair_trades if sh <= t["entry_hour"] < eh]
        s = calc_stats(st)
        parts.append(f"{s['total_r']:>+5.1f}({s['trades']:>2})")
    s_all = calc_stats(pair_trades)
    parts.append(f"{s_all['total_r']:>+5.1f}({s_all['trades']:>2})")
    print(f"  {sym:<22} {parts[0]:>12} {parts[1]:>12} {parts[2]:>12} {parts[3]:>12}")

# ------------------------------------------------------------------
#  5. Best session-only combinations
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  5. SESSION COMBINATIONS (which sessions to trade?)")
print("=" * 70)

combos = [
    ("24/7",            lambda t: True),
    ("Asia only",       lambda t: 0 <= t["entry_hour"] < 8),
    ("London only",     lambda t: 8 <= t["entry_hour"] < 16),
    ("NY only",         lambda t: 16 <= t["entry_hour"] < 24),
    ("London + NY",     lambda t: 8 <= t["entry_hour"] < 24),
    ("Asia + London",   lambda t: 0 <= t["entry_hour"] < 16),
    ("Asia + NY",       lambda t: t["entry_hour"] < 8 or t["entry_hour"] >= 16),
]

print(f"  {'Combo':<18} {'Trades':>7} {'WR':>7} {'TotalR':>9} {'PF':>7} {'AvgR':>8}")
print(f"  {'-'*18} {'-'*7} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")

for name, filt in combos:
    filtered = [t for t in all_trades if filt(t)]
    s = calc_stats(filtered)
    print(f"  {name:<18} {s['trades']:>7} {s['wr']:>6.1f}% {s['total_r']:>+8.1f} {s['pf']:>7.2f} {s['avg_r']:>+7.3f}")

# ------------------------------------------------------------------
#  6. Daily growth cap simulation
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("  6. DAILY GROWTH CAP SIMULATION ($2/trade, start $49)")
print("=" * 70)
print(f"  {'Cap%':>5} {'Final$':>8} {'Return%':>9} {'Taken':>7} {'Skip':>6} {'Days':>5} {'Capped':>7}")
print(f"  {'-'*5} {'-'*8} {'-'*9} {'-'*7} {'-'*6} {'-'*5} {'-'*7}")

for cap in [5, 10, 15, 20, 25, 30, 50, 100]:
    r = simulate_daily_cap(all_trades, cap, risk_usd=2.0, start_equity=49.0)
    print(f"  {cap:>4}% ${r['final_equity']:>7.2f} {r['total_return_pct']:>+8.1f}% {r['trades_taken']:>7} {r['trades_skipped']:>6} {r['days']:>5} {r['days_capped']:>7}")

# Also test with best session combo
print("\n  --- With best session filter applied ---")
# We'll test all combos with 15% cap
for name, filt in combos:
    filtered = [t for t in all_trades if filt(t)]
    if not filtered:
        continue
    r = simulate_daily_cap(filtered, 15, risk_usd=2.0, start_equity=49.0)
    s = calc_stats(filtered)
    print(f"  {name:<18} Final=${r['final_equity']:.2f}  Return={r['total_return_pct']:+.1f}%  "
          f"Trades={r['trades_taken']}/{len(filtered)}  DaysCapped={r['days_capped']}/{r['days']}")

print("\n" + "=" * 70)
print("  DONE")
print("=" * 70)
