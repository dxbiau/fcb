"""Extended backtest on top 3 candidates: GRT, JUP, ORCA."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt

ex = ccxt.bybit({"apiKey": os.environ.get("BYBIT_API_KEY",""),
                  "secret": os.environ.get("BYBIT_API_SECRET",""),
                  "enableRateLimit": True, "options": {"defaultType": "swap"}})
ex.load_markets()

def fetch(sym, limit=1000):
    raw = ex.fetch_ohlcv(sym, "5m", limit=limit)
    return [{"ts":r[0],"o":r[1],"h":r[2],"l":r[3],"c":r[4],"v":r[5]} for r in raw]

def obr(p, c):
    if p["h"]==p["l"] or c["h"]==c["l"]: return 0
    if c["o"]>c["c"] and c["h"]>p["h"] and c["l"]<p["l"] and c["c"]<p["l"]: return 2
    if c["o"]<c["c"] and c["h"]>p["h"] and c["l"]<p["l"] and c["c"]>p["h"]: return 1
    return 0

def confirm(s, c):
    if s==2: return c["c"]>c["o"]
    if s==1: return c["c"]<c["o"]
    return False

def bt(candles, tp_r, fee=0.04):
    trades=[]; i=0
    while i<len(candles)-3:
        s=obr(candles[i],candles[i+1])
        if s==0: i+=1; continue
        if not confirm(s,candles[i+2]): i+=1; continue
        ep=candles[i+3]["o"]; ob=candles[i+1]
        if s==2:
            sl=ob["l"]; rpu=ep-sl
            if rpu<=0: i+=1; continue
            tp=ep+tp_r*rpu; res=None; ei=i+3
            for j in range(i+3,len(candles)):
                if candles[j]["l"]<=sl: res=-1.0-fee; ei=j; break
                if candles[j]["h"]>=tp: res=tp_r-fee; ei=j; break
            if res is None: res=(candles[-1]["c"]-ep)/rpu-fee; ei=len(candles)-1
        else:
            sl=ob["h"]; rpu=sl-ep
            if rpu<=0: i+=1; continue
            tp=ep-tp_r*rpu; res=None; ei=i+3
            for j in range(i+3,len(candles)):
                if candles[j]["h"]>=sl: res=-1.0-fee; ei=j; break
                if candles[j]["l"]<=tp: res=tp_r-fee; ei=j; break
            if res is None: res=(ep-candles[-1]["c"])/rpu-fee; ei=len(candles)-1
        trades.append({"s":"L" if s==2 else "S","r":round(res,3),"ei":ei,
                       "ep":ep,"sl":sl,"tp":tp,"rpu":rpu})
        i=ei+1
    return trades

pairs = ["GRT/USDT:USDT", "JUP/USDT:USDT", "ORCA/USDT:USDT"]

for sym in pairs:
    print("\n" + "="*60)
    print(f"  {sym} -- Extended Backtest")
    print("="*60)
    candles = fetch(sym, 1000)
    print(f"  Candles: {len(candles)}")
    
    best_tp = 2.0
    best_r = -999
    
    for tp_r in [1.0, 1.5, 2.0, 2.5, 3.0]:
        trades = bt(candles, tp_r)
        if not trades:
            print(f"  TP={tp_r}R: 0 trades")
            continue
        wins = sum(1 for t in trades if t["r"]>0)
        total_r = sum(t["r"] for t in trades)
        wr = wins/len(trades)*100
        eq=100; pk=100; mdd=0
        for t in trades:
            eq += t["r"]*2
            if eq>pk: pk=eq
            dd=(pk-eq)/pk*100
            if dd>mdd: mdd=dd
        
        wr_pos = sum(t["r"] for t in trades if t["r"]>0)
        wr_neg = abs(sum(t["r"] for t in trades if t["r"]<0))
        pf = wr_pos/wr_neg if wr_neg>0 else 999
        
        # Streaks
        streak=0; mws=0; mls=0; ct=None
        for t in trades:
            w=t["r"]>0
            if w==ct: streak+=1
            else: ct=w; streak=1
            if w and streak>mws: mws=streak
            if not w and streak>mls: mls=streak
        
        tag=""
        if total_r>5 and wr>=45 and mdd<15: tag=" *** TRADEABLE"
        elif total_r>0: tag=" * edge"
        
        print(f"  TP={tp_r}R: {len(trades):2d}t  W:{wins:2d} L:{len(trades)-wins:2d}  "
              f"WR={wr:4.0f}%  R={total_r:+6.1f}  DD={mdd:4.1f}%  PF={pf:4.2f}  "
              f"WStrk={mws} LStrk={mls}{tag}")
        
        if total_r > best_r:
            best_r = total_r
            best_tp = tp_r
    
    # Trade-by-trade at best TP
    trades = bt(candles, best_tp)
    if trades:
        print(f"\n  Trade-by-trade at {best_tp}R:")
        eq = 100
        for idx_t, t in enumerate(trades):
            eq += t["r"] * 2
            risk_pct = t["rpu"]/t["ep"]*100 if t["ep"]>0 else 0
            # $10 risk sizing
            qty = 10.0 / t["rpu"] if t["rpu"]>0 else 0
            pnl = t["r"] * 10.0  # $10 risk * R
            print(f"    #{idx_t+1:2d} {t['s']}  Entry={t['ep']:.6g}  SL={t['sl']:.6g}  "
                  f"Risk={risk_pct:.2f}%  R={t['r']:+.3f}  PnL=${pnl:+.2f}  Eq={eq:.1f}")
        
        total_pnl = sum(t["r"]*10 for t in trades)
        print(f"\n  TOTAL PnL at $10/trade: ${total_pnl:+.2f}")
        print(f"  Avg trade: ${total_pnl/len(trades):+.2f}")
    
    time.sleep(0.3)

print("\nDone.")
